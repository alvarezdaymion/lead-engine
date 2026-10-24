"""The polite fetcher: robots.txt, timeouts, size cap, per-host rate limit."""

import pytest
from helpers import Clock, html_transport

from lead_engine.fetch import Disallowed, Fetcher, FetchError

SITE = "https://shop.example.com"
PAGE = "<html><head><title>Shop</title></head><body><p>Hello there.</p></body></html>"


def fetcher(transport, clock=None, **kw):
    clock = clock or Clock()
    return Fetcher("lead-engine-test/0.1 (+https://example.com/bot)", transport=transport, sleep=clock.sleep,
                   monotonic=clock.monotonic, **kw), clock


def test_robots_txt_disallow_is_obeyed():
    t = html_transport({SITE + "/robots.txt": "User-agent: *\nDisallow: /private\n", SITE + "/": PAGE,
                        SITE + "/private/team": PAGE})
    f, _ = fetcher(t)
    assert f.get(SITE + "/").ok
    with pytest.raises(Disallowed):
        f.get(SITE + "/private/team")
    assert SITE + "/private/team" not in [u for u, _ in t.calls]
    assert [u for u, _ in t.calls].count(SITE + "/robots.txt") == 1  # read once per host


def test_robots_rules_for_our_agent_apply():
    t = html_transport({SITE + "/robots.txt": "User-agent: lead-engine-test\nDisallow: /\n", SITE + "/": PAGE})
    f, _ = fetcher(t)
    with pytest.raises(Disallowed):
        f.get(SITE + "/")


def test_missing_robots_allows_and_forbidden_robots_disallows():
    f, _ = fetcher(html_transport({SITE + "/": PAGE}))
    assert f.get(SITE + "/").ok
    f, _ = fetcher(html_transport({SITE + "/": PAGE}, statuses={SITE + "/robots.txt": 403}))
    with pytest.raises(Disallowed):
        f.get(SITE + "/")


def test_unreadable_robots_is_transient_not_a_free_pass():
    f, _ = fetcher(html_transport({SITE + "/": PAGE}, statuses={SITE + "/robots.txt": 503}))
    with pytest.raises(FetchError):
        f.get(SITE + "/")


def test_timeouts_and_server_errors_are_transient():
    f, _ = fetcher(html_transport({SITE + "/": PAGE}, timeouts={SITE + "/": 1}))
    with pytest.raises(FetchError, match="timed out"):
        f.get(SITE + "/")
    assert f.get(SITE + "/").ok  # the next try works
    f, _ = fetcher(html_transport({}, statuses={SITE + "/": 500}))
    with pytest.raises(FetchError, match="HTTP 500"):
        f.get(SITE + "/")
    f, _ = fetcher(html_transport({}, statuses={SITE + "/": 429}))
    with pytest.raises(FetchError, match="HTTP 429"):
        f.get(SITE + "/")


def test_client_errors_and_non_html_are_unreadable_pages():
    f, _ = fetcher(html_transport({SITE + "/file": "%PDF"}, headers={SITE + "/file": "application/pdf"}))
    page = f.get(SITE + "/file")
    assert not page.ok and "not HTML" in page.error
    page = f.get(SITE + "/missing")
    assert not page.ok and page.error == "HTTP 404"


def test_bodies_past_the_size_cap_are_truncated():
    big = "<html><body><p>" + "word " * 5000 + "</p><p>tail-marker</p></body></html>"
    f, _ = fetcher(html_transport({SITE + "/": big}), max_bytes=2000)
    page = f.get(SITE + "/")
    assert page.ok and page.truncated and "tail-marker" not in page.text


def test_requests_to_one_host_are_spaced_out():
    t = html_transport({SITE + "/": PAGE, SITE + "/about": PAGE, "https://other.example.org/": PAGE})
    f, clock = fetcher(t, per_host_seconds=2.0)
    f.get(SITE + "/")  # robots.txt, then the page: one wait between them
    f.get("https://other.example.org/")  # another host: no wait for that host's robots.txt
    f.get(SITE + "/about")
    assert clock.ticks == pytest.approx(4.0)


def test_the_user_agent_names_the_tool():
    t = html_transport({SITE + "/": PAGE})
    f, _ = fetcher(t)
    f.get(SITE + "/")
    assert all(h["User-Agent"].startswith("lead-engine-test/0.1") for _, h in t.calls)


def test_charset_from_content_type_is_used():
    t = html_transport({SITE + "/": "<p>caf\u00e9</p>"}, headers={SITE + "/": "text/html; charset=utf-8"})
    f, _ = fetcher(t)
    assert "caf\u00e9" in f.get(SITE + "/").text

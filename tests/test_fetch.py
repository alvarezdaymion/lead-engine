"""The polite fetcher: robots.txt, timeouts, size cap, per-host rate limit."""

import io
import urllib.request
import urllib.response
from email.message import Message

import pytest
from helpers import Clock, html_transport

from lead_engine.fetch import Disallowed, Fetcher, FetchError, Response

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


def test_bodies_past_the_size_cap_are_unreadable():
    big = "<html><body><p>" + "word " * 5000 + "</p><p>tail-marker</p></body></html>"
    f, _ = fetcher(html_transport({SITE + "/": big}), max_bytes=2000)
    with pytest.raises(FetchError, match="page size limit"):
        f.get(SITE + "/")


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


def test_native_transport_rejects_an_external_redirect_before_fetching_it(monkeypatch):
    calls = []

    class LocalPages(urllib.request.BaseHandler):
        handler_order = 100

        def https_open(self, request):
            calls.append(request.full_url)
            headers = Message()
            headers["Location"] = "https://outside.example.org/about"
            status = 404 if request.full_url.endswith("robots.txt") else 302
            result = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, status)
            result.msg = "Synthetic response"
            return result

    build = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: build(LocalPages(), *handlers))
    f = Fetcher("lead-engine-test", per_host_seconds=0)
    with pytest.raises(Disallowed):
        f.get(SITE + "/")
    assert calls == [SITE + "/robots.txt", SITE + "/"]


def test_redirect_destinations_obey_robots_and_request_spacing():
    transport = html_transport({SITE + "/robots.txt": "User-agent: *\nDisallow: /private\n",
                                SITE + "/about": PAGE})

    def redirect(url, *args):
        if url == SITE + "/":
            return Response(302, {"location": "/about"}, b"", url)
        if url == SITE + "/team":
            return Response(302, {"location": "/private"}, b"", url)
        return transport(url, *args)

    f, clock = fetcher(redirect)
    assert f.get(SITE + "/").final_url == SITE + "/about"
    with pytest.raises(Disallowed):
        f.get(SITE + "/team")
    assert SITE + "/private" not in [u for u, _ in transport.calls]
    assert clock.ticks == 6.0


@pytest.mark.parametrize("status", [200, 503, 0])
def test_fetch_diagnostics_and_snapshots_remove_url_secrets(status):
    url = "https://shop.example.com/about?token=private#private"

    def transport(target, *args):
        if target.endswith("robots.txt"):
            return Response(404, {}, b"", target)
        if status == 0:
            raise OSError("connection failed for " + target)
        return Response(status, {"content-type": "text/html"}, PAGE.encode(), target)

    f, _ = fetcher(transport)
    try:
        result = str(f.get(url).to_dict())
    except (FetchError, Disallowed) as error:
        result = str(error)
    assert "private" not in result and "token=" not in result and "user:" not in result

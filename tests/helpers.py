"""Shared test helpers: a fake clock, fake transports and an engine wired to them. Tests never touch the
network."""

import datetime as dt

from lead_engine.fetch import Response

START = dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.UTC)


class Clock:
    """A clock the test moves by hand; sleeping only advances a counter."""

    def __init__(self, start=START):
        self.now = start
        self.ticks = 0.0

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += dt.timedelta(minutes=minutes)

    def sleep(self, seconds):
        self.ticks += seconds

    def monotonic(self):
        return self.ticks


def html_transport(pages, statuses=None, timeouts=None, headers=None):
    """A fake transport over {url: body}. Unknown URLs are 404 (so robots.txt is absent unless given)."""
    statuses = statuses or {}
    timeouts = dict(timeouts or {})
    calls = []

    def transport(url, sent_headers, timeout, max_bytes):
        calls.append((url, sent_headers))
        if timeouts.get(url, 0) > 0:
            timeouts[url] -= 1
            raise TimeoutError("timed out")
        if url in statuses:
            return Response(statuses[url], {"content-type": "text/html"}, b"", url)
        if url in pages:
            ctype = "text/plain" if url.endswith("robots.txt") else "text/html; charset=utf-8"
            ctype = (headers or {}).get(url, ctype)
            return Response(200, {"content-type": ctype}, pages[url].encode()[: max_bytes + 1], url)
        return Response(404, {"content-type": "text/html"}, b"", url)

    transport.calls = calls
    return transport


def make_engine(tmp_path, clock, transport=None, writer=None, suppressions=None, settings=None, discover=None,
                handoff=None, voice=None):
    """An engine on the demo's local sites (no faults unless `transport` adds them)."""
    from lead_engine import config
    from lead_engine.demo import SiteTransport
    from lead_engine.engine import Engine
    from lead_engine.fetch import Fetcher
    from lead_engine.gate import Suppressions
    from lead_engine.store import Store
    from lead_engine.voice import EXAMPLE, load_voice
    from lead_engine.writer import TemplateWriter

    cfg = config.from_dict(settings or {}, tmp_path)
    voice = voice or load_voice(EXAMPLE)
    store = Store(tmp_path / "state" / "leads.db", clock=clock)
    fetcher = Fetcher("lead-engine-test/0.1", transport=transport or SiteTransport(timeouts={}, down=set()),
                      sleep=clock.sleep, monotonic=clock.monotonic)
    return Engine(cfg.data, store, fetcher=fetcher, writer=writer or TemplateWriter(voice), voice=voice,
                  suppressions=suppressions if suppressions is not None else Suppressions(), clock=clock,
                  discover=discover, handoff=handoff)


def add_site(store, host, company, market="Cedar Hollow, OR", keyword="bookkeeping firm", source="test"):
    return store.add_lead(domain=host, company=company, website="https://%s/" % host, market=market,
                          keyword=keyword, source=source)

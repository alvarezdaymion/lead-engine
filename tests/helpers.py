"""Shared test helpers: a fake clock and fake transports. Tests never touch the network."""

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

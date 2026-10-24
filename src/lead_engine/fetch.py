"""A polite fetcher for business websites.

- robots.txt is read once per host and obeyed. A missing robots.txt (404) allows everything, a 401 or
  403 disallows everything, and one that can't be read (5xx, timeout) is a transient failure, so the
  lead is retried later instead of crawled.
- Every request has a timeout and a size cap; bodies past the cap are truncated, never buffered whole.
- Requests to the same host are spaced at least `per_host_seconds` apart.
- The user agent names this tool honestly; it never pretends to be a browser.

`transport` is swappable so tests and the demo serve local HTML with no network.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .evidence import Page
from .html import parse_html


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str  # final URL after redirects


Transport = Callable[[str, dict[str, str], float, int], Response]


class FetchError(Exception):
    """A transient failure (timeout, connection error, 429 or 5xx). The step is retried later."""


class Disallowed(Exception):
    """robots.txt does not allow this URL for our user agent."""


def urllib_transport(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> Response:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(max_bytes + 1), r.geturl())
    except urllib.error.HTTPError as e:
        body = e.read(max_bytes + 1) if e.fp else b""
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body, url)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


@dataclass
class Fetcher:
    user_agent: str
    timeout: float = 15.0
    max_bytes: int = 1_000_000
    per_host_seconds: float = 2.0
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    requests: int = 0
    _robots: dict[str, RobotFileParser] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)

    def _request(self, url: str) -> Response:
        host = host_of(url)
        last = self._last.get(host)
        if last is not None:
            wait = self.per_host_seconds - (self.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"}
        self.requests += 1
        try:
            resp = self.transport(url, headers, self.timeout, self.max_bytes)
        except OSError as e:  # TimeoutError, URLError and socket errors are all OSError
            reason = "timed out" if isinstance(e, TimeoutError) else getattr(e, "reason", None) or type(e).__name__
            raise FetchError("%s (%s)" % (url, reason)) from None
        finally:
            self._last[host] = self.monotonic()
        if resp.status == 429 or resp.status >= 500:
            raise FetchError("%s returned HTTP %d" % (url, resp.status))
        return resp

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        key = "%s://%s" % (parts.scheme, parts.netloc.lower())
        rp = self._robots.get(key)
        if rp is None:
            rp = RobotFileParser()
            resp = self._request(key + "/robots.txt")  # FetchError propagates: retry later
            if resp.status in (401, 403):
                rp.disallow_all = True
            elif resp.status >= 400:
                rp.allow_all = True
            else:
                rp.parse(resp.body[: self.max_bytes].decode("utf-8", "replace").splitlines())
            self._robots[key] = rp
        return rp.can_fetch(self.user_agent, url)

    def get(self, url: str) -> Page:
        """Fetch one page. Raises Disallowed or FetchError; a 4xx or non-HTML answer is a page with
        ok=False, because retrying it would not help."""
        if not self.allowed(url):
            raise Disallowed(url)
        resp = self._request(url)
        if resp.status >= 400:
            return Page(url, resp.url, resp.status, False, error="HTTP %d" % resp.status)
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype.lower():
            return Page(url, resp.url, resp.status, False, error="not HTML (%s)" % (ctype or "no content type"))
        truncated = len(resp.body) > self.max_bytes
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            body = resp.body[: self.max_bytes].decode(charset, "replace")
        except LookupError:
            body = resp.body[: self.max_bytes].decode("utf-8", "replace")
        parsed = parse_html(body, resp.url)
        return Page(url, resp.url, resp.status, True, parsed.title, parsed.text, parsed.links, parsed.emails,
                    parsed.jsonld, truncated)

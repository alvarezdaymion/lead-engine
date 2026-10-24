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
from urllib.robotparser import RobotFileParser

from .errors import InputError, SafeError, diagnostic
from .evidence import Page
from .html import parse_html
from .http import NoRedirect, join_url, response_complete, response_headers, safe_url, split_url


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str  # requested URL; transports must not follow redirects


Transport = Callable[[str, dict[str, str], float, int], Response]


class FetchError(SafeError):
    """A transient failure (timeout, connection error, 429 or 5xx). The step is retried later."""


class Disallowed(SafeError):
    """robots.txt does not allow this URL for our user agent."""


class OffsiteRedirect(Disallowed):
    """A redirect would leave the business's own host."""


def urllib_transport(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> Response:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as r:
            return Response(r.status, response_headers(r.headers), r.read(max_bytes + 1), r.geturl())
    except urllib.error.HTTPError as e:
        body = e.read(max_bytes + 1) if e.fp else b""
        return Response(e.code, response_headers(e.headers or {}), body, url)


def host_of(url: str) -> str:
    try:
        return (split_url(url).hostname or "").lower().removeprefix("www.")
    except InputError:
        return ""


def _parts(url: str):
    try:
        return split_url(url)
    except InputError as error:
        raise FetchError(str(error)) from None


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
            resp.headers = response_headers(resp.headers)
            if type(resp.status) is not int or not 100 <= resp.status <= 599:
                raise FetchError("invalid HTTP status: %s" % safe_url(url))
            if len(resp.body) > self.max_bytes:
                kind = "robots.txt" if url.endswith("/robots.txt") else "page"
                raise FetchError("%s exceeded the %s size limit" % (safe_url(url), kind))
            if not response_complete(resp.headers, resp.body, resp.status):
                kind = "robots.txt" if url.endswith("/robots.txt") else "HTTP"
                raise FetchError("incomplete %s response: %s" % (kind, safe_url(url)))
        except Exception as e:
            reason = "timed out" if isinstance(e, TimeoutError) else diagnostic(e)
            raise FetchError("%s (%s)" % (safe_url(url), reason)) from None
        finally:
            self._last[host] = self.monotonic()
        if resp.status == 429 or resp.status >= 500:
            raise FetchError("%s returned HTTP %d" % (safe_url(url), resp.status))
        return resp

    def _follow(self, url: str, *, robots: bool = False) -> Response:
        host = host_of(url)
        for _ in range(6):
            parts = _parts(url)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None:
                raise Disallowed("unsupported public URL: %s" % safe_url(url))
            if host_of(url) != host:
                raise OffsiteRedirect("redirect leaves the business's host: %s" % safe_url(url))
            if not robots and not self.allowed(url):
                raise Disallowed(safe_url(url))
            resp = self._request(url)
            if resp.url != url:
                _parts(resp.url)
                if host_of(resp.url) != host:
                    raise OffsiteRedirect("redirect leaves the business's host: %s" % safe_url(resp.url))
                raise FetchError("transport followed a redirect without checking robots.txt")
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise FetchError("redirect has no destination: %s" % safe_url(url))
                try:
                    url = join_url(url, location)
                except InputError as error:
                    raise FetchError(str(error)) from None
                continue
            if 300 <= resp.status < 400:
                raise FetchError("unsupported redirect: %s" % safe_url(url))
            return resp
        raise FetchError("too many redirects: %s" % safe_url(url))

    def allowed(self, url: str) -> bool:
        parts = _parts(url)
        key = "%s://%s" % (parts.scheme, parts.netloc.lower())
        rp = self._robots.get(key)
        if rp is None:
            rp = RobotFileParser()
            resp = self._follow(key + "/robots.txt", robots=True)
            if resp.status in (401, 403):
                rp.disallow_all = True
            elif resp.status >= 400:
                rp.allow_all = True
            else:
                try:
                    rp.parse(resp.body.decode("utf-8", "replace").splitlines())
                except Exception as error:
                    raise FetchError("robots.txt parsing failed: %s" % diagnostic(error)) from None
            self._robots[key] = rp
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception as error:
            raise FetchError("robots.txt permission check failed: %s" % diagnostic(error)) from None

    def get(self, url: str) -> Page:
        """Fetch one page. Raises Disallowed or FetchError; a 4xx or non-HTML answer is a page with
        ok=False, because retrying it would not help."""
        resp = self._follow(url)
        url, final_url = safe_url(url), safe_url(resp.url)
        if resp.status >= 400:
            return Page(url, final_url, resp.status, False, error="HTTP %d" % resp.status)
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype.lower():
            return Page(url, final_url, resp.status, False, error="not HTML")
        truncated = len(resp.body) > self.max_bytes
        if truncated:
            raise FetchError("%s exceeded the page size limit" % safe_url(url))
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            body = resp.body[: self.max_bytes].decode(charset, "replace")
        except LookupError:
            body = resp.body[: self.max_bytes].decode("utf-8", "replace")
        parsed = parse_html(body, resp.url)
        if not parsed.complete:
            raise FetchError("incomplete HTML scan: %s" % safe_url(url))
        return Page(url, final_url, resp.status, True, parsed.title, parsed.text,
                    [(safe_url(link), text) for link, text in parsed.links], parsed.emails,
                    parsed.jsonld, truncated, elements=parsed.elements, sections=parsed.sections,
                    quotations=parsed.quotations, headings=parsed.headings, rejected=parsed.rejected,
                    rejected_contexts=parsed.rejected_contexts, http_complete=True)

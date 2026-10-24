"""A tiny JSON-over-HTTPS client for vendor APIs. Stdlib only.

`transport` is swappable so tests never touch the network. Errors never include request headers or
query strings, so API keys can't leak into logs, state or the terminal.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from . import __version__
from .errors import InputError, SafeError, diagnostic

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]

USER_AGENT = "lead-engine/%s (+https://github.com/alvarezdaymion/lead-engine)" % __version__


class HttpError(SafeError, RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        """Rate limits, server errors and network failures are worth retrying later."""
        return self.status == 0 or self.status == 429 or self.status >= 500


def split_url(url: str) -> urllib.parse.SplitResult:
    try:
        if not isinstance(url, str):
            raise TypeError
        parts = urllib.parse.urlsplit(url)
        _ = parts.port
        return parts
    except (ValueError, TypeError, UnicodeError) as error:
        raise InputError("URL parsing failed: %s (<invalid url>)" % type(error).__name__) from None


def join_url(base: str, relative: str) -> str:
    try:
        joined = urllib.parse.urljoin(base, relative)
        split_url(joined)
        return joined
    except (ValueError, TypeError, UnicodeError) as error:
        raise InputError("URL joining failed: %s (<invalid url>)" % type(error).__name__) from None


def safe_url(url: str) -> str:
    try:
        parts = split_url(url)
    except InputError:
        return "<invalid url>"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "<invalid url>"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def response_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        key = key.lower()
        result[key] = result[key] + ", " + value if key in result else value
    return result


def response_complete(headers: dict[str, str], body: bytes, status: int = 200) -> bool:
    if (status in (206, 226) or "content-range" in headers
            or "multipart/byteranges" in headers.get("content-type", "").lower()):
        return False
    declared = headers.get("content-length")
    if declared is None:
        return True
    if not isinstance(declared, str) or not declared.isascii() or not declared.isdecimal():
        return False
    try:
        return int(declared) == len(body)
    except ValueError:
        return False


def _read_response(response: Any, status: int) -> bytes:
    body = response.read()
    if not response_complete(response_headers(response.headers), body, status):
        raise HttpError("incomplete HTTP response")
    return body


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as resp:
            return resp.status, _read_response(resp, resp.status)
    except urllib.error.HTTPError as e:
        return e.code, _read_response(e, e.code)


class Http:
    def __init__(self, headers: dict[str, str] | None = None, transport: Transport | None = None,
                 timeout: float = 60.0):
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        self.transport = transport or urllib_transport
        self.timeout = timeout

    def _do(self, method: str, url: str, body: bytes | None, extra: dict[str, str]) -> Any:
        where = "%s %s" % (method, safe_url(url))
        if safe_url(url) == "<invalid url>":
            raise HttpError("%s failed: invalid URL" % where)
        try:
            status, raw = self.transport(method, url, {**self.headers, **extra}, body, self.timeout)
        except Exception as e:
            raise HttpError("%s failed: %s" % (where, diagnostic(e))) from None
        if type(status) is not int or not 100 <= status <= 599:
            raise HttpError("%s returned an invalid HTTP status" % where)
        if status in (206, 226):
            raise HttpError("%s returned an incomplete HTTP response" % where)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise HttpError("%s returned HTTP %d with invalid JSON" % (where, status), status) from None
        if not 200 <= status < 300:
            raise HttpError("%s returned HTTP %d" % (where, status), status)
        return data

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            if params:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        except Exception as error:
            raise HttpError("GET %s failed: %s" % (safe_url(url), diagnostic(error))) from None
        return self._do("GET", url, None, {})

    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        try:
            body = json.dumps(payload).encode()
        except Exception as error:
            raise HttpError("POST %s failed: %s" % (safe_url(url), diagnostic(error))) from None
        return self._do("POST", url, body, {"Content-Type": "application/json", **(headers or {})})

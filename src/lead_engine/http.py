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
from http.client import HTTPException
from typing import Any

from . import __version__

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]

USER_AGENT = "lead-engine/%s (+https://github.com/alvarezdaymion/lead-engine)" % __version__


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        """Rate limits, server errors and network failures are worth retrying later."""
        return self.status == 0 or self.status == 429 or self.status >= 500


def safe_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Http:
    def __init__(self, headers: dict[str, str] | None = None, transport: Transport | None = None,
                 timeout: float = 60.0):
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        self.transport = transport or urllib_transport
        self.timeout = timeout

    def _do(self, method: str, url: str, body: bytes | None, extra: dict[str, str]) -> Any:
        where = "%s %s" % (method, safe_url(url))
        try:
            status, raw = self.transport(method, url, {**self.headers, **extra}, body, self.timeout)
        except (OSError, HTTPException, ValueError) as e:
            raise HttpError("%s failed: %s" % (where, type(e).__name__)) from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise HttpError("%s returned HTTP %d with invalid JSON" % (where, status), status) from None
        if not 200 <= status < 300:
            raise HttpError("%s returned HTTP %d" % (where, status), status)
        return data

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return self._do("GET", url, None, {})

    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        body = json.dumps(payload).encode()
        return self._do("POST", url, body, {"Content-Type": "application/json", **(headers or {})})

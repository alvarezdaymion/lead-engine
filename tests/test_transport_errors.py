"""Native request validation failures expose only controlled diagnostic fields."""

import http.client
import json
import urllib.request

import pytest
from helpers import Clock, add_site, make_engine

from lead_engine.fetch import Response, urllib_transport
from lead_engine.http import Http, HttpError
from lead_engine.instantly import InstantlyClient

SITE = "https://shop.example.com/"
MARKER = "synthetic-secret-marker"


@pytest.mark.parametrize("error", [http.client.InvalidURL, http.client.HTTPException, ValueError])
def test_fetch_native_validation_errors_never_persist_request_secrets(tmp_path, monkeypatch, error):
    class Opener:
        def open(self, *args, **kwargs):
            raise error("Authorization: Bearer " + MARKER)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())

    def transport(url, headers, timeout, max_bytes):
        if url.endswith("robots.txt"):
            return Response(404, {}, b"", url)
        if url == SITE:
            return Response(302, {"location": SITE + "about?token=" + MARKER + " invalid"}, b"", url)
        return urllib_transport(url, headers, timeout, max_bytes)

    engine = make_engine(tmp_path, Clock(), transport=transport)
    ident = add_site(engine.store, "shop.example.com", "Shop")
    result = engine.tick(discover=False, handoff=False)
    diagnostics = json.dumps(result) + json.dumps(engine.store.events()) + engine.store.get(ident)["last_error"]
    assert MARKER not in diagnostics and "Bearer" not in diagnostics and "?token=" not in diagnostics
    assert engine.store.get(ident)["attempts"] == 1


@pytest.mark.parametrize("error", [http.client.InvalidURL, http.client.HTTPException, ValueError])
def test_api_native_validation_errors_hide_headers_and_query_secrets(monkeypatch, error):
    class Opener:
        def open(self, *args, **kwargs):
            raise error("Authorization: Bearer " + MARKER)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(HttpError) as caught:
        Http({"Authorization": "Bearer " + MARKER}).get(SITE + "?token=" + MARKER)
    assert MARKER not in str(caught.value) and "Bearer" not in str(caught.value)
    assert error.__name__ in str(caught.value)


def test_invalid_api_key_header_cannot_expose_the_bearer_value(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise ValueError("Invalid header value: Bearer " + MARKER)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(HttpError) as caught:
        InstantlyClient(MARKER + "\ninvalid").accounts()
    assert MARKER not in str(caught.value) and "Bearer" not in str(caught.value)

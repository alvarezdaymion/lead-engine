"""Vendor requests reject redirects and never expose server-provided error details."""

import io
import urllib.request
import urllib.response
from email.message import Message

import pytest

from lead_engine.http import Http, HttpError


def test_api_redirect_is_rejected_before_credentials_leave_the_origin(monkeypatch):
    calls = []

    class Vendor(urllib.request.BaseHandler):
        handler_order = 100

        def https_open(self, request):
            calls.append(request)
            headers = Message()
            headers["Location"] = "https://outside.example.org/accounts"
            result = urllib.response.addinfourl(io.BytesIO(b"{}"), headers, request.full_url, 302)
            result.msg = "Synthetic redirect"
            return result

    build = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: build(Vendor(), *handlers))
    with pytest.raises(HttpError):
        Http({"Authorization": "Bearer synthetic-key"}).get("https://api.example.com/accounts")
    assert len(calls) == 1 and calls[0].host == "api.example.com"


def test_api_errors_omit_echoed_credentials_and_sensitive_url_parts():
    client = Http({"Authorization": "Bearer synthetic-secret"},
                  lambda *args: (401, b'{"message": "bad key synthetic-secret"}'))
    with pytest.raises(HttpError) as error:
        client.get("https://user:synthetic-secret@api.example.com/accounts?key=synthetic-secret#synthetic-secret")
    assert "synthetic-secret" not in str(error.value) and "user:" not in str(error.value)

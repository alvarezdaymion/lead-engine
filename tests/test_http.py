"""Vendor requests reject redirects and never expose server-provided error details."""

import io
import json
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from types import SimpleNamespace

import pytest
from test_visible_invalidation import process

from lead_engine.handoff import run_handoff
from lead_engine.http import Http, HttpError, response_complete
from lead_engine.instantly import InstantlyClient

SITE = "https://api.example.com/reply"
API_CAP = 1_000_000


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


@pytest.mark.parametrize("padding", [" ", "\t", " \t"])
def test_content_length_accepts_only_http_optional_whitespace(padding):
    assert response_complete({"content-length": padding + "2" + padding}, b"{}")


@pytest.mark.parametrize("declared", [" 3\t", "2 0", "2\n", "2\r", "\u00a02", "\u0662", "+2", "2, 2", ""])
def test_content_length_whitespace_does_not_hide_malformed_or_mismatched_values(declared):
    assert not response_complete({"content-length": declared}, b"{}")


@pytest.mark.parametrize("padding", [" ", "\t"])
def test_whitespace_in_a_valid_handoff_reply_completes_intent_once(tmp_path, monkeypatch, padding):
    engine, lead = process(tmp_path)
    payload = b'{"created_leads":[{"index":0,"id":"lead-1"}]}'
    calls = []

    class Reply(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(len(payload)) + padding}

    def open_(*args, **kwargs):
        calls.append(args)
        return Reply(payload)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_))
    suppression = tmp_path / "suppressions.csv"
    suppression.write_text("email,domain\n")
    for _ in range(2):
        run_handoff(engine.store, {**engine.s["handoff"], "mode": "live"}, suppressions=engine.suppressions,
                    out_dir=tmp_path / "out", client=InstantlyClient("fake-key"), campaign_id="campaign-1",
                    suppression_file=suppression)
    assert len(calls) == 1 and engine.store.unresolved_handoffs() == 0
    assert engine.store.get(lead["id"])["stage"] == "handed_off"


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize("declared", [False, True])
def test_api_native_reads_are_bounded_and_oversized_bodies_never_reach_json(monkeypatch, status, declared):
    payload = b'{"ok":true}' + b" " * API_CAP
    reads, replies = [], []

    class Reply(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    def open_(*args, **kwargs):
        reply = Reply(payload)
        reply.status = status
        reply.headers = {"Content-Length": str(len(payload))} if declared else {}
        replies.append(reply)
        if status >= 400:
            raise urllib.error.HTTPError(SITE, status, "synthetic error", reply.headers, reply)
        return reply

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_))
    parsed = []
    monkeypatch.setattr(json, "loads", lambda raw: parsed.append(raw))
    with pytest.raises(HttpError, match="size limit") as caught:
        Http().get(SITE)
    assert caught.value.transient and not parsed
    assert reads == [API_CAP + 1]
    assert all(reply.closed for reply in replies)


def test_complete_api_json_at_the_size_cap_is_accepted(monkeypatch):
    payload = b'{"ok":true}'
    payload += b" " * (API_CAP - len(payload))

    class Reply(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(API_CAP)}

    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *args: SimpleNamespace(open=lambda *a, **k: Reply(payload)))
    assert Http().get(SITE) == {"ok": True}


def test_injected_api_transports_cannot_bypass_the_response_cap(monkeypatch):
    parsed = []
    monkeypatch.setattr(json, "loads", lambda raw: parsed.append(raw))
    with pytest.raises(HttpError, match="size limit"):
        Http(transport=lambda *args: (200, b" " * (API_CAP + 1))).get(SITE)
    assert not parsed

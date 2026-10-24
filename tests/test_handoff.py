"""Hand-off to the email platform (dry-run by default) and the Instantly client's guarded writes."""

import json
import urllib.parse

import pytest
from helpers import add_site, make_engine

from lead_engine.gate import Suppressions
from lead_engine.handoff import lead_payload, run_handoff
from lead_engine.http import HttpError
from lead_engine.instantly import InstantlyClient

KEY = "test-key-not-real"
SETTINGS = {"mode": "dry-run", "max_leads_per_run": 100, "batch_size": 100}


class FakeApi:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, json.loads(body) if body else None))
        assert headers["Authorization"] == "Bearer " + KEY
        key = (method, urllib.parse.urlsplit(url).path)
        if key not in self.routes:
            return 404, json.dumps({"message": "no route"}).encode()
        resp = self.routes[key]
        status, data = resp(url, body) if callable(resp) else (200, resp)
        return status, json.dumps(data).encode()


def ready_leads(tmp_path, clock, n=2):
    engine = make_engine(tmp_path, clock)
    for host, name in [("harbor-ledger.example.com", "Harbor Ledger Co."),
                       ("copperfield-inspections.example.org", "Copperfield Inspections")][:n]:
        add_site(engine.store, host, name)
    engine.tick()
    assert engine.store.counts()["ready"] == n
    return engine


def test_dry_run_writes_exactly_what_live_mode_would_send_and_sends_nothing(tmp_path, clock):
    engine = ready_leads(tmp_path, clock)
    settings = {**SETTINGS, "batch_size": 1}
    out = run_handoff(engine.store, settings, suppressions=Suppressions(), out_dir=tmp_path / "out", client=None,
                      campaign_id="camp-1")
    assert out["mode"] == "dry-run" and out["would_add"] == 2
    requests = json.loads(open(out["file"]).read())["requests"]
    assert [r["url"] for r in requests] == ["https://api.instantly.ai/api/v2/leads/add"] * 2  # one per batch
    assert requests[0]["body"]["leads"][0]["email"] == "dana.whitlock@example.com"
    assert engine.store.counts()["ready"] == 2  # still ready: going live later hands them off

    api = FakeApi({("POST", "/api/v2/leads/add"): {"created_leads": [{"index": 0, "id": "lead-a1"}]}})
    run_handoff(engine.store, {**settings, "mode": "live"}, suppressions=Suppressions(), out_dir=tmp_path / "out",
                client=InstantlyClient(KEY, transport=api), campaign_id="camp-1")
    assert [body for _, _, body in api.calls] == [r["body"] for r in requests]


def test_dry_run_without_a_campaign_uses_a_placeholder(tmp_path, clock):
    engine = ready_leads(tmp_path, clock, n=1)
    out = run_handoff(engine.store, SETTINGS, suppressions=Suppressions(), out_dir=tmp_path / "out", campaign_id="")
    body = json.loads(open(out["file"]).read())["requests"][0]["body"]
    assert body["campaign_id"] == "<INSTANTLY_CAMPAIGN_ID>" and body["skip_if_in_workspace"] is True


def test_live_hand_off_marks_created_leads_and_drops_skipped_ones(tmp_path, clock):
    engine = ready_leads(tmp_path, clock)
    api = FakeApi({("POST", "/api/v2/leads/add"): {
        "status": "success", "total_sent": 2, "leads_uploaded": 1, "duplicated_leads": 1, "in_blocklist": 0,
        "invalid_email_count": 0, "skipped_count": 0, "created_leads": [{"index": 0, "id": "lead-a1"}]}})
    client = InstantlyClient(KEY, transport=api)
    out = run_handoff(engine.store, {**SETTINGS, "mode": "live"}, suppressions=Suppressions(),
                      out_dir=tmp_path / "out", client=client, campaign_id="camp-1")
    assert out["added"] == 1 and out["skipped"] == 1
    sent = api.calls[0][2]
    assert sent["campaign_id"] == "camp-1" and sent["skip_if_in_workspace"] is True and len(sent["leads"]) == 2
    first, second = engine.store.by_stage(("handed_off", "dropped"))
    assert (first["stage"], first["platform_id"]) == ("handed_off", "lead-a1")
    assert (second["stage"], second["reason"]) == ("dropped", "platform_skipped")
    assert '"duplicated_leads": 1' in second["detail"]


def test_a_platform_failure_leaves_leads_ready_and_their_repairs_untouched(tmp_path, clock):
    engine = ready_leads(tmp_path, clock)
    api = FakeApi({("POST", "/api/v2/leads/add"): lambda u, b: (500, {"message": "upstream down"})})
    out = run_handoff(engine.store, {**SETTINGS, "mode": "live"}, suppressions=Suppressions(),
                      out_dir=tmp_path / "out", client=InstantlyClient(KEY, transport=api), campaign_id="camp-1")
    assert out["added"] == 0 and "HTTP 500" in out["errors"][0]
    assert all(x["stage"] == "ready" and x["attempts"] == 0 for x in engine.store.by_stage(("ready",)))


def test_suppressions_are_checked_again_right_before_sending(tmp_path, clock):
    engine = ready_leads(tmp_path, clock, n=1)
    out = run_handoff(engine.store, SETTINGS, suppressions=Suppressions(frozenset({"dana.whitlock@example.com"})),
                      out_dir=tmp_path / "out", campaign_id="")
    assert out["would_add"] == 0
    assert engine.store.reasons("dropped") == {"suppressed": 1}


def test_off_mode_and_a_missing_campaign_send_nothing(tmp_path, clock):
    engine = ready_leads(tmp_path, clock, n=1)
    assert run_handoff(engine.store, {**SETTINGS, "mode": "off"}, suppressions=Suppressions(),
                       out_dir=tmp_path) == {"mode": "off"}
    api = FakeApi({})
    out = run_handoff(engine.store, {**SETTINGS, "mode": "live"}, suppressions=Suppressions(), out_dir=tmp_path,
                      client=InstantlyClient(KEY, transport=api), campaign_id="")
    assert out["error"] == "set INSTANTLY_CAMPAIGN_ID" and api.calls == []


def test_the_payload_carries_the_opener_and_its_source(tmp_path, clock):
    engine = ready_leads(tmp_path, clock, n=1)
    p = lead_payload(engine.store.by_stage(("ready",))[0])
    assert set(p) == {"email", "first_name", "last_name", "company_name", "website", "personalization",
                      "custom_variables"}
    assert p["custom_variables"]["opener_source"].startswith("https://harbor-ledger.example.com/")


@pytest.mark.parametrize("path", ["/api/v2/campaigns/camp-1/activate", "/api/v2/campaigns", "/api/v2/accounts",
                                  "/api/v2/emails/reply", "/api/v2/leads"])
def test_any_other_write_is_refused_before_a_request_is_made(path):
    api = FakeApi({})
    client = InstantlyClient(KEY, transport=api)
    with pytest.raises(PermissionError):
        client._post(path, {})
    assert api.calls == []


def test_accounts_are_paged_and_errors_never_include_the_key():
    pages = iter([{"items": [{"email": "a@example.com"}], "next_starting_after": "c1"},
                  {"items": [{"email": "b@example.com"}]}])
    api = FakeApi({("GET", "/api/v2/accounts"): lambda u, b: (200, next(pages))})
    assert [a["email"] for a in InstantlyClient(KEY, transport=api).accounts()] == ["a@example.com", "b@example.com"]
    assert "starting_after=c1" in api.calls[1][1]
    broken = InstantlyClient(KEY, transport=FakeApi({}))
    with pytest.raises(HttpError) as e:
        broken.add_leads("camp-1", [{"email": "x@example.com"}])
    assert KEY not in str(e.value) and "HTTP 404" in str(e.value)


def test_add_leads_validates_its_input_and_a_missing_key_fails_clearly(monkeypatch):
    client = InstantlyClient(KEY, transport=FakeApi({}))
    with pytest.raises(ValueError):
        client.add_leads("", [{"email": "x@example.com"}])
    with pytest.raises(ValueError):
        client.add_leads("camp-1", [])
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="INSTANTLY_API_KEY"):
        InstantlyClient()

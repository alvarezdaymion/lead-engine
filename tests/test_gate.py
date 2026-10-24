"""The compliance gate before a lead becomes campaign-ready."""

import copy
import datetime as dt

import pytest
from helpers import add_site, html_transport, make_engine

from lead_engine.gate import Suppressions, gate, is_suppressed, load_suppressions
from lead_engine.outcome import Advance, Drop, StepError
from lead_engine.voice import EXAMPLE, Voice, load_voice

NOW = dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def written(tmp_path, clock):
    """A real lead taken through verify and write on the demo site, stopped before the gate."""
    engine = make_engine(tmp_path, clock)
    lead_id = add_site(engine.store, "harbor-ledger.example.com", "Harbor Ledger Co.")
    lead = engine.store.get(lead_id)
    for _ in range(2):
        out = engine.step(lead)
        engine.store.advance(lead_id, out.stage, out.data)
        lead = engine.store.get(lead_id)
    assert lead["stage"] == "written"
    return lead


NONE = Suppressions()


def run_gate(lead, suppressions=NONE, voice=None, dup=None):
    return gate(lead, suppressions=suppressions, voice=voice or load_voice(EXAMPLE), duplicate_of=dup, now=NOW)


def test_a_clean_lead_becomes_ready(written):
    out = run_gate(written)
    assert isinstance(out, Advance) and out.stage == "ready" and out.data["gated_at"] == "2026-03-02T09:00:00+00:00"


def test_suppressed_addresses_and_domains_are_dropped(written, tmp_path):
    p = tmp_path / "suppressions.csv"
    p.write_text("email,domain\nDana.Whitlock@example.com,\n,unsubscribed.example.org\n")
    s = load_suppressions(p)
    assert run_gate(written, s).reason == "suppressed"
    assert is_suppressed("x@example.net", "shop.unsubscribed.example.org", s)
    assert not is_suppressed("x@example.net", "shop.example.net", s)


def test_an_address_already_ready_elsewhere_is_a_duplicate(written):
    out = run_gate(written, dup=7)
    assert isinstance(out, Drop) and out.reason == "duplicate_email" and "#7" in out.detail


def test_evidence_that_no_longer_matches_its_page_fails(written):
    tampered = copy.deepcopy(written)
    for p in tampered["data"]["pages"]:
        p["text"] = p["text"].replace("Dana", "Pat")
    with pytest.raises(StepError, match="no longer matches"):
        run_gate(tampered)


def test_gate_rechecks_the_person_address_relationship(written):
    tampered = copy.deepcopy(written)
    data = tampered["data"]
    email_fact = next(f for f in data["facts"] if f["id"] == data["email"]["fact_id"])
    url = email_fact["source_url"]
    source = next(p for p in data["pages"] if p["final_url"] == url)
    context = "Dana Whitlock, Owner; payroll clerk Lee Sample answers at lee.s@example.com."
    source["text"] += "\n" + context
    source["emails"] = [("lee.s@example.com", context, "text")]
    email_fact.update(value={"email": "lee.s@example.com", "binding": "printed beside the name"},
                      quote=context, method="text")
    data["email"]["address"] = "lee.s@example.com"
    with pytest.raises(StepError):
        run_gate(tampered)


def test_gate_person_must_match_the_cited_person_facts(written):
    tampered = copy.deepcopy(written)
    tampered["data"]["person"].update(full_name="Pat Example", first_name="Pat", last_name="Example")
    for p in tampered["data"]["pages"]:
        p["text"] += "\nPat Example, Owner."
    email_fact = next(f for f in tampered["data"]["facts"] if f["id"] == tampered["data"]["email"]["fact_id"])
    source = next(p for p in tampered["data"]["pages"] if p["final_url"] == email_fact["source_url"])
    context = "Email Pat: pat@example.com"
    source["text"] += "\n" + context
    source["emails"] = [("pat@example.com", context, "text")]
    email_fact.update(value={"email": "pat@example.com", "binding": "address matches the name"},
                      quote=context, method="text")
    tampered["data"]["email"]["address"] = "pat@example.com"
    with pytest.raises(StepError):
        run_gate(tampered)


def test_gate_preserves_a_configured_specific_role(tmp_path, clock):
    from helpers import html_transport
    transport = html_transport({"https://shop.example.com/": "<p>Pat Example, Senior Principal.</p>"
                               "<p>Email Pat: pat@example.com</p><p>Founded in 2012.</p>"})
    engine = make_engine(tmp_path, clock, transport=transport, settings={"verify": {"roles": ["senior principal"]}})
    lead_id = add_site(engine.store, "shop.example.com", "Shop")
    engine.tick(discover=False, handoff=False)
    assert engine.store.get(lead_id)["stage"] == "ready"


def test_gate_uses_the_configured_role_even_when_a_stronger_role_is_present(tmp_path, clock):
    transport = html_transport({"https://shop.example.com/": "<p>Pat Example, President.</p>"
                               "<p>Pat Example founded the firm in 2012.</p><p>Email Pat: pat@example.com</p>"})
    engine = make_engine(tmp_path, clock, transport=transport, settings={"verify": {"roles": ["president"]}})
    lead_id = add_site(engine.store, "shop.example.com", "Shop")
    engine.tick(discover=False, handoff=False)
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "ready" and lead["data"]["person"]["role"] == "President"


def test_gate_accepts_individually_proven_nickname_variants(tmp_path, clock):
    site = "https://shop.example.com/"
    body = ('<script type="application/ld+json">{"@type":"LocalBusiness",'
            '"founder":{"@type":"Person","name":"Robert Example"}}</script>'
            '<p>Bob Example, Owner.</p><p>Email Robert: robert@example.com</p><p>Founded in 2012.</p>')
    engine = make_engine(tmp_path, clock, transport=html_transport({site: body}))
    lead_id = add_site(engine.store, "shop.example.com", "Shop")
    engine.tick(discover=False, handoff=False)
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "ready" and lead["attempts"] == 0
    names = {f["value"]["full_name"] for f in lead["data"]["facts"] if f["kind"] == "decision_maker"}
    assert names == {"Robert Example", "Bob Example"}


def test_an_opener_that_breaks_changed_voice_rules_goes_back_to_be_rewritten(written):
    voice = load_voice(EXAMPLE)
    stricter = Voice(**{**voice.__dict__, "banned_phrases": voice.banned_phrases + ["since"]})
    out = run_gate(written, voice=stricter)
    assert isinstance(out, Advance) and out.stage == "verified"
    assert "opener" not in out.data and "banned phrase" in out.data["rewrite_reason"]


def test_missing_suppression_file_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_suppressions(tmp_path / "nope.csv")


@pytest.mark.parametrize("text", ["", "address\nx@example.com\n", "email,domain\ninvalid,\n",
                                  "email,domain\n,https://example.com\n", "email,domain\nx@example.com\n",
                                  'email,domain\n"unfinished\n', "email,Domain\n,example.com\n",
                                  "email,domian\n,example.com\n"])
def test_malformed_suppressions_fail_closed(tmp_path, text):
    path = tmp_path / "suppressions.csv"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_suppressions(path)

"""The compliance gate before a lead becomes campaign-ready."""

import copy
import datetime as dt

import pytest
from helpers import add_site, make_engine

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


def test_an_opener_that_breaks_changed_voice_rules_goes_back_to_be_rewritten(written):
    voice = load_voice(EXAMPLE)
    stricter = Voice(**{**voice.__dict__, "banned_phrases": voice.banned_phrases + ["since"]})
    out = run_gate(written, voice=stricter)
    assert isinstance(out, Advance) and out.stage == "verified"
    assert "opener" not in out.data and "banned phrase" in out.data["rewrite_reason"]


def test_missing_suppression_file_is_an_empty_list(tmp_path):
    assert len(load_suppressions(tmp_path / "nope.csv")) == 0

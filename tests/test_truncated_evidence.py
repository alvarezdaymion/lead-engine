"""Incomplete pages cannot qualify a lead or cross either handoff boundary."""

import pytest
from helpers import Clock, add_site, html_transport, make_engine

from lead_engine.evidence import Page, supported
from lead_engine.handoff import run_handoff
from lead_engine.outcome import Advance, Drop

SITE = "https://shop.example.com/"
OWNER = "<p>Pat Example, Owner.</p>"
CONTACT = "<p>Email Pat Example: pat@example.com</p>"
FACT = "<p>Our firm was founded in 2012.</p>"


def engine_at(tmp_path, body):
    engine = make_engine(tmp_path, Clock(), transport=html_transport({SITE: body}))
    ident = add_site(engine.store, "shop.example.com", "Shop")
    return engine, ident


def test_a_capped_page_retries_even_when_the_read_prefix_contains_complete_templates(tmp_path):
    prefix = OWNER + CONTACT + FACT
    engine, ident = engine_at(tmp_path, prefix + "<p>Pat Example retired.</p>")
    engine.fetcher.max_bytes = len(prefix)
    engine.tick(discover=False, handoff=False)
    lead = engine.store.get(ident)
    assert (lead["stage"], lead["attempts"]) == ("discovered", 1)
    assert "size limit" in lead["last_error"]


@pytest.mark.parametrize("truncated", [True, None, "false", 0])
def test_gate_rejects_missing_malformed_or_truncated_snapshot_flags(tmp_path, truncated):
    engine, ident = engine_at(tmp_path, OWNER + CONTACT + FACT)
    for _ in range(2):
        out = engine.step(engine.store.get(ident))
        assert isinstance(out, Advance)
        engine.store.advance(ident, out.stage, out.data)
    lead = engine.store.get(ident)
    lead["data"]["pages"][0]["truncated"] = truncated
    out = engine.step(lead)
    assert isinstance(out, Drop) and out.reason == "incomplete_evidence"


@pytest.mark.parametrize("mode", ["dry-run", "live"])
def test_neither_handoff_mode_exports_or_posts_a_truncated_snapshot(tmp_path, mode):
    engine, ident = engine_at(tmp_path, OWNER + CONTACT + FACT)
    engine.tick(discover=False, handoff=False)
    lead = engine.store.get(ident)
    assert lead["stage"] == "ready"
    lead["data"]["pages"][0]["truncated"] = True
    engine.store.advance(ident, "ready", lead["data"])
    calls = []

    class Client:
        def add_leads(self, *args):
            calls.append(args)
            return {"created_leads": [{"index": 0, "id": "lead-1"}]}

    suppressions = tmp_path / "suppressions.csv"
    suppressions.write_text("email,domain\n")
    out = run_handoff(engine.store, {**engine.s["handoff"], "mode": mode}, suppressions=engine.suppressions,
                      client=Client(), campaign_id="campaign-1", suppression_file=suppressions,
                      out_dir=tmp_path / "out")
    assert not calls and not out.get("would_add")
    assert engine.store.get(ident)["reason"] == "incomplete_evidence"


def test_supported_never_proves_facts_from_a_truncated_snapshot(tmp_path):
    engine, ident = engine_at(tmp_path, OWNER + CONTACT + FACT)
    out = engine.step(engine.store.get(ident))
    assert isinstance(out, Advance)
    pages = [Page.from_dict(p) for p in out.data["pages"]]
    pages[0].truncated = True
    assert not any(supported(fact, pages, company="Shop") for fact in out.data["facts"])

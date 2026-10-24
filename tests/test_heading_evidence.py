"""Heading provenance and company facts use the same policy through snapshot replay."""

import json

import pytest
from helpers import Clock, add_site, html_transport, make_engine

from lead_engine.evidence import Page, complete, supported
from lead_engine.handoff import run_handoff
from lead_engine.html import parse_html
from lead_engine.outcome import Advance, Drop

SITE = "https://shop.example.com/"
IDENTITY = "<p>Pat Example, Owner.</p>"
CONTACT = "<p>Email Pat Example: pat@example.com</p>"
FACT = "<p>Our firm was founded in 2012.</p>"


def pipeline(tmp_path, body, company="Shop", *, stop=False):
    engine = make_engine(tmp_path, Clock(), transport=html_transport({SITE: body}))
    ident = add_site(engine.store, "shop.example.com", company)
    if stop:
        for _ in range(2):
            out = engine.step(engine.store.get(ident))
            assert isinstance(out, Advance)
            engine.store.advance(ident, out.stage, out.data)
    else:
        engine.tick(discover=False, handoff=False)
    return engine, engine.store.get(ident)


def test_heading_ancestry_serializes_as_exact_individual_strings():
    parsed = parse_html("<h1>Ledger / Payroll</h1><h2>Our <b>team</b></h2>" + IDENTITY + CONTACT, SITE)
    assert parsed.headings == ["Ledger / Payroll", "Our team"]
    assert ("Pat Example, Owner.", [("Ledger / Payroll", True), ("Our team", True)]) in parsed.sections
    assert "Ledger / Payroll" not in parsed.elements
    page = Page(SITE, SITE, 200, True, text=parsed.text, elements=parsed.elements,
                sections=parsed.sections, headings=parsed.headings)
    restored = Page.from_dict(json.loads(json.dumps(page.to_dict())))
    assert restored.sections == page.sections
    assert restored.headings == page.headings


@pytest.mark.parametrize("heading", ["Pat Example", "<strong>Pat</strong> Example", "PAT EXAMPLE"])
def test_proven_person_heading_does_not_invalidate_identity_or_contact(tmp_path, heading):
    body = "<h2>%s</h2>" % heading + IDENTITY + CONTACT + "<h2>Services</h2>" + FACT
    engine, lead = pipeline(tmp_path, body, stop=True)
    assert complete(lead["data"])
    out = engine.step(lead)
    assert isinstance(out, Advance) and out.stage == "ready"


@pytest.mark.parametrize("fact", [
    "Pat Example was founded in 2012.",
    "Pat Example opened in 2012.",
    "Pat Example has served Cedar Hollow since 2012.",
    "Pat Example specializes in bookkeeping.",
    "Pat Example won Best Shop in 2025.",
])
def test_owner_named_company_facts_remain_eligible_through_gate_replay(tmp_path, fact):
    engine, lead = pipeline(tmp_path, IDENTITY + CONTACT + "<p>%s</p>" % fact, "Pat Example", stop=True)
    pages = [Page.from_dict(p) for p in lead["data"]["pages"]]
    assert all(supported(f, pages, company="Pat Example", market="Cedar Hollow, OR")
               for f in lead["data"]["facts"])
    out = engine.step(lead)
    assert isinstance(out, Advance) and out.stage == "ready"


def test_two_company_facts_in_one_element_invalidate_a_repeated_person_name(tmp_path):
    facts = "<p>Pat Example opened in 2012. Pat Example was founded in 2012.</p>"
    _, lead = pipeline(tmp_path, IDENTITY + CONTACT + facts, "Pat Example")
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("company,heading,stage", [
    ("Ledger / Payroll", "Ledger / Payroll", "ready"),
    ("Shop", "About / Contact", "dropped"),
    ("Shop", "About / Contact / Team", "dropped"),
])
def test_literal_heading_separators_never_change_heading_boundaries(tmp_path, company, heading, stage):
    _, lead = pipeline(tmp_path, "<h2>%s</h2>" % heading + IDENTITY + CONTACT + FACT, company)
    assert lead["stage"] == stage


def test_nested_permitted_headings_remain_individually_permitted(tmp_path):
    _, lead = pipeline(tmp_path, "<h1>About</h1><h2>Contact</h2>" + IDENTITY + CONTACT + FACT)
    assert lead["stage"] == "ready"


@pytest.mark.parametrize("extra", [
    "<p>Pat Example</p>",
    "<h3>Pat Example retired.</h3>",
    "<p>Pat Example was founded in 2012 or 2013.</p>",
    "<p>Pat Example has served Other Place since 2012.</p>",
])
def test_allowed_heading_does_not_hide_unsupported_body_or_business_claims(tmp_path, extra):
    body = "<h2>Pat Example</h2>" + IDENTITY + CONTACT + extra + FACT
    _, lead = pipeline(tmp_path, body, "Pat Example")
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("extra", ["<p>Pat Example</p>", "<h3>Pat Example retired.</h3>"])
def test_gate_replays_provenance_before_exempting_person_headings(tmp_path, extra):
    engine, lead = pipeline(tmp_path, IDENTITY + CONTACT + FACT, stop=True)
    parsed = parse_html("<h2>Pat Example</h2>" + IDENTITY + CONTACT + extra + FACT, SITE)
    lead["data"]["pages"][0].update(text=parsed.text, elements=parsed.elements, sections=parsed.sections,
                                    headings=parsed.headings, quotations=parsed.quotations)
    out = engine.step(lead)
    assert isinstance(out, Drop) and out.reason == "unproven_evidence"


@pytest.mark.parametrize("mode", ["dry-run", "live"])
@pytest.mark.parametrize("malformed", ["missing-provenance", "joined-ancestry", "non-string-heading"])
def test_handoff_drops_unreplayable_heading_metadata_before_any_write(tmp_path, mode, malformed):
    engine, lead = pipeline(tmp_path, IDENTITY + CONTACT + FACT)
    assert lead["stage"] == "ready"
    page = lead["data"]["pages"][0]
    if malformed == "missing-provenance":
        page.pop("headings", None)
    elif malformed == "joined-ancestry":
        page["sections"] = [["Pat Example, Owner.", "About / Contact"]]
    else:
        page["sections"] = [["Pat Example, Owner.", [None]]]
    engine.store.advance(lead["id"], "ready", lead["data"])
    calls = []

    class Client:
        def add_leads(self, *args):
            calls.append(args)
            return {"created_leads": [{"index": 0, "id": "lead-1"}]}

    suppression = tmp_path / "suppressions.csv"
    suppression.write_text("email,domain\n")
    out = run_handoff(engine.store, {**engine.s["handoff"], "mode": mode}, suppressions=engine.suppressions,
                      out_dir=tmp_path / "out", client=Client(), campaign_id="campaign-1",
                      suppression_file=suppression)
    assert not calls and not out.get("would_add")
    assert engine.store.get(lead["id"])["reason"] == "incomplete_evidence"


@pytest.mark.parametrize("previous,new", [("h2", "h1"), ("h2", "h2"), ("h1", "h1")])
@pytest.mark.parametrize("kind", ["identity", "contact"])
def test_new_heading_templates_discard_finished_sibling_ancestry_during_replay(tmp_path, previous, new, kind):
    heading = "Pat Example, Owner." if kind == "identity" else "Email Pat Example: pat@example.com"
    body = IDENTITY if kind == "contact" else ""
    body += "<%s>Clients</%s><%s>%s</%s>" % (previous, previous, new, heading, new)
    body += "<h1>Contact</h1>" + CONTACT + "<h1>Services</h1>" + FACT
    engine, lead = pipeline(tmp_path, body, stop=True)
    snapshot = Page.from_dict(json.loads(json.dumps(lead["data"]["pages"][0])))
    assert all(supported(fact, [snapshot], company="Shop", market=lead["market"])
               for fact in lead["data"]["facts"])
    out = engine.step(lead)
    assert isinstance(out, Advance) and out.stage == "ready"


@pytest.mark.parametrize("heading,parents", [("Pat Example, Owner.", [("About", True)]),
                                            ("Our team", [("About", True)])])
def test_opening_heading_clears_its_level_before_recording_text(heading, parents):
    parsed = parse_html("<h1>About</h1><h2>Clients</h2><h3>Other Shop</h3><h2>%s</h2>" % heading, SITE)
    assert (heading, parents) in parsed.sections


def test_heading_templates_still_require_their_genuine_parent_section(tmp_path):
    body = "<h1>Other Shop</h1><h2>Pat Example, Owner.</h2><h1>Contact</h1>" + CONTACT
    body += "<h1>Services</h1>" + FACT
    _, lead = pipeline(tmp_path, body)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")

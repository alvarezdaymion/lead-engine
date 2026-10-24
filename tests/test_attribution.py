"""Attribution requires a complete, unambiguous local assertion and reproducible snapshots."""

import pytest
from helpers import Clock, add_site, html_transport, make_engine

from lead_engine.evidence import Page, supported
from lead_engine.gate import gate
from lead_engine.html import parse_html
from lead_engine.outcome import Advance, Drop
from lead_engine.verify import find_people, opener_facts

SITE = "https://shop.example.com/"
PROVEN = "<p>Our firm was founded in 2013.</p>"
OWNER = "<p>Pat Example, Owner.</p>"
CONTACT = "<p>Email Pat Example: pat@example.com</p>"


def process(tmp_path, body, other_pages=None, stop_before_gate=False):
    engine = make_engine(tmp_path, Clock(), transport=html_transport({SITE: body, **(other_pages or {})}))
    lead_id = add_site(engine.store, "shop.example.com", "Shop")
    if stop_before_gate:
        for _ in range(2):
            result = engine.step(engine.store.get(lead_id))
            assert isinstance(result, Advance)
            engine.store.advance(lead_id, result.stage, result.data)
    else:
        engine.tick(discover=False, handoff=False)
    return engine, engine.store.get(lead_id)


@pytest.mark.parametrize("anchor", ["Email", "pat@example.com"])
@pytest.mark.parametrize("repeated", [False, True])
def test_staff_mailto_and_repeated_addresses_never_bind_to_owner(tmp_path, anchor, repeated):
    staff = '<p>Pat Sample handles payroll: <a href="mailto:pat@example.com">%s</a></p>' % anchor
    _, lead = process(tmp_path, OWNER + (CONTACT if repeated else "") + staff + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_direct_email")


def test_html_preserves_each_contact_occurrence_and_its_element():
    parsed = parse_html('<p>Pat Example: <a href="mailto:pat@example.com">Email</a></p>'
                        '<p>Lee Sample: <a href="mailto:pat@example.com">Email</a></p>', SITE)
    assert parsed.emails == [("pat@example.com", "Pat Example: Email", "mailto"),
                             ("pat@example.com", "Lee Sample: Email", "mailto")]


@pytest.mark.parametrize("claim", [
    "<h2>Our supplier: Other Shop</h2><p>Founded in 2013.</p>",
    "<p>Founded in 2013, Other Shop supplies our paper.</p>",
    "<p>Our firm was not founded in 2013.</p>",
    "<p>Founded in 2013.</p>",
    "<p>Our firm was founded in 2013, unlike Other Shop.</p>",
    "<p>Voted Best Bookkeeper in 2025: Other Shop.</p>",
    "<h2>Client case study: Other Shop</h2><p>We specialize in payroll.</p>",
    "<p>Our customer says we specialize in payroll.</p>",
])
def test_only_explicit_unambiguous_business_assertions_supply_openers(tmp_path, claim):
    _, lead = process(tmp_path, OWNER + CONTACT + claim)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_opener_facts")


@pytest.mark.parametrize("status", ["Pat Example recently retired.", "Pat Example has quietly stepped down.",
                                    "Pat Example, who retired in 2024, no longer works here.",
                                    "Pat Example is no longer our owner."])
def test_personal_departure_overrides_a_schema_role(tmp_path, status):
    schema = ('<script type="application/ld+json">{"@type":"LocalBusiness","name":"Shop",'
              '"founder":{"@type":"Person","name":"Pat Example"}}</script>')
    _, lead = process(tmp_path, schema + "<p>%s</p>" % status + CONTACT + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("description", ["Pat Example has an assistant who handles bookings.",
                                         "Pat Example employs an assistant for bookings."])
def test_extra_personal_descriptions_are_outside_the_allowlist(tmp_path, description):
    _, lead = process(tmp_path, OWNER + "<p>%s</p>" % description + CONTACT + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_gate_reproduces_departure_and_contact_decisions_from_all_pages(tmp_path):
    about = SITE + "about"
    body = ('<a href="/about">About</a>' + OWNER + '<p>Pat Sample, CEO.</p>'
            '<p>Email Pat Sample: pat@example.com</p>' + PROVEN)
    engine, lead = process(tmp_path, body, {about: "<p>Pat Example recently retired.</p>"}, stop_before_gate=True)
    assert {p["final_url"] for p in lead["data"]["pages"]} == {SITE, about}
    result = engine.step(lead)
    assert isinstance(result, Advance) and result.stage == "ready"
    assert result.data["person"]["full_name"] == "Pat Sample"
    lead["data"]["pages"] = [p for p in lead["data"]["pages"] if p["final_url"] != about]
    result = gate(lead, suppressions=engine.suppressions, voice=engine.voice, duplicate_of=None,
                  now=engine.clock(), roles=engine.s["verify"]["roles"])
    assert isinstance(result, Drop) and result.reason == "incomplete_evidence"


@pytest.mark.parametrize("names", [("Bob", "Robert"), ("Robert", "Bob")])
@pytest.mark.parametrize("contact_name", [0, 1])
def test_each_individually_proven_variant_binds_the_contact(tmp_path, names, contact_name):
    body = "".join("<p>%s Example, Owner.</p>" % name for name in names)
    chosen = names[contact_name]
    _, lead = process(tmp_path, body + "<p>Email %s Example: %s@example.com</p>" % (chosen, chosen.lower()) + PROVEN)
    assert lead["stage"] == "ready"
    assert len([f for f in lead["data"]["facts"] if f["kind"] == "decision_maker"]) == 2


def test_an_unproven_nickname_is_not_a_contact_variant(tmp_path):
    _, lead = process(tmp_path, "<p>Bob Example, Owner.</p><p>Email Robert: robert@example.com</p>" + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_direct_email")


@pytest.mark.parametrize("identity", ["Pat Example is not our owner.", "Pat Example, Owner of Other Shop.",
                                      "Our client Pat Example, Owner of Other Shop.",
                                      "Pat Example, Owner of Acme.", "Pat Example founded Acme in 2013.",
                                      "Pat Example, assistant to the Owner."])
def test_identity_requires_a_local_current_role_for_the_prospect(tmp_path, identity):
    _, lead = process(tmp_path, "<p>%s</p>" % identity + CONTACT + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_role_cards_cannot_be_assembled_across_unrelated_elements(tmp_path):
    _, lead = process(tmp_path, "<div>Pat Example</div><div>Owner</div>" + CONTACT + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_bare_address_does_not_prove_a_personal_contact(tmp_path):
    _, lead = process(tmp_path, OWNER + "<p>pat@example.com</p>" + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_direct_email")


@pytest.mark.parametrize("context", ["Pat Example, Owner; email Lee: pat@example.com",
                                     "Email Pat Example, or ask Lee at pat@example.com"])
def test_an_owner_mention_does_not_bind_another_contacts_address(tmp_path, context):
    _, lead = process(tmp_path, OWNER + "<p>%s</p>" % context + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_quoted_fragment_cannot_hide_ambiguous_fact_subject():
    page = Page(SITE, SITE, 200, True, text="Founded in 2013, Other Shop supplies our paper.")
    assert not opener_facts([page], company="Shop")
    fact = {"kind": "founded", "value": {"year": "2013"}, "source_url": SITE,
            "quote": "Founded in 2013", "method": "text", "subject": "Shop"}
    assert not supported(fact, [page], company="Shop")


def test_plain_text_lines_cannot_supply_an_html_role_card():
    page = Page(SITE, SITE, 200, True, text="Pat Example\nOwner")
    assert find_people([page], ["owner"], "Shop") == []


def test_client_section_does_not_supply_the_prospects_owner(tmp_path):
    body = '<h2>Our client: Other Shop</h2>' + OWNER + CONTACT + '<h2>Shop</h2>' + PROVEN
    _, lead = process(tmp_path, body)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_schema_affiliation_must_name_the_prospect(tmp_path):
    schema = ('<script type="application/ld+json">{"@type":"LocalBusiness","name":"Other Shop",'
              '"owner":{"@type":"Person","name":"Pat Example"}}</script>')
    _, lead = process(tmp_path, schema + CONTACT + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_schema_title_cannot_override_a_conflicting_business_affiliation(tmp_path):
    schema = ('<script type="application/ld+json">{"@type":"LocalBusiness","name":"Shop",'
              '"owner":{"@type":"Person","name":"Pat Example","jobTitle":"Owner of Acme"}}</script>')
    _, lead = process(tmp_path, schema + CONTACT + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_retired_schema_founder_cannot_be_restored_by_a_role_card(tmp_path):
    schema = ('<script type="application/ld+json">{"@type":"LocalBusiness","name":"Shop",'
              '"founder":{"@type":"Person","name":"Pat Example"}}</script>')
    _, lead = process(tmp_path, schema + '<div><p>Pat Example</p><p>Owner</p><p>Retired</p></div>' + CONTACT + PROVEN)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_legacy_snapshots_without_complete_attribution_metadata_fail_closed(tmp_path):
    engine, lead = process(tmp_path, OWNER + CONTACT + PROVEN, stop_before_gate=True)
    lead["data"].pop("evidence_version", None)
    result = engine.step(lead)
    assert isinstance(result, Drop) and result.reason == "incomplete_evidence"


def test_gate_drops_disqualifying_evidence_instead_of_repairing_it(tmp_path):
    engine, lead = process(tmp_path, OWNER + CONTACT + PROVEN, stop_before_gate=True)
    lead["data"]["pages"][0]["text"] += "\nPat Example, who recently retired, no longer works here."
    lead["data"]["pages"][0]["elements"].append("Pat Example, who recently retired, no longer works here.")
    result = engine.step(lead)
    assert isinstance(result, Drop) and result.reason == "unproven_evidence"


def test_gate_reconciles_a_conflicting_address_occurrence(tmp_path):
    engine, lead = process(tmp_path, OWNER + CONTACT + PROVEN, stop_before_gate=True)
    snapshot = lead["data"]["pages"][0]
    context = "Lee Sample: pat@example.com"
    snapshot["text"] += "\n" + context
    snapshot["emails"].append(("pat@example.com", context, "text"))
    result = engine.step(lead)
    assert isinstance(result, Drop) and result.reason == "unproven_evidence"

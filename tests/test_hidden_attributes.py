"""Encoded attributes cannot manufacture evidence or hide visible conflicts."""

from types import SimpleNamespace

import pytest
from test_visible_invalidation import BASE, CONTACT, FACT, OWNER, SITE, process, replace_snapshot

from lead_engine.handoff import run_handoff
from lead_engine.html import parse_html
from lead_engine.outcome import Drop


def attribute_value(text):
    return bytes([42, *(ord(char) ^ 42 for char in text)]).hex()


def attribute_element(text):
    return '<p data-cfemail="%s"></p>' % attribute_value(text)


def protected_contact():
    return '<p><a href="/cdn-cgi/l/email-protection#%s">Email Pat Example</a></p>' % attribute_value("pat@example.com")


MAIL_CONTACT = '<p><a href="mailto:pat@example.com">Email Pat Example</a></p>'
HIDDEN_BODY = "".join(attribute_element(text) for text in
                      ("Pat Example, Owner.", "Email Pat Example: pat@example.com", "Our firm was founded in 2012."))


@pytest.mark.parametrize("text", ["Pat Example, Owner.", "Email Pat Example: pat@example.com",
                                 "Our firm was founded in 2012.", "Pat Example retired."])
def test_hidden_attributes_never_become_snapshot_text_or_contacts(text):
    parsed = parse_html(attribute_element(text), SITE)
    assert parsed.complete
    assert parsed.text == ""
    assert not parsed.elements and not parsed.emails and not parsed.headings
    assert not parsed.rejected and not parsed.rejected_contexts


@pytest.mark.parametrize("body,reason", [
    (attribute_element("Pat Example, Owner.") + CONTACT + FACT, "decision_maker_not_found"),
    (OWNER + attribute_element("Email Pat Example: pat@example.com") + FACT, "no_direct_email"),
    (OWNER + CONTACT + attribute_element("Our firm was founded in 2012."), "no_opener_facts"),
    (HIDDEN_BODY, "decision_maker_not_found"),
    (OWNER + protected_contact() + FACT, "decision_maker_not_found"),
])
def test_hidden_attributes_and_protection_links_cannot_qualify_leads(tmp_path, body, reason):
    _, lead = process(tmp_path, body)
    assert (lead["stage"], lead["reason"]) == ("dropped", reason)


@pytest.mark.parametrize("text", ["Pat Example retired.", "Email Lee Sample: pat@example.com"])
def test_hidden_attribute_values_do_not_manufacture_conflicts_either(tmp_path, text):
    _, lead = process(tmp_path, BASE + attribute_element(text))
    assert lead["stage"] == "ready"


@pytest.mark.parametrize("text,reason", [("Pat Example retired.", "decision_maker_not_found"),
                                        ("Email Lee Sample: pat@example.com", "no_direct_email")])
def test_visible_conflicts_in_protection_markup_still_invalidate_proof(tmp_path, text, reason):
    extra = '<p><a data-cfemail="%s" href="/cdn-cgi/l/email-protection#%s">%s</a></p>' % (
        attribute_value("ignored@example.org"), attribute_value("ignored@example.org"), text)
    _, lead = process(tmp_path, BASE + extra)
    assert (lead["stage"], lead["reason"]) == ("dropped", reason)


@pytest.mark.parametrize("contact", [CONTACT, MAIL_CONTACT,
                                     '<p><a href="/cdn-cgi/l/email-protection#00">'
                                     'Email Pat Example: pat@example.com</a></p>'])
def test_visible_contact_templates_and_actual_mailto_links_remain_provable(tmp_path, contact):
    _, lead = process(tmp_path, OWNER + contact + FACT)
    assert lead["stage"] == "ready"


@pytest.mark.parametrize("kind", ["attributes", "protection-link"])
@pytest.mark.parametrize("boundary", ["gate", "dry-run", "live"])
def test_each_boundary_replays_the_visible_only_scan(tmp_path, kind, boundary):
    original = BASE if kind == "attributes" else OWNER + MAIL_CONTACT + FACT
    engine, lead = process(tmp_path, original, stop=boundary == "gate")
    replace_snapshot(lead, HIDDEN_BODY if kind == "attributes" else OWNER + protected_contact() + FACT)
    if boundary == "gate":
        result = engine.step(lead)
        assert isinstance(result, Drop) and result.reason == "unproven_evidence"
        return
    assert_handoff_drops(engine, lead, tmp_path, boundary, "unproven_evidence")


@pytest.mark.parametrize("boundary", ["gate", "dry-run", "live"])
def test_scans_that_could_include_decoded_attributes_require_fresh_qualification(tmp_path, boundary):
    engine, lead = process(tmp_path, stop=boundary == "gate")
    lead["data"]["pages"][0]["text_scan_version"] = 2
    if boundary == "gate":
        result = engine.step(lead)
        assert isinstance(result, Drop) and result.reason == "incomplete_evidence"
        return
    assert_handoff_drops(engine, lead, tmp_path, boundary, "incomplete_evidence")


def assert_handoff_drops(engine, lead, tmp_path, mode, reason):
    engine.store.advance(lead["id"], "ready", lead["data"])
    calls = []
    suppressions = tmp_path / "suppressions.csv"
    suppressions.write_text("email,domain\n")
    result = run_handoff(engine.store, {**engine.s["handoff"], "mode": mode}, suppressions=engine.suppressions,
                         out_dir=tmp_path / "out", client=SimpleNamespace(add_leads=lambda *args: calls.append(args)),
                         campaign_id="campaign-1", suppression_file=suppressions)
    assert not calls and not result.get("would_add")
    saved = engine.store.get(lead["id"])
    assert (saved["stage"], saved["reason"]) == ("dropped", reason)

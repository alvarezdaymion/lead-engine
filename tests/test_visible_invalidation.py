"""Every unaccepted visible occurrence counts against identity and contact proof."""

import pytest
from helpers import Clock, add_site, html_transport, make_engine

from lead_engine.handoff import run_handoff
from lead_engine.html import parse_html
from lead_engine.outcome import Advance, Drop

SITE = "https://shop.example.com/"
OWNER = "<p>Pat Example, Owner.</p>"
CONTACT = "<p>Email Pat Example: pat@example.com</p>"
FACT = "<p>Our firm was founded in 2012.</p>"
BASE = OWNER + CONTACT + FACT


def process(tmp_path, body=BASE, *, stop=False):
    engine = make_engine(tmp_path, Clock(), transport=html_transport({SITE: body}))
    ident = add_site(engine.store, "shop.example.com", "Shop")
    if stop:
        for _ in range(2):
            out = engine.step(engine.store.get(ident))
            assert isinstance(out, Advance)
            engine.store.advance(ident, out.stage, out.data)
    else:
        engine.tick(discover=False, handoff=False)
    return engine, engine.store.get(ident)


def replace_snapshot(lead, body):
    parsed = parse_html(body, SITE)
    lead["data"]["pages"][0].update(text=parsed.text, elements=parsed.elements, emails=parsed.emails,
                                    sections=parsed.sections, headings=parsed.headings, rejected=parsed.rejected,
                                    rejected_contexts=parsed.rejected_contexts)


@pytest.mark.parametrize("suffix", ["RETIRED", "DECEASED", "Smith retired", "Stone retired", "CEO retired"])
@pytest.mark.parametrize("tag", ["p", "div", "h2", "pre"])
def test_any_whole_word_name_repeat_in_unaccepted_text_invalidates_person(tmp_path, suffix, tag):
    _, lead = process(tmp_path, BASE + "<%s>Pat Example %s.</%s>" % (tag, suffix, tag))
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_accepted_other_person_templates_use_their_exact_captured_full_name(tmp_path):
    other = "<p>Pat Example Smith, CEO.</p><p>Email Pat Example Smith: lee@example.com</p>"
    _, lead = process(tmp_path, OWNER + CONTACT + other + FACT)
    assert lead["stage"] == "ready"
    assert lead["data"]["person"]["full_name"] == "Pat Example"


@pytest.mark.parametrize("body,reason", [
    (BASE + "<p><svg><text>Pat Example retired.</text></svg></p>", "decision_maker_not_found"),
    (BASE + "<p>Email Lee Sample: <svg><text>pat@example.com</text></svg></p>", "no_direct_email"),
    (BASE + "<p>Email Lee Sample: pat@<svg><text>example.com</text></svg></p>", "no_direct_email"),
    (BASE + "<svg><text>Email Lee Sample: pat@example.com</text></svg>", "no_direct_email"),
    (BASE + "<h2><svg><text>Pat Example RETIRED.</text></svg></h2>", "decision_maker_not_found"),
])
def test_svg_text_is_ineligible_evidence_but_always_counts_against_proof(tmp_path, body, reason):
    _, lead = process(tmp_path, body)
    assert (lead["stage"], lead["reason"]) == ("dropped", reason)


@pytest.mark.parametrize("tag", ["div", "section", "article", "p", "span"])
def test_nested_layout_children_cannot_split_a_repeated_name_or_address(tmp_path, tag):
    extra = "<li>Pat <%s>Example retired.</%s></li>" % (tag, tag)
    _, lead = process(tmp_path, BASE + extra)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_accepted_descendant_templates_are_exempt_inside_rejected_parents(tmp_path):
    _, lead = process(tmp_path, BASE + "<li><p>Pat Example, Owner.</p></li>")
    assert lead["stage"] == "ready"


def test_gate_checks_complete_rejected_context_around_an_unaccepted_plain_child(tmp_path):
    engine, lead = process(tmp_path, stop=True)
    replace_snapshot(lead, BASE + "<li>Pat <p>Example retired.</p></li>")
    out = engine.step(lead)
    assert isinstance(out, Drop) and out.reason == "unproven_evidence"


@pytest.mark.parametrize("heading", ["<q>Our team</q>", "Our team<img>", "Our team<custom></custom>",
                                     "Our team<svg></svg>"])
def test_ineligible_heading_cannot_authorize_evidence_in_its_section(tmp_path, heading):
    body = "<section><h2>%s</h2>" % heading + OWNER + CONTACT + "</section>" + FACT
    _, lead = process(tmp_path, body)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("closing", ["</h2><h2>Services</h2>", ""])
def test_nested_blocks_cannot_escape_a_heading_before_it_closes(tmp_path, closing):
    _, lead = process(tmp_path, CONTACT + "<h2>Our team" + OWNER + closing + FACT)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("extra", ["<p>Pat Example RETIRED.</p>",
                                 "<p>Email Lee Sample: <svg><text>pat@example.com</text></svg></p>"])
def test_gate_replays_every_visible_conflict_from_the_saved_snapshot(tmp_path, extra):
    engine, lead = process(tmp_path, stop=True)
    replace_snapshot(lead, BASE + extra)
    out = engine.step(lead)
    assert isinstance(out, Drop) and out.reason == "unproven_evidence"


@pytest.mark.parametrize("mode", ["dry-run", "live"])
@pytest.mark.parametrize("problem", ["svg-conflict", "ineligible-heading", "old-scan"])
def test_both_handoff_modes_reject_unproven_or_incompletely_scanned_snapshots(tmp_path, mode, problem):
    engine, lead = process(tmp_path)
    assert lead["stage"] == "ready"
    if problem == "svg-conflict":
        replace_snapshot(lead, BASE + "<svg><text>Email Lee Sample: pat@example.com</text></svg>")
    elif problem == "ineligible-heading":
        replace_snapshot(lead, "<h2><q>Our team</q></h2>" + BASE)
    else:
        lead["data"]["pages"][0].pop("text_scan_version", None)
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
    saved = engine.store.get(lead["id"])
    assert saved["stage"] == "dropped"
    assert saved["reason"] == ("incomplete_evidence" if problem == "old-scan" else "unproven_evidence")

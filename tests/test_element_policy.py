"""Only plain block elements containing one complete template supply evidence."""

import json

import pytest
from helpers import Clock, add_site, html_transport, make_engine

from lead_engine.evidence import Page, supported
from lead_engine.html import parse_html
from lead_engine.outcome import Advance, Drop

SITE = "https://shop.example.com/"
OWNER = "<p>Pat Example, Owner.</p>"
CONTACT = "<p>Email Pat Example: pat@example.com</p>"
FACT = "<p>Our firm was founded in 2012.</p>"


def process(tmp_path, identity=OWNER, contact=CONTACT, fact=FACT, extra="", company="Shop", *, stop=False):
    engine = make_engine(tmp_path, Clock(), transport=html_transport({SITE: identity + contact + fact + extra}))
    ident = add_site(engine.store, "shop.example.com", company)
    if stop:
        for _ in range(2):
            out = engine.step(engine.store.get(ident))
            assert isinstance(out, Advance)
            engine.store.advance(ident, out.stage, out.data)
    else:
        engine.tick(discover=False, handoff=False)
    return engine, engine.store.get(ident)


@pytest.mark.parametrize("tag", ["label", "button", "cite", "q", "blockquote", "pre", "figure", "div", "svg"])
def test_any_non_formatting_child_rejects_the_entire_identity_element(tmp_path, tag):
    _, lead = process(tmp_path, identity="<p>Pat Example, Owner <%s>until 2020</%s>.</p>" % (tag, tag))
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("child", ["<hr>", "<img>", "<input>", "<svg></svg>", "<p></p>", "<custom></custom>"])
def test_even_textless_non_formatting_children_reject_each_evidence_kind(tmp_path, child):
    _, lead = process(tmp_path, identity="<p>Pat Example, Owner.%s</p>" % child)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("tag", ["div", "section", "body", "pre", "figure", "label", "button"])
def test_non_evidence_containers_never_supply_identity_text(tmp_path, tag):
    _, lead = process(tmp_path, identity="<%s>Pat Example, Owner.</%s>" % (tag, tag))
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("tag", ["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "dt", "dd", "address"])
def test_fixed_evidence_tags_with_plain_inline_formatting_qualify(tmp_path, tag):
    identity = "<%s><strong>Pat</strong> <span>Example</span>, <em>Owner</em>.</%s>" % (tag, tag)
    _, lead = process(tmp_path, identity=identity + "<h1>Contact</h1>", fact="<h1>Services</h1>" + FACT)
    assert lead["stage"] == "ready"


@pytest.mark.parametrize("kind", ["identity", "contact", "fact"])
def test_a_second_sentence_rejects_the_whole_evidence_element(tmp_path, kind):
    kwargs = {kind: {"identity": "<p>Pat Example, Owner. Our team is small.</p>",
                     "contact": "<p>Email Pat Example: pat@example.com. Contact us.</p>",
                     "fact": "<p>Our firm was founded in 2012. We specialize in payroll.</p>"}[kind]}
    _, lead = process(tmp_path, **kwargs)
    assert lead["stage"] == "dropped"


@pytest.mark.parametrize("extra", ["<p>Our guide is titled <q>Owner</q>.</p>",
                                 "<p>Our guide is titled <q>2012</q>.</p>"])
def test_unrelated_quoted_elements_do_not_reject_plain_evidence_elsewhere(tmp_path, extra):
    _, lead = process(tmp_path, extra=extra)
    assert lead["stage"] == "ready"


@pytest.mark.parametrize("other", ["Pat Example Smith", "Pat Example Stone"])
@pytest.mark.parametrize("description", [", CEO.", " retired."])
def test_longer_whole_names_never_invalidate_an_owner_with_a_name_prefix(tmp_path, other, description):
    _, lead = process(tmp_path, extra="<p>%s%s</p>" % (other, description))
    assert lead["stage"] == "ready"
    assert lead["data"]["person"]["full_name"] == "Pat Example"


@pytest.mark.parametrize("extra", ["<div>Pat Example, Owner.</div>", "<p>Pat Example retired.</p>",
                                 "<p>Pat Example, Owner. Our team is small.</p>"])
def test_name_repeats_in_ineligible_text_invalidate_a_separately_proven_person(tmp_path, extra):
    _, lead = process(tmp_path, extra=extra)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("tag", ["q", "cite", "label", "button", "custom"])
def test_ineligible_children_cannot_hide_a_repeated_full_name(tmp_path, tag):
    extra = "<p>Pat <%s>Example</%s> retired.</p>" % (tag, tag)
    _, lead = process(tmp_path, extra=extra)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("tag", ["q", "cite", "label", "button", "custom"])
def test_ineligible_children_cannot_hide_a_conflicting_address_occurrence(tmp_path, tag):
    extra = "<p>Email Lee Sample: pat@<%s>example.com</%s></p>" % (tag, tag)
    _, lead = process(tmp_path, extra=extra)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_direct_email")


@pytest.mark.parametrize("name", ["PAT EXAMPLE", "pat example", "Pat EXAMPLE"])
def test_casing_cannot_hide_an_unsupported_repeat_of_the_same_whole_name(tmp_path, name):
    _, lead = process(tmp_path, extra="<p>%s retired.</p>" % name)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_an_ambiguous_run_of_repeated_names_does_not_hide_the_owner(tmp_path):
    _, lead = process(tmp_path, extra="<p>Pat Example Pat Example</p>")
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("company", ["Shop Inc.", "Shop Co.", "Shop Ltd."])
def test_company_abbreviations_end_whole_elements_without_sentence_splitting(tmp_path, company):
    _, lead = process(tmp_path, company=company, identity="<p>Pat Example is the owner of %s</p>" % company)
    assert lead["stage"] == "ready"


@pytest.mark.parametrize("fact", ["Our firm was founded in 2012", "Our firm opened in 2012",
                                 "We specialize in payroll", "Our firm has served Cedar Hollow since 2012",
                                 "We won Best Shop in 2025"])
def test_opener_templates_allow_one_optional_final_period(tmp_path, fact):
    _, lead = process(tmp_path, fact="<p>%s</p>" % fact)
    assert lead["stage"] == "ready"


def test_mailto_template_allows_one_optional_final_period(tmp_path):
    _, lead = process(tmp_path, contact='<p><a href="mailto:pat@example.com">Email Pat Example.</a></p>')
    assert lead["stage"] == "ready"


def test_gate_replays_structural_rejection_and_all_retained_text(tmp_path):
    engine, lead = process(tmp_path, stop=True)
    parsed = parse_html("<p>Pat Example, Owner <label>until 2020</label>.</p>" + CONTACT + FACT, SITE)
    lead["data"]["pages"][0].update(text=parsed.text, elements=parsed.elements, emails=parsed.emails,
                                    sections=parsed.sections, headings=parsed.headings, rejected=parsed.rejected)
    out = engine.step(lead)
    assert isinstance(out, Drop) and out.reason == "unproven_evidence"


def test_snapshots_replay_the_element_policy_after_serialization(tmp_path):
    _, lead = process(tmp_path, extra="<p>Our guide is titled <q>Owner</q>.</p>")
    assert lead["stage"] == "ready"
    pages = [Page.from_dict(p) for p in json.loads(json.dumps(lead["data"]["pages"]))]
    assert pages[0].rejected
    assert all(supported(f, pages, company="Shop", market="Cedar Hollow, OR") for f in lead["data"]["facts"])

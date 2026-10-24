"""Only documented whole assertions may qualify a lead or survive gate replay."""

import json

import pytest
from helpers import Clock, add_site, html_transport, make_engine

from lead_engine.evidence import Page, supported
from lead_engine.handoff import run_handoff
from lead_engine.outcome import Advance
from lead_engine.verify import find_people, opener_facts

SITE = "https://shop.example.com/"
IDENTITY = "<p>Pat Example, Owner.</p>"
CONTACT = "<p>Email Pat Example: pat@example.com</p>"
FACT = "<p>Our firm was founded in 2012.</p>"


def pipeline(tmp_path, identity=IDENTITY, contact=CONTACT, fact=FACT, stop=False):
    engine = make_engine(tmp_path, Clock(), transport=html_transport({SITE: identity + contact + fact}))
    ident = add_site(engine.store, "shop.example.com", "Shop")
    if stop:
        for _ in range(2):
            out = engine.step(engine.store.get(ident))
            assert isinstance(out, Advance)
            engine.store.advance(ident, out.stage, out.data)
    else:
        engine.tick(discover=False, handoff=False)
    return engine, engine.store.get(ident)


@pytest.mark.parametrize("text", ["Pat Example, Owner", "Pat Example - Owner", "Owner: Pat Example",
    "Pat Example is the owner of Shop", "Pat Example is our owner", "Meet Pat Example, our owner"])
def test_exact_identity_forms_and_gate_replay(tmp_path, text):
    engine, lead = pipeline(tmp_path, identity="<p>%s.</p>" % text, stop=True)
    out = engine.step(lead)
    assert isinstance(out, Advance) and out.stage == "ready"


@pytest.mark.parametrize("tail", [" until 2020", " from 2001 to 2020", " or CEO", "?", ", has an assistant",
                                  ", age 40", " of Other Shop", " since 2010"])
def test_role_templates_reject_all_extra_text(tmp_path, tail):
    _, lead = pipeline(tmp_path, identity="<p>Pat Example, Owner%s</p>" % tail)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("identity", ["<p>Pat Example, Owner<br>of Other Shop.</p>",
    "<p>Pat Example, Owner<br>until 2020.</p>",
    IDENTITY + "<p>Pat Example, our founder,<br>has retired.</p>"])
def test_identity_line_breaks_cannot_hide_extra_claims(tmp_path, identity):
    _, lead = pipeline(tmp_path, identity=identity)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("suffix", ["of Other Shop.", "until 2020.", "or President."])
def test_plain_text_page_newlines_are_not_sentence_boundaries(tmp_path, suffix):
    body = "<body>Pat Example, Owner\n%s</body>" % suffix
    engine = make_engine(tmp_path, Clock(), transport=html_transport({
        SITE: '<a href="/about">About</a>' + CONTACT + FACT,
        SITE + "about": body,
    }))
    ident = add_site(engine.store, "shop.example.com", "Shop")
    engine.tick(discover=False, handoff=False)
    lead = engine.store.get(ident)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("extra", ["Lee Sample recently retired.", "Our previous office was downtown.",
                                  "Our assistant handles bookings."])
def test_independent_sentences_do_not_modify_a_complete_identity(tmp_path, extra):
    _, lead = pipeline(tmp_path, identity="<p>Pat Example, Owner. %s</p>" % extra)
    assert lead["stage"] == "ready"


@pytest.mark.parametrize("prefix", ["Do not use this address:", "Our supplier's contact:", "Call someone else:"])
@pytest.mark.parametrize("method", ["text", "mailto"])
def test_contact_elements_keep_text_before_breaks(tmp_path, prefix, method):
    value = "Email Pat Example: pat@example.com" if method == "text" else (
        '<a href="mailto:pat@example.com">Email Pat Example</a>')
    _, lead = pipeline(tmp_path, contact="<p>%s<br>%s</p>" % (prefix, value))
    assert lead["stage"] == "dropped" and lead["reason"] in {"no_direct_email", "decision_maker_not_found"}


@pytest.mark.parametrize("value", ["Pat Example: pat@example.com", "Email Pat Example: pat@example.com",
                                   "Pat Example pat@example.com"])
def test_only_full_name_contact_forms_are_accepted(tmp_path, value):
    _, lead = pipeline(tmp_path, contact="<p>%s</p>" % value)
    assert lead["stage"] == "ready"


@pytest.mark.parametrize("value", ["Email Pat: pat@example.com", "Reach Pat Example at pat@example.com",
                                   "Email Pat Example directly: pat@example.com",
                                   "Questions? Email Pat Example: pat@example.com"])
def test_other_contact_phrasings_drop(tmp_path, value):
    _, lead = pipeline(tmp_path, contact="<p>%s</p>" % value)
    assert lead["stage"] == "dropped" and lead["reason"] in {"no_direct_email", "decision_maker_not_found"}


@pytest.mark.parametrize("anchor", ["Pat Example", "Email Pat Example"])
def test_exact_mailto_elements_qualify(tmp_path, anchor):
    _, lead = pipeline(tmp_path, contact='<p><a href="mailto:pat@example.com">%s</a></p>' % anchor)
    assert lead["stage"] == "ready"


@pytest.mark.parametrize("fact", ["Our firm was founded in 2012?", "Our firm was founded in 2012 or 2013.",
    "Our firm was founded in 2012-2013.", "Our firm was founded in about 2012.",
    "Our firm was founded in 2012, before Other Shop.", "We specialize in payroll or tax preparation.",
    "Our firm specializes in payroll for Other Shop.", "We won Best Shop or Best Bookkeeper in 2025."])
def test_fact_forms_cannot_consume_alternatives_or_hedges(tmp_path, fact):
    _, lead = pipeline(tmp_path, fact="<p>%s</p>" % fact)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_opener_facts")


@pytest.mark.parametrize("fact,kind,value", [
    ("Shop was founded in 2012.", "founded", {"year": "2012"}),
    ("Our shop opened in 2012.", "founded", {"year": "2012"}),
    ("Our team has served Cedar Hollow since 2012.", "serving", {"place": "Cedar Hollow", "year": "2012"}),
    ("Our company specializes in tax preparation.", "specialty", {"specialty": "tax preparation"}),
    ("We won Best Bookkeeper in 2025.", "award", {"award": "Best Bookkeeper", "year": "2025"}),
])
def test_exact_fact_forms_are_supported_and_survive_the_gate(tmp_path, fact, kind, value):
    _, lead = pipeline(tmp_path, fact="<p>%s</p>" % fact)
    assert lead["stage"] == "ready"
    stored = next(f for f in lead["data"]["facts"] if f["kind"] == kind)
    assert stored["value"] == value
    pages = [Page.from_dict(p) for p in lead["data"]["pages"]]
    assert supported(stored, pages, company="Shop", market="Cedar Hollow, OR")


def test_serving_place_does_not_prove_a_founding_year():
    page = Page(SITE, SITE, 200, True, text="Our firm has served Other Shop since 2012.")
    assert not opener_facts([page], company="Shop", market="Cedar Hollow, OR")


def test_schema_requires_person_title_and_direct_business_affiliation(tmp_path):
    node = {"@type": "Person", "name": "Pat Example", "jobTitle": "Owner", "worksFor": {"name": "Shop"}}
    _, lead = pipeline(tmp_path, identity='<script type="application/ld+json">%s</script>' % json.dumps(node))
    assert lead["stage"] == "ready"
    for field in ("@type", "name", "jobTitle", "worksFor"):
        altered = dict(node)
        altered.pop(field)
        page = Page(SITE, SITE, 200, True, jsonld=[altered])
        assert find_people([page], ["owner"], "Shop") == []


@pytest.mark.parametrize("body", [
    '<blockquote><p>Our firm specializes in payroll.</p><p>Other Shop</p></blockquote>',
    '<h2>Other Shop</h2><p>Our firm was founded in 2012.</p>',
])
def test_third_party_structural_context_never_supplies_a_fact(tmp_path, body):
    _, lead = pipeline(tmp_path, fact=body)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_opener_facts")


@pytest.mark.parametrize("mode", ["dry-run", "live"])
@pytest.mark.parametrize("version", [None, 1, 2])
def test_handoff_never_exports_or_posts_legacy_ready_records(tmp_path, mode, version):
    engine, lead = pipeline(tmp_path)
    assert lead["stage"] == "ready"
    data = lead["data"]
    data.pop("evidence_version", None)
    if version is not None:
        data["evidence_version"] = version
    engine.store.db.execute("UPDATE leads SET data_json = ? WHERE id = ?", (json.dumps(data), lead["id"]))
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
    assert (saved["stage"], saved["reason"]) == ("dropped", "incomplete_evidence")


def test_handoff_rejects_a_malformed_current_snapshot_manifest(tmp_path):
    engine, lead = pipeline(tmp_path)
    lead["data"].update(pages_read=[None], pages=[{"ok": True}])
    engine.store.advance(lead["id"], "ready", lead["data"])
    out = run_handoff(engine.store, engine.s["handoff"], suppressions=engine.suppressions, out_dir=tmp_path / "out")
    assert out["would_add"] == 0
    assert engine.store.get(lead["id"])["reason"] == "incomplete_evidence"

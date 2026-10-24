"""Verification: decision-makers, addresses and opener facts proven from the business's own pages."""

import datetime as dt

import pytest
from helpers import Clock, add_site, html_transport, make_engine

from lead_engine import verify
from lead_engine.config import DEFAULTS
from lead_engine.demo import SITES, SiteTransport
from lead_engine.evidence import Page, quote_in, supported
from lead_engine.fetch import Fetcher
from lead_engine.html import parse_html
from lead_engine.outcome import Advance, Drop, StepError
from lead_engine.verify import find_email, find_people, opener_facts, research

SETTINGS = DEFAULTS["verify"]
TODAY = dt.date(2026, 3, 2)


def page(text, url="https://shop.example.com/about", emails=(), jsonld=()):
    return Page(url, url, 200, True, "", text, [], list(emails), list(jsonld))


def run(host, transport=None, company="Shop", settings=SETTINGS):
    clock = Clock()
    f = Fetcher("lead-engine-test", transport=transport or SiteTransport(timeouts={}, down=set()), sleep=clock.sleep,
                monotonic=clock.monotonic)
    lead = {"id": 1, "company": company, "website": "https://%s/" % host, "market": "Cedar Hollow, OR"}
    return research(lead, f, settings, TODAY)


def process_local_site(tmp_path, body):
    engine = make_engine(tmp_path, Clock(), transport=html_transport({"https://shop.example.com/": body}))
    lead_id = add_site(engine.store, "shop.example.com", "Shop")
    engine.tick(discover=False, handoff=False)
    return engine, engine.store.get(lead_id)


@pytest.mark.parametrize("sentence,name,role", [
    ("Dana Whitlock, Owner", "Dana Whitlock", "Owner"),
    ("Meet Dana Whitlock, our founder and lead bookkeeper.", "Dana Whitlock", "Founder"),
    ("Owner: Dana Whitlock", "Dana Whitlock", "Owner"),
    ("Dana Whitlock is the owner of the firm.", "Dana Whitlock", "Owner"),
    ("The firm was founded in 2009 by Dana J. Whitlock.", "Dana J. Whitlock", "Founder"),
    ("Dana Whitlock started the firm after a decade in restaurants.", "Dana Whitlock", "Founder"),
    ("Hi, I'm Dana Whitlock, the owner and only inspector.", "Dana Whitlock", "Owner"),
    ("Our firm is owned and operated by Luis de Vega since the start.", "Luis de Vega", "Owner"),
    ("Mary O'Neil serves as president.", "Mary O'Neil", "President"),
])
def test_role_sentences_name_the_decision_maker_with_a_verbatim_quote(sentence, name, role):
    p = page("Welcome.\n" + sentence)
    people = find_people([p], SETTINGS["roles"], "Harbor Ledger Co.")
    assert people and people[0].full_name == name and people[0].role == role
    fact = people[0].facts[0]
    assert fact["method"] == "text" and supported(fact, [p])


def test_team_cards_pair_the_owner_with_the_name_above_not_below():
    parsed = parse_html('<div><h3>Marcus Bell</h3><p>Owner</p></div>'
                        '<div><h3>Priya Nandakumar</h3><p>Payroll Specialist</p></div>', SITE)
    p = page(parsed.text)
    p.elements = parsed.elements
    people = find_people([p], SETTINGS["roles"], "Brightline Books")
    assert [x.full_name for x in people] == ["Marcus Bell"]
    assert people[0].facts[0]["quote"] == "Marcus Bell Owner"


def test_a_heading_above_a_name_counts():
    parsed = parse_html('<section><h2>Meet the Owner</h2><p>Rosa Delgado</p></section>'
                        '<p>Rosa has inspected homes for years.</p>', SITE)
    p = page(parsed.text)
    p.elements = parsed.elements
    assert find_people([p], SETTINGS["roles"], "Copperfield")[0].full_name == "Rosa Delgado"


def test_schema_org_founder_and_employee_nodes():
    ld = [{"@type": "LocalBusiness", "name": "Shop", "founder": {"@type": "Person", "name": "Pat Example"},
           "employee": [{"@type": "Person", "name": "Lee Sample", "jobTitle": "Office Manager"}]}]
    p = page("Nothing named here.", jsonld=ld)
    people = find_people([p], SETTINGS["roles"], "Shop")
    assert [x.full_name for x in people] == ["Pat Example"]  # an office manager is not a decision-maker
    fact = people[0].facts[0]
    assert fact["method"] == "schema.org" and fact["quote"].startswith("founder: ") and supported(fact, [p])


@pytest.mark.parametrize("text", [
    "About Us\nOwner Stories",            # heading words are not names
    "We help homeowners save on taxes.",  # "owner" inside another word
    "Harbor Ledger founded in 2009.",     # the company name is not a person
    "Owner: Dana",                         # a first name alone is not proof
    "Dana Whitlock, the founder's daughter, runs the front desk.",  # someone else's role
    "Dana Whitlock is the former owner.",  # not the decision-maker any more
])
def test_things_that_are_not_proof(text):
    assert find_people([page(text)], SETTINGS["roles"], "Harbor Ledger") == []


@pytest.mark.parametrize("text", ["Former owner Pat Example has retired.", "Pat Example\nFormer Owner",
    "Pat Example, Owner (retired).", "Pat Example\nOwner\nRetired in 2024.",
    "Late founder Pat Example opened the firm.", "Pat Example, previous president.",
    "Pat Example\nAssistant to the Owner", "Pat Example, Owner. Retired in 2024."])
def test_non_current_role_contexts_are_not_proof(text):
    assert find_people([page(text)], SETTINGS["roles"], "Shop") == []


@pytest.mark.parametrize("role_text", ["Not the Owner", "Never an Owner", "Pat Example is not the owner.",
                                      "The firm was not founded by Pat Example."])
def test_negated_ownership_cannot_pass_the_pipeline(tmp_path, role_text):
    from helpers import add_site, make_engine
    site = "https://shop.example.com/"
    body = ("<div>Pat Example</div><div>%s</div><p>Email Pat Example: pat@example.com</p>"
            "<p>Our firm was founded in 2012.</p>" % role_text)
    engine = make_engine(tmp_path, Clock(), transport=html_transport({site: body}))
    lead_id = add_site(engine.store, "shop.example.com", "Shop")
    engine.tick(discover=False, handoff=False)
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "dropped" and lead["reason"] == "decision_maker_not_found"


def test_negated_schema_role_cannot_supply_a_decision_maker():
    p = page("Team", jsonld=[{"@type": "Person", "name": "Pat Example", "jobTitle": "Not the Owner"}])
    assert find_people([p], SETTINGS["roles"], "Shop") == []


def test_non_current_schema_titles_are_not_proof():
    p = page("Team", jsonld=[{"@type": "Person", "name": "Pat Example", "jobTitle": "Former Owner"}])
    assert find_people([p], SETTINGS["roles"], "Shop") == []


def test_assistant_schema_title_is_not_an_owner():
    p = page("Team", jsonld=[{"@type": "Person", "name": "Pat Example", "jobTitle": "Assistant to the Owner"}])
    assert find_people([p], SETTINGS["roles"], "Shop") == []


@pytest.mark.parametrize("sentence", ["Pat Example founded the firm.", "The firm was owned by Pat Example."])
def test_inferred_roles_must_be_enabled(sentence):
    assert find_people([page(sentence)], ["president"], "Shop") == []


def test_a_sentence_on_the_site_beats_a_schema_node_alone():
    ld = [{"@type": "LocalBusiness", "name": "Shop", "founder": {"@type": "Person", "name": "Lee Sample"}}]
    p = page("Owner: Pat Example", jsonld=ld)
    assert [x.full_name for x in find_people([p], SETTINGS["roles"], "Shop")] == ["Pat Example", "Lee Sample"]


def test_the_strongest_role_wins_and_others_are_kept_as_also_named():
    p = page("Jordan Lake is the general manager.\nCasey Moss, Owner")
    people = find_people([p], SETTINGS["roles"], "Shop")
    assert [x.full_name for x in people] == ["Casey Moss", "Jordan Lake"]


def person(first="Dana", last="Whitlock"):
    p = page("%s %s, Owner" % (first, last))
    return find_people([p], SETTINGS["roles"], "Shop")[0]


@pytest.mark.parametrize("address,context,ok", [
    ("dana.whitlock@example.com", "Write to dana.whitlock@example.com", False),  # no separate name
    ("dw@example.com", "Email Dana: dw@example.com", True),                      # printed beside the name
    ("info@example.com", "Email Dana: info@example.com", False),                 # shared inbox
    ("dana@example.org", "Email Dana: dana@example.org", False),                 # another domain
    ("danaw77@example.net", "Questions? danaw77@example.net", False),            # free mail, not beside the name
    ("danaw77@example.net", "Dana answers at danaw77@example.net", True),        # free mail beside the name
    ("pat.r@example.com", "Billing: pat.r@example.com", False),                  # not tied to Dana
])
def test_which_addresses_count_as_the_decision_makers(address, context, ok, monkeypatch):
    monkeypatch.setattr(verify, "FREE_MAIL", {"example.net"})  # stands in for gmail-style providers
    p = page(context, url="https://shop.example.com/contact", emails=[(address, context, "text")])
    fact, rejected = find_email([p], person(), "shop.example.com")
    assert (fact is not None) is ok
    if ok:
        assert fact["value"]["email"] == address and supported(fact, [p])
    else:
        assert rejected


def test_an_address_that_belongs_to_another_named_person_is_rejected():
    ctx = "Dana and Priya: priya.n@example.com"
    p = page(ctx, emails=[("priya.n@example.com", ctx, "text")])
    other = person("Priya", "Nandakumar")
    fact, rejected = find_email([p], person(), "shop.example.com", others=[other])
    assert fact is None and "ambiguous or conflicting" in rejected[0]


def test_mailto_addresses_are_cited_by_their_link():
    p = page("Contact", emails=[("dana.whitlock@example.com", "Email Dana", "mailto")])
    fact, _ = find_email([p], person(), "shop.example.com")
    assert fact["quote"] == "mailto:dana.whitlock@example.com" and supported(fact, [p])


@pytest.mark.parametrize("method", ["text", "mailto"])
def test_email_cannot_supply_its_own_nearby_name(method, monkeypatch):
    monkeypatch.setattr(verify, "FREE_MAIL", {"example.net"})
    address = "dana@example.net"
    p = page("Contact", emails=[(address, "Write to " + address, method)])
    assert find_email([p], person(), "shop.example.com")[0] is None


def test_staff_address_is_not_bound_to_the_owner_in_the_same_paragraph():
    context = "Dana Whitlock, Owner; payroll clerk Lee Sample can be reached at lee.s@example.com."
    p = page(context, emails=[("lee.s@example.com", context, "text")])
    assert find_email([p], person(), "shop.example.com")[0] is None


@pytest.mark.parametrize("contact", [
    "<p>Pat Example, Owner; payroll clerk Lee answers at lee.s@example.com.</p>",
    "<p>Pat Example, Owner, payroll clerk Lee answers at lee.s@example.com.</p>",
    "<p>Pat Example, Owner, payroll clerk Pat answers at pat@example.com.</p>",
    "<p>Pat Example, Owner.</p><p>Pat Sample handles payroll: pat@example.com.</p>",
    '<p>Pat Example, Owner.</p><p>Pat Sample handles payroll: '
    '<a href="mailto:pat@example.com">Email Pat Sample</a></p>',
])
def test_conflicting_staff_contacts_do_not_reach_ready(tmp_path, contact):
    _, lead = process_local_site(tmp_path, contact + "<p>Our firm was founded in 2012.</p>")
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_direct_email")


def test_staff_contacts_in_the_same_element_remain_ambiguous(tmp_path):
    body = ("<p>Pat Example, Owner, pat@example.com; payroll clerk Lee answers at lee.s@example.com.</p>"
            "<p>Our firm was founded in 2012.</p>")
    _, lead = process_local_site(tmp_path, body)
    assert lead["stage"] == "dropped" and lead["reason"] == "no_direct_email"


@pytest.mark.parametrize("retirement", ["Pat Example retired in 2020 and no longer works here.",
                                       "Pat Example has stepped down."])
def test_visible_retirement_overrides_schema_founder(tmp_path, retirement):
    body = ('<script type="application/ld+json">{"@type":"LocalBusiness",'
            '"founder":{"@type":"Person","name":"Pat Example"}}</script>'
            '<p>%s</p><p>Email Pat Example: pat@example.com</p><p>Our firm was founded in 2012.</p>' % retirement)
    _, lead = process_local_site(tmp_path, body)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("owner", ["<p>Pat Example, Owner.</p>", "<div><p>Pat Example</p><p>Owner</p></div>"])
@pytest.mark.parametrize("adjacent", ["Our assistant handles bookings.", "Our previous office is closed.",
                                     "Lee Sample has retired."])
def test_unrelated_adjacent_text_does_not_hide_the_current_owner(tmp_path, owner, adjacent):
    _, lead = process_local_site(tmp_path, owner + "<p>%s</p><p>Email Pat Example: pat@example.com</p>"
                                 "<p>Our firm was founded in 2012.</p>" % adjacent)
    assert lead["stage"] == "ready" and lead["data"]["person"]["full_name"] == "Pat Example"


@pytest.mark.parametrize("owner", ["<p>Pat Example, Owner.</p><p>Our assistant handles bookings.</p>",
                                  "<p>Pat Example, Owner.</p><p>Retired clients are welcome.</p>"])
def test_unrelated_status_clauses_do_not_disqualify_the_owner(tmp_path, owner):
    _, lead = process_local_site(tmp_path, owner + "<p>Email Pat Example: pat@example.com</p>"
                                 "<p>Our firm was founded in 2012.</p>")
    assert lead["stage"] == "ready" and lead["data"]["person"]["full_name"] == "Pat Example"


def test_departure_on_another_page_excludes_only_the_departed_founder():
    home = page("Pat Example, Owner.", jsonld=[{"@type": "LocalBusiness", "founder": {
        "@type": "Person", "name": "Lee Sample"}}])
    history = page("Lee Sample retired in 2020 and no longer works here.", url="https://shop.example.com/history")
    people = find_people([home, history], SETTINGS["roles"], "Shop")
    assert [person.full_name for person in people] == ["Pat Example"]


@pytest.mark.parametrize("claim", ["Our supplier was founded in 2012.", "Other Shop was founded in 2012.",
    "Pat Example founded Other Shop in 2012.", "Our client has 15 years of experience.",
    "Our vendor is specializing in payroll.", "Our supplier was voted Best Shop in 2025."])
def test_third_party_facts_do_not_become_business_openers(tmp_path, claim):
    _, lead = process_local_site(tmp_path, "<p>Pat Example, Owner.</p><p>Email Pat Example: pat@example.com</p>"
                                 "<p>%s</p>" % claim)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_opener_facts")


@pytest.mark.parametrize("claim", ["Shop was founded in 2012.", "Our firm was founded in 2012.",
                                  "Pat Example founded Shop in 2012."])
def test_business_founding_claims_keep_their_proven_subject(tmp_path, claim):
    _, lead = process_local_site(tmp_path, "<p>Pat Example, Owner.</p><p>Email Pat Example: pat@example.com</p>"
                                 "<p>%s</p>" % claim)
    assert lead["stage"] == "ready"
    founding = next(f for f in lead["data"]["facts"] if f["kind"] == "founded")
    assert founding["subject"] == "Shop"


def test_absent_opener_facts_wait_for_an_unreadable_page(tmp_path, clock):
    from helpers import add_site, make_engine
    transport = html_transport({SITE + "/": '<a href="/about">About</a><p>Pat Example, Owner.</p>'
                               '<p>Email Pat Example: pat@example.com</p>'}, timeouts={SITE + "/about": 1})
    engine = make_engine(tmp_path, clock, transport=transport)
    lead_id = add_site(engine.store, "shop.example.com", "Shop")
    engine.tick(discover=False, handoff=False)
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "discovered" and lead["attempts"] == 1


def test_opener_facts_and_their_values():
    text = ("Dana Whitlock founded Harbor Ledger Co. in 2009 after a decade in restaurants.\n"
            "Our firm has been in business since 2014.\nWe have 15 years of experience with payroll.\n"
            "We specialize in bookkeeping for restaurants and cafes, so you get someone who knows the trade.\n"
            "We were voted Best Bookkeeper in Cedar Hollow 2025.\n"
            "Since 2020 prices have gone up.\nEstablished in 2031.")
    facts = opener_facts([page(text)], TODAY, company="Harbor Ledger Co.")
    values = [(f["kind"], f["value"]) for f in facts]
    assert ("founded", {"year": "2009"}) in values and ("founded", {"year": "2014"}) in values
    assert ("years", {"years": "15"}) in values
    assert ("specialty", {"specialty": "bookkeeping for restaurants and cafes"}) in values
    assert ("award", {"award": "Best Bookkeeper in Cedar Hollow 2025", "verb": "voted"}) in values
    assert all(v != {"year": "2020"} and v != {"year": "2031"} for _, v in values)  # loose or future years
    p = page(text)
    assert all(quote_in(f["quote"], p.text) for f in facts)


def test_research_keeps_every_fact_with_its_source_and_proof():
    out = run("harbor-ledger.example.com", company="Harbor Ledger Co.")
    assert isinstance(out, Advance) and out.stage == "verified"
    d = out.data
    pages = [Page.from_dict(p) for p in d["pages"]]
    assert d["person"]["full_name"] == "Dana Whitlock" and d["email"]["address"] == "dana.whitlock@example.com"
    assert {f["kind"] for f in d["facts"]} >= {"decision_maker", "email", "founded", "specialty"}
    for fact in d["facts"]:
        assert fact["source_url"].startswith("https://harbor-ledger.example.com/") and fact["quote"]
        assert supported(fact, pages), fact
    assert "https://harbor-ledger.example.com/services" not in d["pages_read"]  # not an about/team/contact page


def test_every_demo_lead_that_verifies_is_fully_supported():
    for site in sorted(p.name for p in SITES.iterdir()):
        out = run(site, company=site.split(".")[0].replace("-", " ").title())
        if isinstance(out, Advance):
            pages = [Page.from_dict(p) for p in out.data["pages"]]
            assert all(supported(f, pages) for f in out.data["facts"]), site


def test_late_page_evidence_survives_snapshot_serialization():
    body = "".join("<p>General shop information item %d.</p>" % i for i in range(1400))
    body += "<p>Pat Example, Owner.</p><p>Email Pat: pat@example.com</p><p>Our firm was founded in 2012.</p>"
    out = run("shop.example.com", html_transport({SITE + "/": body}))
    assert isinstance(out, Advance)
    snapshots = [Page.from_dict(p) for p in out.data["pages"]]
    assert all(supported(f, snapshots) for f in out.data["facts"])


def test_absent_fit_terms_wait_for_unreadable_pages():
    transport = html_transport({SITE + "/": HOME}, timeouts={SITE + "/about": 1})
    with pytest.raises(StepError, match="could not read"):
        run("shop.example.com", transport, settings={**SETTINGS, "require_any": ["bookkeeping"]})


def test_nothing_named_is_a_drop_but_an_unreadable_page_is_a_retry():
    out = run("summit-tax.example.com")
    assert isinstance(out, Drop) and out.reason == "decision_maker_not_found"
    flaky = SiteTransport(timeouts={"https://brightline-books.example.com/about": 1}, down=set())
    with pytest.raises(StepError, match="could not read https://brightline-books.example.com/about"):
        run("brightline-books.example.com", flaky)


def test_shared_inbox_only_is_dropped_with_the_reason():
    out = run("juniper-home.example.org")
    assert isinstance(out, Drop) and out.reason == "no_direct_email" and "shared inbox" in out.detail


SITE = "https://shop.example.com"
HOME = '<html><body><a href="/about">About</a><p>Bike repair.</p></body></html>'
ABOUT = ("<html><body><p>Pat Example, Owner. Write to Pat Example at pat@example.com</p>"
         "<p>Franchise opportunities available.</p></body></html>")


def test_robots_off_site_redirects_dead_sites_and_fit_rules():
    blocked = html_transport({SITE + "/robots.txt": "User-agent: *\nDisallow: /\n", SITE + "/": HOME})
    assert run("shop.example.com", blocked).reason == "robots_disallowed"
    assert run("shop.example.com", html_transport({})).reason == "site_unavailable"

    def redirect(url, headers, timeout, max_bytes):
        from lead_engine.fetch import Response
        if url.endswith("robots.txt"):
            return Response(404, {}, b"", url)
        return Response(200, {"content-type": "text/html"}, b"<p>parked</p>", "https://parking.example.net/")
    assert run("shop.example.com", redirect).reason == "redirected_off_site"

    site = html_transport({SITE + "/": HOME, SITE + "/about": ABOUT})
    assert run("shop.example.com", site).reason == "excluded_term"
    strict = {**SETTINGS, "exclude_any": [], "require_any": ["bookkeeping"]}
    assert run("shop.example.com", html_transport({SITE + "/": HOME, SITE + "/about": ABOUT}),
               settings=strict).reason == "fit_not_shown"
    relaxed = {**SETTINGS, "exclude_any": []}
    out = run("shop.example.com", html_transport({SITE + "/": HOME, SITE + "/about": ABOUT}), settings=relaxed)
    assert isinstance(out, Advance) and out.data["person"]["full_name"] == "Pat Example"

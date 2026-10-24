"""Persisted facts must prove their values and relationships from the cited snapshot."""

import copy
import datetime as dt

import pytest

from lead_engine.evidence import Page, supported
from lead_engine.verify import find_people, opener_facts


def source(text):
    return Page("https://shop.example.com/", "https://shop.example.com/", 200, True, text=text)


@pytest.mark.parametrize("kind,value", [("founded", {"year": "1998"}), ("years", {"years": "15"}),
    ("specialty", {"specialty": "plumbing"}), ("award", {"award": "Best Shop", "verb": "voted"}),
    ("unknown", {"year": "1998"})])
def test_quote_presence_does_not_prove_unrelated_values(kind, value):
    page = source("We specialize in bookkeeping.")
    fact = {"kind": kind, "value": value, "method": "text", "source_url": page.url, "quote": page.text}
    assert not supported(fact, [page])


def test_supported_rechecks_every_opener_kind_and_rejects_changed_values():
    page = source("Our firm was founded in 2012.\nWe have 15 years of experience.\nWe specialize in bookkeeping.\n"
                  "We were voted Best Bookkeeper in 2025.")
    facts = opener_facts([page], dt.date(2026, 3, 2))
    assert {f["kind"] for f in facts} == {"founded", "years", "specialty", "award"}
    for fact in facts:
        assert supported(fact, [page])
        altered = copy.deepcopy(fact)
        key = next(iter(altered["value"]))
        altered["value"][key] = "unrelated"
        assert not supported(altered, [page])
        altered = {**fact, "method": "unknown"}
        assert not supported(altered, [page])


def test_decision_maker_values_must_match_the_proven_role():
    page = source("Pat Example, President.")
    fact = find_people([page], ["president"], "Shop")[0].facts[0]
    assert supported(fact, [page])
    fact["value"]["role"] = "Owner"
    assert not supported(fact, [page])


def test_schema_role_values_and_method_must_match_the_node():
    page = source("Team")
    page.jsonld = [{"@type": "Person", "name": "Pat Example", "jobTitle": "Owner", "worksFor": {"name": "Shop"}}]
    fact = find_people([page], ["owner"], "Shop")[0].facts[0]
    assert supported(fact, [page])
    fact["value"]["role"] = "President"
    assert not supported(fact, [page])


def test_a_quote_about_a_supplier_does_not_prove_the_business_founding_year():
    page = source("Our supplier was founded in 2012.")
    fact = {"kind": "founded", "value": {"year": "2012"}, "source_url": page.url,
            "method": "text", "quote": page.text}
    assert not supported(fact, [page])


def test_an_excerpt_cannot_remove_a_third_party_subject_from_the_snapshot():
    page = source("Our supplier was founded in 2012.")
    fact = {"kind": "founded", "value": {"year": "2012"}, "source_url": page.url,
            "method": "text", "quote": "founded in 2012."}
    assert not supported(fact, [page])

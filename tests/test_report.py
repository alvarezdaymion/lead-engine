"""CSV identity evidence matches the selected person and role."""

import csv

import pytest
from helpers import Clock, add_site, html_transport, make_engine

from lead_engine.report import export_csv


def ready_lead(tmp_path, names=("Pat Example", "Pat Example")):
    body = ("<p>%s, Principal.</p><p>%s, Owner.</p><p>Email %s: pat@example.com</p>"
            "<p>Our firm was founded in 2012.</p>" % (*names, names[0]))
    engine = make_engine(tmp_path, Clock(), transport=html_transport({"https://shop.example.com/": body}))
    ident = add_site(engine.store, "shop.example.com", "Shop")
    engine.tick(discover=False, handoff=False)
    lead = engine.store.get(ident)
    assert lead["stage"] == "ready"
    return engine, lead


@pytest.mark.parametrize("names", [("Pat Example", "Pat Example"), ("Bob Example", "Robert Example")])
def test_csv_role_quote_proves_the_selected_role_and_person(tmp_path, names):
    engine, lead = ready_lead(tmp_path, names)
    path = tmp_path / "ready.csv"
    assert export_csv(engine.store, path) == 1
    with path.open(newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["role"] == lead["data"]["person"]["role"] == "Owner"
    assert row["role_quote"] == "%s, Owner." % names[1]


def test_export_fails_closed_when_no_cited_fact_matches_the_person_and_role(tmp_path):
    engine, lead = ready_lead(tmp_path)
    for fact in lead["data"]["facts"]:
        if fact["kind"] == "decision_maker" and fact["value"]["role"] == "Owner":
            fact["value"].update(full_name="Lee Sample", first_name="Lee", last_name="Sample")
    engine.store.advance(lead["id"], "ready", lead["data"])
    path = tmp_path / "ready.csv"
    with pytest.raises(ValueError, match="evidence matches the exported person and role"):
        export_csv(engine.store, path)
    assert not path.exists()

"""Full names bind a selected person's role and address at every output boundary."""

import copy
from types import SimpleNamespace

import pytest
from test_visible_invalidation import FACT, process

from lead_engine.evidence import Page, supported
from lead_engine.gate import load_suppressions
from lead_engine.handoff import run_handoff
from lead_engine.outcome import Drop
from lead_engine.report import export_csv

OWNER = "<p>Pat Alex Example, Owner.</p>"
MANAGER = "<p>Pat Jo Example, General Manager.</p>"
OWNER_EMAIL = "<p>Email Pat Alex Example: owner@example.com</p>"
MANAGER_EMAIL = "<p>Email Pat Jo Example: manager@example.com</p>"
BODY = OWNER + MANAGER + OWNER_EMAIL + MANAGER_EMAIL + FACT


@pytest.mark.parametrize("first,second", [("Pat Alex Example", "Pat Jo Example"),
                                        ("Bob Example", "Robert Alex Example"),
                                        ("Pat Al Example", "Patrick Albert Example")])
def test_different_remaining_name_components_never_share_roles_or_contacts(tmp_path, first, second):
    body = "<p>%s, Owner.</p><p>%s, General Manager.</p><p>Email %s: person@example.com</p>" % (first, second, second)
    _, lead = process(tmp_path, body + FACT)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_direct_email")


def test_first_name_nicknames_merge_only_with_matching_complete_remaining_names(tmp_path):
    body = ("<p>Bob Alex Example, Principal.</p><p>Robert Alex Example, Owner.</p>"
            "<p>Email Bob Alex Example: bob@example.com</p>" + FACT)
    _, lead = process(tmp_path, body)
    assert lead["stage"] == "ready" and lead["data"]["person"]["role"] == "Owner"
    names = {fact["value"]["full_name"] for fact in lead["data"]["facts"] if fact["kind"] == "decision_maker"}
    assert names == {"Bob Alex Example", "Robert Alex Example"}


@pytest.mark.parametrize("boundary", ["gate", "dry-run", "live"])
@pytest.mark.parametrize("change", ["other-email", "other-person", "other-role", "other-person-facts"])
def test_required_facts_must_prove_the_complete_selected_person_role_email_relationship(tmp_path, boundary, change):
    engine, lead = process(tmp_path, BODY, stop=boundary == "gate")
    data = lead["data"]
    identities = [fact for fact in data["facts"] if fact["kind"] == "decision_maker"]
    owner = next(fact for fact in identities if fact["value"]["full_name"] == "Pat Alex Example")
    manager = copy.deepcopy(owner)
    manager.update(id="manager-identity", quote="Pat Jo Example, General Manager.")
    manager["value"].update(full_name="Pat Jo Example", role="General Manager")
    data["facts"].append(manager)
    pages = [Page.from_dict(page) for page in data["pages"]]
    assert supported(owner, pages, company="Shop") and supported(manager, pages, company="Shop")
    data["person"]["fact_ids"] = [owner["id"]]
    if change == "other-email":
        email = next(fact for fact in data["facts"] if fact["id"] == data["email"]["fact_id"])
        email["value"].update(email="manager@example.com", full_name="Pat Jo Example")
        email["quote"] = "Email Pat Jo Example: manager@example.com"
        data["email"]["address"] = "manager@example.com"
        assert supported(owner, pages, company="Shop") and supported(email, pages, company="Shop")
    elif change == "other-person":
        data["person"]["full_name"] = "Pat Jo Example"
    elif change == "other-role":
        data["person"]["role"] = "General Manager"
    else:
        data["person"]["fact_ids"] = [manager["id"]]
    if boundary == "gate":
        result = engine.step(lead)
        assert isinstance(result, Drop)
        return
    engine.store.advance(lead["id"], "ready", data)
    calls = []
    suppression = tmp_path / "suppressions.csv"
    suppression.write_text("email,domain\n")
    result = run_handoff(engine.store, {**engine.s["handoff"], "mode": boundary},
                         suppressions=engine.suppressions, out_dir=tmp_path / "out",
                         client=SimpleNamespace(add_leads=lambda *args: calls.append(args)),
                         campaign_id="campaign-1", suppression_file=suppression,
                         roles=engine.s["verify"]["roles"])
    assert not calls and not result.get("would_add")
    assert engine.store.get(lead["id"])["stage"] == "dropped"


def test_exported_role_quote_cannot_belong_to_a_different_full_name(tmp_path):
    engine, lead = process(tmp_path, OWNER + OWNER_EMAIL + FACT)
    fact = next(fact for fact in lead["data"]["facts"] if fact["kind"] == "decision_maker")
    fact["value"]["full_name"] = "Pat Jo Example"
    engine.store.advance(lead["id"], "ready", lead["data"])
    path = tmp_path / "ready.csv"
    with pytest.raises(ValueError):
        export_csv(engine.store, path)
    assert not path.exists()


def test_live_handoff_rechecks_identity_after_selecting_candidates(tmp_path, monkeypatch):
    engine, lead = process(tmp_path, BODY)
    reloads, calls = [], []
    path = tmp_path / "suppressions.csv"
    path.write_text("email,domain\n")

    def reload(path):
        reloads.append(path)
        if len(reloads) == 2:
            data = engine.store.get(lead["id"])["data"]
            data["person"]["full_name"] = "Pat Jo Example"
            engine.store.advance(lead["id"], "ready", data)
        return load_suppressions(path)

    monkeypatch.setattr("lead_engine.handoff.load_suppressions", reload)
    run_handoff(engine.store, {**engine.s["handoff"], "mode": "live"}, suppressions=engine.suppressions,
                out_dir=tmp_path / "out", client=SimpleNamespace(add_leads=lambda *args: calls.append(args)),
                campaign_id="campaign-1", suppression_file=path, roles=engine.s["verify"]["roles"])
    assert len(reloads) == 2 and not calls
    assert engine.store.get(lead["id"])["stage"] == "dropped"
    assert engine.store.unresolved_handoffs() == 0

"""Backlog re-qualification of the stranded pile."""

from helpers import add_site, make_engine

from lead_engine.backlog import import_stranded, requalify, stranded
from lead_engine.demo import DATA, SiteTransport
from lead_engine.names import same_person, split_name
from lead_engine.outcome import SharedOutage

CSV_HEADER = "company,website,first_name,last_name,email\n"


def write_csv(tmp_path, rows):
    p = tmp_path / "stranded.csv"
    p.write_text(CSV_HEADER + "".join(rows))
    return p


def test_import_queues_leads_for_the_backlog_only(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    added = import_stranded(engine, DATA / "stranded.csv")
    assert added == 3
    assert import_stranded(engine, DATA / "stranded.csv") == 0  # known domains are skipped
    assert engine.tick()["leads"] == []  # the scheduled run leaves imported leads to `backlog`
    assert len(stranded(engine)) == 3


def test_same_person_under_a_nickname_is_recovered_and_reaches_ready(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    import_stranded(engine, write_csv(tmp_path, [
        "Calder & Finch Bookkeeping,calder-finch.example.com,Bob,Calder,bob@example.com\n"]))
    result = requalify(engine)
    row = result["leads"][0]
    assert row["result"] == "recovered" and "same person as Bob Calder" in row["detail"]
    lead = engine.store.get(row["id"])
    assert lead["stage"] == "ready" and lead["attempts"] == 0
    assert lead["data"]["backlog"]["prior"]["first_name"] == "Bob"
    assert lead["data"]["person"]["full_name"] == "Robert Calder"  # the site's proven name is what goes out


def test_a_different_decision_maker_is_recorded_and_the_lead_continues(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    import_stranded(engine, write_csv(tmp_path, [
        "Northwind Lawn Care,https://northwind-lawn.example.org/,Elena,Park,elena@example.org\n"]))
    row = requalify(engine)["leads"][0]
    assert row["result"] == "changed" and row["detail"] == "Owen Reyes (was Elena Park)"
    assert row["then"] == "ready"
    kinds = [e["kind"] for e in engine.store.events(row["id"])]
    assert "backlog_changed" in kinds


def test_a_lead_that_can_no_longer_be_proven_is_dropped(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    import_stranded(engine, write_csv(tmp_path, [
        "Fernhill Plumbing,https://fernhill-plumbing.example.com/,Gary,Holt,\n"]))
    row = requalify(engine)["leads"][0]
    lead = engine.store.get(row["id"])
    assert row["result"] == "dropped" and (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_an_unreadable_site_is_inconclusive_and_left_exactly_as_it_was(tmp_path, clock):
    engine = make_engine(tmp_path, clock, SiteTransport(timeouts={}, down={"harbor-ledger.example.com"}))
    lead_id = add_site(engine.store, "harbor-ledger.example.com", "Harbor Ledger Co.")
    for minutes in (0, 31, 61):
        clock.advance(minutes)
        engine.tick()
    before = engine.store.get(lead_id)
    assert before["stage"] == "retired"
    result = requalify(engine)
    after = engine.store.get(lead_id)
    assert result["counts"] == {"inconclusive": 1}
    assert {k: after[k] for k in ("stage", "attempts", "reason", "detail")} == \
        {k: before[k] for k in ("stage", "attempts", "reason", "detail")}


def test_a_retired_lead_is_recovered_with_a_fresh_budget_once_its_site_is_back(tmp_path, clock):
    transport = SiteTransport(timeouts={}, down={"harbor-ledger.example.com"})
    engine = make_engine(tmp_path, clock, transport)
    lead_id = add_site(engine.store, "harbor-ledger.example.com", "Harbor Ledger Co.")
    for minutes in (0, 31, 61):
        clock.advance(minutes)
        engine.tick()
    assert engine.store.get(lead_id)["stage"] == "retired"
    transport.down.clear()
    row = requalify(engine)["leads"][0]
    lead = engine.store.get(lead_id)
    assert row["result"] == "recovered" and lead["stage"] == "ready" and lead["attempts"] == 0


def test_stale_research_leads_count_as_stranded(tmp_path, clock):
    engine = make_engine(tmp_path, clock, settings={"engine": {"stale_hours": 48}})
    lead_id = add_site(engine.store, "harbor-ledger.example.com", "Harbor Ledger Co.")
    assert stranded(engine) == []
    clock.advance(49 * 60)
    assert [x["id"] for x in stranded(engine)] == [lead_id]


def test_the_limit_bounds_one_backlog_run_and_a_rerun_continues(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    import_stranded(engine, DATA / "stranded.csv")
    assert requalify(engine, limit=1)["rechecked"] == 1
    assert requalify(engine)["rechecked"] == 2
    assert requalify(engine)["rechecked"] == 0


def test_backlog_runs_are_recorded(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    requalify(engine)
    run = engine.store.runs(1)[0]
    assert run["kind"] == "backlog" and run["finished_at"]


def test_shared_writer_outage_stops_the_backlog_without_touching_the_rest(tmp_path, clock):
    class UnavailableWriter:
        calls = 0

        def draft(self, *args):
            self.calls += 1
            raise SharedOutage("model unavailable")

    writer = UnavailableWriter()
    engine = make_engine(tmp_path, clock, writer=writer)
    import_stranded(engine, DATA / "stranded.csv")
    before = engine.store.by_stage(("discovered",))
    result = requalify(engine)
    assert writer.calls == 1 and result["rechecked"] == 1
    assert result["stop_reason"] == "shared outage: model unavailable"
    assert all(engine.store.get(lead["id"]) == lead for lead in before[1:])
    assert engine.store.get(before[0]["id"])["attempts"] == 0


def test_same_person_matching():
    assert same_person("Bob", "Calder", "Robert", "Calder")
    assert same_person("Dana", "Whitlock Jr.", "dana", "Whitlock")
    assert same_person("Jos\u00e9", "N\u00fa\u00f1ez", "Jose", "Nunez")
    assert same_person("Mary", "O'Neil", "Mary", "ONeil")
    assert same_person("Ana", "de Vega", "Ana", "Vega")
    assert not same_person("Elena", "Park", "Owen", "Reyes")
    assert not same_person("Dana", "Whitlock", "Dana", "Bell")
    assert not same_person("", "Whitlock", "", "Whitlock")


def test_split_name():
    assert split_name("Dana J. Whitlock") == ("Dana", "Whitlock")
    assert split_name("Luis de Vega") == ("Luis", "de Vega")
    assert split_name("Sam Ortiz Jr.") == ("Sam", "Ortiz")
    assert split_name("Prince") == ("Prince", "")

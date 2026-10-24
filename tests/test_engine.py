"""The repair cycle: stage transitions, retries with backoff, retirement, crash resume, shared outages."""

import pytest
from helpers import add_site, make_engine

from lead_engine.demo import SiteTransport
from lead_engine.engine import Engine, TickLock
from lead_engine.outcome import SharedOutage
from lead_engine.writer import TemplateWriter

HARBOR = "harbor-ledger.example.com"
BRIGHT = "brightline-books.example.com"


class Crash(BaseException):
    """A killed process: nothing in the engine catches BaseException."""


class CrashingWriter(TemplateWriter):
    def __init__(self, voice, times=1):
        super().__init__(voice)
        self.left = times

    def drafts(self, lead):
        if self.left:
            self.left -= 1
            raise Crash()
        return super().drafts(lead)


def test_a_lead_moves_through_every_stage_in_one_run(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    lead_id = add_site(engine.store, HARBOR, "Harbor Ledger Co.")
    summary = engine.tick()
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "ready" and lead["lease_until"] is None and lead["attempts"] == 0
    kinds = [e["kind"] for e in engine.store.events(lead_id)]
    assert kinds == ["verified", "written", "ready"]
    assert summary["outcomes"] == {"ready": 1}
    d = lead["data"]
    assert d["person"]["full_name"] == "Dana Whitlock" and d["email"]["address"] == "dana.whitlock@example.com"
    assert d["opener"]["text"] and d["opener"]["fact_ids"] and "gated_at" in d


def test_a_step_that_fails_twice_then_succeeds_is_repaired(tmp_path, clock):
    transport = SiteTransport(timeouts={"https://%s/about" % BRIGHT: 2}, down=set())
    engine = make_engine(tmp_path, clock, transport)
    lead_id = add_site(engine.store, BRIGHT, "Brightline Books")

    engine.tick()
    lead = engine.store.get(lead_id)
    assert (lead["stage"], lead["attempts"]) == ("discovered", 1)
    assert "timed out" in lead["last_error"]
    assert lead["next_attempt_at"] == "2026-03-02T09:30:00+00:00"  # first backoff: 30 minutes

    clock.advance(20)  # not due yet: the run leaves it alone
    assert engine.tick()["leads"] == []

    clock.advance(11)
    engine.tick()
    lead = engine.store.get(lead_id)
    assert (lead["stage"], lead["attempts"]) == ("discovered", 2)
    assert lead["next_attempt_at"] == "2026-03-02T10:31:00+00:00"  # doubled: 60 minutes

    clock.advance(61)
    line = engine.tick()["leads"][0]
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "ready" and lead["attempts"] == 0 and lead["last_error"] == ""
    assert line["attempts_before"] == 2
    assert lead["data"]["person"]["full_name"] == "Marcus Bell"


def test_a_lead_is_retired_with_the_reason_after_max_repairs(tmp_path, clock):
    transport = SiteTransport(timeouts={}, down={HARBOR})
    engine = make_engine(tmp_path, clock, transport, settings={"engine": {"max_repairs": 3}})
    lead_id = add_site(engine.store, HARBOR, "Harbor Ledger Co.")
    for minutes in (0, 31, 61):
        clock.advance(minutes)
        engine.tick()
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "retired" and lead["reason"] == "repairs_exhausted" and lead["attempts"] == 3
    assert lead["detail"].startswith("discovered: ") and "HTTP 503" in lead["detail"]
    clock.advance(10_000)
    assert engine.tick()["leads"] == []  # retired is terminal: never claimed again


def test_backoff_is_capped(tmp_path, clock):
    transport = SiteTransport(timeouts={}, down={HARBOR})
    engine = make_engine(tmp_path, clock, transport, settings={
        "engine": {"max_repairs": 10, "backoff_minutes": 30, "max_backoff_minutes": 90}})
    lead_id = add_site(engine.store, HARBOR, "Harbor Ledger Co.")
    waits = []
    for _ in range(5):
        engine.tick()
        lead = engine.store.get(lead_id)
        due = lead["next_attempt_at"]
        waits.append(due)
        clock.now = clock.now.fromisoformat(due)
    gaps = [int((clock.now.fromisoformat(b) - clock.now.fromisoformat(a)).total_seconds() // 60)
            for a, b in zip(waits, waits[1:], strict=False)]
    assert gaps == [60, 90, 90, 90]


def test_a_crash_mid_run_resumes_from_the_last_checkpoint(tmp_path, clock):
    transport = SiteTransport(timeouts={}, down=set())
    engine = make_engine(tmp_path, clock, transport)
    engine.writer = CrashingWriter(engine.voice, times=1)
    lead_id = add_site(engine.store, HARBOR, "Harbor Ledger Co.")

    with pytest.raises(Crash):
        engine.tick()
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "verified" and lead["lease_until"] is not None  # checkpointed, lease still held
    assert engine.store.runs(1)[0]["finished_at"] is None
    fetched = len(transport.calls)

    clock.advance(10)  # lease (20 min) still valid: another run must not touch it
    assert engine.tick()["leads"] == []

    clock.advance(15)
    summary = engine.tick()
    assert summary["resumed"] == [{"id": lead_id, "company": "Harbor Ledger Co.", "stage": "verified",
                                   "outcome": "resumed"}]
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "ready"
    assert len(transport.calls) == fetched  # resumed at "verified": the site was not fetched again
    kinds = [e["kind"] for e in engine.store.events(lead_id)]
    assert kinds == ["verified", "lease_expired", "written", "ready"]


def test_a_lead_that_keeps_killing_its_worker_is_retired(tmp_path, clock):
    engine = make_engine(tmp_path, clock, settings={"engine": {"max_repairs": 3}})
    engine.writer = CrashingWriter(engine.voice, times=99)
    lead_id = add_site(engine.store, HARBOR, "Harbor Ledger Co.")
    for _ in range(3):
        with pytest.raises(Crash):
            engine.tick()
        clock.advance(21)
    summary = engine.tick()
    lead = engine.store.get(lead_id)
    assert lead["stage"] == "retired" and "stopped mid-step" in lead["detail"]
    assert summary["resumed"][0]["outcome"] == "retired"


def test_a_shared_outage_releases_the_lead_without_using_its_repairs(tmp_path, clock):
    class Down(TemplateWriter):
        def drafts(self, lead):
            raise SharedOutage("model API rate limited")

    engine = make_engine(tmp_path, clock)
    engine.writer = Down(engine.voice)
    first = add_site(engine.store, HARBOR, "Harbor Ledger Co.")
    add_site(engine.store, BRIGHT, "Brightline Books")
    summary = engine.tick()
    assert summary["stop_reason"] == "shared outage: model API rate limited"
    assert len(summary["leads"]) == 1  # the run stopped instead of failing every lead
    lead = engine.store.get(first)
    assert (lead["stage"], lead["attempts"], lead["lease_until"]) == ("verified", 0, None)


def test_failures_in_a_row_end_the_run_early(tmp_path, clock):
    hosts = ["down-%d.example.com" % i for i in range(8)]
    engine = make_engine(tmp_path, clock, SiteTransport(timeouts={}, down=set(hosts)),
                         settings={"engine": {"stop_after_errors": 3}})
    for h in hosts:
        add_site(engine.store, h, h)
    summary = engine.tick()
    assert len(summary["leads"]) == 3 and summary["stop_reason"] == "3 failures in a row"


def test_a_dropped_lead_keeps_its_reason_and_is_never_claimed_again(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    lead_id = add_site(engine.store, "summit-tax.example.com", "Summit Tax Partners")
    engine.tick()
    lead = engine.store.get(lead_id)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")
    assert lead["data"]["pages_read"][0] == "https://summit-tax.example.com/"
    clock.advance(10_000)
    assert engine.tick()["leads"] == []


def test_finishing_work_comes_before_starting_new_work(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    newer = add_site(engine.store, HARBOR, "Harbor Ledger Co.")
    older = add_site(engine.store, BRIGHT, "Brightline Books")
    engine.store.advance(older, "verified", {"x": 1})
    assert engine.store.claim(20)["id"] == older
    assert engine.store.claim(20)["id"] == newer
    assert engine.store.claim(20) is None  # both leased


def test_discovery_runs_when_the_queue_is_low_and_its_failure_does_not_block_work(tmp_path, clock):
    def broken(store):
        raise RuntimeError("search API down")

    engine = make_engine(tmp_path, clock, discover=broken)
    add_site(engine.store, HARBOR, "Harbor Ledger Co.")
    summary = engine.tick()
    assert summary["discovery"] == {"error": "RuntimeError: search API down"}
    assert summary["outcomes"] == {"ready": 1}


def test_lead_budget_and_time_budget(tmp_path, clock):
    engine = make_engine(tmp_path, clock)
    add_site(engine.store, HARBOR, "Harbor Ledger Co.")
    add_site(engine.store, BRIGHT, "Brightline Books")
    summary = engine.tick(max_leads=1)
    assert len(summary["leads"]) == 1 and summary["stop_reason"] == "lead budget reached"


def test_a_second_run_cannot_start_while_one_holds_the_lock(tmp_path):
    with TickLock(tmp_path / "run.lock") as first:
        with TickLock(tmp_path / "run.lock") as second:
            assert first is True and second is False
    with TickLock(tmp_path / "run.lock") as again:
        assert again is True


def test_engine_rejects_an_unknown_stage(tmp_path, clock):
    engine: Engine = make_engine(tmp_path, clock)
    with pytest.raises(ValueError):
        engine.step({"stage": "ready", "id": 1})

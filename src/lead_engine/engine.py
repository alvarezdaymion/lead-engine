"""One scheduled run, from discovery to campaign-ready lead, with a repair cycle for every lead.

A run:
1. Reaps expired leases: leads whose worker stopped mid-step count one failed attempt and resume from
   their last checkpoint.
2. Discovers new businesses when fewer than `min_queue` leads are due.
3. Claims due leads one at a time, most advanced first, and pushes each through every step it can:
   verify -> write -> gate. Each finished step is checkpointed. A step returns Advance or Drop, or raises:
   - StepError or any other exception: a failed attempt. The lead keeps its checkpoint and retries after
     a backoff; after `max_repairs` failed attempts at a stage it is retired with the reason.
   - SharedOutage: the lead is released untouched and the run stops, so an outage of the model API or
     the platform never burns repair budgets.
   `stop_after_errors` failures in a row also stop the run, because that pattern means a shared problem.
4. Hands ready leads to the email platform (dry-run by default).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .fetch import Fetcher
from .gate import Suppressions, gate
from .outcome import Advance, Drop, SharedOutage, StepError
from .store import RESEARCH, Store, utc_now
from .verify import research
from .voice import Voice
from .writer import write

MAX_STEPS_PER_LEAD = 6  # verify, write, gate, plus one rewrite loop, with room to spare


class Engine:
    def __init__(self, settings: dict[str, Any], store: Store, *, fetcher: Fetcher, writer: Any, voice: Voice,
                 suppressions: Suppressions, discover: Callable[[Store], dict[str, Any]] | None = None,
                 handoff: Callable[[Store], dict[str, Any]] | None = None,
                 clock: Callable[[], dt.datetime] | None = None,
                 notify: Callable[[str, dict[str, Any]], None] | None = None):
        self.s = settings
        self.store = store
        self.fetcher = fetcher
        self.writer = writer
        self.voice = voice
        self.suppressions = suppressions
        self.discover = discover
        self.handoff = handoff
        self.clock = clock or utc_now
        self.notify = notify or (lambda kind, payload: None)  # live progress: resumed, discovery, lead, handoff

    @property
    def engine(self) -> dict[str, Any]:
        return self.s["engine"]

    def step(self, lead: dict[str, Any]) -> Advance | Drop:
        stage = lead["stage"]
        if stage == "discovered":
            return research(lead, self.fetcher, self.s["verify"], self.clock().date())
        if stage == "verified":
            recent = self.store.recent_openers(max(1, self.voice.distinct_window))
            return write(lead, self.writer, self.voice, recent)
        if stage == "written":
            dup = self.store.email_taken(lead["data"]["email"]["address"], lead["id"])
            return gate(lead, suppressions=self.suppressions, voice=self.voice, duplicate_of=dup, now=self.clock())
        raise ValueError("no step for stage %r" % stage)

    def process(self, lead: dict[str, Any]) -> str:
        """Advance one leased lead as far as it goes. Returns 'ready' or 'dropped'; raises on failure."""
        lead = dict(lead)
        for _ in range(MAX_STEPS_PER_LEAD):
            if lead["stage"] not in RESEARCH:
                break
            outcome = self.step(lead)
            if isinstance(outcome, Drop):
                self.store.drop(lead["id"], outcome.reason, outcome.detail, outcome.data)
                self.store.event(lead["id"], "dropped", {"reason": outcome.reason, "detail": outcome.detail})
                return "dropped"
            self.store.advance(lead["id"], outcome.stage, outcome.data)
            self.store.event(lead["id"], outcome.stage, {"from": lead["stage"]})
            lead["stage"], lead["data"] = outcome.stage, outcome.data
        else:
            raise StepError("lead did not settle after %d steps" % MAX_STEPS_PER_LEAD)
        self.store.release(lead["id"])
        return lead["stage"]

    def work(self, lead: dict[str, Any]) -> dict[str, Any]:
        """Process one lead and turn any failure into the repair cycle. Returns a line for the summary."""
        line = {"id": lead["id"], "company": lead["company"], "from": lead["stage"],
                "attempts_before": lead["attempts"]}
        try:
            line["outcome"] = self.process(lead)
        except SharedOutage as e:
            self.store.release(lead["id"])
            self.store.event(lead["id"], "released", {"error": str(e)})
            line.update(outcome="released", detail=str(e))
            return line
        except Exception as e:  # anything else is this lead's failure alone
            error = str(e) if isinstance(e, StepError) else "%s: %s" % (type(e).__name__, e)
            e_ = self.engine
            outcome, attempts, next_at = self.store.fail(
                lead["id"], error, max_repairs=int(e_["max_repairs"]), backoff_minutes=float(e_["backoff_minutes"]),
                max_backoff_minutes=float(e_["max_backoff_minutes"]))
            self.store.event(lead["id"], "failed", {"error": error, "attempt": attempts, "outcome": outcome})
            line.update(outcome=outcome, detail=error, attempt=attempts, next_attempt_at=next_at)
            return line
        current = self.store.get(lead["id"])
        line.update(stage=current["stage"], reason=current["reason"], detail=current["detail"])
        return line

    def tick(self, *, max_leads: int | None = None, discover: bool = True, handoff: bool = True) -> dict[str, Any]:
        e = self.engine
        run_id = self.store.start_run("tick")
        started = self.clock()
        deadline = started + dt.timedelta(minutes=float(e["max_minutes"]))
        summary: dict[str, Any] = {"started_at": started.isoformat(timespec="seconds"), "resumed": [],
                                   "discovery": None, "leads": [], "stop_reason": ""}
        for lead_id, outcome in self.store.reap_expired(int(e["max_repairs"])):
            lead = self.store.get(lead_id)
            self.store.event(lead_id, "lease_expired", {"outcome": outcome})
            item = {"id": lead_id, "company": lead["company"], "stage": lead["stage"],
                    "outcome": "retired" if outcome == "retired" else "resumed"}
            summary["resumed"].append(item)
            self.notify("resumed", item)
        if discover and self.discover and self.store.claimable_count() < int(self.s["discovery"]["min_queue"]):
            try:
                found = self.discover(self.store)
                summary["discovery"] = {**found, "skipped": dict(found.get("skipped") or {})}
            except Exception as ex:  # a failed search never blocks leads already queued
                summary["discovery"] = {"error": "%s: %s" % (type(ex).__name__, ex)}
                self.store.event(None, "discovery_failed", summary["discovery"])
            self.notify("discovery", summary["discovery"])
        budget = int(max_leads if max_leads is not None else e["max_leads_per_tick"])
        in_a_row = 0
        while len(summary["leads"]) < budget:
            if self.clock() >= deadline:
                summary["stop_reason"] = "time budget spent"
                break
            lead = self.store.claim(float(e["lease_minutes"]))
            if lead is None:
                summary["stop_reason"] = "no lead due"
                break
            line = self.work(lead)
            summary["leads"].append(line)
            self.notify("lead", line)
            if line["outcome"] == "released":
                summary["stop_reason"] = "shared outage: %s" % line["detail"]
                break
            in_a_row = in_a_row + 1 if line["outcome"] in ("retry", "retired") else 0
            if in_a_row >= int(e["stop_after_errors"]):
                summary["stop_reason"] = "%d failures in a row" % in_a_row
                break
        else:
            summary["stop_reason"] = "lead budget reached"
        summary["outcomes"] = dict(Counter(x["outcome"] for x in summary["leads"]))
        if handoff and self.handoff:
            try:
                summary["handoff"] = self.handoff(self.store)
            except Exception as ex:
                summary["handoff"] = {"error": "%s: %s" % (type(ex).__name__, ex)}
            self.notify("handoff", summary["handoff"])
        self.notify("stop", {"reason": summary["stop_reason"]})
        summary["stages"] = self.store.counts()
        self.store.finish_run(run_id, summary)
        return summary


class TickLock:
    """Non-blocking single-instance lock: a second run that starts while one is going exits at once.
    Uses fcntl, so it works on macOS and Linux; elsewhere it is a no-op and the scheduler must not
    overlap runs."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> bool:
        try:
            import fcntl
        except ImportError:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def __exit__(self, *exc: object) -> None:
        if self.handle is not None:
            self.handle.close()

"""Durable per-lead state in SQLite. This is what makes a crashed or stalled run harmless.

Stages, in order:  discovered -> verified -> written -> ready -> handed_off
Terminal:          handed_off, dropped (proven unfit or unprovable), retired (repair budget used up)

- Every finished step is checkpointed before the next one starts, so a run that dies mid-lead loses at
  most the step in flight.
- A worker leases one lead at a time. A lease that expires without being released means the worker
  stopped mid-step; the next run counts that as a failed attempt and resumes the lead from its last
  checkpoint. A lead that keeps killing its worker is retired like any other repeated failure.
- A failed step records the error and schedules a retry with exponential backoff. After `max_repairs`
  failed attempts at a stage the lead is retired with the reason, never left in limbo.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

STAGES = ("discovered", "verified", "written", "ready", "handed_off", "dropped", "retired")
RESEARCH = ("discovered", "verified", "written")
TERMINAL = ("handed_off", "dropped", "retired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    company TEXT NOT NULL,
    website TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT '',
    keyword TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'discovered',
    reason TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    next_attempt_at TEXT NOT NULL,
    lease_until TEXT,
    email TEXT NOT NULL DEFAULT '',
    platform_id TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_due ON leads(stage, next_attempt_at);
CREATE INDEX IF NOT EXISTS leads_email ON leads(email);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cells (
    market TEXT NOT NULL,
    keyword TEXT NOT NULL,
    searched_at TEXT NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (market, keyword)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS handoff_writes (
    email TEXT PRIMARY KEY,
    lead_id INTEGER NOT NULL,
    campaign_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unresolved',
    at TEXT NOT NULL
);
"""

Clock = Callable[[], dt.datetime]
# Imported stranded leads wait for `lead-engine backlog`, which compares them with what the old tool had.
NOT_BACKLOG = " AND NOT (stage = 'discovered' AND source = 'backlog-import')"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path | str, clock: Clock | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or utc_now
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def now(self) -> str:
        return iso(self.clock())

    @staticmethod
    def _lead(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        d["data"] = json.loads(d.pop("data_json") or "{}")
        return d

    # leads -----------------------------------------------------------------

    def add_lead(self, *, domain: str, company: str, website: str, market: str = "", keyword: str = "",
                 source: str = "", data: dict[str, Any] | None = None) -> int | None:
        now = self.now()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO leads (domain, company, website, market, keyword, source, next_attempt_at,"
            " data_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (domain, company, website, market, keyword, source, now, json.dumps(data or {}), now, now))
        return int(cur.lastrowid) if cur.rowcount else None

    def get(self, lead_id: int) -> dict[str, Any]:
        lead = self._lead(self.db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())
        if lead is None:
            raise KeyError(lead_id)
        return lead

    def by_domain(self, domain: str) -> dict[str, Any] | None:
        return self._lead(self.db.execute("SELECT * FROM leads WHERE domain = ?", (domain,)).fetchone())

    def known_domains(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT domain FROM leads")}

    def by_stage(self, stages: tuple[str, ...], limit: int | None = None) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(stages))
        sql = "SELECT * FROM leads WHERE stage IN (%s) ORDER BY updated_at, id" % marks
        if limit is not None:
            sql += " LIMIT %d" % int(limit)
        return [self._lead(r) for r in self.db.execute(sql, stages)]

    def claimable_count(self) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM leads WHERE stage IN ('discovered', 'verified', 'written')"
            " AND next_attempt_at <= ? AND lease_until IS NULL" + NOT_BACKLOG, (self.now(),)).fetchone()[0])

    def reap_expired(self, max_repairs: int) -> list[tuple[int, str]]:
        """Leases that ran out without a release: the worker stopped mid-step. Each counts as a failed
        attempt; the lead resumes from its last checkpoint right away, or is retired if out of repairs."""
        now = self.now()
        rows = self.db.execute(
            "SELECT id, stage FROM leads WHERE lease_until IS NOT NULL AND lease_until < ?"
            " AND stage IN ('discovered', 'verified', 'written')", (now,)).fetchall()
        out = []
        for row in rows:
            outcome = self._failed(int(row["id"]), "stopped mid-step (lease expired)", max_repairs, 0, 0)
            out.append((int(row["id"]), outcome[0]))
        return out

    def claim(self, lease_minutes: float) -> dict[str, Any] | None:
        """Lease the most advanced lead that is due: finishing work beats starting new work."""
        now = self.clock()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM leads WHERE stage IN ('discovered', 'verified', 'written')"
                " AND next_attempt_at <= ? AND lease_until IS NULL" + NOT_BACKLOG +
                " ORDER BY CASE stage WHEN 'written' THEN 0 WHEN 'verified' THEN 1 ELSE 2 END, next_attempt_at, id"
                " LIMIT 1", (iso(now),)).fetchone()
            if row is not None:
                self.db.execute("UPDATE leads SET lease_until = ? WHERE id = ?",
                                (iso(now + dt.timedelta(minutes=lease_minutes)), row["id"]))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.lease(int(row["id"]), 0) if row is not None else None

    def lease(self, lead_id: int, lease_minutes: float) -> dict[str, Any]:
        if lease_minutes:
            until = iso(self.clock() + dt.timedelta(minutes=lease_minutes))
            self.db.execute("UPDATE leads SET lease_until = ? WHERE id = ?", (until, lead_id))
        return self.get(lead_id)

    def advance(self, lead_id: int, stage: str, data: dict[str, Any]) -> None:
        """Checkpoint a finished step. The lease is kept: the worker carries on with the next step."""
        if stage not in STAGES or stage in ("dropped", "retired"):
            raise ValueError("not a forward stage: %s" % stage)
        email = (data.get("email") or {}).get("address", "")
        now = self.now()
        self.db.execute(
            "UPDATE leads SET stage = ?, data_json = ?, email = ?, attempts = 0, last_error = '', reason = '',"
            " detail = '', next_attempt_at = ?, updated_at = ? WHERE id = ?",
            (stage, json.dumps(data), email, now, now, lead_id))

    def release(self, lead_id: int) -> None:
        self.db.execute("UPDATE leads SET lease_until = NULL WHERE id = ?", (lead_id,))

    def drop(self, lead_id: int, reason: str, detail: str = "", data: dict[str, Any] | None = None) -> None:
        self._close(lead_id, "dropped", reason, detail, data)

    def _close(self, lead_id: int, stage: str, reason: str, detail: str, data: dict[str, Any] | None = None) -> None:
        sets = "stage = ?, reason = ?, detail = ?, lease_until = NULL, updated_at = ?"
        args: list[Any] = [stage, reason, detail[:1000], self.now()]
        if data is not None:
            sets += ", data_json = ?"
            args.append(json.dumps(data))
        self.db.execute("UPDATE leads SET %s WHERE id = ?" % sets, (*args, lead_id))

    def fail(self, lead_id: int, error: str, *, max_repairs: int, backoff_minutes: float,
             max_backoff_minutes: float) -> tuple[str, int, str]:
        """Record a failed attempt. Returns ("retry", attempts, next_attempt_at) or ("retired", attempts, "")."""
        return self._failed(lead_id, error, max_repairs, backoff_minutes, max_backoff_minutes)

    def _failed(self, lead_id: int, error: str, max_repairs: int, backoff: float, cap: float) -> tuple[str, int, str]:
        lead = self.get(lead_id)
        attempts = int(lead["attempts"]) + 1
        if attempts >= max_repairs:
            self.db.execute("UPDATE leads SET attempts = ?, last_error = ? WHERE id = ?",
                            (attempts, error[:1000], lead_id))
            self._close(lead_id, "retired", "repairs_exhausted", "%s: %s" % (lead["stage"], error))
            return "retired", attempts, ""
        wait = min(backoff * 2 ** (attempts - 1), cap) if backoff else 0
        next_at = iso(self.clock() + dt.timedelta(minutes=wait))
        self.db.execute(
            "UPDATE leads SET attempts = ?, last_error = ?, next_attempt_at = ?, lease_until = NULL, updated_at = ?"
            " WHERE id = ?", (attempts, error[:1000], next_at, self.now(), lead_id))
        return "retry", attempts, next_at

    def hand_off(self, lead_id: int, platform_id: str) -> None:
        self.db.execute("UPDATE leads SET stage = 'handed_off', platform_id = ?, lease_until = NULL, updated_at = ?"
                        " WHERE id = ? AND stage = 'ready'", (platform_id, self.now(), lead_id))

    def handoff_recorded(self, email: str) -> bool:
        return self.db.execute("SELECT 1 FROM handoff_writes WHERE email = ?", (email.lower(),)).fetchone() is not None

    def handoff_candidates(self, limit: int) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM leads WHERE stage = 'ready' AND NOT EXISTS"
                               " (SELECT 1 FROM handoff_writes WHERE handoff_writes.email = lower(leads.email))"
                               " ORDER BY updated_at, id LIMIT ?", (limit,))
        return [self._lead(row) for row in rows]

    def unresolved_handoffs(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM handoff_writes WHERE status = 'unresolved'").fetchone()[0])

    def begin_handoff(self, batch: list[dict[str, Any]], campaign_id: str, payloads: list[dict[str, Any]]) -> bool:
        """Commit intent before the external write. An unresolved intent must never be sent again."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for lead, payload in zip(batch, payloads, strict=True):
                current = self.get(lead["id"])
                if current != lead or current["stage"] != "ready" or self.handoff_recorded(lead["email"]):
                    self.db.execute("ROLLBACK")
                    return False
                self.db.execute("INSERT INTO handoff_writes (email, lead_id, campaign_id, payload_json, at)"
                                " VALUES (?, ?, ?, ?, ?)",
                                (lead["email"].lower(), lead["id"], campaign_id, json.dumps(payload), self.now()))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return True

    def finish_handoff(self, batch: list[dict[str, Any]], created: dict[int, str], counts: dict[str, int]) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for index, lead in enumerate(batch):
                if index in created:
                    self.hand_off(lead["id"], created[index])
                else:
                    self.drop(lead["id"], "platform_skipped", json.dumps(counts))
                self.db.execute("UPDATE handoff_writes SET status = 'resolved' WHERE email = ?",
                                (lead["email"].lower(),))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def reopen(self, lead_id: int, stage: str, data: dict[str, Any]) -> None:
        """Put a stranded or retired lead back into the pipeline with a fresh repair budget."""
        if stage not in RESEARCH:
            raise ValueError("not a research stage: %s" % stage)
        self.advance(lead_id, stage, data)
        self.release(lead_id)

    def email_taken(self, email: str, exclude_id: int) -> int | None:
        row = self.db.execute("SELECT id FROM leads WHERE email = ? AND id != ? AND stage IN ('ready', 'handed_off')",
                              (email, exclude_id)).fetchone()
        return int(row[0]) if row else None

    def recent_openers(self, limit: int = 10) -> list[str]:
        rows = self.db.execute("SELECT data_json FROM leads WHERE stage IN ('written', 'ready', 'handed_off')"
                               " ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            text = (json.loads(r[0]).get("opener") or {}).get("text")
            if text:
                out.append(text)
        return out

    def stale(self, hours: float) -> list[dict[str, Any]]:
        cutoff = iso(self.clock() - dt.timedelta(hours=hours))
        rows = self.db.execute("SELECT * FROM leads WHERE stage IN ('discovered', 'verified', 'written')"
                               " AND lease_until IS NULL AND updated_at < ? AND next_attempt_at <= ? ORDER BY id",
                               (cutoff, self.now())).fetchall()
        return [self._lead(r) for r in rows]

    def counts(self) -> dict[str, int]:
        found = {r[0]: int(r[1]) for r in self.db.execute("SELECT stage, COUNT(*) FROM leads GROUP BY stage")}
        return {s: found.get(s, 0) for s in STAGES}

    def reasons(self, stage: str) -> dict[str, int]:
        return {r[0]: int(r[1]) for r in self.db.execute(
            "SELECT reason, COUNT(*) FROM leads WHERE stage = ? GROUP BY reason ORDER BY COUNT(*) DESC", (stage,))}

    def waiting(self) -> list[dict[str, Any]]:
        """Leads waiting out a backoff after a failed attempt."""
        rows = self.db.execute("SELECT * FROM leads WHERE stage IN ('discovered', 'verified', 'written')"
                               " AND attempts > 0 AND next_attempt_at > ? ORDER BY next_attempt_at", (self.now(),))
        return [self._lead(r) for r in rows]

    def leased(self) -> list[dict[str, Any]]:
        return [self._lead(r) for r in self.db.execute("SELECT * FROM leads WHERE lease_until IS NOT NULL")]

    # events, cells and runs ----------------------------------------------------

    def event(self, lead_id: int | None, kind: str, detail: dict[str, Any] | None = None) -> None:
        self.db.execute("INSERT INTO events (lead_id, kind, detail_json, at) VALUES (?, ?, ?, ?)",
                        (lead_id, kind, json.dumps(detail or {}, default=str)[:10000], self.now()))

    def events(self, lead_id: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events", ()
        if lead_id is not None:
            sql, args = sql + " WHERE lead_id = ?", (lead_id,)
        rows = self.db.execute(sql + " ORDER BY id", args).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]

    def cells_searched(self) -> dict[tuple[str, str], str]:
        return {(r[0], r[1]): r[2] for r in self.db.execute("SELECT market, keyword, searched_at FROM cells")}

    def record_cell(self, market: str, keyword: str, found: int) -> None:
        self.db.execute("INSERT INTO cells (market, keyword, searched_at, found) VALUES (?, ?, ?, ?)"
                        " ON CONFLICT (market, keyword) DO UPDATE SET searched_at = excluded.searched_at,"
                        " found = excluded.found", (market, keyword, self.now(), found))

    def start_run(self, kind: str) -> int:
        return int(self.db.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, self.now())).lastrowid)

    def finish_run(self, run_id: int, summary: dict[str, Any]) -> None:
        self.db.execute("UPDATE runs SET finished_at = ?, summary_json = ? WHERE id = ?",
                        (self.now(), json.dumps(summary, default=str), run_id))

    def runs(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "summary": json.loads(r["summary_json"])} for r in rows]

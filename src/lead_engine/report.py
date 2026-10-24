"""Plain-text summaries for the terminal, shared by `run`, `backlog`, `status` and the demo."""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any

from .evidence import normalize
from .http import safe_url
from .names import name_key
from .store import Store

EXPORT_COLUMNS = [
    "company", "website", "first_name", "last_name", "role", "role_source_url", "role_quote", "email",
    "email_source_url", "email_quote", "opener", "opener_source_url", "opener_quote", "market", "keyword", "stage",
]


def hhmm(value: str) -> str:
    try:
        return dt.datetime.fromisoformat(value).strftime("%H:%M")
    except ValueError:
        return value


def lead_line(store: Store, line: dict[str, Any], max_repairs: int) -> str:
    outcome = line["outcome"]
    label = "%-32s" % ("#%d %s" % (line["id"], line["company"]))[:32]
    if outcome == "ready":
        lead = store.get(line["id"])
        p, note = lead["data"]["person"], ""
        if line.get("attempts_before"):
            note = "repaired after %d failed attempt(s); " % line["attempts_before"]
        return "  %s ready     %s%s (%s), %s" % (label, note, p["full_name"], p["role"], lead["email"])
    if outcome == "dropped":
        return "  %s dropped   %s: %s" % (label, line.get("reason", ""), line.get("detail", ""))
    if outcome == "retry":
        return "  %s retry     %s (attempt %d of %d, next try %s)" % (label, line["detail"], line["attempt"],
                                                                     max_repairs,
                                                                     hhmm(line["next_attempt_at"]))
    if outcome == "retired":
        return "  %s retired   repairs_exhausted after %d attempts: %s" % (label, line["attempt"], line["detail"])
    return "  %s %-9s %s" % (label, outcome, line.get("detail", ""))


def event_lines(store: Store, kind: str, payload: dict[str, Any], max_repairs: int) -> list[str]:
    """Lines for one engine notification (see Engine.notify)."""
    if kind == "resumed":
        verb = "retired" if payload["outcome"] == "retired" else "resumed"
        return ["  %-32s %-9s worker stopped mid-step: lease expired, counted as a failed attempt, resuming at %s"
                % (("#%d %s" % (payload["id"], payload["company"]))[:32], verb, payload["stage"])]
    if kind == "discovery":
        if payload.get("error"):
            return ["  discovery failed: %s" % payload["error"]]
        if not (payload.get("searches") or payload.get("added")):
            return []
        skipped = ", ".join("%s %d" % kv for kv in sorted((payload.get("skipped") or {}).items()))
        return ["  discovered %d businesses in %d search(es)%s" % (
            payload["added"], len(payload.get("searches", [])), " (skipped %s)" % skipped if skipped else "")]
    if kind == "lead":
        return [lead_line(store, payload, max_repairs)]
    if kind == "handoff":
        if payload.get("mode") == "dry-run" and payload.get("would_add"):
            return ["  hand-off (dry run): would add %d lead(s) to campaign %s" % (payload["would_add"],
                                                                                payload["campaign_id"])]
        if payload.get("mode") == "live":
            errors = payload.get("errors") or ([payload["error"]] if payload.get("error") else [])
            lines = ["  hand-off: added %d, skipped %d%s" % (payload.get("added", 0), payload.get("skipped", 0),
                                                            ", error: %s" % errors[0] if errors else "")]
            if payload.get("unresolved"):
                lines.append("  hand-off: %d awaiting reconciliation; automatic resubmission blocked"
                             % payload["unresolved"])
            return lines
        if payload.get("error"):
            return ["  hand-off failed: %s" % payload["error"]]
        return []
    if kind == "stop" and payload.get("reason") not in ("", "no lead due", "lead budget reached"):
        return ["  stopped early: %s" % payload["reason"]]
    return []


def export_csv(store: Store, path: Path, stages: tuple[str, ...] = ("ready",)) -> int:
    rows = []
    for lead in store.by_stage(stages):
        d = lead["data"]
        facts = {f["id"]: f for f in d.get("facts", [])}
        person = d["person"]
        identity_facts = [facts[fid] for fid in person["fact_ids"]]
        role = next((fact for fact in identity_facts if fact["kind"] == "decision_maker"
                     and normalize(fact["value"].get("role", "")) == normalize(person["role"])
                     and name_key(fact["value"].get("first_name", ""), fact["value"].get("last_name", ""))
                     == name_key(person["first_name"], person["last_name"])), None)
        if role is None:
            raise ValueError("no evidence matches the exported person and role")
        email = facts[d["email"]["fact_id"]]
        opener = d.get("opener") or {}
        rows.append({
            "company": lead["company"], "website": safe_url(lead["website"]), "first_name": d["person"]["first_name"],
            "last_name": d["person"]["last_name"], "role": d["person"]["role"], "role_source_url": role["source_url"],
            "role_quote": role["quote"], "email": d["email"]["address"], "email_source_url": email["source_url"],
            "email_quote": email["quote"], "opener": opener.get("text", ""),
            "opener_source_url": opener.get("source_url", ""), "opener_quote": opener.get("quote", ""),
            "market": lead["market"], "keyword": lead["keyword"], "stage": lead["stage"],
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def status_lines(store: Store) -> list[str]:
    counts = store.counts()
    out = ["Leads by stage: " + ", ".join("%s %d" % kv for kv in counts.items())]
    unresolved = store.unresolved_handoffs()
    if unresolved:
        out.append("Hand-off awaiting reconciliation: %d" % unresolved)
    for stage in ("dropped", "retired"):
        reasons = store.reasons(stage)
        if reasons:
            out.append("%s: %s" % (stage.capitalize(), ", ".join("%s %d" % kv for kv in reasons.items())))
    waiting = store.waiting()
    if waiting:
        out.append("Waiting to retry: %d (next at %s)" % (len(waiting), waiting[0]["next_attempt_at"]))
        for lead in waiting[:5]:
            out.append("  #%d %s: attempt %d failed: %s" % (lead["id"], lead["company"], lead["attempts"],
                                                           lead["last_error"][:120]))
    leased = store.leased()
    if leased:
        now = store.now()
        expired = [x for x in leased if x["lease_until"] < now]
        out.append("Leased now: %d (%d expired; the next run counts them as failed attempts and resumes them)"
                   % (len(leased), len(expired)))
    runs = store.runs(5)
    if runs:
        out.append("Last runs:")
        for r in runs:
            if not r["finished_at"]:
                out.append("  #%d %s started %s, did not finish" % (r["id"], r["kind"], r["started_at"]))
                continue
            s = r["summary"]
            outcomes = ", ".join("%s %d" % kv for kv in sorted((s.get("outcomes") or {}).items())) or "nothing due"
            out.append("  #%d %s %s: %s%s" % (r["id"], r["kind"], r["started_at"], outcomes,
                                             " (%s)" % s["stop_reason"] if s.get("stop_reason") else ""))
    return out

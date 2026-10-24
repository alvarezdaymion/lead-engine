"""Backlog re-qualification: re-check the stranded pile and recover what can be proven.

The pile is every lead that stopped short of a campaign: leads retired after their repair budget ran
out, leads that sat untouched in a research stage for `stale_hours`, and half-researched leads imported
from another tool (`--import stranded.csv`). Each is verified again from a fresh read of its website:

- recovered: the decision-maker is proven again (the same person; nicknames and suffixes count, so do
  matching addresses). The lead re-enters at `verified` with a fresh repair budget and continues to
  write and gate in the same run.
- changed: the site now proves a different decision-maker. The old name is recorded and the lead
  continues with the new person.
- dropped: the site is readable but nothing can be proven any more.
- inconclusive: the site could not be read right now. The lead is left exactly as it was for a later run.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import InputError, diagnostic, sanitized
from .fetch import host_of
from .names import same_person
from .outcome import Drop, SharedOutage
from .verify import research

IMPORT_COLUMNS = ("company", "website", "first_name", "last_name", "email")


@sanitized
def import_stranded(engine: Engine, path: Path | str) -> int:
    """Queue half-researched leads from another tool. Known domains are skipped. Returns rows added."""
    store = engine.store
    added = 0
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = {"company", "website"} - set(reader.fieldnames or [])
        if missing:
            raise InputError("stranded CSV is missing required columns: %s" % ", ".join(sorted(missing)))
        for row in reader:
            website = (row.get("website") or "").strip()
            if website and "://" not in website:
                website = "https://" + website
            host = host_of(website)
            if not host:
                continue
            prior = {k: (row.get(k) or "").strip() for k in ("first_name", "last_name", "email")}
            lead_id = store.add_lead(domain=host, company=(row.get("company") or "").strip() or host, website=website,
                                     market=(row.get("market") or "").strip(),
                                     keyword=(row.get("keyword") or "").strip(),
                                     source="backlog-import", data={"prior": prior})
            if lead_id:
                added += 1
    return added


def stranded(engine: Engine) -> list[dict[str, Any]]:
    store = engine.store
    pile = {x["id"]: x for x in store.by_stage(("retired",))}
    pile.update({x["id"]: x for x in store.stale(float(engine.s["engine"]["stale_hours"]))})
    pile.update({x["id"]: x for x in store.by_stage(("discovered",)) if x["source"] == "backlog-import"})
    return [pile[k] for k in sorted(pile)]


def _prior(lead: dict[str, Any]) -> dict[str, str]:
    d = lead["data"]
    if d.get("prior"):
        return d["prior"]
    person = d.get("person") or {}
    return {"first_name": person.get("first_name", ""), "last_name": person.get("last_name", ""),
            "email": (d.get("email") or {}).get("address", "")}


def requalify(engine: Engine, limit: int | None = None) -> dict[str, Any]:
    store = engine.store
    run_id = store.start_run("backlog")
    pile = stranded(engine)[: limit if limit is not None else None]
    rows: list[dict[str, Any]] = []
    stop_reason = ""
    for lead in pile:
        prior = _prior(lead)
        row: dict[str, Any] = {"id": lead["id"], "company": lead["company"], "was": lead["stage"]}
        try:
            outcome = research(lead, engine.fetcher, engine.s["verify"], engine.clock().date())
        except SharedOutage as e:
            stop_reason = "shared outage: %s" % diagnostic(e)
            break
        except Exception as e:
            row.update(result="inconclusive", detail=diagnostic(e))
            store.event(lead["id"], "backlog_inconclusive", {"error": diagnostic(e)})
            rows.append(row)
            continue
        if isinstance(outcome, Drop):
            store.drop(lead["id"], outcome.reason, outcome.detail, outcome.data)
            store.event(lead["id"], "backlog_dropped", {"reason": outcome.reason})
            row.update(result="dropped", detail="%s: %s" % (outcome.reason, outcome.detail))
            rows.append(row)
            continue
        person = outcome.data["person"]
        same = (same_person(prior.get("first_name", ""), prior.get("last_name", ""), person["first_name"],
                            person["last_name"])
                or (prior.get("email") and prior["email"].lower() == outcome.data["email"]["address"]))
        result = "recovered" if same or not prior.get("last_name") else "changed"
        outcome.data["backlog"] = {"result": result, "prior": prior, "rechecked_at": store.now()}
        store.reopen(lead["id"], "verified", outcome.data)
        store.event(lead["id"], "backlog_" + result, {"prior": prior, "now": person["full_name"]})
        was = ("%s %s" % (prior.get("first_name", ""), prior.get("last_name", ""))).strip()
        note = ""
        if result == "changed":
            note = " (was %s)" % was
        elif was and was != person["full_name"]:
            note = " (same person as %s)" % was
        row.update(result=result, detail=person["full_name"] + note)
        line = engine.work(store.lease(lead["id"], float(engine.s["engine"]["lease_minutes"])))
        row["then"] = line.get("stage") or line["outcome"]
        if line.get("reason"):
            row["then"] += " (%s)" % line["reason"]
        rows.append(row)
        if line["outcome"] == "released":
            stop_reason = "shared outage: %s" % line["detail"]
            break
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    store.finish_run(run_id, {"outcomes": counts, "rechecked": len(rows), "stop_reason": stop_reason})
    return {"rechecked": len(rows), "counts": counts, "leads": rows, "stop_reason": stop_reason}

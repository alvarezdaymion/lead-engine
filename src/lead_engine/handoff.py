"""Hand campaign-ready leads to the email platform. This tool never sends email itself.

Modes (`[handoff] mode`):
- off: nothing happens; ready leads wait.
- dry-run (default): the exact requests live mode would send are written to out/handoff/ and nothing is
  sent. Leads stay `ready`, so switching to live later hands them off.
- live: leads are added to the campaign in batches. Leads the platform creates become `handed_off` with
  its lead id; leads it skips (already in the workspace, blocklisted or invalid) are dropped with reason
  `platform_skipped`. An uncertain request leaves its leads `ready` for manual reconciliation and does
  not use their repair budget. Its durable write record prevents automatic resubmission.

The suppression list is checked again right before each batch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import diagnostic, sanitized
from .fetch import host_of
from .gate import Suppressions, evidence_problem, is_suppressed, load_suppressions
from .http import HttpError, safe_url
from .instantly import API, leads_add_body
from .store import Store


def lead_payload(lead: dict[str, Any]) -> dict[str, Any]:
    d = lead["data"]
    return {
        "email": d["email"]["address"],
        "first_name": d["person"]["first_name"],
        "last_name": d["person"]["last_name"],
        "company_name": lead["company"],
        "website": safe_url(lead["website"]),
        "personalization": d["opener"]["text"],
        "custom_variables": {
            "role": d["person"]["role"],
            "opener_source": d["opener"]["source_url"],
            "market": lead.get("market", ""),
        },
    }


@sanitized
def run_handoff(store: Store, settings: dict[str, Any], *, suppressions: Suppressions, out_dir: Path,
                client: Any = None, campaign_id: str | None = None,
                suppression_file: Path | None = None, roles: list[str] | None = None) -> dict[str, Any]:
    mode = settings["mode"]
    if mode == "off":
        return {"mode": "off"}
    if mode == "live":
        suppressions = load_suppressions(suppression_file)
    leads = []
    for lead in store.handoff_candidates(int(settings["max_leads_per_run"])):
        problem = evidence_problem(lead, roles)
        if problem:
            store.drop(lead["id"], problem.reason, problem.detail)
            continue
        if is_suppressed(lead["email"], host_of(lead["website"]), suppressions):
            store.drop(lead["id"], "suppressed", lead["email"])
            continue
        leads.append(lead)
    campaign_id = campaign_id if campaign_id is not None else os.environ.get("INSTANTLY_CAMPAIGN_ID", "")
    size = max(1, min(1000, int(settings["batch_size"])))
    batches = [leads[i:i + size] for i in range(0, len(leads), size)]
    if mode == "dry-run":
        if not leads:
            return {"mode": "dry-run", "would_add": 0}
        target = campaign_id or "<INSTANTLY_CAMPAIGN_ID>"
        requests = [{"method": "POST", "url": API + "/api/v2/leads/add",
                     "body": leads_add_body(target, [lead_payload(x) for x in batch])} for batch in batches]
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / ("dry-run-%s.json" % store.now().replace(":", "").replace("+0000", "Z"))
        path.write_text(json.dumps({"requests": requests}, indent=2) + "\n")
        return {"mode": "dry-run", "would_add": len(leads), "file": str(path), "campaign_id": target}
    if not campaign_id:
        return {"mode": "live", "error": "set INSTANTLY_CAMPAIGN_ID", "added": 0}
    added, skipped, errors = 0, 0, []
    for batch in batches:
        current_suppressions = load_suppressions(suppression_file)
        eligible = []
        for original in batch:
            lead = store.get(original["id"])
            if lead["stage"] != "ready" or store.handoff_recorded(lead["email"]):
                continue
            problem = evidence_problem(lead, roles)
            if problem:
                store.drop(lead["id"], problem.reason, problem.detail)
                continue
            if is_suppressed(lead["email"], host_of(lead["website"]), current_suppressions):
                store.drop(lead["id"], "suppressed", lead["email"])
                continue
            eligible.append(lead)
        batch = eligible
        if not batch:
            continue
        payloads = [lead_payload(x) for x in batch]
        if not store.begin_handoff(batch, campaign_id, payloads):
            continue
        try:
            resp = client.add_leads(campaign_id, payloads)
            created, counts = _batch_result(resp, len(batch))
        except Exception as e:
            errors.append(diagnostic(e))
            store.event(None, "handoff_failed", {"error": diagnostic(e), "leads": [x["id"] for x in batch]})
            break
        store.finish_handoff(batch, created, counts)
        added += len(created)
        skipped += len(batch) - len(created)
        store.event(None, "handoff", {"campaign_id": campaign_id, "added": len(created), "batch": len(batch),
                                      **counts})
    return {"mode": "live", "added": added, "skipped": skipped, "errors": errors, "campaign_id": campaign_id,
            "unresolved": store.unresolved_handoffs()}


def _batch_result(response: Any, size: int) -> tuple[dict[int, str], dict[str, int]]:
    error = "unresolved hand-off: response does not account for the entire batch; reconcile before retrying"
    if not isinstance(response, dict) or not isinstance(response.get("created_leads"), list):
        raise HttpError(error)
    if "status" in response and response["status"] != "success":
        raise HttpError(error)
    created: dict[int, str] = {}
    for item in response["created_leads"]:
        if not isinstance(item, dict):
            raise HttpError(error)
        index, platform_id = item.get("index"), item.get("id")
        if (type(index) is not int or not 0 <= index < size or index in created
                or not isinstance(platform_id, str) or not platform_id.strip() or platform_id in created.values()):
            raise HttpError(error)
        created[index] = platform_id
    counts = {k: response.get(k, 0) for k in ("duplicated_leads", "in_blocklist", "invalid_email_count",
                                          "skipped_count")}
    if any(type(n) is not int or n < 0 for n in counts.values()) or len(created) + sum(counts.values()) != size:
        raise HttpError(error)
    for key, expected in (("total_sent", size), ("leads_uploaded", len(created))):
        if key in response and (type(response[key]) is not int or response[key] != expected):
            raise HttpError(error)
    return created, counts

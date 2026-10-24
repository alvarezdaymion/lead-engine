"""Hand campaign-ready leads to the email platform. This tool never sends email itself.

Modes (`[handoff] mode`):
- off: nothing happens; ready leads wait.
- dry-run (default): the exact request body is written to out/handoff/ and nothing is sent. Leads stay
  `ready`, so switching to live later hands them off.
- live: leads are added to the campaign in batches. Leads the platform creates become `handed_off` with
  its lead id; leads it skips (already in the workspace, blocklisted or invalid) are dropped with reason
  `platform_skipped`. A failed request leaves its leads `ready` for the next run and does not use their
  repair budget, because the problem is the platform, not the lead.

The suppression list is checked again right before each batch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .fetch import host_of
from .gate import Suppressions, is_suppressed
from .http import HttpError
from .store import Store


def lead_payload(lead: dict[str, Any]) -> dict[str, Any]:
    d = lead["data"]
    return {
        "email": d["email"]["address"],
        "first_name": d["person"]["first_name"],
        "last_name": d["person"]["last_name"],
        "company_name": lead["company"],
        "website": lead["website"],
        "personalization": d["opener"]["text"],
        "custom_variables": {
            "role": d["person"]["role"],
            "opener_source": d["opener"]["source_url"],
            "market": lead.get("market", ""),
        },
    }


def run_handoff(store: Store, settings: dict[str, Any], *, suppressions: Suppressions, out_dir: Path,
                client: Any = None, campaign_id: str | None = None) -> dict[str, Any]:
    mode = settings["mode"]
    if mode == "off":
        return {"mode": "off"}
    leads = []
    for lead in store.by_stage(("ready",), limit=int(settings["max_leads_per_run"])):
        if is_suppressed(lead["email"], host_of(lead["website"]), suppressions):
            store.drop(lead["id"], "suppressed", lead["email"])
            continue
        leads.append(lead)
    campaign_id = campaign_id if campaign_id is not None else os.environ.get("INSTANTLY_CAMPAIGN_ID", "")
    if mode == "dry-run":
        if not leads:
            return {"mode": "dry-run", "would_add": 0}
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / ("dry-run-%s.json" % store.now().replace(":", "").replace("+0000", "Z"))
        body = {"endpoint": "POST https://api.instantly.ai/api/v2/leads/add",
                "campaign_id": campaign_id or "<INSTANTLY_CAMPAIGN_ID>", "skip_if_in_workspace": True,
                "verify_leads_on_import": False, "leads": [lead_payload(x) for x in leads]}
        path.write_text(json.dumps(body, indent=2) + "\n")
        return {"mode": "dry-run", "would_add": len(leads), "file": str(path), "campaign_id": body["campaign_id"]}
    if not campaign_id:
        return {"mode": "live", "error": "set INSTANTLY_CAMPAIGN_ID", "added": 0}
    added, skipped, errors = 0, 0, []
    size = max(1, min(1000, int(settings["batch_size"])))
    for i in range(0, len(leads), size):
        batch = leads[i:i + size]
        try:
            resp = client.add_leads(campaign_id, [lead_payload(x) for x in batch])
        except HttpError as e:
            errors.append(str(e))
            store.event(None, "handoff_failed", {"error": str(e), "leads": [x["id"] for x in batch]})
            break
        created = {int(c["index"]): str(c.get("id", "")) for c in resp.get("created_leads", [])}
        counts = {k: resp.get(k, 0) for k in ("duplicated_leads", "in_blocklist", "invalid_email_count",
                                               "skipped_count")}
        for index, lead in enumerate(batch):
            if index in created:
                store.hand_off(lead["id"], created[index])
                added += 1
            else:
                store.drop(lead["id"], "platform_skipped", json.dumps(counts))
                skipped += 1
        store.event(None, "handoff", {"campaign_id": campaign_id, "added": len(created), "batch": len(batch),
                                      **counts})
    return {"mode": "live", "added": added, "skipped": skipped, "errors": errors, "campaign_id": campaign_id}

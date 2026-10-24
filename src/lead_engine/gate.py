"""The compliance gate: the last check before a lead is campaign-ready.

- The address and the business's domain are checked against the suppression list (unsubscribes,
  bounces, do-not-contact). The hand-off checks it again right before sending to the platform.
- An address already ready or handed off on another lead is a duplicate.
- Every fact the lead relies on (decision-maker, email, the opener's facts) is proven again against the
  stored page snapshot.
- The opener is checked again against the current voice rules; if those changed since it was written,
  the lead goes back one stage to be rewritten.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import Page, supported
from .fetch import host_of
from .outcome import Advance, Drop, StepError
from .voice import Voice, check_opener


@dataclass(frozen=True)
class Suppressions:
    emails: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()

    def __len__(self) -> int:
        return len(self.emails) + len(self.domains)


def load_suppressions(path: Path | None) -> Suppressions:
    """CSV with an `email` column, a `domain` column, or both. A missing file means an empty list."""
    if path is None or not Path(path).exists():
        return Suppressions()
    emails, domains = set(), set()
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            email = (row.get("email") or "").strip().lower()
            domain = (row.get("domain") or "").strip().lower().removeprefix("@").removeprefix("www.")
            if email:
                emails.add(email)
            if domain:
                domains.add(domain)
    return Suppressions(frozenset(emails), frozenset(domains))


def is_suppressed(email: str, site_host: str, s: Suppressions) -> bool:
    email = email.strip().lower()
    hosts = {email.rpartition("@")[2], site_host.lower()}
    return email in s.emails or any(h == d or h.endswith("." + d) for h in hosts if h for d in s.domains)


def gate(lead: dict[str, Any], *, suppressions: Suppressions, voice: Voice, duplicate_of: int | None,
         now: dt.datetime) -> Advance | Drop:
    data = dict(lead["data"])
    email = data["email"]["address"]
    if is_suppressed(email, host_of(lead["website"]), suppressions):
        return Drop("suppressed", email)
    if duplicate_of is not None:
        return Drop("duplicate_email", "%s is already on lead #%d" % (email, duplicate_of))
    pages = [Page.from_dict(p) for p in data.get("pages", [])]
    facts = {f["id"]: f for f in data.get("facts", [])}
    needed = data["person"]["fact_ids"] + [data["email"]["fact_id"]] + data["opener"]["fact_ids"]
    for fid in needed:
        if fid not in facts or not supported(facts[fid], pages):
            raise StepError("fact %s no longer matches its stored page" % fid)
    problems = check_opener(data["opener"]["text"], voice=voice, cited=[facts[i] for i in data["opener"]["fact_ids"]],
                            company=lead["company"], market=lead.get("market", ""),
                            first_name=data["person"]["first_name"])
    if problems:
        data.pop("opener")
        data["rewrite_reason"] = problems[0]
        return Advance("verified", data)
    missing = [k for k, v in (("first name", data["person"]["first_name"]), ("last name", data["person"]["last_name"]),
                              ("company", lead["company"]), ("website", lead["website"])) if not v]
    if missing:
        return Drop("missing_fields", ", ".join(missing))
    data["gated_at"] = now.isoformat(timespec="seconds")
    return Advance("ready", data)

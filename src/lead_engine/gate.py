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
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .evidence import Page, supported
from .fetch import host_of
from .html import EMAIL_RE
from .names import name_key
from .outcome import Advance, Drop, StepError
from .verify import find_email, find_people
from .voice import Voice, check_opener


@dataclass(frozen=True)
class Suppressions:
    emails: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()

    def __len__(self) -> int:
        return len(self.emails) + len(self.domains)


def load_suppressions(path: Path | None) -> Suppressions:
    """Read a complete CSV with an `email` column, a `domain` column, or both. Fail closed on invalid input."""
    if path is None:
        raise ValueError("a suppression file is required")
    emails, domains = set(), set()
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        try:
            reader = csv.DictReader(f, strict=True)
            fields = reader.fieldnames or []
            if not fields or set(fields) - {"email", "domain"} or len(fields) != len(set(fields)):
                raise ValueError("suppression CSV columns must be email, domain, or both, without duplicates")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("suppression CSV has an incomplete row")
                email = (row.get("email") or "").strip().lower()
                domain = (row.get("domain") or "").strip().lower().removeprefix("@").removeprefix("www.")
                if email:
                    if not EMAIL_RE.fullmatch(email):
                        raise ValueError("suppression CSV has an invalid email")
                    emails.add(email)
                if domain:
                    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+",
                                        domain):
                        raise ValueError("suppression CSV has an invalid domain")
                    domains.add(domain)
        except csv.Error:
            raise ValueError("suppression CSV is malformed") from None
    return Suppressions(frozenset(emails), frozenset(domains))


def is_suppressed(email: str, site_host: str, s: Suppressions) -> bool:
    email = email.strip().lower()
    hosts = {email.rpartition("@")[2], site_host.lower()}
    return email in s.emails or any(h == d or h.endswith("." + d) for h in hosts if h for d in s.domains)


def gate(lead: dict[str, Any], *, suppressions: Suppressions, voice: Voice, duplicate_of: int | None,
         now: dt.datetime, roles: list[str] | None = None) -> Advance | Drop:
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
        if fid not in facts or not supported(facts[fid], pages, company=lead["company"]):
            raise StepError("fact %s no longer matches its stored page" % fid)
    person_data = data["person"]
    person_facts = [facts[fid] for fid in person_data["fact_ids"]]
    key = name_key(person_data["first_name"], person_data["last_name"])
    if (not person_facts or any(f["kind"] != "decision_maker" or
            name_key(f["value"].get("first_name", ""), f["value"].get("last_name", "")) != key for f in person_facts)
            or not any(all(f["value"].get(k) == person_data[k] for k in ("full_name", "first_name", "last_name"))
                       for f in person_facts)
            or not any(f["value"].get("role") == person_data["role"] for f in person_facts)):
        raise StepError("person does not match the cited decision-maker facts")
    policy = roles if roles is not None else [person_data["role"]]
    if not policy:
        raise StepError("decision-maker role policy is empty")
    people = find_people(pages, policy, lead["company"])
    person = next((p for p in people if name_key(p.first_name, p.last_name) == key
                   and p.role == person_data["role"]), None)
    email_fact = facts[data["email"]["fact_id"]]
    if person is None or email_fact["kind"] != "email" or email_fact["value"].get("email") != email:
        raise StepError("person and email do not match their evidence")
    email_pages = [replace(p, emails=[item for item in p.emails if item[0] == email]) for p in pages]
    proven_email, _ = find_email(email_pages, person, host_of(lead["website"]),
                                 others=[p for p in people if p != person])
    if proven_email is None:
        raise StepError("email is not unambiguously tied to the decision-maker")
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

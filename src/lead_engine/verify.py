"""Prove identities, contacts and business facts using README's exact whole-text allowlist.

All fetched snapshots survive checkpointing. Unsupported attribution drops; unreadable pages retry.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .evidence import EVIDENCE_VERSION, Page, normalize, schema_people, schema_quote, sentences, supported
from .fetch import Disallowed, Fetcher, FetchError, OffsiteRedirect, host_of
from .http import safe_url
from .names import name_key, split_name
from .outcome import Advance, Drop, StepError

DEFAULT_ROLES = ["owner", "co-owner", "founder", "co-founder", "proprietor", "president", "ceo", "principal",
                 "managing partner", "managing director", "general manager"]
ROLE_RANK = {"owner": 0, "co-owner": 0, "founder": 0, "co-founder": 0, "proprietor": 0, "president": 1,
             "ceo": 1, "principal": 1, "managing partner": 2, "managing director": 2, "general manager": 3}
PAGE_HINTS = [("about", 0), ("team", 0), ("owner", 0), ("founder", 0), ("story", 1), ("meet", 1),
              ("staff", 1), ("people", 1), ("leadership", 1), ("who-we-are", 1), ("bio", 1), ("contact", 2)]
ASSET_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".css", ".js", ".ico",
                  ".mp4", ".mp3", ".xml")
SHARED_INBOXES = {
    "info", "hello", "hi", "contact", "office", "admin", "support", "sales", "team", "billing", "booking",
    "bookings", "frontdesk", "inquiries", "enquiries", "service", "services", "help", "accounts", "mail",
    "email", "noreply", "no-reply", "webmaster", "marketing", "careers", "jobs", "hr", "orders",
    "reception", "appointments", "schedule", "privacy", "legal", "press", "media", "quotes", "estimates",
}
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com",
             "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com"}
PUBLIC_SUFFIXES = {"co.uk", "org.uk", "com.au", "net.au", "co.nz", "co.za", "com.br", "co.jp", "com.mx"}
NAME = r"[A-Z][a-z]+(?: [A-Z][a-z]+){1,2}"
SPECIALTIES = (
    "bookkeeping", "payroll", "tax preparation", "financial reporting",
    "bookkeeping for restaurants and cafes", "older homes and historic districts",
    "weekly mowing and spring cleanups",
)
AWARDS = ("Best Bookkeeper", "Best Shop", "Best Home Inspector")
HEADINGS = frozenset({"about", "about us", "our team", "meet the team", "team", "contact", "contact us",
                      "our story", "history", "leadership", "services"})
OPENER_KINDS = ("award", "founded", "serving", "specialty")


@dataclass
class Person:
    full_name: str
    first_name: str
    last_name: str
    role: str
    rank: int
    facts: list[dict[str, Any]] = field(default_factory=list)


def surface(text: str) -> str:
    return " ".join(text.replace("\u2013", "-").replace("\u2014", "-").split())


def assertions(page: Page) -> list[str]:
    scopes = page.elements if page.elements else [page.text]
    return list(dict.fromkeys(sentence for scope in scopes for sentence in sentences(surface(scope))))


def _scope_allowed(page: Page, scope: str, company: str, names: set[str] | None = None) -> bool:
    text = normalize(scope)
    if any(text in normalize(quote) for quote in page.quotations):
        return False
    allowed = HEADINGS | {normalize(company)} | {normalize(n) for n in names or set()}
    return all(all(normalize(part) in allowed for part in heading.split(" / "))
               for content, heading in page.sections if text in normalize(content))


def _identity(text: str, roles: list[str], company: str) -> tuple[str, str] | None:
    role = "|".join(re.escape(surface(r)) for r in roles if surface(r))
    if not role:
        return None
    name = rf"(?P<name>{NAME})"
    title = rf"(?P<role>(?i:{role}))"
    forms = [rf"{name}, {title}", rf"{name} - {title}", rf"{title}: {name}",
             rf"{name} (?i:is our) {title}", rf"(?i:Meet) {name}, (?i:our) {title}"]
    if company:
        forms.append(rf"{name} (?i:is the) {title} (?i:of {re.escape(surface(company))})")
    for form in forms:
        match = re.fullmatch(form + r"\.?", surface(text))
        if match and normalize(match["name"]) != normalize(company):
            return match["name"], match["role"]
    return None


def _contact(address: str, context: str, method: str, names: set[str]) -> str | None:
    for name in sorted(names):
        if method == "mailto":
            if surface(context) in (name, "Email " + name):
                return name
        elif method == "text":
            form = rf"(?:{re.escape(name)}: |Email {re.escape(name)}: |{re.escape(name)} )"
            if re.fullmatch(form + rf"(?i:{re.escape(address)})\.?", surface(context)):
                return name
    return None


def _fact(kind: str, value: dict[str, Any], page: Page, quote: str, method: str, company: str) -> dict[str, Any]:
    return {"kind": kind, "value": value, "source_url": page.final_url, "quote": quote,
            "method": method, "subject": company}


def _person_fact(name: str, role: str, page: Page, quote: str, method: str, company: str) -> dict[str, Any]:
    first, last = split_name(name)
    value = {"full_name": name, "first_name": first, "last_name": last, "role": role[:1].upper() + role[1:],
             "role_from": "jobTitle" if method == "schema.org" else "text"}
    return _fact("decision_maker", value, page, quote, method, company)


def _variants(person: Person) -> set[str]:
    return {f["value"]["full_name"] for f in person.facts}


def find_people(pages: list[Page], roles: list[str], company: str) -> list[Person]:
    found = []
    for page in pages:
        for prop, node in schema_people(page.jsonld, company):
            name, title = node.get("name"), node.get("jobTitle")
            if (isinstance(name, str) and re.fullmatch(NAME, name) and isinstance(title, str)
                    and normalize(title) in {normalize(r) for r in roles}):
                found.append(_person_fact(name, title, page, schema_quote(prop, node), "schema.org", company))
        for sentence in assertions(page):
            match = _identity(sentence, roles, company)
            if match and _scope_allowed(page, sentence, company, {match[0]}):
                found.append(_person_fact(*match, page, sentence, "text", company))
    people: dict[tuple[str, str], Person] = {}
    for fact in found:
        value = fact["value"]
        key = name_key(value["first_name"], value["last_name"])
        rank = ROLE_RANK.get(normalize(value["role"]), len(ROLE_RANK))
        person = people.setdefault(key, Person(value["full_name"], value["first_name"], value["last_name"],
                                              value["role"], rank))
        if rank < person.rank:
            person.role, person.rank = value["role"], rank
        if fact not in person.facts:
            person.facts.append(fact)
    eligible = []
    for person in people.values():
        names = _variants(person)
        proven = True
        for page in pages:
            for sentence in assertions(page):
                if not any(re.search(r"\b%s\b" % re.escape(name), sentence, re.I) for name in names):
                    continue
                identity = _identity(sentence, roles, company)
                contacts = [(a, c, m) for a, c, m in page.emails if normalize(sentence) == normalize(c)]
                if not (identity and identity[0] in names) and not any(
                        _contact(a, c, m, names) for a, c, m in contacts):
                    proven = False
        if proven:
            person.facts.sort(key=lambda fact: fact["method"] != "text")
            eligible.append(person)
    return sorted(eligible, key=lambda person: (person.rank, all(f["method"] != "text" for f in person.facts)))


def on_site_domain(domain: str, host: str) -> bool:
    if "." not in domain or domain in PUBLIC_SUFFIXES:
        return False
    return domain == host or host.endswith("." + domain) or domain.endswith("." + host)


def find_email(pages: list[Page], person: Person, host: str, allow_free_mail: bool = True,
               others: list[Person] | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    occurrences: dict[str, list[tuple[Page, str, str]]] = {}
    for page in pages:
        for address, context, method in page.emails:
            occurrences.setdefault(address, []).append((page, context, method))
    names = _variants(person)
    rejected, candidates = [], []
    company = str(person.facts[0].get("subject") or "") if person.facts else ""
    for address, items in occurrences.items():
        local, _, domain = address.partition("@")
        own = on_site_domain(domain, host)
        if local.split("+", 1)[0] in SHARED_INBOXES:
            reason = "is a shared inbox"
        elif not own and not (allow_free_mail and domain in FREE_MAIL):
            reason = "is not on the business's domain"
        elif any(not _contact(address, context, method, names) or not _scope_allowed(page, context, company, names)
                 for page, context, method in items):
            reason = "does not match an exact full-name contact template"
        else:
            page, context, method = items[0]
            full_name = _contact(address, context, method, names)
            quote = "mailto:" + address if method == "mailto" else context
            value = {"email": address, "full_name": full_name, "binding": "printed beside the name"}
            candidates.append((not own, _fact("email", value, page, quote, method, company)))
            continue
        rejected.append("%s %s" % (address, reason))
    candidates.sort(key=lambda item: item[0])
    return (candidates[0][1] if candidates else None), rejected


def opener_facts(pages: list[Page], today: dt.date | None = None, *, company: str = "", market: str = ""
                 ) -> list[dict[str, Any]]:
    subjects = ["We", "Our firm", "Our shop", "Our company", "Our team"] + ([surface(company)] if company else [])
    subject = "(?:" + "|".join(re.escape(s) for s in subjects) + ")"
    year = r"(?P<year>(?:19|20)\d{2})"
    specialty = "(?P<specialty>" + "|".join(re.escape(s) for s in SPECIALTIES) + ")"
    award = "(?P<award>" + "|".join(re.escape(s) for s in AWARDS) + ")"
    forms = [("founded", rf"{subject} was founded in {year}\."),
             ("founded", rf"{subject} opened in {year}\."),
             ("specialty", rf"{subject} specializes in {specialty}\."),
             ("specialty", rf"We specialize in {specialty}\."),
             ("award", rf"{subject} won {award} in {year}\.")]
    place = surface(market.split(",")[0])
    if place:
        forms.append(("serving", rf"{subject} has served (?P<place>{re.escape(place)}) since {year}\."))
    out = []
    seen = set()
    for page in pages:
        for sentence in assertions(page):
            if not _scope_allowed(page, sentence, company):
                continue
            for kind, form in forms:
                match = re.fullmatch(form, sentence, re.I)
                if not match:
                    continue
                value = match.groupdict()
                if "year" in value and not 1900 <= int(value["year"]) <= (today or dt.date.today()).year:
                    continue
                key = kind, tuple(value.items())
                if key not in seen:
                    seen.add(key)
                    out.append(_fact(kind, value, page, sentence, "text", company))
    return out


def candidate_urls(home: Page, host: str, limit: int) -> list[str]:
    scored: dict[str, int] = {}
    home_key = home.final_url.split("?")[0].rstrip("/")
    for url, text in home.links:
        if host_of(url) != host:
            continue
        parts = urlsplit(url)
        path, label = parts.path.lower(), text.lower()
        if path.endswith(ASSET_SUFFIXES):
            continue
        rank = min((r for hint, r in PAGE_HINTS if hint in path or hint.replace("-", " ") in label), default=None)
        key = "%s://%s%s" % (parts.scheme, parts.netloc, parts.path.rstrip("/") or "/")
        if rank is None or key.rstrip("/") == home_key:
            continue
        scored[key] = min(rank, scored.get(key, 9))
    return [u for u, _ in sorted(scored.items(), key=lambda kv: (kv[1], kv[0]))][:limit]


def _fit_drop(pages: list[Page], require_any: list[str], exclude_any: list[str]) -> Drop | None:
    for term in exclude_any:
        for page in pages:
            for sentence in sentences(page.text):
                if term.lower() in sentence.lower():
                    return Drop("excluded_term", '"%s" on %s: "%s"' % (term, page.final_url, sentence[:200]))
    if require_any and not any(t.lower() in page.text.lower() for t in require_any for page in pages):
        return Drop("fit_not_shown", "none of %s appear on %d page(s)" % (", ".join(require_any), len(pages)))
    return None


def research(lead: dict[str, Any], fetcher: Fetcher, settings: dict[str, Any], today: dt.date | None = None
             ) -> Advance | Drop:
    """Read the lead's site and return Advance("verified", data) with every fact and its evidence, or Drop.
    Raises StepError when a transient failure stands between the lead and a decision."""
    site = lead["website"]
    host = host_of(site)
    try:
        home = fetcher.get(site)
    except OffsiteRedirect as e:
        return Drop("redirected_off_site", str(e))
    except Disallowed:
        return Drop("robots_disallowed", "robots.txt does not allow %s" % safe_url(site))
    except FetchError as e:
        raise StepError(str(e)) from None
    if not home.ok:
        return Drop("site_unavailable", "%s: %s" % (safe_url(site), home.error))
    if host_of(home.final_url) != host:
        return Drop("redirected_off_site", "%s redirects to %s" % (safe_url(site), safe_url(home.final_url)))
    pages, failed = [home], []
    for url in candidate_urls(home, host, int(settings["max_pages"]) - 1):
        try:
            page = fetcher.get(url)
        except Disallowed:
            continue
        except FetchError as e:
            failed.append(str(e))
            continue
        if page.ok and host_of(page.final_url) == host:
            pages.append(page)
    read = [p.final_url for p in pages]

    def unproven(reason: str, detail: str) -> Drop:
        if failed:
            raise StepError("could not read %s; deciding after a retry" % failed[0])
        return Drop(reason, detail, {"pages_read": read})

    drop = _fit_drop(pages, list(settings.get("require_any") or []), list(settings.get("exclude_any") or []))
    if drop:
        if drop.reason == "fit_not_shown":
            return unproven(drop.reason, drop.detail)
        return drop
    people = find_people(pages, list(settings["roles"]), lead.get("company", ""))
    if not people:
        return unproven("decision_maker_not_found",
                        "no eligible exact %s template on %d page(s)" % ("/".join(settings["roles"][:3]), len(pages)))
    person = people[0]
    email, rejected = find_email(pages, person, host, bool(settings.get("allow_free_mail", True)), people[1:])
    if email is None:
        why = "; ".join(rejected[:3]) or "no address published"
        return unproven("no_direct_email", "%s (%s): %s" % (person.full_name, person.role, why))
    openers = opener_facts(pages, today, company=lead.get("company", ""), market=lead.get("market", ""))
    if failed and not openers:
        raise StepError("could not read %s; deciding opener facts after a retry" % failed[0])
    if not openers:
        return unproven("no_opener_facts", "no complete assertion matches an opener template")
    facts = person.facts + [email] + openers
    for i, f in enumerate(facts, 1):
        f["id"] = "f%d" % i
    data = {
        "evidence_version": EVIDENCE_VERSION,
        "person": {"full_name": person.full_name, "first_name": person.first_name, "last_name": person.last_name,
                   "role": person.role, "fact_ids": [f["id"] for f in person.facts]},
        "email": {"address": email["value"]["email"], "fact_id": email["id"]},
        "facts": facts,
        "also_named": ["%s (%s)" % (p.full_name, p.role) for p in people[1:4]],
        "pages_read": read,
        "pages": [p.to_dict() for p in pages],
    }
    snapshots = [Page.from_dict(p) for p in data["pages"]]
    if not all(supported(f, snapshots, company=lead.get("company", ""), market=lead.get("market", ""),
                         roles=settings["roles"]) for f in facts):
        raise StepError("extracted facts do not match the stored page snapshots")
    return Advance("verified", data)

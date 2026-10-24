"""Verification: prove who decides at a business from the business's own website, or drop the lead.

The homepage and up to `max_pages - 1` of its about, team and contact pages are read. A decision-maker
is kept only with evidence: a sentence on the site that names the person with an owner-level role
("Dana Whitlock founded Harbor Ledger Co. in 2009"), a team card ("Dana Whitlock" / "Owner"), or a
schema.org founder or employee node. The email must be published on the site, on the business's domain
(or a free-mail address printed beside the person's name), and tied to that person. Facts for the
opener (founding year, years in business, specialty, awards) are collected the same way.

A page that fails to load is not proof of absence: when nothing provable was found and a page failed
transiently, the step raises StepError so the lead is retried instead of dropped.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .evidence import Page, schema_people, schema_quote, sentences
from .fetch import Disallowed, Fetcher, FetchError, host_of
from .names import canonical_first, split_name, tokens
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
# Capitalized words that show up next to role words in headings and menus but are never part of a name.
NOT_NAME_WORDS = {
    "about", "our", "team", "meet", "contact", "us", "the", "welcome", "home", "book", "call", "today", "free",
    "learn", "more", "read", "story", "stories", "blog", "news", "founder", "owner", "president", "principal",
    "message", "from", "note", "letter", "hours", "location", "locations", "privacy", "terms", "copyright",
    "follow", "get", "started", "request", "quote", "estimate", "schedule", "why", "choose", "what", "how",
    "who", "we", "services", "service", "pricing", "reviews", "testimonials", "gallery", "faq", "company",
    "group", "studio", "solutions", "partners", "associates", "consulting", "office", "street", "avenue",
    "road", "suite", "inc", "llc", "ltd", "co", "and", "of", "for", "with", "a", "an", "my", "your",
}

_WORD = r"(?:Mc|Mac|O['\u2019]|D['\u2019])?[A-Z][a-z]+(?:-[A-Z][a-z]+)?"
_NAME = rf"{_WORD}(?:\s+[A-Z]\.)?(?:\s+(?:de|del|da|di|van|von|der|le|la)\b)?(?:\s+{_WORD}){{1,2}}"
NOT_POSSESSIVE = r"(?!['\u2019]s\b)"  # "the founder's daughter" names someone else
VERB_ROLE = {"founded": "Founder", "co-founded": "Co-founder", "started": "Founder", "established": "Founder",
             "opened": "Founder", "launched": "Founder", "owns": "Owner", "owned": "Owner",
             "owned and operated": "Owner", "run": "Owner"}


@dataclass
class Person:
    full_name: str
    first_name: str
    last_name: str
    role: str
    rank: int
    facts: list[dict[str, Any]] = field(default_factory=list)


def _role_regex(roles: list[str]) -> str:
    alts = sorted({r.strip().lower() for r in roles if r.strip()}, key=len, reverse=True)
    parts = [re.escape(r).replace(r"\-", "-?").replace(r"\ ", r"\s+") for r in alts]
    return r"(?i:%s)" % "|".join(parts)


def _patterns(role: str) -> list[re.Pattern[str]]:
    n = _NAME
    return [
        re.compile(rf"(?P<name>{n}),\s+(?:(?i:the|our|and)\s+)?(?:(?i:proud|lead|head|sole)\s+)?(?P<role>{role})"
                   rf"\b{NOT_POSSESSIVE}"),
        re.compile(rf"\b(?P<role>{role})\s*(?:[:\-\u2013\u2014|]\s*|\s+)(?P<name>{n})(?=\s*(?:$|[.,;:!?)|]|\s+"
                   rf"(?i:is|has|and|started|founded|opened|runs|leads|brings)\b))"),
        re.compile(rf"(?P<name>{n})\s+(?i:is|serves as|works as|became)\s+(?:(?i:the|our|its)\s+)?"
                   rf"(?:(?i:proud|sole|current)\s+)?(?P<role>{role})\b{NOT_POSSESSIVE}"),
        re.compile(rf"(?P<verb>(?i:founded|started|established|opened|owned and operated|owned|run))\s+"
                   rf"(?:(?i:in)\s+\d{{4}}\s+)?(?i:by)\s+(?P<name>{n})"),
        re.compile(rf"(?P<name>{n})\s+(?P<verb>(?i:co-founded|founded|started|opened|owns|launched))\s"),
        re.compile(rf"(?i:I'm|I am|my name is)\s+(?P<name>{n}),?\s+(?:(?i:and\s+)?(?i:I'm|I am)\s+)?"
                   rf"(?:(?i:the|your)\s+)?(?P<role>{role})\b{NOT_POSSESSIVE}"),
    ]


def _crop(text: str, start: int, end: int, limit: int = 300) -> str:
    """A verbatim window of `text` around [start, end), at most `limit` characters, on word boundaries."""
    if len(text) <= limit:
        return text
    pad = max(0, (limit - (end - start)) // 2)
    a, b = max(0, start - pad), min(len(text), end + pad)
    while a > 0 and not text[a - 1].isspace():
        a += 1
    while b < len(text) and not text[b].isspace():
        b -= 1
    return text[a:b].strip()


def _clean_name(raw: str, company_tokens: set[str]) -> str | None:
    words = raw.replace("\u2019", "'").split()
    while len(words) > 2 and set(tokens(words[0])) & NOT_NAME_WORDS:  # "Meet Dana Whitlock" -> "Dana Whitlock"
        words.pop(0)
    while len(words) > 2 and tokens(words[-1]) and tokens(words[-1])[0] in company_tokens:
        words.pop()
    name_tokens = [t for w in words for t in tokens(w)]
    if len(words) < 2 or len(words) > 4 or not name_tokens:
        return None
    if any(t in NOT_NAME_WORDS for t in name_tokens):
        return None
    if set(name_tokens) <= company_tokens:
        return None
    return " ".join(words)


def _rank(role: str) -> int:
    key = " ".join(role.lower().split())
    return min((rank for word, rank in ROLE_RANK.items() if re.search(r"\b%s\b" % re.escape(word), key)), default=4)


def _fact(kind: str, value: dict[str, Any], page: Page, quote: str, method: str) -> dict[str, Any]:
    return {"kind": kind, "value": value, "source_url": page.final_url, "quote": quote, "method": method}


def _person_fact(name: str, role: str, page: Page, quote: str, method: str, role_from: str) -> dict[str, Any]:
    first, last = split_name(name)
    role = " ".join(role.split())
    role = role[:1].upper() + role[1:]
    value = {"full_name": name, "first_name": first, "last_name": last, "role": role, "role_from": role_from}
    return _fact("decision_maker", value, page, quote, method)


def find_people(pages: list[Page], roles: list[str], company: str) -> list[Person]:
    role_rx = _role_regex(roles)
    role_only = re.compile(rf"\b{role_rx}\b{NOT_POSSESSIVE}")
    patterns = _patterns(role_rx)
    name_line = re.compile(rf"^{_NAME}$")
    heading_role = re.compile(rf"^(?:(?i:meet)\s+)?(?:(?i:our|the|your)\s+)?(?P<role>{role_rx}):?$")
    company_tokens = set(tokens(company))
    found: list[dict[str, Any]] = []
    for page in pages:
        for prop, node in schema_people(page.jsonld):
            name = _clean_name(str(node["name"]), company_tokens)
            title = str(node.get("jobTitle") or "")
            role, role_from = (title, "jobTitle") if title else ({"founder": "Founder", "owner": "Owner"}.get(prop, ""),
                                                                 "property")
            if name and role and role_only.search(role):
                found.append(_person_fact(name, role, page, schema_quote(prop, node), "schema.org", role_from))
        for sentence in sentences(page.text):
            for rx in patterns:
                for m in rx.finditer(sentence):
                    name = _clean_name(m.group("name"), company_tokens)
                    if not name:
                        continue
                    groups = m.groupdict()
                    role = groups.get("role") or VERB_ROLE.get(" ".join((groups.get("verb") or "").lower().split()), "")
                    if role:
                        quote = _crop(sentence, m.start(), m.end())
                        found.append(_person_fact(name, role, page, quote, "text", "text"))
        lines = page.lines
        for i, line in enumerate(lines):
            if len(line.split()) > 8 or not role_only.search(line):
                continue
            role = role_only.search(line).group(0)
            pair = None
            if i > 0 and name_line.match(lines[i - 1]):
                pair = (lines[i - 1], "%s %s" % (lines[i - 1], line))
            elif i + 1 < len(lines) and heading_role.match(line) and name_line.match(lines[i + 1]):
                pair = (lines[i + 1], "%s %s" % (line, lines[i + 1]))
            if pair:
                name = _clean_name(pair[0], company_tokens)
                if name:
                    found.append(_person_fact(name, role, page, pair[1], "text", "text"))
    people: dict[tuple[str, str], Person] = {}
    for f in found:
        v = f["value"]
        key = (canonical_first(v["first_name"]), " ".join(tokens(v["last_name"])))
        p = people.get(key)
        rank = _rank(v["role"])
        if p is None:
            p = people[key] = Person(v["full_name"], v["first_name"], v["last_name"], v["role"], rank)
        elif rank < p.rank:
            p.role, p.rank = v["role"], rank
        if not any(x["quote"] == f["quote"] and x["source_url"] == f["source_url"] for x in p.facts):
            p.facts.append(f)
    for p in people.values():
        p.facts.sort(key=lambda f: f["method"] != "text")  # readable sentences first, schema.org after
    # Strongest role first; a sentence on the site beats a schema.org node alone; more sources beat fewer.
    ranked = sorted(people.values(), key=lambda p: (p.rank, all(f["method"] != "text" for f in p.facts),
                                                    -len({f["source_url"] for f in p.facts}), -len(p.facts)))
    return [p for p in ranked if p.rank < 4]


def local_parts(first: str, last: str) -> set[str]:
    f = "".join(tokens(first))
    last_tokens = tokens(last)
    la = "".join(last_tokens)
    if not f or not la:
        return set()
    out = {f, la, f + la, f + "." + la, f + "_" + la, f + "-" + la, f[0] + la, f[0] + "." + la, f + la[0],
           f + "." + la[0], la + f, la + "." + f}
    if len(last_tokens) > 1:
        out |= {f + last_tokens[-1], f + "." + last_tokens[-1], f[0] + last_tokens[-1]}
    return out


def on_site_domain(domain: str, host: str) -> bool:
    """The address is on the business's own domain: the site's host, a parent of it, or a subdomain."""
    if "." not in domain or domain in PUBLIC_SUFFIXES:
        return False
    return domain == host or host.endswith("." + domain) or domain.endswith("." + host)


def find_email(pages: list[Page], person: Person, host: str, allow_free_mail: bool = True,
               others: list[Person] | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    wanted = local_parts(person.first_name, person.last_name)
    theirs = {part: o.full_name for o in others or [] for part in local_parts(o.first_name, o.last_name)}
    first_rx = re.compile(r"\b%s\b" % re.escape(person.first_name), re.I)
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    rejected: list[str] = []
    for page in pages:
        for address, context, method in page.emails:
            local, _, domain = address.partition("@")
            base = local.split("+", 1)[0]
            matches_name = base in wanted
            beside = bool(first_rx.search(context))
            on_site = on_site_domain(domain, host)
            free = domain in FREE_MAIL
            if base in SHARED_INBOXES:
                rejected.append("%s is a shared inbox" % address)
            elif base in theirs and not matches_name:
                rejected.append("%s belongs to %s" % (address, theirs[base]))
            elif not on_site and not (free and allow_free_mail):
                rejected.append("%s is not on the business's domain" % address)
            elif free and not beside:
                rejected.append("%s is free mail not printed beside %s" % (address, person.full_name))
            elif not (matches_name or beside):
                rejected.append("%s is not tied to %s" % (address, person.full_name))
            else:
                binding = "address matches the name" if matches_name else "printed beside the name"
                if method == "mailto":
                    quote, how = "mailto:" + address, "mailto"
                else:
                    at = context.lower().find(address)
                    quote, how = _crop(context, max(0, at), max(0, at) + len(address)), "text"
                fact = _fact("email", {"email": address, "binding": binding}, page, quote, how)
                rank = (0 if on_site else 1, 0 if matches_name else 1)
                if best is None or rank < best[0]:
                    best = (rank, fact)
    return (best[1] if best else None), list(dict.fromkeys(rejected))


FACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # "founded Harbor Ledger Co. in 2009", "established in 1998", "opened the shop in 2012"
    ("founded", re.compile(r"\b(?i:founded|established|opened)(?:\s+(?:[A-Z][\w&.'-]*|the|our|its|this|"
                           r"(?i:company|firm|business|shop|practice))){0,5}\s+(?i:in)\s+((?:19|20)\d{2})\b")),
    # "in business since 2011", "serving Cedar Hollow since 2014", "opened our doors in 2012"
    ("founded", re.compile(r"\b(?i:opened (?:our|its|the) doors|in business since|owned since|operated since|"
                           r"serving [A-Za-z ,.'-]{2,40} since)\s+(?:(?i:in)\s+)?((?:19|20)\d{2})\b")),
    ("years", re.compile(r"\b(?:over |more than |nearly |almost )?(\d{1,2})\+?\s+years\s+(?:of\s+experience|"
                         r"in\s+business|in\s+the\s+(?:industry|trade|business)|serving)", re.I)),
    ("specialty", re.compile(r"\b(?:we|I)\s+speciali[sz]e\s+in\s+([^.!?;:]{3,90})", re.I)),
    ("specialty", re.compile(r"\bspeciali[sz]ing\s+in\s+([^.!?;:]{3,90})", re.I)),
    ("award", re.compile(r"\b(voted|named)\s+((?:the\s+)?best\b[^.!?;:]{3,70})", re.I)),
]


def _phrase(raw: str, limit: int = 60) -> str:
    text = re.split(r",\s+(?:and\s+)?(?:we|so|because|which)\b|\s+(?:so|because)\s+", raw.strip())[0]
    text = text.strip(" ,;:-")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0]
    return text.rstrip(" ,;:-")


def opener_facts(pages: list[Page], today: dt.date | None = None) -> list[dict[str, Any]]:
    year_now = (today or dt.date.today()).year
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        for sentence in sentences(page.text):
            for kind, rx in FACT_PATTERNS:
                m = rx.search(sentence)
                if not m:
                    continue
                raw = m.group(m.lastindex or 1)
                if kind == "founded" and not 1900 <= int(raw) <= year_now:
                    continue
                if kind == "years" and not 2 <= int(raw) <= 99:
                    continue
                value = {"founded": {"year": raw}, "years": {"years": raw}}.get(kind) or {kind: _phrase(raw)}
                if kind == "award":
                    value["verb"] = m.group(1).lower()
                key = (kind, str(sorted(value.items())))
                if key in seen or not all(value.values()):
                    continue
                seen.add(key)
                out.append(_fact(kind, value, page, _crop(sentence, m.start(), m.end()), "text"))
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
    except Disallowed:
        return Drop("robots_disallowed", "robots.txt does not allow %s" % site)
    except FetchError as e:
        raise StepError(str(e)) from None
    if not home.ok:
        return Drop("site_unavailable", "%s: %s" % (site, home.error))
    if host_of(home.final_url) != host:
        return Drop("redirected_off_site", "%s redirects to %s" % (site, home.final_url))
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
        return drop
    people = find_people(pages, list(settings["roles"]), lead.get("company", ""))
    if not people:
        return unproven("decision_maker_not_found",
                        "no named %s on %d page(s)" % ("/".join(settings["roles"][:3]), len(pages)))
    person = people[0]
    email, rejected = find_email(pages, person, host, bool(settings.get("allow_free_mail", True)), people[1:])
    if email is None:
        why = "; ".join(rejected[:3]) or "no address published"
        return unproven("no_direct_email", "%s (%s): %s" % (person.full_name, person.role, why))
    facts = person.facts + [email] + opener_facts(pages, today)
    for i, f in enumerate(facts, 1):
        f["id"] = "f%d" % i
    cited = {f["source_url"] for f in facts}
    data = {
        "person": {"full_name": person.full_name, "first_name": person.first_name, "last_name": person.last_name,
                   "role": person.role, "fact_ids": [f["id"] for f in person.facts]},
        "email": {"address": email["value"]["email"], "fact_id": email["id"]},
        "facts": facts,
        "also_named": ["%s (%s)" % (p.full_name, p.role) for p in people[1:4]],
        "pages_read": read,
        "pages": [p.to_dict() for p in pages if p.final_url in cited],
    }
    return Advance("verified", data)

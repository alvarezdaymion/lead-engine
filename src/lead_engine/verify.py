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

from .evidence import Page, normalize, schema_people, schema_quote, sentences, supported
from .fetch import Disallowed, Fetcher, FetchError, OffsiteRedirect, host_of
from .html import EMAIL_RE
from .http import safe_url
from .names import name_key, split_name, tokens
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
    "email", "write", "send", "reach",
    "road", "suite", "inc", "llc", "ltd", "co", "and", "of", "for", "with", "a", "an", "my", "your",
}

_WORD = r"(?:Mc|Mac|O['\u2019]|D['\u2019])?[A-Z][a-z]+(?:-[A-Z][a-z]+)?"
_NAME = rf"{_WORD}(?:\s+[A-Z]\.)?(?:\s+(?:de|del|da|di|van|von|der|le|la)\b)?(?:\s+{_WORD}){{1,2}}"
NOT_POSSESSIVE = r"(?!['\u2019]s\b)"  # "the founder's daughter" names someone else
VERB_ROLE = {"founded": "Founder", "co-founded": "Co-founder", "started": "Founder", "established": "Founder",
             "opened": "Founder", "launched": "Founder", "owns": "Owner", "owned": "Owner",
             "owned and operated": "Owner", "run": "Owner"}
NON_CURRENT = re.compile(r"\b(?:former|previous|retired|retiring|late|deceased|ex-owner|ex-president|"
                         r"stepped down|no longer|passed away|assistant|deputy|vice|acting|interim)\b", re.I)
UNCERTAIN = re.compile(r"\b(?:not|never|no|isn't|wasn't|isn’t|wasn’t|used to|on behalf|may|might)\b", re.I)


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


def _other_name(text: str, name: str, company: str = "") -> bool:
    allowed = {tuple(tokens(value)) for value in (name, company) if value}
    for match in re.finditer(_NAME, text):
        words = tokens(match.group())
        while words and words[0] in NOT_NAME_WORDS:
            words.pop(0)
        if len(words) > 1 and tuple(words) not in allowed:
            return True
    return False


def _identity_context(text: str, name: str, company: str, role_rx: str) -> bool:
    without_roles = re.sub(rf"\b{role_rx}\b", "", text)
    if (UNCERTAIN.search(text) or NON_CURRENT.search(text) or _other_name(without_roles, name, company)
            or re.search(r"\b(?:client|customer|supplier|testimonial|vendor|partner)\b", without_roles, re.I)):
        return False
    return True


def _section_allows(page: Page, scope: str, company: str, names: set[str] | None = None) -> bool:
    for content, heading in page.sections:
        if normalize(scope) not in normalize(content):
            continue
        for name in (names or set()) | {company}:
            if name:
                heading = re.sub(r"\b%s\b" % re.escape(name), "", heading, flags=re.I)
        if set(tokens(heading)) - NOT_NAME_WORDS - {"history", "leadership", "are", "do", "work"}:
            return False
    return True


def _affiliation(sentence: str, match: re.Match[str], company: str) -> bool:
    groups = match.groupdict()
    if groups.get("verb"):
        if match.start("name") < match.start("verb"):
            target = sentence[match.end():]
        else:
            return _self_subject(sentence[:match.start()], company)
    else:
        if not groups.get("role"):
            return False
        target = sentence[match.end("role"):]
        relation = re.match(r"\s+(?:of|at|for)\s+", target, re.I)
        if not relation:
            return True
        target = target[relation.end():]
    subjects = [company, "our firm", "the firm", "our company", "the company", "our business", "the business",
                "our shop", "the shop", "our practice", "the practice"]
    return any(subject and re.match(re.escape(subject) + r"(?!\w)", target, re.I) for subject in subjects)


def _excluded_people(pages: list[Page], facts: list[dict[str, Any]], negative: re.Pattern[str]) -> set[tuple[str, str]]:
    aliases = {f["value"]["full_name"]: name_key(f["value"]["first_name"], f["value"]["last_name"]) for f in facts}
    known = set(aliases.values())
    excluded = set()
    for page in pages:
        cards = [element for element in page.elements if "\n" in element and
                 re.fullmatch(_NAME, element.split("\n")[0])]
        for text in page.lines + cards:
            status = re.sub(r"\b(?:has|employs|hires)\s+(?:an?|the|his|her|their)\s+(?:assistant|deputy)\b",
                            "employs staff", text, flags=re.I)
            if not negative.search(status):
                continue
            for name, key in aliases.items():
                if re.search(r"\b%s\b" % re.escape(name), text, re.I):
                    excluded.add(key)
            for match in re.finditer(_NAME, text):
                name = _clean_name(match.group(), set())
                if name and (key := name_key(*split_name(name))) in known:
                    excluded.add(key)
    return excluded


def find_people(pages: list[Page], roles: list[str], company: str) -> list[Person]:
    role_rx = _role_regex(roles)
    verbs = "|".join(re.escape(v) for v in sorted(VERB_ROLE, key=len, reverse=True))
    negated = re.compile(rf"\b(?:not|never|no|isn['\u2019]t|wasn['\u2019]t)\s+"
                         rf"(?:(?:the|an?|our|your|its|current|actual)\s+)*(?:{role_rx}|{verbs})\b", re.I)
    negative = re.compile(NON_CURRENT.pattern + "|" + UNCERTAIN.pattern, re.I)

    def rejected_context(text: str) -> bool:
        return bool(NON_CURRENT.search(text) or negated.search(text))

    role_only = re.compile(rf"\b{role_rx}\b{NOT_POSSESSIVE}")
    patterns = _patterns(role_rx)
    name_line = re.compile(rf"^{_NAME}$")
    heading_role = re.compile(rf"^(?:(?i:meet)\s+)?(?:(?i:our|the|your)\s+)?(?P<role>{role_rx}):?$")
    company_tokens = set(tokens(company))
    found: list[dict[str, Any]] = []
    for page in pages:
        for prop, node in schema_people(page.jsonld, company):
            name = _clean_name(str(node["name"]), company_tokens)
            title = str(node.get("jobTitle") or "")
            role, role_from = (title, "jobTitle") if title else ({"founder": "Founder", "owner": "Owner"}.get(prop, ""),
                                                                 "property")
            if name and role and role_only.fullmatch(role) and not rejected_context(role):
                found.append(_person_fact(name, role, page, schema_quote(prop, node), "schema.org", role_from))
        for sentence in sentences(page.text):
            for rx in patterns:
                for m in rx.finditer(sentence):
                    name = _clean_name(m.group("name"), company_tokens)
                    if not name or not _identity_context(sentence, name, company, role_rx):
                        continue
                    groups = m.groupdict()
                    role = groups.get("role") or VERB_ROLE.get(" ".join((groups.get("verb") or "").lower().split()), "")
                    if (role and role_only.fullmatch(role) and _affiliation(sentence, m, company)
                            and _section_allows(page, sentence, company, {name})):
                        quote = _crop(sentence, m.start(), m.end())
                        found.append(_person_fact(name, role, page, quote, "text", "text"))
        for element in page.elements:
            lines = element.split("\n")
            if len(lines) != 2:
                continue
            for name, title in (lines, lines[::-1]):
                role = role_only.search(title)
                if (not role or not name_line.fullmatch(name) or not heading_role.fullmatch(title)
                        and not role_only.fullmatch(title)):
                    continue
                name = _clean_name(name, company_tokens)
                if (name and _identity_context(element, name, company, role_rx)
                        and _section_allows(page, element, company, {name})):
                    found.append(_person_fact(name, role.group(), page, " ".join(lines), "text", "text"))
    excluded = _excluded_people(pages, found, negative)
    people: dict[tuple[str, str], Person] = {}
    for f in found:
        if company:
            f["subject"] = company
        v = f["value"]
        key = name_key(v["first_name"], v["last_name"])
        if key in excluded:
            continue
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


def on_site_domain(domain: str, host: str) -> bool:
    """The address is on the business's own domain: the site's host, a parent of it, or a subdomain."""
    if "." not in domain or domain in PUBLIC_SUFFIXES:
        return False
    return domain == host or host.endswith("." + domain) or domain.endswith("." + host)


def _variants(person: Person) -> set[str]:
    return {person.full_name} | {f["value"]["full_name"] for f in person.facts}


def _contact_binding(context: str, address: str, person: Person, others: list[Person]) -> bool:
    text = EMAIL_RE.sub("", context)
    if UNCERTAIN.search(text) or NON_CURRENT.search(text):
        return False
    variants = _variants(person)
    # Full names disambiguate shared first names; an unproven nickname never supplies a binding.
    for name in variants:
        text = re.sub(r"\b%s\b" % re.escape(name), "", text, flags=re.I)
    if any(_clean_name(m.group(), set()) for m in re.finditer(_NAME, text)):
        return False
    firsts = {split_name(name)[0] for name in variants}
    other_firsts = {split_name(name)[0] for other in others for name in _variants(other)}
    if any(re.search(r"\b%s\b" % re.escape(name), text, re.I) for name in other_firsts):
        return False
    if re.search(r"\b(?:payroll|clerk|employee|staff|receptionist)\b", text, re.I):
        return False
    names = "|".join(re.escape(name) for name in sorted(variants | firsts, key=len, reverse=True))
    cue = r"(?:email|contact|reach|write to|ask|call or email)"
    connector = r"(?:directly|answers at|can be reached at|at)"
    target = re.escape(address)
    printed = address.lower() in context.lower()
    ending = target if printed else rf"(?:{cue})?"
    binding = re.compile(rf"(?:{cue}\s+)?(?:{names})(?:\s+{connector})?\s*[:,]?\s*"
                         rf"{ending}\s*[.!]?", re.I)
    scopes = sentences(context) if printed else [" ".join(context.split())]
    return any(binding.fullmatch(sentence) for sentence in scopes)


def find_email(pages: list[Page], person: Person, host: str, allow_free_mail: bool = True,
               others: list[Person] | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    occurrences: dict[str, list[tuple[Page, str, str]]] = {}
    for page in pages:
        for address, context, method in page.emails:
            occurrences.setdefault(address, []).append((page, context, method))
    rejected = []
    candidates = []
    for address, items in occurrences.items():
        local, _, domain = address.partition("@")
        on_site = on_site_domain(domain, host)
        if local.split("+", 1)[0] in SHARED_INBOXES:
            reason = "is a shared inbox"
        elif not on_site and not (domain in FREE_MAIL and allow_free_mail):
            reason = "is not on the business's domain"
        elif any(not _contact_binding(context, address, person, others or []) or not _section_allows(
                page, context, str(person.facts[0].get("subject") or "") if person.facts else "", _variants(person))
                 for page, context, _ in items):
            reason = "has ambiguous or conflicting contact evidence for %s" % person.full_name
        else:
            page, context, method = items[0]
            quote = "mailto:" + address if method == "mailto" else context
            fact = _fact("email", {"email": address, "binding": "printed beside the name"}, page, quote, method)
            candidates.append((not on_site, fact))
            continue
        rejected.append("%s %s" % (address, reason))
    candidates.sort(key=lambda item: item[0])
    return (candidates[0][1] if candidates else None), rejected


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


def _self_subject(text: str, company: str) -> bool:
    text = normalize(text).strip(" ,:")
    subjects = r"we|i|(?:our|the|this)\s+(?:company|firm|business|shop|practice|team|doors)"
    if company:
        subjects += "|" + re.escape(normalize(company))
    return bool(re.fullmatch(rf"(?:{subjects})(?:\s+(?:is|are|was|were|has|have|been|in))*", text))


def _business_fact(sentence: str, match: re.Match[str], kind: str, company: str) -> bool:
    prefix = sentence[:match.start()].strip()
    if UNCERTAIN.search(sentence) or NON_CURRENT.search(sentence):
        return False
    actor = ""
    founding = re.fullmatch(r"(?:founded|established|opened)\s*(.*?)\s+in\s+\d{4}", match.group(), re.I)
    if kind == "founded" and founding and founding.group(1).strip():
        target = founding.group(1).strip()
        if not _self_subject(target, company):
            return False
        if not (_self_subject(prefix, company) or re.fullmatch(_NAME, prefix)):
            return False
        actor = prefix
    elif not _self_subject(prefix, company) and not (kind == "specialty" and not prefix and
                                                    re.match(r"(?:we|I)\s+speciali[sz]e", match.group(), re.I)):
        return False
    subject_text = sentence if kind != "award" else sentence[:match.start()] + sentence[match.end():]
    if _other_name(subject_text, actor, company):
        return False
    return not re.search(r"\b(?:supplier|client|vendor|partner|competitor|neighbor|employer)\b", sentence, re.I)


def opener_facts(pages: list[Page], today: dt.date | None = None, *, company: str = "") -> list[dict[str, Any]]:
    year_now = (today or dt.date.today()).year
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        for sentence in sentences(page.text):
            for kind, rx in FACT_PATTERNS:
                m = rx.search(sentence)
                if (not m or not _business_fact(sentence, m, kind, company)
                        or not _section_allows(page, sentence, company)):
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
                fact = _fact(kind, value, page, _crop(sentence, m.start(), m.end()), "text")
                if company:
                    fact["subject"] = company
                out.append(fact)
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
                        "no named %s on %d page(s)" % ("/".join(settings["roles"][:3]), len(pages)))
    person = people[0]
    email, rejected = find_email(pages, person, host, bool(settings.get("allow_free_mail", True)), people[1:])
    if email is None:
        why = "; ".join(rejected[:3]) or "no address published"
        return unproven("no_direct_email", "%s (%s): %s" % (person.full_name, person.role, why))
    openers = opener_facts(pages, today, company=lead.get("company", ""))
    if failed and not openers:
        raise StepError("could not read %s; deciding opener facts after a retry" % failed[0])
    facts = person.facts + [email] + openers
    for i, f in enumerate(facts, 1):
        f["id"] = "f%d" % i
    data = {
        "evidence_version": 2,
        "person": {"full_name": person.full_name, "first_name": person.first_name, "last_name": person.last_name,
                   "role": person.role, "fact_ids": [f["id"] for f in person.facts]},
        "email": {"address": email["value"]["email"], "fact_id": email["id"]},
        "facts": facts,
        "also_named": ["%s (%s)" % (p.full_name, p.role) for p in people[1:4]],
        "pages_read": read,
        "pages": [p.to_dict() for p in pages],
    }
    snapshots = [Page.from_dict(p) for p in data["pages"]]
    if not all(supported(f, snapshots, company=lead.get("company", "")) for f in facts):
        raise StepError("extracted facts do not match the stored page snapshots")
    return Advance("verified", data)

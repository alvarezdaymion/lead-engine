"""Page snapshots and the facts that cite them.

A fact is kept only with the URL it came from and the exact evidence: a quote that appears verbatim in
that page's visible text, a schema.org node in its JSON-LD, or a mailto link in its markup.
`supported` re-checks a fact against the stored snapshot, so the gate can prove every kept fact again
without fetching anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from html import unescape
from typing import Any

EVIDENCE_VERSION = 3


def complete(data: dict[str, Any]) -> bool:
    pages = data.get("pages")
    read = data.get("pages_read")
    if (type(data.get("evidence_version")) is not int or data["evidence_version"] != EVIDENCE_VERSION
            or not isinstance(pages, list) or not pages or not isinstance(read, list) or not read
            or any(not isinstance(url, str) or not url for url in read)):
        return False
    for page in pages:
        if (not isinstance(page, dict) or page.get("ok") is not True
                or type(page.get("status")) is not int or not 200 <= page["status"] < 300
                or any(not isinstance(page.get(key), str) for key in ("url", "final_url", "text"))
                or not page["url"] or not page["final_url"]
                or any(not isinstance(page.get(key), list) for key in
                       ("elements", "emails", "sections", "quotations", "jsonld"))):
            return False
        if (any(not isinstance(value, str) for key in ("elements", "quotations") for value in page[key])
                or any(not isinstance(value, dict) for value in page["jsonld"])
                or any(not isinstance(value, (list, tuple)) or len(value) != size
                       or any(not isinstance(part, str) for part in value)
                       for key, size in (("emails", 3), ("sections", 2)) for value in page[key])):
            return False
    return set(read) == {p["final_url"] for p in pages}


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    ok: bool
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    emails: list[tuple[str, str, str]] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    truncated: bool = False
    error: str = ""
    elements: list[str] = field(default_factory=list)
    sections: list[tuple[str, str]] = field(default_factory=list)
    quotations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Page:
        return cls(**{**d, "links": [tuple(x) for x in d.get("links", [])],
                      "emails": [tuple(x) for x in d.get("emails", [])],
                      "sections": [tuple(x) for x in d.get("sections", [])]})

    @property
    def lines(self) -> list[str]:
        return [line for line in self.text.split("\n") if line]


def normalize(text: str) -> str:
    """Compare text the way a reader would: entities decoded, curly quotes and dashes folded, whitespace
    collapsed, case ignored."""
    text = unescape(text)
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " ")
    return " ".join(text.split()).casefold()


def quote_in(quote: str, haystack: str, min_len: int = 8) -> bool:
    needle = normalize(quote)
    return len(needle) >= min_len and needle in normalize(haystack)


def find_page(pages: list[Page], url: str) -> Page | None:
    key = url.rstrip("/")
    for p in pages:
        if key in (p.url.rstrip("/"), p.final_url.rstrip("/")):
            return p
    return None


def schema_people(jsonld: list[dict], company: str = "") -> list[tuple[str, dict]]:
    """Only explicit Person nodes with a direct worksFor affiliation name the prospect's people."""
    out = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            affiliation = value.get("worksFor")
            if (set(value) == {"@type", "name", "jobTitle", "worksFor"} and value["@type"] == "Person"
                    and isinstance(affiliation, dict) and set(affiliation) == {"name"}
                    and isinstance(affiliation["name"], str) and company
                    and normalize(affiliation["name"]) == normalize(company)):
                out.append(("person", value))
            for item in value.values():
                visit(item)

    visit(jsonld)
    return out


def schema_quote(prop: str, person: dict) -> str:
    keep = {k: person[k] for k in ("@type", "name", "jobTitle", "worksFor") if k in person}
    return "%s: %s" % (prop, json.dumps(keep, sort_keys=True))


def supported(fact: dict[str, Any], pages: list[Page], *, company: str = "", market: str = "",
              roles: list[str] | None = None) -> bool:
    """Whether a fact is still proven by the stored page it cites."""
    from .fetch import host_of
    from .html import EMAIL_RE
    from .verify import DEFAULT_ROLES, OPENER_KINDS, find_email, find_people, opener_facts

    page = find_page(pages, str(fact.get("source_url") or ""))
    if page is None or not page.ok:
        return False
    method = fact.get("method")
    kind = fact.get("kind")
    value = fact.get("value") or {}
    quote = str(fact.get("quote") or "")
    subject = str(fact.get("subject") or "")
    if company and subject and normalize(company) != normalize(subject):
        return False
    company = company or subject
    if not isinstance(value, dict) or not value or any(not isinstance(v, str) or not v for v in value.values()):
        return False
    if method not in ("text", "mailto", "schema.org"):
        return False
    if method == "text" and not quote_in(quote, page.title + "\n" + page.text):
        return False
    if kind == "decision_maker" and method in ("text", "schema.org"):
        role = value.get("role", "")
        if not role:
            return False
        return any(candidate["value"] == value and candidate["method"] == method
                   and normalize(candidate["quote"]) == normalize(quote)
                   for person in find_people(pages, roles if roles is not None else DEFAULT_ROLES + [role], company)
                   for candidate in person.facts
                   if candidate["source_url"] == page.final_url)
    if kind == "email" and method in ("text", "mailto"):
        address = value.get("email", "")
        if not EMAIL_RE.fullmatch(address):
            return False
        filtered = [replace(p, emails=[e for e in p.emails if e[0] == address]) for p in pages]
        for person in find_people(pages, roles if roles is not None else DEFAULT_ROLES, company):
            candidate, _ = find_email(filtered, person, host_of(page.final_url))
            if candidate and all(candidate[key] == fact.get(key) for key in ("value", "quote", "method", "source_url")):
                return True
        return False
    if kind in OPENER_KINDS and method == "text":
        return any(candidate["kind"] == kind and candidate["value"] == value
                   and normalize(candidate["quote"]) == normalize(quote)
                   for candidate in opener_facts([page], company=company, market=market))
    return False


_ABBREVIATIONS = ("Co", "Inc", "Ltd", "Mr", "Mrs", "Ms", "Dr", "St", "Jr", "Sr", "No")
_SENTENCE_END = re.compile(r"(?<=[.!?])" + "".join(r"(?<!\b%s\.)" % a for a in _ABBREVIATIONS)
                           + r"(?<!\b[A-Z]\.)\s+(?=[A-Z\"'(])")


def sentences(text: str) -> list[str]:
    """Split visible text into lines, then sentences, for pattern matching and quoting. Initials and
    common abbreviations ("Dana J. Whitlock", "Harbor Ledger Co.") do not end a sentence."""
    out = []
    for line in text.split("\n"):
        for part in _SENTENCE_END.split(line):
            part = part.strip()
            if part:
                out.append(part)
    return out

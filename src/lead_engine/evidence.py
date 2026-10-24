"""Page snapshots and the facts that cite them.

A fact is kept only with the URL it came from and the exact evidence: a quote that appears verbatim in
that page's visible text, a schema.org node in its JSON-LD, or a mailto link in its markup.
`supported` re-checks a fact against the stored snapshot, so the gate can prove every kept fact again
without fetching anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from html import unescape
from typing import Any


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Page:
        return cls(**{**d, "links": [tuple(x) for x in d.get("links", [])],
                      "emails": [tuple(x) for x in d.get("emails", [])]})

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


def schema_node_matches(node: dict, name: str, role: str = "") -> bool:
    if normalize(str(node.get("name") or "")) != normalize(name):
        return False
    return not role or normalize(str(node.get("jobTitle") or "")) == normalize(role)


def schema_people(jsonld: list[dict]) -> list[tuple[str, dict]]:
    """(property, person node) pairs: founders, employees and members of an organization, and
    standalone Person nodes."""
    out: list[tuple[str, dict]] = []
    for node in jsonld:
        for prop in ("founder", "founders", "employee", "employees", "member", "members", "owner"):
            value = node.get(prop)
            for person in value if isinstance(value, list) else [value]:
                if isinstance(person, dict) and person.get("name"):
                    out.append((prop.rstrip("s"), person))
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Person" in types and node.get("name"):
            out.append(("person", node))
    return out


def schema_quote(prop: str, person: dict) -> str:
    keep = {k: person[k] for k in ("@type", "name", "jobTitle") if k in person}
    return "%s: %s" % (prop, json.dumps(keep, sort_keys=True))


def supported(fact: dict[str, Any], pages: list[Page]) -> bool:
    """Whether a fact is still proven by the stored page it cites."""
    from .html import EMAIL_RE
    from .verify import find_people, opener_facts

    page = find_page(pages, str(fact.get("source_url") or ""))
    if page is None or not page.ok:
        return False
    method = fact.get("method")
    kind = fact.get("kind")
    value = fact.get("value") or {}
    quote = str(fact.get("quote") or "")
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
                   for person in find_people([page], [role], "") for candidate in person.facts)
    if kind == "email" and method in ("text", "mailto"):
        address = value.get("email", "")
        if not EMAIL_RE.fullmatch(address) or value.get("binding") not in (
                "address matches the name", "printed beside the name"):
            return False
        if method == "mailto":
            return quote == "mailto:" + address and any(e == address and m == "mailto" for e, _, m in page.emails)
        return address in {m.group(0).lower() for m in EMAIL_RE.finditer(quote)}
    if kind in ("founded", "years", "specialty", "award") and method == "text":
        passage = Page(page.url, page.final_url, page.status, True, text=quote)
        return any(candidate["kind"] == kind and candidate["value"] == value for candidate in opener_facts([passage]))
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

"""Turn an HTML page into what verification needs: visible text lines, links, published addresses
and schema.org JSON-LD. Stdlib only; malformed markup still yields whatever text it has."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote

from .http import join_url

SKIP_TAGS = {"script", "style", "head"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
EVIDENCE_TAGS = {"p", "li", "td", "th", "dt", "dd", "address"} | HEADING_TAGS
INLINE_TAGS = {"a", "strong", "b", "em", "i", "span", "small", "br"}
CONTAINER_TAGS = {"html", "body", "div", "section", "article", "main", "header", "footer", "nav",
                  "ul", "ol", "dl", "table", "thead", "tbody", "tfoot", "tr"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
             "source", "track", "wbr"}
EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")  # retina names ("@2x.png")


@dataclass
class Parsed:
    title: str = ""
    lines: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, anchor text)
    emails: list[tuple[str, str, str]] = field(default_factory=list)  # (address, context, method)
    jsonld: list[dict] = field(default_factory=list)
    elements: list[str] = field(default_factory=list)
    sections: list[tuple[str, list[tuple[str, bool]]]] = field(default_factory=list)
    quotations: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    rejected_contexts: list[list[tuple[str, str]]] = field(default_factory=list)
    complete: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class _Parser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.mailto: list[tuple[str, str, int | None]] = []
        # The empty tag is the document root; it never supplies evidence.
        self.blocks: list[tuple[str, int, int | None]] = [("", 0, None)]
        self.block_plain: list[bool] = [False]
        self.text_blocks: list[tuple[int | None, list[tuple[str, bool]], list[str]]] = []
        self._text_block: list[str] | None = None
        self._text_owner: int | None = None
        self._text_headings: list[tuple[str, bool]] = []
        self._blocks: list[int] = [0]
        self.headings: dict[int, tuple[str, int | None, bool]] = {}
        self.ld_blocks: list[str] = []
        self._skip = 0
        self._ld: list[str] | None = None
        self._in_title = False
        self._href: str | None = None
        self._mail: str | None = None
        self._anchor: list[str] = []

    def _owner(self) -> int | None:
        return next((i for i in reversed(self._blocks) if self.blocks[i][0] in CONTAINER_TAGS | EVIDENCE_TAGS), 0)

    def _append_text(self, text: str) -> None:
        self.parts.append(text)
        owner = self._owner()
        headings = [(text, plain) for text, _, plain in self.headings.values()]
        if self._text_block is None or owner != self._text_owner or headings != self._text_headings:
            self._text_block = []
            self._text_owner, self._text_headings = owner, headings
            self.text_blocks.append((owner, headings, self._text_block))
        self._text_block.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag not in INLINE_TAGS:
            for block in self._blocks:
                self.block_plain[block] = False
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._ld = []
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "body":  # an unclosed <head> must not hide the page
            self._skip = 0
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in HEADING_TAGS:
            self.headings = {n: value for n, value in self.headings.items() if n < int(tag[1])}
        if tag == "br":
            self._append_text("\n")
        elif tag not in INLINE_TAGS:
            self.parts.append("\n")
            if tag not in VOID_TAGS:
                plain = tag in EVIDENCE_TAGS and all(
                    self.blocks[i][0] in CONTAINER_TAGS | (EVIDENCE_TAGS - HEADING_TAGS) | {""} for i in self._blocks)
                self._blocks.append(len(self.blocks))
                self.blocks.append((tag, len(self.parts), None))
                self.block_plain.append(plain)
        if tag == "a":
            href = a.get("href", "").strip()
            self._anchor = []
            if href.lower().startswith("mailto:"):
                self._mail = unquote(href[7:].split("?", 1)[0]).strip()
            else:
                self._href = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld is not None:
            self.ld_blocks.append("".join(self._ld))
            self._ld = None
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "a":
            text = " ".join("".join(self._anchor).split())[:120]
            if self._mail:
                self.mailto.append((self._mail, text, self._owner()))
            elif self._href is not None:
                absolute = join_url(self.base_url, self._href)
                if absolute.startswith(("http://", "https://")):
                    self.links.append((absolute.split("#", 1)[0], text))
            self._href = self._mail = None
        if tag == "br":
            self._append_text("\n")
        elif tag not in INLINE_TAGS:
            for i in range(len(self._blocks) - 1, -1, -1):
                if self.blocks[self._blocks[i]][0] == tag:
                    if tag in HEADING_TAGS:
                        level = int(tag[1])
                        start = self.blocks[self._blocks[i]][1]
                        heading = " ".join("".join(self.parts[start:]).split())
                        self.headings = {n: value for n, value in self.headings.items() if n < level}
                        self.headings[level] = (heading, self._blocks[i - 1] if i else None,
                                                self.block_plain[self._blocks[i]])
                    self.headings = {n: value for n, value in self.headings.items()
                                     if value[1] not in self._blocks[i:]}
                    for index in self._blocks[i:]:
                        name, start, _ = self.blocks[index]
                        self.blocks[index] = (name, start, len(self.parts))
                        if index != self._blocks[i]:
                            self.block_plain[index] = False
                    del self._blocks[i:]
                    break
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ld is not None:
            self._ld.append(data)
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip:
            return
        self._append_text(data)
        if self._href is not None or self._mail is not None:
            self._anchor.append(data)


def clean_lines(raw: str) -> list[str]:
    out: list[str] = []
    for line in raw.splitlines():
        line = " ".join(line.split())
        if line and (not out or out[-1] != line):
            out.append(line)
    return out


def _flatten_ld(node: object, out: list[dict]) -> None:
    if isinstance(node, list):
        for item in node:
            _flatten_ld(item, out)
    elif isinstance(node, dict):
        out.append(node)
        for key in ("@graph", "mainEntity", "about"):
            if key in node:
                _flatten_ld(node[key], out)


def parse_html(body: str, base_url: str) -> Parsed:
    p = _Parser(base_url)
    complete = False
    try:
        p.feed(body)
        p.close()
        complete = True
    except Exception:  # malformed markup: keep what was parsed
        pass
    p.blocks[0] = ("", 0, len(p.parts))
    lines = clean_lines("".join(p.parts))
    elements = [" ".join("".join(p.parts[start:end]).split()) for _, start, end in p.blocks]
    scopes = [(block, heading, " ".join("".join(parts).split()))
              for block, heading, parts in p.text_blocks if "".join(parts).strip()]

    def eligible(block: int | None) -> bool:
        return block is not None and p.block_plain[block] and p.blocks[block][2] is not None

    accepted = [False] * len(p.parts)
    owners: list[int | None] = [None] * len(p.parts)
    for i, (_, start, end) in enumerate(p.blocks):
        if eligible(i):
            accepted[start:end] = [True] * (end - start)
            owners[start:end] = [i] * (end - start)
    rejected = [text for block, _, text in scopes if not eligible(block)]
    rejected_contexts = []
    for i, (_, start, end) in enumerate(p.blocks):
        if not eligible(i):
            text = " ".join("".join(part for j, part in enumerate(p.parts[start:end], start)
                                    if not accepted[j]).split())
            if text:
                rejected.append(text)
            # Also retain addresses divided by layout separators.
            compact = " ".join("".join(part for j, part in enumerate(p.parts[start:end], start)
                                       if not accepted[j] and part != "\n").split())
            if compact:
                rejected.append(compact)
            context = []
            cursor = start
            stop = end if end is not None else len(p.parts)
            while cursor < stop:
                owner = owners[cursor]
                if owner is None:
                    next_cursor = cursor + 1
                    while next_cursor < stop and owners[next_cursor] is None:
                        next_cursor += 1
                    context.append(("".join(p.parts[cursor:next_cursor]), "text"))
                    cursor = next_cursor
                else:
                    kind = "heading" if p.blocks[owner][0] in HEADING_TAGS else "element"
                    context.append((elements[owner], kind))
                    cursor = p.blocks[owner][2]
            if any(kind != "text" for _, kind in context):
                rejected_contexts.append(context)
    rejected = list(dict.fromkeys(rejected))

    emails: list[tuple[str, str, str]] = []
    for address, anchor, block in p.mailto:
        address = address.lower()
        if EMAIL_RE.fullmatch(address) and not address.endswith(FILE_SUFFIXES):
            context = " ".join(elements[block].split()) if block is not None else anchor
            emails.append((address, context, "mailto" if eligible(block) else "ineligible"))
    for block, _, text in scopes:
        for m in EMAIL_RE.finditer(text):
            address = m.group(0).lower().rstrip(".")
            if not address.endswith(FILE_SUFFIXES):
                emails.append((address, text, "text" if eligible(block) else "ineligible"))
    for text in rejected:
        for match in EMAIL_RE.finditer(text):
            address = match.group().lower().rstrip(".")
            if not address.endswith(FILE_SUFFIXES):
                occurrence = address, text, "ineligible"
                if occurrence not in emails:
                    emails.append(occurrence)
    jsonld: list[dict] = []
    for block in p.ld_blocks:
        for text in (block.strip(), unescape(block.strip())):  # some sites HTML-escape their JSON-LD
            try:
                _flatten_ld(json.loads(text or "null"), jsonld)
                break
            except ValueError:
                continue
    title = " ".join("".join(p.title_parts).split())
    return Parsed(title=title, lines=lines, links=p.links, emails=emails, jsonld=jsonld,
                  elements=list(dict.fromkeys(text for block, _, text in scopes
                                              if eligible(block) and p.blocks[block][0] not in HEADING_TAGS)),
                  sections=[(text, heading) for block, heading, text in scopes if heading and eligible(block)],
                  headings=[elements[i] for i, (tag, _, _) in enumerate(p.blocks)
                            if tag in HEADING_TAGS and eligible(i)],
                  rejected=rejected, rejected_contexts=rejected_contexts, complete=complete)

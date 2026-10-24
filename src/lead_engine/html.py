"""Turn an HTML page into what verification needs: visible text lines, links, published addresses
and schema.org JSON-LD. Stdlib only; malformed markup still yields whatever text it has."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "nav", "li", "ul", "ol", "h1", "h2", "h3", "h4",
    "h5", "h6", "br", "tr", "td", "th", "table", "main", "aside", "blockquote", "figcaption", "form",
    "label", "button", "dd", "dt", "address", "q", "cite",
}
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
    sections: list[tuple[str, str]] = field(default_factory=list)
    quotations: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def decode_cfemail(encoded: str) -> str:
    """Decode a Cloudflare-obfuscated address (hex string, first byte is the XOR key)."""
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except ValueError:
        return ""


class _Parser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.mailto: list[tuple[str, str, int | None]] = []
        self.blocks: list[tuple[str, int, int | None]] = []
        self.text_blocks: list[tuple[int | None, str, list[str]]] = []
        self._text_block: list[str] | None = None
        self._blocks: list[int] = []
        self.headings: dict[int, tuple[str, int | None]] = {}
        self.ld_blocks: list[str] = []
        self._skip = 0
        self._ld: list[str] | None = None
        self._in_title = False
        self._href: str | None = None
        self._mail: str | None = None
        self._anchor: list[str] = []

    def _append_text(self, text: str) -> None:
        self.parts.append(text)
        if self._text_block is None:
            self._text_block = []
            self.text_blocks.append((self._blocks[-1] if self._blocks else None,
                                     " / ".join(text for text, _ in self.headings.values()), self._text_block))
        self._text_block.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
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
        if tag in BLOCK_TAGS:
            if tag == "br":
                self._append_text("\n")
            else:
                self._text_block = None
                self.parts.append("\n")
                self._blocks.append(len(self.blocks))
                self.blocks.append((tag, len(self.parts), None))
        if a.get("data-cfemail"):
            decoded = decode_cfemail(a["data-cfemail"])
            if decoded:
                self._append_text(" %s " % decoded)
        if tag == "a":
            href = a.get("href", "").strip()
            self._anchor = []
            if href.lower().startswith("mailto:"):
                self._mail = unquote(href[7:].split("?", 1)[0]).strip()
            elif "/cdn-cgi/l/email-protection#" in href:
                self._mail = decode_cfemail(href.rsplit("#", 1)[1])
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
        if tag == "a":
            text = " ".join("".join(self._anchor).split())[:120]
            if self._mail:
                self.mailto.append((self._mail, text, self._blocks[-1] if self._blocks else None))
            elif self._href is not None:
                absolute = urljoin(self.base_url, self._href)
                if absolute.startswith(("http://", "https://")):
                    self.links.append((absolute.split("#", 1)[0], text))
            self._href = self._mail = None
        if tag == "br":
            self._append_text("\n")
        elif tag in BLOCK_TAGS:
            self._text_block = None
            for i in range(len(self._blocks) - 1, -1, -1):
                if self.blocks[self._blocks[i]][0] == tag:
                    if re.fullmatch(r"h[1-6]", tag):
                        level = int(tag[1])
                        start = self.blocks[self._blocks[i]][1]
                        heading = " ".join("".join(self.parts[start:]).split())
                        self.headings = {n: value for n, value in self.headings.items() if n < level}
                        self.headings[level] = (heading, self._blocks[i - 1] if i else None)
                    self.headings = {n: value for n, value in self.headings.items()
                                     if value[1] not in self._blocks[i:]}
                    for index in self._blocks[i:]:
                        name, start, _ = self.blocks[index]
                        self.blocks[index] = (name, start, len(self.parts))
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
    try:
        p.feed(body)
        p.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    lines = clean_lines("".join(p.parts))
    elements = [" ".join("".join(p.parts[start:end]).split()) for _, start, end in p.blocks]
    scopes = [(block, heading, " ".join("".join(parts).split()))
              for block, heading, parts in p.text_blocks if "".join(parts).strip()]
    emails: list[tuple[str, str, str]] = []
    for address, anchor, block in p.mailto:
        address = address.lower()
        if EMAIL_RE.fullmatch(address) and not address.endswith(FILE_SUFFIXES):
            context = " ".join(elements[block].split()) if block is not None else anchor
            emails.append((address, context, "mailto"))
    for block, _, text in scopes:
        for m in EMAIL_RE.finditer(text):
            address = m.group(0).lower().rstrip(".")
            if not address.endswith(FILE_SUFFIXES):
                context = elements[block] if block is not None else "".join(p.parts)
                emails.append((address, " ".join(context.split()), "text"))
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
                  elements=list(dict.fromkeys(text for _, _, text in scopes)),
                  sections=[(text, heading) for _, heading, text in scopes if heading],
                  quotations=[elements[i] for i, (tag, _, _) in enumerate(p.blocks) if tag in {"blockquote", "q"}])

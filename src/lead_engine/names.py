"""Person names: split a full name and decide whether two records name the same person.

Two records match when the first names agree (common nicknames count: Bob and Robert) and the last names
share a word, ignoring accents, apostrophes, periods and Jr/Sr/II-IV suffixes.
"""

from __future__ import annotations

import re
import unicodedata

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
NICKNAMES = {
    "al": "albert", "alex": "alexander", "andy": "andrew", "drew": "andrew", "barb": "barbara",
    "ben": "benjamin", "bill": "william", "billy": "william", "will": "william", "bob": "robert",
    "bobby": "robert", "rob": "robert", "robbie": "robert", "cathy": "catherine", "kate": "katherine",
    "katie": "katherine", "kathy": "katherine", "chris": "christopher", "dan": "daniel", "danny": "daniel",
    "dave": "david", "deb": "deborah", "debbie": "deborah", "ed": "edward", "eddie": "edward",
    "greg": "gregory", "jeff": "jeffrey", "jen": "jennifer", "jenny": "jennifer", "jim": "james",
    "jimmy": "james", "jamie": "james", "joe": "joseph", "joey": "joseph", "jon": "jonathan",
    "ken": "kenneth", "larry": "lawrence", "liz": "elizabeth", "beth": "elizabeth", "matt": "matthew",
    "meg": "margaret", "maggie": "margaret", "mike": "michael", "nick": "nicholas", "pat": "patrick",
    "pete": "peter", "rick": "richard", "rich": "richard", "ron": "ronald", "sam": "samuel",
    "steve": "steven", "sue": "susan", "tim": "timothy", "tom": "thomas", "tommy": "thomas",
    "tony": "anthony", "vicky": "victoria",
}


def tokens(value: object) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold()
    text = re.sub(r"['.]", "", text)
    return [t for t in re.split(r"[^a-z]+", text) if t]


def split_name(full: str) -> tuple[str, str]:
    """'Dana J. Whitlock' -> ('Dana', 'Whitlock'); 'Luis de Vega' -> ('Luis', 'de Vega')."""
    parts = [p for p in full.split() if p]
    while len(parts) > 2 and parts[-1].rstrip(".").casefold() in SUFFIXES:
        parts.pop()
    if len(parts) < 2:
        return (parts[0] if parts else "", "")
    first, rest = parts[0], parts[1:]
    rest = [p for p in rest if not re.fullmatch(r"[A-Z]\.?", p)] or rest  # drop middle initials
    if len(rest) >= 2 and rest[-2].islower():  # particles: de, van, von, da ...
        return first, " ".join(rest[-2:])
    return first, rest[-1]


def canonical_first(first: str) -> str:
    t = tokens(first)
    return NICKNAMES.get(t[0], t[0]) if t else ""


def name_key(first: str, last: str) -> tuple[str, str]:
    return canonical_first(first), " ".join(tokens(last))


def same_person(first_a: str, last_a: str, first_b: str, last_b: str) -> bool:
    fa, fb = canonical_first(first_a), canonical_first(first_b)
    la = set(tokens(last_a)) - SUFFIXES
    lb = set(tokens(last_b)) - SUFFIXES
    return bool(fa) and fa == fb and bool(la & lb)

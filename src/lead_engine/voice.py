"""Voice rules for openers, loaded from a TOML file the owner edits (`voice.toml`).

`check_opener` returns every reason an opener can't be used. Besides style (length, sentences, reading
level, banned phrases, no greeting, no em dash) it enforces the evidence rule: an opener must cite at
least one fact, and every number and capitalized name in it must appear in a cited quote, the company
name or the market. That catches invented specifics; it can't judge every claim, so a model writer is
also told to use only the cited facts.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from .evidence import normalize, sentences

EXAMPLE = Path(__file__).parent / "templates" / "voice.example.toml"
GREETING = re.compile(r"^\W*(?:hi|hey|hello|dear|greetings|good (?:morning|afternoon|evening)|yo)\b", re.I)
URL_OR_EMAIL = re.compile(r"https?://|www\.|\S+@\S+\.\w+")
NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20",
    "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80",
    "ninety": "90", "hundred": "100", "dozen": "12",
}
ALWAYS_OK = {"i", "i'm", "i've", "i'd", "i'll", "ok"}
NUMBER = re.compile(r"(?<!\w)[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?!\w)")


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(n.replace(",", "")) for n in NUMBER.findall(text)}


@dataclass
class Voice:
    max_characters: int = 200
    max_sentences: int = 2
    max_grade: float = 9.0
    ban_em_dash: bool = True
    mention_first_name: bool = False
    distinct_opening_words: int = 3
    distinct_window: int = 5
    banned_phrases: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    templates: dict[str, list[str]] = field(default_factory=dict)


def load_voice(path: Path | str) -> Voice:
    with Path(path).open("rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(Voice)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError("unknown keys in %s: %s" % (path, ", ".join(sorted(unknown))))
    voice = Voice(**raw)
    if voice.max_characters < 20 or voice.max_sentences < 1:
        raise ValueError("max_characters must be at least 20 and max_sentences at least 1")
    if not all(isinstance(v, list) for v in voice.templates.values()):
        raise ValueError("each [templates] entry must be a list of strings")
    return voice


def syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 1  # numbers and symbols read as one beat
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def grade(text: str) -> float:
    """Flesch-Kincaid grade level."""
    words = re.findall(r"[A-Za-z0-9'\u2019-]+", text)
    if not words:
        return 0.0
    count = max(1, len(sentences(text)))
    syl = sum(syllables(w) for w in words)
    return 0.39 * len(words) / count + 11.8 * syl / len(words) - 15.59


def opening(text: str, n: int) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9']+", text.lower())[:n])


def _without(text: str, phrase: str) -> str:
    return re.sub(re.escape(phrase), "Company", text, flags=re.I) if phrase else text


def check_opener(opener: str, *, voice: Voice, cited: list[dict[str, Any]], company: str, market: str = "",
                 first_name: str = "", recent: list[str] | None = None) -> list[str]:
    problems: list[str] = []
    text = " ".join(opener.split())
    if not text:
        return ["opener is empty"]
    if len(text) > voice.max_characters:
        problems.append("opener is %d characters, over the %d limit" % (len(text), voice.max_characters))
    if voice.ban_em_dash and ("\u2014" in text or " \u2013 " in text or " -- " in text):
        problems.append("opener contains an em dash")
    if GREETING.search(text):
        problems.append("opener starts with a greeting; the email template already has one")
    if "{" in text or "}" in text:
        problems.append("opener contains a template placeholder")
    if URL_OR_EMAIL.search(text):
        problems.append("opener contains a link or an address")
    lowered = text.casefold()
    for phrase in voice.banned_phrases:
        if phrase.casefold() in lowered:
            problems.append('opener uses a banned phrase: "%s"' % phrase)
    plain = _without(text, company)  # company names like "Harbor Ledger Co." must not count as sentence ends
    if first_name and not voice.mention_first_name and re.search(r"\b%s\b" % re.escape(first_name), plain, re.I):
        problems.append("opener uses the recipient's first name; the greeting already does")
    parts = sentences(plain)
    if len(parts) > voice.max_sentences:
        problems.append("opener has %d sentences, over the %d limit" % (len(parts), voice.max_sentences))
    level = grade(plain)
    if level > voice.max_grade:
        problems.append("reading level is grade %.1f, over the %.1f limit" % (level, voice.max_grade))
    problems += _evidence_problems(text, cited, company, market)
    n = voice.distinct_opening_words
    if n and recent:
        mine = opening(text, n)
        for prev in recent[: voice.distinct_window]:
            if len(mine) == n and opening(prev, n) == mine:
                problems.append('opener starts like a recent one: "%s"' % " ".join(mine))
                break
    return problems


def _evidence_problems(text: str, cited: list[dict[str, Any]], company: str, market: str) -> list[str]:
    if not cited:
        return ["opener cites no fact"]
    quotes = " ".join(normalize(str(f.get("quote") or "")) for f in cited)
    quote_words = set(re.findall(r"[a-z0-9']+", quotes))
    context = set(re.findall(r"[a-z0-9']+", normalize(company + " " + market)))
    problems = []
    numbers = _numbers(quotes + " " + company + " " + market)
    for num in NUMBER.findall(text):
        if Decimal(num.replace(",", "")) not in numbers:
            problems.append("the number %s is not in the cited evidence" % num)
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in NUMBER_WORDS and word not in quote_words and Decimal(NUMBER_WORDS[word]) not in numbers:
            problems.append('"%s" is not in the cited evidence' % word)
    for sentence in sentences(text):
        for word in re.findall(r"[A-Za-z][A-Za-z'\u2019-]*", sentence)[1:]:
            if not word[0].isupper():
                continue
            key = normalize(word).removesuffix("'s")
            known = context | quote_words
            if key in ALWAYS_OK or key in known or all(p in known for p in re.split(r"[-']", key) if p):
                continue
            problems.append('"%s" is not in the cited evidence' % word)
    return list(dict.fromkeys(problems))

"""Opener writers. Both return a Draft: the opener plus the ids of the facts it uses.

- TemplateWriter (default, no keys): fills the `[templates]` in voice.toml with a fact's values. Each fact
  kind (founded, years, specialty, award) has its own templates; the order rotates per lead so consecutive
  leads don't open the same way.
- ClaudeWriter (optional `llm` extra): asks a Claude model for one line from the cited facts only, as
  strict JSON, with one repair round when the draft fails the voice rules.

`write` runs a writer until a draft passes `voice.check_opener`. A lead with no usable fact is dropped;
a lead whose drafts all fail raises StepError, which sends it through the repair cycle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .outcome import Advance, Drop, SharedOutage, StepError
from .voice import Voice, check_opener

OPENER_KINDS = ("award", "founded", "years", "specialty")


@dataclass
class Draft:
    opener: str
    fact_ids: list[str]
    source: str


def city(market: str) -> str:
    return market.split(",")[0].strip()


def opener_facts(lead: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in lead["data"].get("facts", []) if f["kind"] in OPENER_KINDS]


class TemplateWriter:
    name = "template"

    def __init__(self, voice: Voice):
        self.voice = voice

    def drafts(self, lead: dict[str, Any]) -> list[Draft]:
        out: list[Draft] = []
        facts = sorted(opener_facts(lead), key=lambda f: OPENER_KINDS.index(f["kind"]))
        shift = int(lead.get("id") or 0)
        for fact in facts:
            templates = self.voice.templates.get(fact["kind"], [])
            for i in range(len(templates)):
                index = (i + shift) % len(templates)
                ctx = {"company": lead["company"], "city": city(lead.get("market", "")), **fact["value"]}
                ctx.update({k + "_cap": str(v)[:1].upper() + str(v)[1:] for k, v in fact["value"].items()})
                try:
                    text = templates[index].format(**ctx)
                except (KeyError, IndexError, ValueError):
                    continue
                out.append(Draft(text, [fact["id"]], "template:%s#%d" % (fact["kind"], index)))
        return out


SYSTEM = (
    "You write the first line of a cold email to the owner of a small business. The email template already "
    "greets them by first name, so write only what comes after the greeting: one or two short, plain "
    "sentences that react to one specific public fact about their business. Use only the facts you are "
    "given and cite the ids of the facts you used. Do not add numbers, names, places or claims the facts "
    "don't contain. No pitch, no greeting, no em dash, no links. Follow the voice rules exactly."
)
SCHEMA = {
    "type": "object",
    "properties": {"opener": {"type": "string"}, "fact_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["opener", "fact_ids"],
    "additionalProperties": False,
}


class ClaudeWriter:
    name = "claude"

    def __init__(self, settings: dict[str, Any], voice: Voice, client: Any = None):
        self.settings = settings
        self.voice = voice
        self.client = client
        self.calls = 0

    def _client(self) -> Any:
        if self.client is None:
            try:
                import anthropic
            except ImportError:
                raise SharedOutage("writer.mode is claude but the llm extra is missing: uv sync --extra llm") from None
            self.client = anthropic.Anthropic()
        return self.client

    def prompt(self, lead: dict[str, Any], recent: list[str], feedback: list[str]) -> dict[str, Any]:
        facts = [f for f in lead["data"].get("facts", []) if f["kind"] != "email"]
        v = self.voice
        return {
            "company": lead["company"],
            "market": lead.get("market", ""),
            "recipient_role": lead["data"]["person"]["role"],
            "facts": [{"id": f["id"], "kind": f["kind"], "quote": f["quote"], "source_url": f["source_url"]}
                      for f in facts],
            "voice_rules": {
                "max_characters": v.max_characters, "max_sentences": v.max_sentences,
                "max_reading_grade": v.max_grade, "banned_phrases": v.banned_phrases,
                "mention_first_name": v.mention_first_name,
                "do_not_start_like": recent[: v.distinct_window],
            },
            "voice_examples": v.examples,
            "problems_with_your_last_draft": feedback,
        }

    def draft(self, lead: dict[str, Any], recent: list[str], feedback: list[str] | None = None) -> Draft:
        s = self.settings
        kwargs: dict[str, Any] = {
            "model": s["model"], "max_tokens": int(s["max_tokens"]), "system": SYSTEM,
            "output_config": {"effort": s["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
            "messages": [{"role": "user", "content": json.dumps(self.prompt(lead, recent, feedback or []))}],
        }
        client = self._client()
        self.calls += 1
        try:
            if s.get("fallbacks"):
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                   **kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except Exception as e:
            if _is_api_error(e):  # rate limits, outages, bad keys: every lead would fail the same way
                raise SharedOutage("model call failed: %s" % e) from None
            raise
        if getattr(resp, "stop_reason", "") == "refusal":
            raise StepError("the model declined to write this opener")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        try:
            out = json.loads(text)
        except ValueError:
            raise StepError("the model did not return JSON") from None
        return Draft(" ".join(str(out.get("opener", "")).split()), [str(x) for x in out.get("fact_ids", [])],
                     "claude:%s" % s["model"])


def _is_api_error(e: Exception) -> bool:
    """Errors raised by the Anthropic SDK (rate limits, overload, connection, auth), without importing it."""
    return type(e).__module__.split(".")[0] == "anthropic"


def _check(draft: Draft, lead: dict[str, Any], voice: Voice, recent: list[str]) -> list[str]:
    facts = {f["id"]: f for f in lead["data"].get("facts", [])}
    unknown = [i for i in draft.fact_ids if i not in facts or facts[i]["kind"] == "email"]
    if unknown:
        return ["cites facts that don't exist or can't be used: %s" % ", ".join(unknown)]
    return check_opener(draft.opener, voice=voice, cited=[facts[i] for i in draft.fact_ids],
                        company=lead["company"], market=lead.get("market", ""),
                        first_name=lead["data"]["person"]["first_name"], recent=recent)


def write(lead: dict[str, Any], writer: Any, voice: Voice, recent: list[str]) -> Advance | Drop:
    data = dict(lead["data"])
    tried: list[str] = []
    if isinstance(writer, TemplateWriter):
        if not opener_facts(lead):
            return Drop("no_opener_facts", "no founding year, years in business, specialty or award on the site")
        drafts = writer.drafts(lead)
        if not drafts:
            kinds = sorted({f["kind"] for f in opener_facts(lead)})
            return Drop("no_template", "voice.toml has no usable template for: %s" % ", ".join(kinds))
        for d in drafts:
            problems = _check(d, lead, voice, recent)
            if not problems:
                return Advance("written", {**data, "opener": _opener(d, lead)})
            tried.append("%s: %s" % (d.source, problems[0]))
        raise StepError("no template passed the voice rules (%s)" % "; ".join(tried[:3]))
    feedback: list[str] = []
    for _ in range(2):  # one draft, one repair
        d = writer.draft(lead, recent, feedback)
        feedback = _check(d, lead, voice, recent)
        if not feedback:
            return Advance("written", {**data, "opener": _opener(d, lead)})
    raise StepError("the model's opener failed the voice rules twice: %s" % "; ".join(feedback[:3]))


def _opener(d: Draft, lead: dict[str, Any]) -> dict[str, Any]:
    facts = {f["id"]: f for f in lead["data"]["facts"]}
    first = facts[d.fact_ids[0]]
    return {"text": d.opener, "fact_ids": d.fact_ids, "writer": d.source,
            "source_url": first["source_url"], "quote": first["quote"]}

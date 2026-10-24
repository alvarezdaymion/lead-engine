"""Opener writers: templates from evidenced facts, and the optional model writer with a fake client."""

import json
from types import SimpleNamespace

import pytest

from lead_engine.outcome import Advance, Drop, SharedOutage, StepError
from lead_engine.voice import EXAMPLE, Voice, load_voice
from lead_engine.writer import ClaudeWriter, TemplateWriter, write

FACTS = [
    {"id": "f1", "kind": "decision_maker", "value": {"full_name": "Dana Whitlock"}, "source_url": "https://a.example.com/about",
     "quote": "Dana Whitlock founded Harbor Ledger Co. in 2009.", "method": "text"},
    {"id": "f2", "kind": "email", "value": {"email": "dana@example.com"}, "source_url": "https://a.example.com/contact",
     "quote": "Email Dana: dana@example.com", "method": "text"},
    {"id": "f3", "kind": "founded", "value": {"year": "2009"}, "source_url": "https://a.example.com/about",
     "quote": "Dana Whitlock founded Harbor Ledger Co. in 2009.", "method": "text"},
    {"id": "f4", "kind": "specialty", "value": {"specialty": "bookkeeping for restaurants"},
     "source_url": "https://a.example.com/about", "quote": "We specialize in bookkeeping for restaurants.",
     "method": "text"},
]


def lead(facts=FACTS, lead_id=1):
    return {"id": lead_id, "company": "Harbor Ledger Co.", "market": "Cedar Hollow, OR",
            "data": {"facts": facts, "person": {"first_name": "Dana", "last_name": "Whitlock", "role": "Owner"},
                     "email": {"address": "dana@example.com", "fact_id": "f2"}}}


def test_template_writer_writes_from_one_evidenced_fact():
    voice = load_voice(EXAMPLE)
    out = write(lead(), TemplateWriter(voice), voice, recent=[])
    assert isinstance(out, Advance) and out.stage == "written"
    op = out.data["opener"]
    assert op["fact_ids"] == ["f3"] and "2009" in op["text"]
    assert op["source_url"] == "https://a.example.com/about" and op["writer"].startswith("template:founded")


def test_templates_rotate_per_lead_so_openings_vary():
    voice = load_voice(EXAMPLE)
    a = write(lead(lead_id=1), TemplateWriter(voice), voice, recent=[]).data["opener"]["text"]
    b = write(lead(lead_id=2), TemplateWriter(voice), voice, recent=[]).data["opener"]["text"]
    assert a != b


def test_a_recent_opening_pushes_the_writer_to_another_template():
    voice = load_voice(EXAMPLE)
    first = write(lead(), TemplateWriter(voice), voice, recent=[]).data["opener"]["text"]
    second = write(lead(), TemplateWriter(voice), voice, recent=[first]).data["opener"]["text"]
    assert second != first


def test_no_usable_fact_is_a_drop_and_no_passing_draft_is_a_retry():
    voice = load_voice(EXAMPLE)
    assert write(lead(FACTS[:2]), TemplateWriter(voice), voice, []).reason == "no_opener_facts"
    strict = Voice(max_characters=20, templates=voice.templates)
    with pytest.raises(StepError, match="no template passed"):
        write(lead(), TemplateWriter(strict), strict, [])
    empty = Voice(templates={})
    assert isinstance(write(lead(), TemplateWriter(empty), empty, []), Drop)


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if reply == "refusal":
            return SimpleNamespace(stop_reason="refusal", content=[])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(reply))])


class FakeClient:
    def __init__(self, replies):
        self.messages = FakeMessages(replies)
        self.beta = SimpleNamespace(messages=self.messages)


SETTINGS = {"model": "claude-opus-5-5", "effort": "low", "fallbacks": True, "max_tokens": 2000}


def test_model_writer_sends_only_cited_facts_as_strict_json_and_passes():
    voice = load_voice(EXAMPLE)
    client = FakeClient([{"opener": "Since 2009 is a real run for a small firm in Cedar Hollow.", "fact_ids": ["f3"]}])
    out = write(lead(), ClaudeWriter(SETTINGS, voice, client), voice, recent=["Running a shop is hard."])
    assert out.data["opener"]["writer"] == "claude:claude-opus-5-5"
    call = client.messages.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema" and call["output_config"]["effort"] == "low"
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    sent = json.loads(call["messages"][0]["content"])
    assert {f["id"] for f in sent["facts"]} == {"f1", "f3", "f4"}  # the address is never sent
    assert sent["voice_rules"]["do_not_start_like"] == ["Running a shop is hard."]


def test_model_writer_gets_one_repair_round_with_the_problems():
    voice = load_voice(EXAMPLE)
    client = FakeClient([{"opener": "Since 2011 you have been busy.", "fact_ids": ["f3"]},
                         {"opener": "Since 2009 you have been busy.", "fact_ids": ["f3"]}])
    out = write(lead(), ClaudeWriter(SETTINGS, voice, client), voice, [])
    assert out.data["opener"]["text"] == "Since 2009 you have been busy."
    feedback = json.loads(client.messages.calls[1]["messages"][0]["content"])["problems_with_your_last_draft"]
    assert feedback == ["the number 2011 is not in the cited evidence"]


def test_model_writer_failures():
    voice = load_voice(EXAMPLE)
    bad = {"opener": "Since 2011 you have been busy.", "fact_ids": ["f3"]}
    with pytest.raises(StepError, match="failed the voice rules twice"):
        write(lead(), ClaudeWriter(SETTINGS, voice, FakeClient([bad, bad])), voice, [])
    with pytest.raises(StepError, match="declined"):
        write(lead(), ClaudeWriter(SETTINGS, voice, FakeClient(["refusal"])), voice, [])
    ghost = {"opener": "Since 2009 you have been busy.", "fact_ids": ["f9"]}
    with pytest.raises(StepError):
        write(lead(), ClaudeWriter(SETTINGS, voice, FakeClient([ghost, ghost])), voice, [])


def test_api_errors_are_a_shared_outage_not_a_lead_failure():
    RateLimitError = type("RateLimitError", (Exception,), {"__module__": "anthropic._exceptions"})
    voice = load_voice(EXAMPLE)
    writer = ClaudeWriter({**SETTINGS, "fallbacks": False}, voice, FakeClient([RateLimitError("429")]))
    with pytest.raises(SharedOutage, match="model call failed"):
        write(lead(), writer, voice, [])

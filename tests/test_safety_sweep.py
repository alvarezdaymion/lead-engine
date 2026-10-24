"""Untrusted failures and incomplete reads cannot become diagnostics or proof."""

import copy
import io
import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import Clock, add_site, make_engine
from test_placement_warmup import SEEDS, FakeClient, FakeImap
from test_visible_invalidation import BASE, SITE, process, replace_snapshot

from lead_engine import cli, config, placement
from lead_engine.backlog import requalify
from lead_engine.fetch import Fetcher, FetchError, Response, host_of
from lead_engine.gate import load_suppressions
from lead_engine.handoff import run_handoff
from lead_engine.http import Http, HttpError, safe_url
from lead_engine.instantly import InstantlyClient
from lead_engine.outcome import Drop, SharedOutage
from lead_engine.report import export_csv
from lead_engine.sources.csv_import import read_candidates
from lead_engine.store import Store
from lead_engine.voice import Voice, check_opener, load_voice
from lead_engine.writer import ClaudeWriter, Draft, write

MARKER = "synthetic-private-marker"
BAD_URL = "https://user:" + MARKER + "@shop.example.com\uff0fabout?token=" + MARKER


@pytest.mark.parametrize("tag", ["div", "section", "article", "p", "svg"])
@pytest.mark.parametrize("wrapper", ["", "<html>%s</html>", "<body>%s</body>"])
def test_document_root_scans_names_across_unaccepted_children(tmp_path, tag, wrapper):
    body = BASE + "Pat <%s>Example retired.</%s>" % (tag, tag)
    _, lead = process(tmp_path, wrapper % body if wrapper else body)
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


@pytest.mark.parametrize("mode", ["gate", "dry-run", "live"])
def test_document_root_replays_at_every_evidence_boundary(tmp_path, mode):
    engine, lead = process(tmp_path, stop=mode == "gate")
    replace_snapshot(lead, BASE + "Pat <p>Example retired.</p>")
    if mode == "gate":
        assert isinstance(engine.step(lead), Drop)
    else:
        engine.store.advance(lead["id"], "ready", lead["data"])
        calls = []
        client = SimpleNamespace(add_leads=lambda *args: calls.append(args))
        suppressions = tmp_path / "suppressions.csv"
        suppressions.write_text("email,domain\n")
        result = run_handoff(engine.store, {**engine.s["handoff"], "mode": mode},
                             suppressions=engine.suppressions, out_dir=tmp_path / "out", client=client,
                             campaign_id="campaign-1", suppression_file=suppressions)
        assert not calls and not result.get("would_add")
        assert engine.store.get(lead["id"])["stage"] == "dropped"


@pytest.mark.parametrize("extra", ["Email Lee Sample: pat@<div>example.com</div>",
                                 "Email Lee Sample: pat@<p>example.com</p>",
                                 "<p>Email Lee Sample: pat@</p><p>example.com</p>"])
def test_document_root_retains_split_address_occurrences(tmp_path, extra):
    _, lead = process(tmp_path, BASE + extra)
    assert (lead["stage"], lead["reason"]) == ("dropped", "no_direct_email")


def test_only_accepted_children_are_exempt_from_the_document_root(tmp_path):
    _, lead = process(tmp_path, BASE + "<p>Pat</p><p>Example retired.</p>")
    assert (lead["stage"], lead["reason"]) == ("dropped", "decision_maker_not_found")


def test_malformed_robots_rules_have_safe_retryable_errors():
    fetcher = Fetcher("test", transport=lambda url, *args: Response(
        200, {}, ("User-agent: *\nDisallow: " + BAD_URL).encode(), url))
    with pytest.raises(FetchError) as caught:
        fetcher.get(SITE)
    assert MARKER not in str(caught.value) and not fetcher._robots


def test_untrusted_content_type_is_not_persisted_as_an_error(tmp_path):
    def transport(url, *args):
        return Response(404, {}, b"", url) if url.endswith("robots.txt") else Response(
            200, {"content-type": MARKER}, b"", url)

    engine = make_engine(tmp_path, Clock(), transport=transport)
    add_site(engine.store, "shop.example.com", "Shop")
    result = engine.tick(discover=False, handoff=False)
    assert MARKER not in json.dumps(result) + json.dumps(engine.store.events())


def test_old_scans_cannot_bypass_the_document_root_check(tmp_path):
    engine, lead = process(tmp_path, stop=True)
    lead["data"]["pages"][0]["text_scan_version"] = 1
    result = engine.step(lead)
    assert isinstance(result, Drop) and result.reason == "incomplete_evidence"


@pytest.mark.parametrize("where", ["initial", "redirect", "response-url"])
def test_url_parse_failures_never_reach_persisted_diagnostics(tmp_path, where):
    def transport(url, *args):
        if url.endswith("robots.txt"):
            return Response(404, {}, b"", url)
        return Response(302, {"location": BAD_URL}, b"", BAD_URL if where == "response-url" else url)

    engine = make_engine(tmp_path, Clock(), transport=transport)
    ident = add_site(engine.store, "shop.example.com", "Shop")
    if where == "initial":
        engine.store.db.execute("UPDATE leads SET website=? WHERE id=?", (BAD_URL, ident))
        engine.store.db.commit()
    result = engine.tick(discover=False, handoff=False)
    assert MARKER not in json.dumps(result) + json.dumps(engine.store.events())
    assert MARKER not in engine.store.get(ident)["last_error"]
    assert engine.store.get(ident)["attempts"] == 1


def test_url_helpers_are_total_and_never_return_malformed_credentials():
    assert safe_url(BAD_URL) == "<invalid url>"
    assert host_of(BAD_URL) == ""
    assert safe_url("https://user:secret@shop.example.com:bad/?token=secret") == "<invalid url>"


def test_api_url_parse_failure_has_a_fixed_safe_error():
    with pytest.raises(HttpError) as caught:
        Http(transport=lambda *args: (200, b"{}" )).get(BAD_URL)
    assert MARKER not in str(caught.value)
    assert "<invalid url>" in str(caught.value)


@pytest.mark.parametrize("length", [1, 27, "invalid", -1])
def test_short_or_malformed_html_response_retries_without_snapshots(tmp_path, length):
    def transport(url, *args):
        if url.endswith("robots.txt"):
            return Response(404, {}, b"", url)
        declared = len(BASE.encode()) + length if isinstance(length, int) and length > 0 else length
        return Response(200, {"content-type": "text/html", "Content-Length": str(declared)}, BASE.encode(), url)

    engine = make_engine(tmp_path, Clock(), transport=transport)
    ident = add_site(engine.store, "shop.example.com", "Shop")
    engine.tick(discover=False, handoff=False)
    lead = engine.store.get(ident)
    assert (lead["stage"], lead["attempts"]) == ("discovered", 1)
    assert not lead["data"].get("pages")


@pytest.mark.parametrize("status", [200, 302, 404])
def test_all_site_response_types_require_complete_bodies(status):
    calls = []

    def transport(url, *args):
        calls.append(url)
        if url.endswith("robots.txt"):
            return Response(404, {}, b"", url)
        return Response(status, {"content-length": "10", "location": "/next"}, b"", url)

    with pytest.raises(FetchError):
        Fetcher("test", transport=transport, per_host_seconds=0).get(SITE)
    assert calls == [SITE + "robots.txt", SITE]


@pytest.mark.parametrize("status", [200, 400])
def test_native_api_reads_check_length_on_success_and_error(monkeypatch, status):
    headers = Message()
    headers["Content-Length"] = "20"

    class Reply(io.BytesIO):
        def __init__(self):
            super().__init__(b"{}")
            self.status, self.headers = status, headers

    def open_(*args, **kwargs):
        if status >= 400:
            raise urllib.error.HTTPError(SITE, status, MARKER, headers, Reply())
        return Reply()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_))
    with pytest.raises(HttpError) as caught:
        Http().get(SITE)
    assert caught.value.transient
    assert MARKER not in str(caught.value)


@pytest.mark.parametrize("phase", ["lead", "discovery", "handoff"])
def test_unexpected_external_errors_never_enter_engine_state(tmp_path, phase):
    def fail(*args):
        raise OSError(MARKER)

    engine = make_engine(tmp_path, Clock(), discover=fail if phase == "discovery" else None,
                         handoff=fail if phase == "handoff" else None)
    if phase == "lead":
        engine.step = fail
        add_site(engine.store, "shop.example.com", "Shop")
    result = engine.tick()
    assert MARKER not in json.dumps(result) + json.dumps(engine.store.events())
    assert MARKER not in json.dumps([dict(row) for row in engine.store.db.execute("SELECT * FROM runs")])


@pytest.mark.parametrize("failure", ["file", "toml", "config-key", "voice-key"])
def test_cli_file_and_configuration_errors_hide_untrusted_text(tmp_path, capsys, failure):
    path = tmp_path / (MARKER + ".toml")
    if failure == "toml":
        path.write_text("[" + MARKER)
    elif failure == "config-key":
        path.write_text("[" + MARKER + "]\nx = 1\n")
    elif failure == "voice-key":
        path.write_text("[writer]\nvoice_file = 'voice.toml'\n")
        (tmp_path / "voice.toml").write_text(MARKER + " = true\n")
    assert cli.main(["check", "--config", str(path)]) == 2
    assert MARKER not in capsys.readouterr().err


@pytest.mark.parametrize("loader", [config.load, load_voice, read_candidates, load_suppressions, Store])
def test_direct_file_loaders_hide_native_file_errors(tmp_path, loader):
    target = tmp_path / MARKER
    target.mkdir()
    with pytest.raises(Exception) as caught:
        loader(target)
    assert MARKER not in str(caught.value)


def test_backlog_sanitizes_unexpected_read_errors(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, Clock())
    ident = add_site(engine.store, "shop.example.com", "Shop", source="backlog-import")

    def fail(*args):
        raise ValueError(MARKER)

    monkeypatch.setattr("lead_engine.backlog.research", fail)
    result = requalify(engine)
    assert result["leads"][0]["result"] == "inconclusive"
    assert MARKER not in json.dumps(result) + json.dumps(engine.store.events())
    assert engine.store.get(ident)["stage"] == "discovered"


def test_non_sdk_exceptions_from_sdk_calls_are_sanitized_shared_outages(tmp_path):
    engine, lead = process(tmp_path, stop=True)

    def fail(**kwargs):
        raise RuntimeError(MARKER)

    settings = {**engine.s["writer"], "fallbacks": False}
    writer = ClaudeWriter(settings, engine.voice, client=SimpleNamespace(messages=SimpleNamespace(create=fail)))
    with pytest.raises(SharedOutage) as caught:
        writer.draft(lead, [])
    assert MARKER not in str(caught.value) and "RuntimeError" in str(caught.value)


def test_model_rejection_diagnostics_do_not_echo_arbitrary_fact_ids(tmp_path):
    engine, lead = process(tmp_path, stop=True)
    writer = SimpleNamespace(draft=lambda *args: Draft("plain text", [MARKER], "model"))
    with pytest.raises(Exception) as caught:
        write(lead, writer, engine.voice, [])
    assert MARKER not in str(caught.value)


def test_voice_feedback_never_echoes_untrusted_fragments():
    voice = Voice(max_grade=100, banned_phrases=[MARKER])
    problems = check_opener("We like " + MARKER + " OtherPrivateWord 9234.", voice=voice,
                            cited=[{"quote": "Our firm was founded in 2012."}], company="Shop")
    assert problems
    assert all(token not in str(problems) for token in [MARKER, "OtherPrivateWord", "9234"])


@pytest.mark.parametrize("phase", ["connect", "select", "search", "fetch", "logout"])
def test_imap_failures_are_safe_and_never_complete_a_read(phase):
    run = {"token": "seed-1", "seeds": [SEEDS[0].address], "sends": [{"sender": "a@example.com"}]}
    original = copy.deepcopy(run)
    box = FakeImap({"INBOX": [("a@example.com", "seed-1")], "[Gmail]/Spam": []})

    def fail(*args, **kwargs):
        raise RuntimeError(MARKER)

    if phase != "connect":
        setattr(box, phase, fail)
    with pytest.raises(Exception) as caught:
        placement.read(run, SEEDS[:1], connect=fail if phase == "connect" else lambda _: box)
    assert MARKER not in str(caught.value)
    assert run == original


@pytest.mark.parametrize("phase", ["select", "search", "fetch"])
def test_incomplete_imap_results_are_failures_instead_of_placement_evidence(phase):
    run = {"token": "seed-1", "seeds": [SEEDS[0].address], "sends": [{"sender": "a@example.com"}]}
    box = FakeImap({"INBOX": [("a@example.com", "seed-1")], "[Gmail]/Spam": []})
    setattr(box, phase, lambda *args, **kwargs: ("NO", [MARKER.encode()]))
    with pytest.raises(Exception) as caught:
        placement.read(run, SEEDS[:1], connect=lambda _: box)
    assert MARKER not in str(caught.value) and not run.get("read")


def test_account_pagination_cannot_silently_return_an_incomplete_list(monkeypatch):
    monkeypatch.setattr("lead_engine.instantly.MAX_PAGES", 1)
    client = InstantlyClient("fake-key", transport=lambda *args: (200, b'{"items":[],"next_starting_after":"more"}'))
    with pytest.raises(HttpError):
        client.accounts()


def test_failed_seed_journal_write_is_safe_and_never_sends(tmp_path, monkeypatch):
    client = FakeClient()

    def fail(*args, **kwargs):
        raise OSError(MARKER)

    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(Exception) as caught:
        placement.send(client, ["a@example.com"], SEEDS, "Test", "body", dry_run=False, out_dir=tmp_path)
    assert MARKER not in str(caught.value) and not client.sent


def test_export_file_errors_do_not_expose_the_destination(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, Clock())

    def fail(*args, **kwargs):
        raise OSError(MARKER)

    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(Exception) as caught:
        export_csv(engine.store, tmp_path / "ready.csv")
    assert MARKER not in str(caught.value)


def test_unexpected_handoff_failure_is_safe_and_never_repeated(tmp_path):
    engine, lead = process(tmp_path)
    calls = []

    def fail(*args):
        calls.append(args)
        raise RuntimeError(MARKER)

    path = tmp_path / "suppressions.csv"
    path.write_text("email,domain\n")
    for _ in range(2):
        result = run_handoff(engine.store, {**engine.s["handoff"], "mode": "live"},
                             suppressions=engine.suppressions, out_dir=tmp_path / "out",
                             campaign_id="campaign-1", suppression_file=path,
                             client=SimpleNamespace(add_leads=fail))
        assert MARKER not in json.dumps(result) + json.dumps(engine.store.events())
    assert len(calls) == 1 and engine.store.get(lead["id"])["stage"] == "ready"
    assert engine.store.unresolved_handoffs() == 1


def test_imap_short_literal_is_not_a_complete_header():
    run = {"token": "seed-1", "seeds": [SEEDS[0].address], "sends": [{"sender": "a@example.com"}]}
    box = FakeImap({"INBOX": [("a@example.com", "seed-1")], "[Gmail]/Spam": []})
    box.fetch = lambda *args: ("OK", [(b"1 (BODY[HEADER.FIELDS (FROM)] {200}", b"From: a@example.com\r\n\r\n")])
    with pytest.raises(RuntimeError):
        placement.read(run, SEEDS[:1], connect=lambda _: box)
    assert not run.get("read")


@pytest.mark.parametrize("where", ["page", "robots", "api"])
@pytest.mark.parametrize("status,extra", [(206, {}), (200, {"Content-Range": "bytes 0-10/200"})])
def test_partial_http_status_or_range_never_supplies_complete_data(monkeypatch, where, status, extra):
    if where == "api":
        class Reply(io.BytesIO):
            def __init__(self):
                super().__init__(b"{}")
                self.status, self.headers = status, extra

        monkeypatch.setattr(urllib.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *a, **k: Reply()))
        with pytest.raises(HttpError) as caught:
            Http().get(SITE)
        assert caught.value.transient
        return
    calls = []

    def transport(url, *args):
        calls.append(url)
        if where == "page" and url.endswith("robots.txt"):
            return Response(404, {}, b"", url)
        return Response(status, {"content-type": "text/html", **extra}, BASE.encode(), url)

    with pytest.raises(FetchError):
        Fetcher("test", transport=transport, per_host_seconds=0).get(SITE)
    if where == "robots":
        assert calls == [SITE + "robots.txt"]


@pytest.mark.parametrize("mode", ["gate", "dry-run", "live"])
def test_snapshots_require_an_explicit_validated_http_completeness_flag(tmp_path, mode):
    engine, lead = process(tmp_path, stop=mode == "gate")
    lead["data"]["pages"][0].pop("http_complete", None)
    if mode == "gate":
        result = engine.step(lead)
        assert isinstance(result, Drop) and result.reason == "incomplete_evidence"
    else:
        engine.store.advance(lead["id"], "ready", lead["data"])
        path = tmp_path / "suppressions.csv"
        path.write_text("email,domain\n")
        calls = []
        run_handoff(engine.store, {**engine.s["handoff"], "mode": mode}, suppressions=engine.suppressions,
                    out_dir=tmp_path / "out", client=SimpleNamespace(add_leads=lambda *args: calls.append(args)),
                    campaign_id="campaign-1", suppression_file=path)
        assert not calls and engine.store.get(lead["id"])["reason"] == "incomplete_evidence"


@pytest.mark.parametrize("where", ["store", "demo"])
def test_file_creation_failures_are_safe_before_state_exists(tmp_path, monkeypatch, where):
    from lead_engine.demo import run

    def fail(*args, **kwargs):
        raise OSError(MARKER)

    monkeypatch.setattr(Path, "mkdir", fail)
    with pytest.raises(Exception) as caught:
        Store(tmp_path / "new" / "leads.db") if where == "store" else run(tmp_path / "demo")
    assert MARKER not in str(caught.value)


@pytest.mark.parametrize("field", ["stop_reason", "content"])
def test_sdk_response_access_errors_do_not_echo_provider_text(tmp_path, field):
    engine, lead = process(tmp_path, stop=True)

    class Reply:
        @property
        def stop_reason(self):
            if field == "stop_reason":
                raise ValueError(MARKER)
            return "end_turn"

        @property
        def content(self):
            raise ValueError(MARKER)

    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: Reply()))
    writer = ClaudeWriter({**engine.s["writer"], "fallbacks": False}, engine.voice, client=client)
    with pytest.raises(Exception) as caught:
        writer.draft(lead, [])
    assert MARKER not in str(caught.value)


@pytest.mark.parametrize("fit", ["excluded", "required"])
def test_fit_diagnostics_do_not_echo_configured_terms_or_page_text(tmp_path, fit):
    engine, lead = process(tmp_path, BASE + "<p>franchise " + MARKER + "</p>", stop=True)
    lead["stage"] = "discovered"
    if fit == "excluded":
        engine.s["verify"]["exclude_any"] = ["franchise"]
    else:
        engine.s["verify"]["require_any"] = [MARKER + " absent"]
    result = engine.step(lead)
    assert isinstance(result, Drop)
    assert MARKER not in result.detail


@pytest.mark.parametrize("reason", ["max_tokens", "pause_turn", None])
def test_incomplete_model_response_cannot_supply_a_draft(tmp_path, reason):
    engine, lead = process(tmp_path, stop=True)
    reply = SimpleNamespace(stop_reason=reason, content=[SimpleNamespace(type="text", text=json.dumps(
        {"opener": lead["data"]["opener"]["text"], "fact_ids": lead["data"]["opener"]["fact_ids"]}))])
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: reply))
    writer = ClaudeWriter({**engine.s["writer"], "fallbacks": False}, engine.voice, client=client)
    with pytest.raises(Exception) as caught:
        writer.draft(lead, [])
    assert "incomplete" in str(caught.value)


def test_exception_metadata_failures_cannot_bypass_diagnostic_sanitizing():
    from lead_engine.errors import diagnostic

    class Failure(RuntimeError):
        @property
        def status_code(self):
            raise ValueError(MARKER)

    assert diagnostic(Failure(MARKER)) == "Failure"


def test_missing_voice_file_diagnostic_does_not_echo_the_configured_path(tmp_path, capsys):
    path = tmp_path / "settings.toml"
    path.write_text("[writer]\nvoice_file = '" + MARKER + "'\n")
    assert cli.main(["check", "--config", str(path)]) == 1
    output = capsys.readouterr()
    assert MARKER not in output.out + output.err

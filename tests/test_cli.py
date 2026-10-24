"""The command line, end to end, with the network replaced by the demo's local sites."""

import json

import pytest

from lead_engine import cli
from lead_engine.demo import DATA, SiteTransport
from lead_engine.fetch import Fetcher
from lead_engine.http import HttpError


def test_demo_runs_end_to_end_without_keys_or_network(tmp_path, capsys, monkeypatch):
    for var in ("GOOGLE_PLACES_API_KEY", "INSTANTLY_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["demo", "--dir", str(tmp_path / "demo")]) == 0
    out = capsys.readouterr().out
    for expected in ["discovered 7 businesses in 4 search(es) (skipped likely_chain 3)",
                     "#1 Harbor Ledger Co.", "CRASH", "resumed", "repaired after 2 failed attempt(s)",
                     "decision_maker_not_found", "no_direct_email", "suppressed", "retired",
                     "recovered", "changed", "inconclusive", "Totals: 5 ready, 4 dropped, 1 retired, 10 leads in all."]:
        assert expected in out, expected
    assert (tmp_path / "demo" / "out" / "ready.csv").read_text().count("\n") == 6
    assert cli.main(["status", "--config", str(tmp_path / "demo" / "lead-engine.toml")]) == 0
    assert "ready 5" in capsys.readouterr().out


@pytest.mark.parametrize("reset", [False, True])
def test_demo_refuses_unmarked_user_directories(tmp_path, reset):
    directory = tmp_path / "user-data"
    (directory / "state").mkdir(parents=True)
    saved = directory / "state" / "keep.txt"
    saved.write_text("keep")
    config = directory / "lead-engine.toml"
    config.write_text("# keep\n")
    command = ["demo", "--dir", str(directory)] + (["--reset"] if reset else [])
    assert cli.main(command) == 2
    assert saved.read_text() == "keep" and config.read_text() == "# keep\n"


def test_demo_reset_requires_marker_and_explicit_flag(tmp_path, capsys):
    directory = tmp_path / "demo"
    assert cli.main(["demo", "--dir", str(directory)]) == 0
    counts = (directory / "out" / "ready.csv").read_text()
    assert cli.main(["demo", "--dir", str(directory)]) == 2
    assert (directory / "out" / "ready.csv").read_text() == counts
    assert cli.main(["demo", "--dir", str(directory), "--reset"]) == 0
    assert "Totals: 5 ready, 4 dropped, 1 retired, 10 leads in all." in capsys.readouterr().out


def test_init_then_check(tmp_path, capsys, monkeypatch):
    config = tmp_path / "lead-engine.toml"
    assert cli.main(["init", "--config", str(config)]) == 0
    assert (tmp_path / "voice.toml").exists() and (tmp_path / "suppressions.csv").read_text() == "email,domain\n"
    assert cli.main(["init", "--config", str(config)]) == 1  # never overwrites without --force
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    assert cli.main(["check", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "Config OK" in out and "GOOGLE_PLACES_API_KEY missing" in out and "Hand-off: dry-run" in out
    (tmp_path / "voice.toml").unlink()
    assert cli.main(["check", "--config", str(config)]) == 1


def write_config(tmp_path, extra=""):
    (tmp_path / "suppressions.csv").write_text("email,domain\n")
    (tmp_path / "import.csv").write_text(
        "company,website,market,keyword\n"
        "Harbor Ledger Co.,https://harbor-ledger.example.com/,\"Cedar Hollow, OR\",bookkeeping firm\n"
        "Summit Tax Partners,https://summit-tax.example.com/,\"Cedar Hollow, OR\",bookkeeping firm\n")
    (tmp_path / "voice.toml").write_text((cli.TEMPLATES / "voice.example.toml").read_text())
    config = tmp_path / "lead-engine.toml"
    config.write_text('[discovery]\nsource = "csv"\n' + extra)
    return config


@pytest.fixture
def offline(monkeypatch):
    """Route the CLI's fetcher to the demo's local sites."""
    def factory(user_agent, **kw):
        return Fetcher(user_agent, transport=SiteTransport(timeouts={}, down=set()), sleep=lambda s: None)
    monkeypatch.setattr(cli, "Fetcher", factory)


def test_run_status_export_and_handoff(tmp_path, capsys, offline):
    config = write_config(tmp_path)
    assert cli.main(["run", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "discovered 2 businesses" in out and "ready" in out and "decision_maker_not_found" in out
    assert "hand-off (dry run): would add 1 lead(s)" in out
    assert cli.main(["status", "--config", str(config)]) == 0
    assert "ready 1" in capsys.readouterr().out
    assert cli.main(["export", "--config", str(config)]) == 0
    csv_text = (tmp_path / "out" / "ready.csv").read_text()
    assert "dana.whitlock@example.com" in csv_text and "https://harbor-ledger.example.com/about" in csv_text
    assert cli.main(["handoff", "--config", str(config)]) == 0
    assert "would add 1 lead(s)" in capsys.readouterr().out


def test_backlog_command(tmp_path, capsys, offline):
    config = write_config(tmp_path)
    assert cli.main(["backlog", "--config", str(config), "--import", str(DATA / "stranded.csv")]) == 0
    out = capsys.readouterr().out
    assert "3 stranded lead(s) re-checked, 3 imported" in out and "recovered" in out and "changed" in out


def test_live_handoff_without_credentials_reports_instead_of_crashing(tmp_path, capsys, offline, monkeypatch):
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    config = write_config(tmp_path, '[handoff]\nmode = "live"\n')
    assert cli.main(["run", "--config", str(config)]) == 0
    assert "hand-off failed: RuntimeError: set INSTANTLY_API_KEY" in capsys.readouterr().out


class FakeInstantly:
    accounts_list = [{"email": "a1@example.net", "daily_limit": 120}, {"email": "seed-one@example.com"}]

    def __init__(self, *a, **kw):
        self.sent = []

    def accounts(self):
        return self.accounts_list

    def send_test_email(self, *args):
        self.sent.append(args)
        return {"status": "success"}


def test_placement_send_defaults_to_a_dry_run_and_warmup_audits(tmp_path, capsys, monkeypatch):
    config = write_config(tmp_path, '[[placement.seeds]]\naddress = "seed-one@example.com"\nhost = "imap.example.com"\n'
                                    'user_env = "S1_USER"\npassword_env = "S1_PASS"\n')
    monkeypatch.setattr(cli, "InstantlyClient", FakeInstantly)
    assert cli.main(["placement", "send", "--config", str(config)]) == 0
    assert "Dry run: would send 1 test email(s), 1 sending inbox(es) x 1 seed(s)" in capsys.readouterr().out
    assert cli.main(["warmup", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "2 inbox(es), 1 flagged" in out and "daily_limit is 120" in out
    assert cli.main(["warmup", "--config", str(config), "--strict"]) == 1


def test_placement_report_needs_a_read_run(tmp_path, capsys):
    config = write_config(tmp_path)
    run_dir = tmp_path / "out" / "placement"
    run_dir.mkdir(parents=True)
    (run_dir / "run-seed-1.json").write_text(json.dumps({"token": "seed-1", "seeds": [], "sends": []}))
    assert cli.main(["placement", "report", "--config", str(config)]) == 1


def test_partial_placement_command_preserves_progress_and_requires_explicit_resume(tmp_path, monkeypatch):
    config = write_config(tmp_path, '[[placement.seeds]]\naddress = "seed-one@example.com"\nhost = "imap.example.com"\n'
                                    'user_env = "S1_USER"\npassword_env = "S1_PASS"\n')

    class Client(FakeInstantly):
        accounts_list = [{"email": "a@example.com"}, {"email": "b@example.org"}, {"email": "c@example.com"}]

        def send_test_email(self, sender, *args):
            self.sent.append(sender)
            if sender == "b@example.org":
                raise HttpError("connection lost")
            return {"status": "success"}

    client = Client()
    monkeypatch.setattr(cli, "InstantlyClient", lambda: client)
    monkeypatch.setattr(cli.placement_mod.time, "sleep", lambda _: None)
    original = cli.placement_mod.send
    monkeypatch.setattr(cli.placement_mod, "send", lambda *a, **kw: original(*a, **kw, sleep=lambda _: None))
    command = ["placement", "send", "--send", "--config", str(config)]
    assert cli.main(command) == 2
    path = cli.placement_mod.latest(tmp_path / "out" / "placement")
    assert json.loads(path.read_text())["sends"][0]["accepted"] is True
    assert cli.main(command) == 2
    assert client.sent == ["a@example.com", "b@example.org"]
    assert cli.main([*command, "--run", str(path)]) == 1
    assert client.sent == ["a@example.com", "b@example.org", "c@example.com"]
    assert len(list(path.parent.glob("run-*.json"))) == 1


def test_errors_are_reported_cleanly(tmp_path, capsys):
    assert cli.main(["run", "--config", str(tmp_path / "missing.toml")]) == 2
    assert "lead-engine init" in capsys.readouterr().err
    bad = tmp_path / "bad.toml"
    bad.write_text('[handoff]\nmode = "send-everything"\n')
    assert cli.main(["check", "--config", str(bad)]) == 2

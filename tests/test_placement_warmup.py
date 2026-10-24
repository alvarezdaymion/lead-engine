"""Seed-test inbox placement (fake IMAP) and the warmup settings audit."""

import datetime as dt

from lead_engine import placement, warmup
from lead_engine.config import DEFAULTS

SEEDS = [placement.Seed("seed-one@example.com", "imap.example.com", "S1_USER", "S1_PASS", spam="[Gmail]/Spam"),
         placement.Seed("seed-two@example.org", "imap.example.org", "S2_USER", "S2_PASS", spam="Junk")]


class FakeClient:
    def __init__(self):
        self.sent = []

    def send_test_email(self, eaccount, to, subject, html):
        self.sent.append((eaccount, tuple(to), subject))
        return {"status": "success"}


class FakeImap:
    """Folders map to (from address, subject) pairs. Only the calls placement.py makes are implemented."""

    def __init__(self, folders):
        self.folders, self.selected, self.closed = folders, None, False

    def select(self, folder, readonly=False):
        assert readonly
        name = folder.strip('"')
        self.selected = self.folders.get(name)
        return ("OK" if self.selected is not None else "NO"), [b""]

    def search(self, charset, key, value):
        token = value.strip('"')
        hits = [str(i + 1).encode() for i, (_, subj) in enumerate(self.selected) if token in subj]
        return "OK", [b" ".join(hits)]

    def fetch(self, num, spec):
        sender, _ = self.selected[int(num) - 1]
        return "OK", [(b"1 (BODY[HEADER.FIELDS (FROM)] {40}", ("From: Sender <%s>\r\n\r\n" % sender).encode()), b")"]

    def logout(self):
        self.closed = True


def test_send_is_a_dry_run_unless_asked_and_spaces_real_sends():
    client, waits = FakeClient(), []
    senders = ["a1@example.com", "a2@example.com", "b1@example.net"]
    run = placement.send(client, senders, SEEDS, "Quick question", "<p>hi</p>", token="seed-abc123")
    assert client.sent == [] and run["dry_run"] and run["subject"] == "Quick question [seed-abc123]"
    run = placement.send(client, senders, SEEDS, "Quick question", "<p>hi</p>", dry_run=False, sleep=waits.append,
                         token="seed-abc123")
    assert [s[0] for s in client.sent] == senders and waits == [6.5, 6.5]
    assert client.sent[0][1] == ("seed-one@example.com", "seed-two@example.org")
    assert all(s["accepted"] for s in run["sends"])


def test_read_maps_each_sender_to_inbox_spam_or_missing_and_report_benches_bad_domains():
    run = {"token": "seed-abc123", "seeds": [s.address for s in SEEDS],
           "sends": [{"sender": "a1@example.com"}, {"sender": "a2@example.com"},
                     {"sender": "b1@example.net"}]}
    subject = "Quick question [seed-abc123]"
    boxes = {
        "seed-one@example.com": FakeImap({"INBOX": [("a1@example.com", subject),
                                                    ("a2@example.com", subject),
                                                    ("other@example.com", "unrelated")],
                                          "[Gmail]/Spam": [("b1@example.net", subject)]}),
        "seed-two@example.org": FakeImap({"INBOX": [("a1@example.com", subject),
                                                    ("a2@example.com", subject)], "Junk": []}),
    }
    run = placement.read(run, SEEDS, connect=lambda seed: boxes[seed.address])
    assert run["sends"][2]["placement"] == {"seed-one@example.com": "spam", "seed-two@example.org": "missing"}
    assert all(b.closed for b in boxes.values())
    table = placement.report(run)
    assert table["example.com"]["verdict"] == "keep"
    assert table["example.com"]["placements"] == {"inbox": 4}
    assert table["example.net"]["verdict"] == "bench"


def test_runs_are_saved_and_the_latest_is_found(tmp_path):
    path = placement.save({"token": "seed-1", "sends": []}, tmp_path)
    assert placement.latest(tmp_path) == path


NOW = dt.datetime(2026, 3, 2, tzinfo=dt.UTC)
GOOD = {"email": "a1@example.com", "status": 1, "warmup_status": 1, "setup_pending": False,
        "daily_limit": 30, "sending_gap": 10, "stat_warmup_score": 98, "timestamp_warmup_start": "2025-11-01T00:00:00Z",
        "warmup": {"limit": 20, "reply_rate": 30, "increment": "1",
                   "advanced": {"open_rate": 90, "important_rate": 50, "spam_save_rate": 60}}}


def audit(**changes):
    acc = {**GOOD, **changes}
    return warmup.audit_account(acc, DEFAULTS["warmup"], NOW)


def test_a_sane_inbox_passes_and_missing_fields_are_skipped():
    assert audit() == []
    assert warmup.audit_account({"email": "x@example.com"}, DEFAULTS["warmup"], NOW) == []


def test_out_of_range_settings_are_flagged_with_a_fraction_hint():
    problems = audit(warmup={**GOOD["warmup"], "reply_rate": 0.3})
    assert problems == ["warmup reply_rate is 0.3, outside 20-60 (looks like a fraction; the platform reads this "
                        "field as a percent)"]
    assert audit(daily_limit=120) == ["daily_limit is 120, outside 5-50"]
    assert audit(stat_warmup_score=71) == ["warmup score 71 below 90"]
    assert audit(warmup_status=-1, status=-1) == ["account status -1 (connection error)",
                                                  "warmup status -1 (banned)"]


def test_ramp_up_disabled_on_a_new_inbox_is_flagged():
    new = {**GOOD["warmup"], "increment": "disabled"}
    assert audit(warmup=new, timestamp_warmup_start="2026-02-25T00:00:00Z") == [
        "warmup ramp-up is disabled on an inbox that started warming 5 days ago"]
    assert audit(warmup=new) == []  # an old inbox can run without ramp-up


def test_audit_lists_flagged_inboxes_first():
    rows = warmup.audit([GOOD, {**GOOD, "email": "z@example.net", "daily_limit": 0}], DEFAULTS["warmup"], NOW)
    assert rows[0]["email"] == "z@example.net" and rows[1]["problems"] == []

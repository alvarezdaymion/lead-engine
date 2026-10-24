"""Inbox-placement seed test: where do emails from each sending inbox actually land?

A warmup score is not placement. This test measures it directly:
1. send: every sending inbox sends one test email (through the platform's test-email endpoint) to seed
   mailboxes that you own, such as one Gmail and one Outlook address. The subject carries a run token.
   Dry run unless `--send` is given. The endpoint allows 10 requests a minute, so sends are spaced.
2. read: each seed mailbox is opened over IMAP (read-only) and its inbox and spam folders are searched for
   the run token. Each message is matched to its sender by the From address.
3. report: per sending domain, a domain is `keep` only if every seed message from every inbox on it
   landed in an inbox folder. Any spam or missing message benches the whole domain.

This module never changes campaign settings; moving inboxes in or out of a campaign stays a manual
decision. Gmail's Promotions tab is not visible over IMAP, so "inbox" means the INBOX folder.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import json
import os
import secrets
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BAD = ("spam", "missing")


@dataclass
class Seed:
    address: str
    host: str
    user_env: str
    password_env: str
    port: int = 993
    inbox: str = "INBOX"
    spam: str = "Junk"

    @classmethod
    def from_config(cls, d: dict[str, Any]) -> Seed:
        return cls(address=d["address"], host=d["host"], user_env=d["user_env"], password_env=d["password_env"],
                   port=int(d.get("port", 993)), inbox=d.get("inbox", "INBOX"), spam=d.get("spam", "Junk"))


def domain(address: str) -> str:
    return address.rpartition("@")[2].lower()


def send(client: Any, senders: list[str], seeds: list[Seed], subject: str, html: str, *, dry_run: bool = True,
         interval: float = 6.5, sleep: Callable[[float], None] = time.sleep, token: str | None = None
         ) -> dict[str, Any]:
    token = token or "seed-" + secrets.token_hex(3)
    run: dict[str, Any] = {"token": token, "subject": "%s [%s]" % (subject, token), "dry_run": dry_run,
                           "seeds": [s.address for s in seeds], "sends": []}
    for i, sender in enumerate(senders):
        accepted = None
        if not dry_run:
            if i:
                sleep(interval)
            resp = client.send_test_email(sender, run["seeds"], run["subject"], html)
            accepted = resp.get("status") == "success"
        run["sends"].append({"sender": sender, "accepted": accepted})
    return run


def _quote(folder: str) -> str:
    return '"%s"' % folder.replace('"', '\\"')


def _senders_in(imap: Any, folder: str, token: str) -> set[str]:
    status, _ = imap.select(_quote(folder), readonly=True)
    if status != "OK":
        return set()
    status, data = imap.search(None, "SUBJECT", _quote(token))
    found: set[str] = set()
    for num in (data[0].split() if status == "OK" and data and data[0] else []):
        status, parts = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        for part in parts or []:
            if isinstance(part, tuple):
                msg = email.message_from_bytes(part[1])
                found.add(email.utils.parseaddr(msg.get("From", ""))[1].lower())
    return found


def imap_connect(seed: Seed) -> Any:
    user, password = os.environ.get(seed.user_env, ""), os.environ.get(seed.password_env, "")
    if not user or not password:
        raise RuntimeError("set %s and %s for seed %s" % (seed.user_env, seed.password_env, seed.address))
    imap = imaplib.IMAP4_SSL(seed.host, seed.port)
    imap.login(user, password)
    return imap


def read(run: dict[str, Any], seeds: list[Seed], connect: Callable[[Seed], Any] = imap_connect) -> dict[str, Any]:
    by_seed: dict[str, dict[str, str]] = {}
    for seed in seeds:
        if seed.address not in run["seeds"]:
            continue
        imap = connect(seed)
        try:
            inbox = _senders_in(imap, seed.inbox, run["token"])
            spam = _senders_in(imap, seed.spam, run["token"])
        finally:
            imap.logout()
        by_seed[seed.address] = {**{s: "spam" for s in spam}, **{s: "inbox" for s in inbox}}
    for send_ in run["sends"]:
        send_["placement"] = {seed: by_seed.get(seed, {}).get(send_["sender"].lower(), "missing")
                              for seed in run["seeds"]}
    run["read"] = True
    return run


def report(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for s in run["sends"]:
        row = table.setdefault(domain(s["sender"]), {"inboxes": 0, "placements": Counter()})
        row["inboxes"] += 1
        row["placements"].update((s.get("placement") or {}).values())
    for row in table.values():
        bad = sum(n for where, n in row["placements"].items() if where in BAD)
        row["verdict"] = "keep" if row["placements"] and not bad else "bench"
        row["placements"] = dict(row["placements"])
    return dict(sorted(table.items()))


def save(run: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("run-%s.json" % run["token"])
    path.write_text(json.dumps(run, indent=2) + "\n")
    return path


def latest(directory: Path) -> Path:
    runs = sorted(directory.glob("run-*.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise FileNotFoundError("no seed-test runs in %s; run `lead-engine placement send --send` first" % directory)
    return runs[-1]

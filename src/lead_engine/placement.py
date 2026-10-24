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
import hashlib
import imaplib
import json
import os
import re
import secrets
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .html import EMAIL_RE
from .http import HttpError

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
         interval: float = 6.5, sleep: Callable[[float], None] = time.sleep, token: str | None = None,
         out_dir: Path | None = None, run: dict[str, Any] | None = None
         ) -> dict[str, Any]:
    addresses = [s.address.lower() for s in seeds]
    senders = [s.lower() for s in senders]
    if (not addresses or any(not EMAIL_RE.fullmatch(s) for s in addresses + senders)
            or len(set(senders)) != len(senders) or len(set(addresses)) != len(addresses)
            or set(senders) & set(addresses)):
        raise ValueError("seed test needs distinct valid sending and seed addresses")
    if not dry_run and out_dir is None:
        raise ValueError("live seed sends require a durable run directory")
    digest = hashlib.sha256(html.encode()).hexdigest()
    if run is not None:
        if dry_run or out_dir is None:
            raise ValueError("resume requires --send and the saved run directory")
        _validate_resume(run, senders, addresses, subject, digest)
        path = out_dir / ("run-%s.json" % run["token"])
        if json.loads(path.read_text()) != run:
            raise ValueError("seed run changed; reload it before resuming")
    else:
        if not dry_run:
            for path in out_dir.glob("run-*.json"):
                previous = json.loads(path.read_text())
                _validate_saved_run(previous)
                if path.name != "run-%s.json" % previous["token"]:
                    raise ValueError("seed run filename does not match its token")
                if any(s["accepted"] is not True for s in previous["sends"]):
                    raise ValueError("unfinished seed run; resume with placement send --send --run %s" % path)
        token = token or "seed-" + secrets.token_hex(6)
        run = {"version": 1, "token": token, "subject": "%s [%s]" % (subject, token), "dry_run": dry_run,
               "body_sha256": digest, "seeds": addresses,
               "sends": [{"sender": s, "accepted": None, "status": "pending"} for s in senders]}
        if not dry_run:
            if (out_dir / ("run-%s.json" % token)).exists():
                raise ValueError("seed run already exists; resume it explicitly")
            save(run, out_dir)
    attempted = False
    for item in run["sends"]:
        if dry_run or item["status"] != "pending":
            continue
        if attempted:
            sleep(interval)
        item["status"] = "unresolved"
        save(run, out_dir)
        resp = client.send_test_email(item["sender"], run["seeds"], run["subject"], html)
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise HttpError("unresolved seed send: response did not confirm acceptance")
        item.update(accepted=True, status="accepted")
        save(run, out_dir)
        attempted = True
    return run


def _validate_saved_run(run: Any) -> None:
    if not isinstance(run, dict):
        raise ValueError("seed run must be a JSON object")
    token = run.get("token")
    items = run.get("sends")
    if (run.get("version") != 1 or not isinstance(token, str) or not re.fullmatch(r"seed-[a-z0-9-]+", token)
            or run.get("dry_run") is not False or not isinstance(run.get("body_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", run["body_sha256"])
            or not isinstance(run.get("subject"), str) or not run["subject"].endswith(" [%s]" % token)
            or not isinstance(run.get("seeds"), list) or not run["seeds"] or not isinstance(items, list)
            or any(not isinstance(s, dict) or not isinstance(s.get("sender"), str) for s in items)):
        raise ValueError("seed run is incomplete or malformed")
    addresses = run["seeds"] + [s["sender"] for s in items]
    if (any(not isinstance(a, str) or not EMAIL_RE.fullmatch(a) for a in addresses)
            or len(set(a.lower() for a in addresses)) != len(addresses)):
        raise ValueError("seed run has invalid or repeated addresses")
    for item in items:
        status = item.get("status")
        if (status not in ("accepted", "pending", "unresolved") or "accepted" not in item
                or (item["accepted"] is not True if status == "accepted" else item["accepted"] is not None)):
            raise ValueError("seed run has an invalid send status")


def _validate_resume(run: dict[str, Any], senders: list[str], addresses: list[str], subject: str, digest: str) -> None:
    _validate_saved_run(run)
    if (run["body_sha256"] != digest or run["seeds"] != addresses
            or run["subject"] != "%s [%s]" % (subject, run["token"])
            or [s["sender"] for s in run["sends"]] != senders):
        raise ValueError("seed run does not match the current senders, seeds, subject and body")


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
        by_seed[seed.address] = {**{s: "inbox" for s in inbox}, **{s: "spam" for s in spam}}
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
    if not isinstance(run.get("token"), str) or not re.fullmatch(r"seed-[a-z0-9-]+", run["token"]):
        raise ValueError("invalid seed run token")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("run-%s.json" % run["token"])
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w") as f:
        f.write(json.dumps(run, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
    return path


def latest(directory: Path) -> Path:
    runs = sorted(directory.glob("run-*.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise FileNotFoundError("no seed-test runs in %s; run `lead-engine placement send --send` first" % directory)
    return runs[-1]

"""Warmup audit: read every sending inbox's settings from the platform and flag values out of range.

Read-only (GET /api/v2/accounts). The ranges live in `[warmup]` in lead-engine.toml and are starting
points, not platform rules. Percent fields that hold a value between 0 and 1 get an extra hint, because
the platform reads them as percentages: 0.3 means 0.3%, not 30%. A missing field is skipped, not flagged.
A high warmup score is not proof of inbox placement; the seed test measures that.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

ACCOUNT_STATUS = {1: "active", 2: "paused", 3: "paused for maintenance", -1: "connection error",
                  -2: "soft bounce error", -3: "sending error"}
WARMUP_STATUS = {0: "paused", 1: "active", -1: "banned", -2: "spam folder unknown", -3: "permanent suspension"}


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _range(problems: list[str], label: str, value: Any, bounds: list[float], percent: bool = False) -> None:
    v = _num(value)
    if v is None:
        return
    lo, hi = float(bounds[0]), float(bounds[1])
    if not lo <= v <= hi:
        msg = "%s is %g, outside %g-%g" % (label, v, lo, hi)
        if percent and 0 < v <= 1:
            msg += " (looks like a fraction; the platform reads this field as a percent)"
        problems.append(msg)


def audit_account(acc: dict[str, Any], r: dict[str, Any], now: dt.datetime) -> list[str]:
    problems: list[str] = []
    status = acc.get("status")
    if status is not None and status != 1:
        problems.append("account status %s (%s)" % (status, ACCOUNT_STATUS.get(status, "unknown")))
    wstatus = acc.get("warmup_status")
    if wstatus is not None and wstatus != 1:
        problems.append("warmup status %s (%s)" % (wstatus, WARMUP_STATUS.get(wstatus, "unknown")))
    if acc.get("setup_pending"):
        problems.append("setup is pending")
    _range(problems, "daily_limit", acc.get("daily_limit"), r["daily_limit"])
    _range(problems, "sending_gap minutes", acc.get("sending_gap"), r["sending_gap"])
    warmup = acc.get("warmup") or {}
    _range(problems, "warmup limit", warmup.get("limit"), r["warmup_limit"])
    _range(problems, "warmup reply_rate", warmup.get("reply_rate"), r["reply_rate"], percent=True)
    advanced = warmup.get("advanced") or {}
    for key in ("open_rate", "important_rate", "spam_save_rate"):
        _range(problems, "warmup %s" % key, advanced.get(key), r[key], percent=True)
    score = _num(acc.get("stat_warmup_score"))
    if score is not None and score < float(r["min_warmup_score"]):
        problems.append("warmup score %g below %g" % (score, float(r["min_warmup_score"])))
    started = acc.get("timestamp_warmup_start")
    if warmup.get("increment") == "disabled" and started:
        try:
            age = (now - dt.datetime.fromisoformat(str(started).replace("Z", "+00:00"))).days
        except ValueError:
            age = None
        if age is not None and age < int(r["ramp_days"]):
            problems.append("warmup ramp-up is disabled on an inbox that started warming %d days ago" % age)
    return problems


def audit(accounts: list[dict[str, Any]], ranges: dict[str, Any], now: dt.datetime) -> list[dict[str, Any]]:
    rows = [{"email": a.get("email", ""), "problems": audit_account(a, ranges, now)} for a in accounts]
    return sorted(rows, key=lambda x: (not x["problems"], x["email"]))

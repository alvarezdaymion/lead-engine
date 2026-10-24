"""Load lead-engine.toml and fill in defaults. Credentials never live here; they come from the environment."""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .verify import DEFAULT_ROLES

DEFAULTS: dict[str, Any] = {
    "discovery": {
        "source": "places",              # places | csv
        "allow_paid": False,             # explicit permission for billable Places searches
        "csv_path": "import.csv",
        "markets": [],
        "keywords": [],
        "min_queue": 20,                 # discover when fewer leads than this are due
        "cells_per_tick": 2,             # (market, keyword) searches per run
        "pages_per_search": 1,           # Places pages per search, 20 results each, at most 3
        "region_code": "US",             # Places region bias (two-letter country code)
        "max_locations_per_domain": 2,   # more places sharing one website than this = a chain
        "refresh_days": 90,              # a searched cell may be searched again after this
        "exclude_domains": [],
    },
    "verify": {
        "roles": DEFAULT_ROLES,
        "max_pages": 6,                  # homepage plus about, team and contact pages
        "require_any": [],               # at least one of these must appear on the site
        "exclude_any": ["franchise opportunities", "find a location near you"],
        "allow_free_mail": True,         # a gmail-style address counts only when printed beside the name
    },
    "fetch": {
        "user_agent": "lead-engine/0.1 (+https://github.com/alvarezdaymion/lead-engine)",
        "timeout_seconds": 15,
        "max_bytes": 1_000_000,
        "per_host_seconds": 2.0,
    },
    "engine": {
        "max_repairs": 3,                # failed attempts at one stage before a lead is retired
        "backoff_minutes": 30,           # first retry after 30 min, then 60, 120 ... up to the cap
        "max_backoff_minutes": 720,
        "lease_minutes": 20,
        "max_minutes": 12,               # time budget for one run
        "max_leads_per_tick": 50,
        "stop_after_errors": 5,          # failures in a row that end the run early
        "stale_hours": 48,               # backlog: research leads untouched this long count as stranded
    },
    "writer": {
        "mode": "template",              # template | claude
        "voice_file": "voice.toml",
        "model": "claude-opus-5-5",
        "effort": "low",
        "fallbacks": True,
        "max_tokens": 2000,
    },
    "gate": {"suppression_file": "suppressions.csv"},
    "handoff": {
        "mode": "dry-run",               # off | dry-run | live
        "max_leads_per_run": 100,
        "batch_size": 100,
    },
    "warmup": {
        "daily_limit": [5, 50],
        "sending_gap": [3, 1440],
        "warmup_limit": [10, 40],
        "reply_rate": [20, 60],
        "open_rate": [50, 100],
        "important_rate": [10, 100],
        "spam_save_rate": [10, 100],
        "min_warmup_score": 90,
        "ramp_days": 14,
    },
    "placement": {
        "subject": "Quick question",
        "body_file": "",
        "sender_domains": [],
        "seeds": [],
    },
    "paths": {"state": "state", "out": "out"},
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Config:
    data: dict[str, Any]
    root: Path

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def path(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def state_dir(self) -> Path:
        return self.path(self.data["paths"]["state"])

    @property
    def out_dir(self) -> Path:
        return self.path(self.data["paths"]["out"])


def validate(d: dict[str, Any]) -> None:
    unknown = set(d) - set(DEFAULTS)
    if unknown:
        raise ValueError("unknown section(s): %s" % ", ".join(sorted(unknown)))
    if d["discovery"]["source"] not in ("places", "csv"):
        raise ValueError("discovery.source must be places or csv")
    if type(d["discovery"]["allow_paid"]) is not bool:
        raise ValueError("discovery.allow_paid must be true or false")
    if d["writer"]["mode"] not in ("template", "claude"):
        raise ValueError("writer.mode must be template or claude")
    if d["handoff"]["mode"] not in ("off", "dry-run", "live"):
        raise ValueError("handoff.mode must be off, dry-run or live")
    if int(d["engine"]["max_repairs"]) < 1:
        raise ValueError("engine.max_repairs must be at least 1")
    if not d["verify"]["roles"]:
        raise ValueError("verify.roles is empty")
    if int(d["verify"]["max_pages"]) < 1:
        raise ValueError("verify.max_pages must be at least 1")
    for seed in d["placement"]["seeds"]:
        missing = {"address", "host", "user_env", "password_env"} - set(seed)
        if missing:
            raise ValueError("placement seed %s is missing %s" % (seed.get("address", "?"), ", ".join(sorted(missing))))


def from_dict(data: dict[str, Any], root: Path | str = ".") -> Config:
    merged = _merge(DEFAULTS, data)
    validate(merged)
    return Config(merged, Path(root).resolve())


def load(path: Path | str) -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError("%s not found. Run `lead-engine init` first." % p)
    with p.open("rb") as f:
        data = tomllib.load(f)
    return from_dict(data, p.parent)

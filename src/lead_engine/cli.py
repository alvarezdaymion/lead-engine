"""Command line: lead-engine demo | init | check | run | status | backlog | export | handoff | placement | warmup."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import placement as placement_mod
from . import warmup as warmup_mod
from .backlog import import_stranded, requalify
from .engine import Engine, TickLock
from .fetch import Fetcher
from .gate import Suppressions, load_suppressions
from .handoff import run_handoff
from .instantly import InstantlyClient
from .report import event_lines, export_csv, status_lines
from .sources import Discovery
from .sources.csv_import import read_candidates
from .sources.places import PlacesSource
from .store import Store
from .voice import load_voice
from .writer import ClaudeWriter, TemplateWriter

TEMPLATES = Path(__file__).parent / "templates"
SAMPLE_BODY = ("Hi {{firstName}},<br/><br/>{{personalization}}<br/><br/>This is a seed test of the first email."
               "<br/><br/>Thanks")


def _env(*names: str) -> str:
    return ", ".join("%s %s" % (n, "set" if os.environ.get(n) else "missing") for n in names)


def build_discovery(cfg: config_mod.Config, suppressions: Suppressions) -> Any:
    d = cfg["discovery"]
    if d["source"] == "csv":
        path = cfg.path(d["csv_path"])
        return Discovery(d, None, suppressions, lambda: dt.datetime.now(dt.UTC), csv_rows=lambda: read_candidates(path))
    if not d["markets"] or not d["keywords"]:
        def missing(store: Store) -> dict[str, Any]:
            raise RuntimeError("set discovery.markets and discovery.keywords in lead-engine.toml")
        return missing
    try:
        source = PlacesSource(max_pages=int(d["pages_per_search"]), region_code=str(d["region_code"]))
    except RuntimeError as e:
        message = str(e)

        def unavailable(store: Store) -> dict[str, Any]:
            raise RuntimeError(message)
        return unavailable
    return Discovery(d, source, suppressions, lambda: dt.datetime.now(dt.UTC))


def build_engine(cfg: config_mod.Config, notify: Any = None) -> Engine:
    voice = load_voice(cfg.path(cfg["writer"]["voice_file"]))
    suppressions = load_suppressions(cfg.path(cfg["gate"]["suppression_file"]))
    store = Store(cfg.state_dir / "leads.db")
    f = cfg["fetch"]
    fetcher = Fetcher(f["user_agent"], timeout=float(f["timeout_seconds"]), max_bytes=int(f["max_bytes"]),
                      per_host_seconds=float(f["per_host_seconds"]))
    writer = TemplateWriter(voice) if cfg["writer"]["mode"] == "template" else ClaudeWriter(cfg["writer"], voice)

    def handoff(s: Store) -> dict[str, Any]:
        client = InstantlyClient() if cfg["handoff"]["mode"] == "live" else None
        return run_handoff(s, cfg["handoff"], suppressions=suppressions, out_dir=cfg.out_dir / "handoff", client=client,
                           suppression_file=cfg.path(cfg["gate"]["suppression_file"]))

    return Engine(cfg.data, store, fetcher=fetcher, writer=writer, voice=voice, suppressions=suppressions,
                  discover=build_discovery(cfg, suppressions), handoff=handoff, notify=notify)


def _printer(holder: dict[str, Any], max_repairs: int) -> Any:
    def show(kind: str, payload: dict[str, Any]) -> None:
        for line in event_lines(holder["store"], kind, payload, max_repairs):
            print(line, flush=True)
    return show


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run
    return run(Path(args.dir), reset=args.reset)


def cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.config)
    voice = dest.parent / "voice.toml"
    if dest.exists() and not args.force:
        print("%s already exists (use --force to overwrite)" % dest)
        return 1
    shutil.copyfile(TEMPLATES / "lead-engine.example.toml", dest)
    if not voice.exists() or args.force:
        shutil.copyfile(TEMPLATES / "voice.example.toml", voice)
    supp = dest.parent / "suppressions.csv"
    if not supp.exists():
        supp.write_text("email,domain\n")
    print("Wrote %s, %s and %s." % (dest, voice, supp))
    print("Edit markets, keywords and the voice rules, copy .env.example to .env and fill in credentials, then run: "
          "uv run --env-file .env lead-engine check")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    print("Config OK: %s" % Path(args.config).resolve())
    d = cfg["discovery"]
    if d["source"] == "places":
        print("Discovery: Google Places, %d market(s) x %d keyword(s) = %d searches; %s"
              % (len(d["markets"]), len(d["keywords"]), len(d["markets"]) * len(d["keywords"]),
                 _env("GOOGLE_PLACES_API_KEY")))
    else:
        path = cfg.path(d["csv_path"])
        print("Discovery: CSV %s (%s)" % (path, "found" if path.exists() else "missing"))
    f, v = cfg["fetch"], cfg["verify"]
    print("Verify: roles %s; up to %d pages per site; robots.txt obeyed; %.1f s between requests to one site"
          % (", ".join(v["roles"][:4]) + (" ..." if len(v["roles"]) > 4 else ""), v["max_pages"],
             f["per_host_seconds"]))
    print("User agent: %s" % f["user_agent"])
    voice_path = cfg.path(cfg["writer"]["voice_file"])
    try:
        voice = load_voice(voice_path)
    except FileNotFoundError:
        print("Voice rules: %s is missing. Run `lead-engine init` or copy the example." % voice_path)
        return 1
    print("Voice rules: %s OK (%d chars, %d sentences, grade %.1f, %d banned phrases, templates: %s)"
          % (voice_path.name, voice.max_characters, voice.max_sentences, voice.max_grade, len(voice.banned_phrases),
             ", ".join("%s %d" % (k, len(t)) for k, t in voice.templates.items()) or "none"))
    w = cfg["writer"]
    if w["mode"] == "claude":
        try:
            import anthropic  # noqa: F401
            sdk = "installed"
        except ImportError:
            sdk = "missing: uv sync --extra llm"
        print("Writer: claude (%s, effort %s), anthropic SDK %s, %s" % (w["model"], w["effort"], sdk,
                                                                      _env("ANTHROPIC_API_KEY")))
    else:
        print("Writer: template (no keys needed)")
    supp_path = cfg.path(cfg["gate"]["suppression_file"])
    supp = load_suppressions(supp_path)
    print("Suppressions: %s, %d address(es) and %d domain(s)%s" % (
        supp_path.name, len(supp.emails), len(supp.domains), "" if supp_path.exists() else " (file missing)"))
    h = cfg["handoff"]
    print("Hand-off: %s; %s%s" % (h["mode"], _env("INSTANTLY_API_KEY", "INSTANTLY_CAMPAIGN_ID"),
                                  "" if h["mode"] == "live" else " (only live mode uses them)"))
    seeds = cfg["placement"]["seeds"]
    print("Seed test: %d seed mailbox(es)%s" % (len(seeds), "".join(
        "; %s %s" % (s["address"], _env(s["user_env"], s["password_env"])) for s in seeds)))
    print("State: %s" % (cfg.state_dir / "leads.db"))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    with TickLock(cfg.state_dir / "run.lock") as acquired:
        if not acquired:
            print("Another run is in progress; exiting.")
            return 0
        holder: dict[str, Any] = {}
        engine = build_engine(cfg, notify=None if args.quiet else _printer(holder, int(cfg["engine"]["max_repairs"])))
        holder["store"] = engine.store
        if not args.quiet:
            print("Run at %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        summary = engine.tick(max_leads=args.max_leads, discover=not args.no_discover, handoff=not args.no_handoff)
        if not args.quiet:
            print("Stages: " + ", ".join("%s %d" % kv for kv in summary["stages"].items()))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    path = cfg.state_dir / "leads.db"
    if not path.exists():
        print("No state yet at %s. Run `lead-engine run` first." % path)
        return 0
    for line in status_lines(Store(path)):
        print(line)
    return 0


def cmd_backlog(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    with TickLock(cfg.state_dir / "run.lock") as acquired:
        if not acquired:
            print("A run is in progress; try again when it finishes.")
            return 0
        engine = build_engine(cfg)
        imported = import_stranded(engine, args.import_csv) if args.import_csv else 0
        result = requalify(engine, args.limit)
        print("Backlog: %d stranded lead(s) re-checked%s" % (result["rechecked"],
                                                           ", %d imported" % imported if args.import_csv else ""))
        for r in result["leads"]:
            then = " -> %s" % r["then"] if r.get("then") else ""
            print("  #%-4d %-28s %-12s %s%s" % (r["id"], r["company"][:28], r["result"], r["detail"][:100], then))
        print("Results: " + (", ".join("%s %d" % kv for kv in sorted(result["counts"].items())) or "nothing stranded"))
        if result["stop_reason"]:
            print("Stopped: " + result["stop_reason"])
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    out = Path(args.out) if args.out else cfg.out_dir / ("%s.csv" % args.stage)
    n = export_csv(Store(cfg.state_dir / "leads.db"), out, (args.stage,))
    print("Wrote %d %s lead(s) to %s" % (n, args.stage, out))
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    with TickLock(cfg.state_dir / "run.lock") as acquired:
        if not acquired:
            print("A run is in progress; it hands off at the end.")
            return 0
        engine = build_engine(cfg)
        result = engine.handoff(engine.store) if engine.handoff else {}
        lines = event_lines(engine.store, "handoff", result, 0) or ["  hand-off: %s" % json.dumps(result)]
        print("\n".join(lines))
    return 0


def cmd_placement(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    p = cfg["placement"]
    seeds = [placement_mod.Seed.from_config(s) for s in p["seeds"]]
    out = cfg.out_dir / "placement"
    if args.action == "send":
        if not seeds:
            print("Add [[placement.seeds]] to %s first." % args.config)
            return 1
        with TickLock(out / "send.lock") as acquired:
            if not acquired:
                raise RuntimeError("another seed test is in progress")
            client = InstantlyClient()
            seed_addresses = {s.address.lower() for s in seeds}
            wanted = {d.lower() for d in p["sender_domains"]}
            senders = sorted(a["email"] for a in client.accounts() if a.get("email")
                             and a["email"].lower() not in seed_addresses
                             and (not wanted or placement_mod.domain(a["email"]) in wanted))
            html = cfg.path(p["body_file"]).read_text() if p["body_file"] else SAMPLE_BODY
            saved = None
            if args.run:
                path = Path(args.run)
                if path.resolve().parent != out.resolve():
                    raise ValueError("resume file must be in the configured placement output directory")
                saved = json.loads(path.read_text())
            run = placement_mod.send(client, senders, seeds, p["subject"], html, dry_run=not args.send,
                                     out_dir=out, run=saved)
        if not args.send:
            print("Dry run: would send %d test email(s), %d sending inbox(es) x %d seed(s). Add --send to send."
                  % (len(senders) * len(seeds), len(senders), len(seeds)))
            return 0
        path = out / ("run-%s.json" % run["token"])
        unresolved = sum(s["status"] == "unresolved" for s in run["sends"])
        print("Seed test: %d inbox(es); %d accepted, %d unresolved. Saved %s. "
              "Wait about 5 minutes, then: lead-engine placement read"
              % (len(run["sends"]), sum(s["accepted"] is True for s in run["sends"]), unresolved, path))
        return 1 if unresolved else 0
    path = Path(args.run) if args.run else placement_mod.latest(out)
    run = json.loads(path.read_text())
    if args.action == "read":
        with TickLock(out / "send.lock") as acquired:
            if not acquired:
                raise RuntimeError("another seed test is in progress")
            run = placement_mod.read(json.loads(path.read_text()), seeds)
            placement_mod.save(run, path.parent)
        missing = sum(1 for s in run["sends"] for v in s["placement"].values() if v == "missing")
        print("Read %d send(s) x %d seed(s); %d not found. Saved %s" % (len(run["sends"]), len(run["seeds"]),
                                                                       missing, path))
        return 0
    if not run.get("read"):
        print("This run has not been read yet: lead-engine placement read")
        return 1
    print("Seed test %s: a domain is kept only if every seed message from every inbox on it reached the inbox."
          % run["token"])
    for dom, row in placement_mod.report(run).items():
        where = ", ".join("%s %d" % kv for kv in sorted(row["placements"].items()))
        print("  %-32s %2d inbox(es)  %-36s %s" % (dom, row["inboxes"], where, row["verdict"].upper()))
    return 0


def cmd_warmup(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    rows = warmup_mod.audit(InstantlyClient().accounts(), cfg["warmup"], dt.datetime.now(dt.UTC))
    flagged = [r for r in rows if r["problems"]]
    print("Warmup audit: %d inbox(es), %d flagged" % (len(rows), len(flagged)))
    for r in rows:
        print("  %-40s %s" % (r["email"], "; ".join(r["problems"]) or "ok"))
    return 1 if args.strict and flagged else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lead-engine", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def command(name: str, help_: str, fn: Any) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        if name != "demo":
            p.add_argument("--config", default="lead-engine.toml")
        return p

    d = command("demo", "run the whole pipeline on fictional businesses, no keys or network", cmd_demo)
    d.add_argument("--dir", default="lead-engine-demo", help="where the demo writes its state and exports")
    d.add_argument("--reset", action="store_true", help="replace output in an existing marked demo directory")
    i = command("init", "write lead-engine.toml, voice.toml and suppressions.csv from the examples", cmd_init)
    i.add_argument("--force", action="store_true")
    command("check", "validate the config and show which credentials are set", cmd_check)
    r = command("run", "one scheduled run: discover, verify, write, gate, hand off", cmd_run)
    r.add_argument("--max-leads", type=int, help="process at most this many leads")
    r.add_argument("--no-discover", action="store_true", help="only work on leads already queued")
    r.add_argument("--no-handoff", action="store_true")
    r.add_argument("--quiet", action="store_true")
    command("status", "lead counts by stage, drop and retire reasons, retries, last runs", cmd_status)
    b = command("backlog", "re-check stranded leads and recover what can be proven", cmd_backlog)
    b.add_argument("--import", dest="import_csv", help="CSV of half-researched leads from another tool")
    b.add_argument("--limit", type=int)
    e = command("export", "write leads with every fact's source URL and quote to CSV", cmd_export)
    e.add_argument("--stage", default="ready", choices=["ready", "handed_off"])
    e.add_argument("--out")
    command("handoff", "hand ready leads to the email platform now (respects [handoff] mode)", cmd_handoff)
    pl = command("placement", "inbox-placement seed test: send, read, report", cmd_placement)
    pl.add_argument("action", choices=["send", "read", "report"])
    pl.add_argument("--send", action="store_true", help="really send the test emails (default is a dry run)")
    pl.add_argument("--run", help="seed-test run file (default: the latest)")
    w = command("warmup", "flag sending inboxes whose warmup settings are out of range", cmd_warmup)
    w.add_argument("--strict", action="store_true", help="exit 1 when any inbox is flagged")
    args = ap.parse_args(argv)
    try:
        return int(args.fn(args))
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

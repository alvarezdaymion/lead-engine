"""`lead-engine demo`: the whole pipeline on fictional businesses, with no keys and no network.

Every business, person and address here is fictional; websites are local HTML files in demo_data/sites,
served by a fake transport, and the clock is simulated so three scheduled runs take no real time.
What happens:

- Run 1 (09:00): two searches find 10 places. Three share one website and are skipped as a chain.
  Harbor Ledger Co. becomes campaign-ready. Brightline Books' about page times out, so it is retried
  instead of dropped. Summit Tax Partners names nobody and is dropped for missing proof. Oakridge
  Bookkeeping's site returns HTTP 503. Then the run itself stops mid-step while writing Copperfield
  Inspections' opener (a simulated crash), leaving its lease held.
- Run 2 (09:31): the expired lease is counted as a failed attempt and Copperfield resumes from its
  checkpoint without fetching again. Juniper only publishes a shared inbox; Stonegate's owner is on the
  suppression list.
- Run 3 (10:32): Brightline's about page loads on the third try and the lead is repaired. Oakridge fails a
  third time and is retired with the reason.
- Backlog (10:35): three half-researched leads from another tool plus the retired one are re-checked.
- Export: campaign-ready leads go to a CSV and to a hand-off dry run. Nothing is sent.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

from . import config as config_mod
from .backlog import import_stranded, requalify
from .engine import Engine
from .fetch import Fetcher, Response, host_of
from .gate import load_suppressions
from .handoff import run_handoff
from .report import event_lines, export_csv
from .sources import Candidate, Discovery
from .store import Store
from .voice import load_voice
from .writer import TemplateWriter

DATA = Path(__file__).parent / "demo_data"
SITES = DATA / "sites"
START = dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.UTC)
PLACES = {
    ("bookkeeping firm", "Cedar Hollow, OR"): [
        ("Harbor Ledger Co.", "https://harbor-ledger.example.com/"),
        ("Brightline Books", "https://brightline-books.example.com/"),
        ("Summit Tax Partners", "https://summit-tax.example.com/"),
        ("Oakridge Bookkeeping", "https://oakridge-bookkeeping.example.com/"),
    ],
    ("home inspection", "Port Alder, WA"): [
        ("Copperfield Inspections", "https://copperfield-inspections.example.org/"),
        ("Juniper Home Inspection", "https://juniper-home.example.org/"),
        ("Stonegate Home Inspection", "https://stonegate-inspect.example.org/"),
        ("Keystone Inspect North", "https://keystone-inspect.example.org/"),
        ("Keystone Inspect South", "https://keystone-inspect.example.org/"),
        ("Keystone Inspect Downtown", "https://keystone-inspect.example.org/"),
    ],
}


class SimulatedCrash(BaseException):
    """Stands in for a killed process: not an Exception, so nothing inside the run catches it."""


class Clock:
    def __init__(self, start: dt.datetime):
        self.now = start
        self.ticks = 0.0

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += dt.timedelta(minutes=minutes)

    def sleep(self, seconds: float) -> None:
        self.ticks += seconds

    def monotonic(self) -> float:
        return self.ticks


class SiteTransport:
    """Serves demo_data/sites/<host>/<page>.html. Scripted faults: Brightline's about page times out
    twice, Oakridge's site always answers 503."""

    def __init__(self, sites: Path = SITES, timeouts: dict[str, int] | None = None, down: set[str] | None = None):
        self.sites = sites
        self.timeouts = dict(timeouts if timeouts is not None else {"https://brightline-books.example.com/about": 2})
        self.down = down if down is not None else {"oakridge-bookkeeping.example.com"}
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> Response:
        self.calls.append(url)
        if self.timeouts.get(url, 0) > 0:
            self.timeouts[url] -= 1
            raise TimeoutError("timed out")
        host = host_of(url)
        path = url.split(host, 1)[1].split("?")[0].strip("/")
        if path == "robots.txt":
            return Response(404, {"content-type": "text/plain"}, b"", url)
        if host in self.down:
            return Response(503, {"content-type": "text/html"}, b"busy", url)
        f = self.sites / host / ((path or "index") + ".html")
        if not f.exists():
            return Response(404, {"content-type": "text/html"}, b"not found", url)
        return Response(200, {"content-type": "text/html; charset=utf-8"}, f.read_bytes()[: max_bytes + 1], url)


class DemoPlaces:
    name = "demo-places"

    def search(self, keyword: str, market: str) -> list[Candidate]:
        found = PLACES.get((keyword, market), [])
        return [Candidate(company, site, market, keyword, "demo") for company, site in found]


class CrashOnce(TemplateWriter):
    """The template writer, except the run dies the first time it writes for one company."""

    def __init__(self, voice: Any, company: str):
        super().__init__(voice)
        self.company = company
        self.crashed = False

    def drafts(self, lead: dict[str, Any]) -> list[Any]:
        if lead["company"] == self.company and not self.crashed:
            self.crashed = True
            raise SimulatedCrash("the run stopped while writing an opener")
        return super().drafts(lead)


def demo_config(root: Path) -> config_mod.Config:
    return config_mod.from_dict({
        "discovery": {"markets": ["Cedar Hollow, OR", "Port Alder, WA"],
                      "keywords": ["bookkeeping firm", "home inspection"], "cells_per_tick": 4},
        "gate": {"suppression_file": str(DATA / "suppressions.csv")},
        "writer": {"voice_file": str(Path(__file__).parent / "templates" / "voice.example.toml")},
    }, root)


def build(root: Path, transport: SiteTransport | None = None, clock: Clock | None = None,
          notify: Any = None) -> tuple[Engine, Clock, config_mod.Config]:
    cfg = demo_config(root)
    clock = clock or Clock(START)
    voice = load_voice(cfg["writer"]["voice_file"])
    suppressions = load_suppressions(Path(cfg["gate"]["suppression_file"]))
    store = Store(cfg.state_dir / "leads.db", clock=clock)
    fetcher = Fetcher(cfg["fetch"]["user_agent"], transport=transport or SiteTransport(), sleep=clock.sleep,
                      monotonic=clock.monotonic)
    engine = Engine(cfg.data, store, fetcher=fetcher, writer=CrashOnce(voice, "Copperfield Inspections"), voice=voice,
                    suppressions=suppressions, clock=clock, notify=notify,
                    discover=Discovery(cfg["discovery"], DemoPlaces(), suppressions, clock),
                    handoff=lambda s: run_handoff(s, cfg["handoff"], suppressions=suppressions,
                                                  out_dir=cfg.out_dir / "handoff", campaign_id=""))
    return engine, clock, cfg


def run(root: Path, *, reset: bool = False) -> int:
    if Path(root).is_symlink():
        raise ValueError("demo destination must not be a symlink")
    root = Path(root).resolve()
    marker = root / ".lead-engine-demo"
    signature = "lead-engine synthetic demo v1\n"
    if root.exists() and any(root.iterdir()):
        if (not reset or marker.is_symlink() or not marker.is_file() or marker.read_text() != signature):
            raise ValueError("demo destination is occupied; choose an empty directory, or use --reset on a marked demo")
        if any((root / name).is_symlink() for name in ("state", "out", "lead-engine.toml")):
            raise ValueError("demo output paths must not be symlinks")
        for name in ("state", "out"):
            if (root / name).exists():
                shutil.rmtree(root / name)
    else:
        root.mkdir(parents=True, exist_ok=True)
        with marker.open("x") as f:
            f.write(signature)
    holder: dict[str, Any] = {}

    def show(kind: str, payload: dict[str, Any]) -> None:
        for line in event_lines(holder["engine"].store, kind, payload, int(holder["cfg"]["engine"]["max_repairs"])):
            print(line)

    engine, clock, cfg = build(root, notify=show)
    holder.update(engine=engine, cfg=cfg)
    store = engine.store
    print("lead-engine demo: fictional businesses served from local HTML. No keys, no network, nothing sent.\n")
    for minutes in (0, 31, 61):
        clock.advance(minutes)
        print("Run at %s" % clock().strftime("%H:%M"))
        try:
            engine.tick()
        except SimulatedCrash:
            lead = store.leased()[0]
            print("  %-32s CRASH     the run stopped mid-step while writing its opener (simulated); the lease"
                  " stays held" % ("#%d %s" % (lead["id"], lead["company"]))[:32])
    clock.advance(3)
    imported = import_stranded(engine, DATA / "stranded.csv")
    result = requalify(engine)
    retired_here = sum(1 for r in result["leads"] if r["was"] == "retired")
    print("Backlog at %s: %d stranded lead(s) re-checked (%d imported from another tool, %d retired here)"
          % (clock().strftime("%H:%M"), result["rechecked"], imported, retired_here))
    for r in result["leads"]:
        then = " -> %s" % r["then"] if r.get("then") else ""
        print("  %-32s %-12s %s%s" % (("#%d %s" % (r["id"], r["company"]))[:32], r["result"], r["detail"][:100], then))
    csv_path = cfg.out_dir / "ready.csv"
    exported = export_csv(store, csv_path)
    hand = run_handoff(store, cfg["handoff"], suppressions=engine.suppressions, out_dir=cfg.out_dir / "handoff",
                       campaign_id="")
    print("\nCampaign-ready openers, each built from a quoted fact on the business's own site:")
    for lead in store.by_stage(("ready",)):
        op = lead["data"]["opener"]
        print('  %s: "%s"' % (lead["company"], op["text"]))
        print('      evidence: "%s" (%s)' % (op["quote"][:100], op["source_url"]))
    counts = store.counts()
    print("\nTotals: %d ready, %d dropped, %d retired, %d leads in all." % (
        counts["ready"], counts["dropped"], counts["retired"], sum(counts.values())))
    print("Export: %d lead(s) with every fact's source URL and quote in %s" % (exported, csv_path))
    print("Hand-off dry run: would add %d lead(s); the exact requests are in %s"
          % (hand.get("would_add", 0), hand.get("file", "-")))
    (root / "lead-engine.toml").write_text(
        "# Points `lead-engine status` and `lead-engine export` at the demo's state. Not for real runs.\n"
        "[handoff]\nmode = \"off\"\n")
    print("Inspect it: lead-engine status --config %s" % (root / "lead-engine.toml"))
    store.close()
    return 0

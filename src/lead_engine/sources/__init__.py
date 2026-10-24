"""Discovery: turn (market, keyword) searches or a CSV into new `discovered` leads.

A search cell is one market and one keyword ("bookkeeping firm" in "Cedar Hollow, OR"). Cells that were
never searched go first, then the ones searched longest ago once `refresh_days` have passed. Results are
filtered before they become leads: a real website is required, directories, social profiles and booking
platforms are skipped, so are domains already known or suppressed, and a domain that shows up for more
than `max_locations_per_domain` places in one search is treated as a chain rather than an owner-run
business.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..fetch import host_of
from ..gate import Suppressions, is_suppressed
from ..store import Store, iso

NOT_BUSINESS_SITES = {
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "tiktok.com", "youtube.com",
    "yelp.com", "google.com", "goo.gl", "linktr.ee", "linkin.bio", "sites.google.com", "business.site",
    "wixsite.com", "squarespace.com", "godaddysites.com", "square.site", "github.io", "thumbtack.com",
    "angi.com", "homeadvisor.com", "houzz.com", "nextdoor.com", "bbb.org", "yellowpages.com", "groupon.com",
    "vagaro.com", "booksy.com", "mindbodyonline.com", "calendly.com", "acuityscheduling.com",
}


@dataclass
class Candidate:
    company: str
    website: str
    market: str = ""
    keyword: str = ""
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Source(Protocol):
    name: str

    def search(self, keyword: str, market: str) -> list[Candidate]: ...


def not_a_business_site(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in NOT_BUSINESS_SITES)


def next_cells(store: Store, markets: list[str], keywords: list[str], count: int, refresh_days: float,
               now: dt.datetime) -> list[tuple[str, str]]:
    searched = store.cells_searched()
    cutoff = iso(now - dt.timedelta(days=refresh_days))
    fresh = [(m, k) for k in keywords for m in markets if (m, k) not in searched]
    due = sorted((searched[(m, k)], (m, k)) for k in keywords for m in markets
                  if (m, k) in searched and searched[(m, k)] < cutoff)
    return (fresh + [cell for _, cell in due])[:count]


def admit_candidates(store: Store, candidates: list[Candidate], *, suppressions: Suppressions,
                     exclude: set[str], max_locations: int) -> tuple[list[int], Counter]:
    """Add the candidates that pass the filters. Returns the new lead ids and a count of skip reasons."""
    skipped: Counter = Counter()
    per_domain = Counter(host_of(c.website) for c in candidates if c.website.startswith(("http://", "https://")))
    known = store.known_domains()
    added: list[int] = []
    for c in candidates:
        host = host_of(c.website)
        if not c.website.startswith(("http://", "https://")) or not host:
            skipped["no_website"] += 1
        elif not_a_business_site(host) or host in exclude:
            skipped["not_a_business_site"] += 1
        elif max_locations and per_domain[host] > max_locations:
            skipped["likely_chain"] += 1
        elif host in known:
            skipped["already_known"] += 1
        elif is_suppressed("", host, suppressions):
            skipped["suppressed"] += 1
        else:
            website = c.website.strip().split("#")[0]
            if "utm_" in website:  # map listings add tracking parameters to the homepage link
                website = website.split("?")[0]
            lead_id = store.add_lead(domain=host, company=c.company.strip() or host, website=website,
                                     market=c.market, keyword=c.keyword, source=c.source,
                                     data={"discovery": c.extra} if c.extra else None)
            if lead_id:
                known.add(host)
                added.append(lead_id)
    return added, skipped


class Discovery:
    """Callable used by the engine when the queue runs low."""

    def __init__(self, settings: dict[str, Any], source: Source | None, suppressions: Suppressions,
                 clock: Callable[[], dt.datetime], csv_rows: Callable[[], list[Candidate]] | None = None):
        self.s = settings
        self.source = source
        self.suppressions = suppressions
        self.clock = clock
        self.csv_rows = csv_rows

    def __call__(self, store: Store) -> dict[str, Any]:
        exclude = {d.lower().removeprefix("www.") for d in self.s.get("exclude_domains", [])}
        max_loc = int(self.s.get("max_locations_per_domain", 2))
        result: dict[str, Any] = {"added": 0, "searches": [], "skipped": Counter()}
        if self.csv_rows is not None:
            added, skipped = admit_candidates(store, self.csv_rows(), suppressions=self.suppressions,
                                              exclude=exclude, max_locations=0)
            result["added"] += len(added)
            result["skipped"].update(skipped)
            return result
        if self.source is None:
            return result
        cells = next_cells(store, list(self.s["markets"]), list(self.s["keywords"]), int(self.s["cells_per_tick"]),
                           float(self.s["refresh_days"]), self.clock())
        for market, keyword in cells:
            found = self.source.search(keyword, market)
            added, skipped = admit_candidates(store, found, suppressions=self.suppressions, exclude=exclude,
                                              max_locations=max_loc)
            store.record_cell(market, keyword, len(added))
            result["added"] += len(added)
            result["skipped"].update(skipped)
            result["searches"].append({"market": market, "keyword": keyword, "results": len(found),
                                       "added": len(added)})
        return result

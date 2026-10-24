"""Discovery: Google Places Text Search behind a fake transport, CSV import, and the candidate filters."""

import json

import pytest
from helpers import Clock

from lead_engine.gate import Suppressions
from lead_engine.http import HttpError
from lead_engine.sources import Candidate, Discovery, admit_candidates, next_cells
from lead_engine.sources.csv_import import read_candidates
from lead_engine.sources.places import FIELDS, URL, PlacesSource
from lead_engine.store import Store

KEY = "test-places-key"


class FakePlaces:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, json.loads(body)))
        if not self.pages:
            return 403, json.dumps({"error": {"message": "quota"}}).encode()
        return 200, json.dumps(self.pages.pop(0)).encode()


def place(name, site, status="OPERATIONAL"):
    p = {"id": "p-" + name[:3].lower(), "displayName": {"text": name}, "businessStatus": status,
         "formattedAddress": "1 Main St"}
    if site:
        p["websiteUri"] = site
    return p


def test_text_search_request_and_parsing():
    fake = FakePlaces([{"places": [place("Harbor Ledger Co.", "https://harbor-ledger.example.com/"),
                                   place("Closed Books", "https://closed.example.com/", "CLOSED_PERMANENTLY"),
                                   place("No Site Studio", None)]}])
    found = PlacesSource(KEY, transport=fake).search("bookkeeping firm", "Cedar Hollow, OR")
    assert [(c.company, c.website, c.market, c.keyword) for c in found] == [
        ("Harbor Ledger Co.", "https://harbor-ledger.example.com/", "Cedar Hollow, OR", "bookkeeping firm")]
    method, url, headers, body = fake.calls[0]
    assert (method, url) == ("POST", URL) and KEY not in url
    assert headers["X-Goog-Api-Key"] == KEY and headers["X-Goog-FieldMask"] == FIELDS
    assert body == {"textQuery": "bookkeeping firm in Cedar Hollow, OR", "pageSize": 20, "regionCode": "US"}


def test_pages_follow_the_next_page_token_up_to_the_limit():
    fake = FakePlaces([{"places": [place("A Co", "https://a.example.com/")], "nextPageToken": "t1"},
                       {"places": [place("B Co", "https://b.example.com/")], "nextPageToken": "t2"},
                       {"places": [place("C Co", "https://c.example.com/")]}])
    found = PlacesSource(KEY, transport=fake, max_pages=2).search("plumber", "Port Alder, WA")
    assert [c.company for c in found] == ["A Co", "B Co"]
    assert fake.calls[1][3]["pageToken"] == "t1" and len(fake.calls) == 2


def test_errors_are_raised_without_the_key(monkeypatch):
    with pytest.raises(HttpError) as e:
        PlacesSource(KEY, transport=FakePlaces([])).search("plumber", "Port Alder, WA")
    assert KEY not in str(e.value) and "HTTP 403" in str(e.value)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_PLACES_API_KEY"):
        PlacesSource()


def test_candidate_filters(tmp_path):
    store = Store(tmp_path / "s.db", clock=Clock())
    store.add_lead(domain="known.example.com", company="Known", website="https://known.example.com/")
    cands = [Candidate("Good Co", "https://www.good.example.com/"),
             Candidate("Profile", "https://www.facebook.com/someprofile"),
             Candidate("Known", "https://known.example.com/"),
             Candidate("Blocked", "https://blocked.example.org/"),
             Candidate("Mute", "https://mute.example.net/"),
             Candidate("No Site", "")] + [Candidate("Chain %d" % i, "https://chain.example.com/") for i in range(3)]
    added, skipped = admit_candidates(store, cands, suppressions=Suppressions(domains=frozenset({"mute.example.net"})),
                                      exclude={"blocked.example.org"}, max_locations=2)
    assert len(added) == 1 and store.get(added[0])["domain"] == "good.example.com"
    tracked, _ = admit_candidates(store, [Candidate("Tracked", "https://tracked.example.com/?utm_source=maps")],
                                  suppressions=Suppressions(), exclude=set(), max_locations=2)
    assert store.get(tracked[0])["website"] == "https://tracked.example.com/"
    assert dict(skipped) == {"not_a_business_site": 2, "already_known": 1, "suppressed": 1, "no_website": 1,
                             "likely_chain": 3}


def test_cells_go_unsearched_first_then_oldest_after_refresh(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "s.db", clock=clock)
    markets, keywords = ["A", "B"], ["k1", "k2"]
    assert next_cells(store, markets, keywords, 3, 90, clock()) == [("A", "k1"), ("B", "k1"), ("A", "k2")]
    for cell in [("A", "k1"), ("B", "k1"), ("A", "k2"), ("B", "k2")]:
        store.record_cell(*cell, found=1)
        clock.advance(1)
    assert next_cells(store, markets, keywords, 2, 90, clock()) == []
    clock.advance(91 * 24 * 60)
    assert next_cells(store, markets, keywords, 2, 90, clock()) == [("A", "k1"), ("B", "k1")]


def test_discovery_searches_cells_and_records_them(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "s.db", clock=clock)

    class Source:
        name = "fake"

        def search(self, keyword, market):
            return [Candidate("%s %s" % (keyword, market), "https://%s-%s.example.com/" % (keyword, market.lower()),
                              market, keyword)]

    d = Discovery({"markets": ["a"], "keywords": ["x", "y"], "cells_per_tick": 5, "refresh_days": 90}, Source(),
                  Suppressions(), clock)
    result = d(store)
    assert result["added"] == 2 and len(result["searches"]) == 2
    assert d(store)["added"] == 0  # both cells searched; nothing due until refresh_days pass


def test_csv_import(tmp_path):
    p = tmp_path / "import.csv"
    p.write_text("company,website,market,keyword\nHarbor Ledger Co.,harbor-ledger.example.com,\"Cedar Hollow, OR\","
                 "bookkeeping firm\n")
    [c] = read_candidates(p)
    assert (c.company, c.website, c.market) == ("Harbor Ledger Co.", "https://harbor-ledger.example.com",
                                                "Cedar Hollow, OR")
    store = Store(tmp_path / "s.db", clock=Clock())
    d = Discovery({}, None, Suppressions(), Clock(), csv_rows=lambda: read_candidates(p))
    assert d(store)["added"] == 1 and d(store)["added"] == 0
    bad = tmp_path / "bad.csv"
    bad.write_text("name,url\nx,y\n")
    with pytest.raises(ValueError, match="company, website"):
        read_candidates(bad)

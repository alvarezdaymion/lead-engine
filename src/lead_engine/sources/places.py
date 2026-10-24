"""Google Places API (New), Text Search: businesses for a keyword in a market.

Env:
  GOOGLE_PLACES_API_KEY   an API key restricted to the Places API (New)

Request: POST https://places.googleapis.com/v1/places:searchText with the key in X-Goog-Api-Key (never in
the URL) and a field mask asking only for what discovery uses. Asking for `places.websiteUri` makes each
request bill at the Text Search Enterprise rate; see Google's pricing page. Up to 20 results per page and
`max_pages` pages per search (Google returns at most 60 results per query). Places that aren't
OPERATIONAL or have no website are skipped.
"""

from __future__ import annotations

import os

from ..http import Http, Transport
from . import Candidate

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = "places.id,places.displayName,places.websiteUri,places.formattedAddress,places.businessStatus,nextPageToken"


class PlacesSource:
    name = "places"

    def __init__(self, api_key: str | None = None, transport: Transport | None = None, max_pages: int = 1,
                 region_code: str = "US", *, allow_paid: bool = False):
        self.allow_paid = allow_paid
        self.key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")
        if not self.key:
            raise RuntimeError("set GOOGLE_PLACES_API_KEY")
        self.http = Http(transport=transport, timeout=30)
        self.max_pages = max(1, min(3, int(max_pages)))
        self.region_code = region_code

    def search(self, keyword: str, market: str) -> list[Candidate]:
        if self.allow_paid is not True:
            raise PermissionError("paid Places discovery requires discovery.allow_paid = true")
        body: dict[str, object] = {"textQuery": "%s in %s" % (keyword, market), "pageSize": 20}
        if self.region_code:
            body["regionCode"] = self.region_code
        headers = {"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": FIELDS}
        out: list[Candidate] = []
        for _ in range(self.max_pages):
            resp = self.http.post_json(URL, body, headers)
            for place in resp.get("places", []):
                if place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL" or not place.get("websiteUri"):
                    continue
                out.append(Candidate(
                    company=(place.get("displayName") or {}).get("text", ""),
                    website=place["websiteUri"],
                    market=market,
                    keyword=keyword,
                    source="places",
                    extra={"place_id": place.get("id", ""), "address": place.get("formattedAddress", "")},
                ))
            token = resp.get("nextPageToken")
            if not token:
                break
            body["pageToken"] = token
        return out

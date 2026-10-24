"""Instantly API v2: the email platform the hand-off, the seed test and the warmup audit talk to.

Env:
  INSTANTLY_API_KEY       an API key with leads:create (hand-off), accounts:read (warmup audit) and
                          emails:create (seed test only)
  INSTANTLY_CAMPAIGN_ID   the campaign that receives campaign-ready leads

Reads: GET /api/v2/accounts (sending inboxes and their warmup settings).
Writes: exactly two. POST /api/v2/leads/add (add leads to the campaign; the platform does the sending on
its own schedule) and POST /api/v2/emails/test (one test email, used only by the seed test). `_post`
refuses any other path before a request is made, so this tool cannot launch, pause or edit a campaign,
change an account or send to a prospect.
"""

from __future__ import annotations

import os
from typing import Any

from .errors import InputError, OperationError
from .http import Http, HttpError, Transport

API = "https://api.instantly.ai"
ALLOWED_POSTS = ("/api/v2/leads/add", "/api/v2/emails/test")
MAX_PAGES = 50


def leads_add_body(campaign_id: str, leads: list[dict[str, Any]]) -> dict[str, Any]:
    """The request body for adding leads to a campaign. The dry run writes exactly this."""
    return {
        "campaign_id": campaign_id,
        "leads": leads,
        "skip_if_in_workspace": True,  # never add someone who is already in any campaign or list
        "verify_leads_on_import": False,
    }


class InstantlyClient:
    def __init__(self, api_key: str | None = None, transport: Transport | None = None):
        key = api_key or os.environ.get("INSTANTLY_API_KEY", "")
        if not key:
            raise OperationError("set INSTANTLY_API_KEY")
        self.http = Http({"Authorization": "Bearer " + key}, transport, timeout=60)

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        """The only write path."""
        if path not in ALLOWED_POSTS:
            raise PermissionError("refused write: only adding leads and sending a seed test are allowed")
        return self.http.post_json(API + path, body)

    def add_leads(self, campaign_id: str, leads: list[dict[str, Any]]) -> dict[str, Any]:
        if not campaign_id:
            raise InputError("a campaign id is required")
        if not 1 <= len(leads) <= 1000:
            raise InputError("add between 1 and 1000 leads per request")
        return self._post("/api/v2/leads/add", leads_add_body(campaign_id, leads))

    def send_test_email(self, eaccount: str, to: list[str], subject: str, html: str) -> dict[str, Any]:
        body = {"eaccount": eaccount, "to_address_email_list": ",".join(to), "subject": subject,
                "body": {"html": html}}
        return self._post("/api/v2/emails/test", body)

    def accounts(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": 100}
        for _ in range(MAX_PAGES):
            page = self.http.get(API + "/api/v2/accounts", params)
            if (not isinstance(page, dict) or not isinstance(page.get("items"), list)
                    or any(not isinstance(item, dict) for item in page["items"])):
                raise HttpError("incomplete account list response")
            items.extend(page.get("items", []))
            after = page.get("next_starting_after")
            if not after:
                break
            params = {"limit": 100, "starting_after": after}
        else:
            raise HttpError("incomplete account list: pagination limit reached")
        return items

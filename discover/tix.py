"""Tix Africa GraphQL — discovery events (ticketed organizers)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("ig.tix")

_GQL = "https://core.tix.africa/graphql"
_QUERY = """
query fetchDiscoveryEvents(
  $keyword: String
  $page: Int
  $per: Int
  $country: SupportedCountries
) {
  fetchDiscoveryEvents(
    keyword: $keyword
    page: $page
    per: $per
    country: $country
  ) {
    events {
      edges {
        node {
          id
          slug
          title
          customName
          address
          locationName
          country
          startDate
          repeats
          eventType
          headerImage
          discoveryImage
          currency
          tickets {
            edges {
              node {
                id
                price
                priceWithFees
                status
                inviteOnly
              }
            }
          }
        }
      }
    }
  }
}
"""


_DETAIL_QUERY = """
query fetchEventBySlug($slug: String!) {
  fetchEventBySlug(slug: $slug) {
    id
    slug
    title
    description
    address
    locationName
    startDate
    endDate
    repeats
    currency
    timezone
    headerImage
    user {
      id
      firstName
      lastName
      displayName
      username
      bio
      website
      email
      phone
      country
    }
  }
}
"""

_TAG_STRIP = re.compile(r"<[^>]+>")


def fetch_tix_event_detail(slug: str) -> dict[str, Any] | None:
    """
    Full event record for one slug.

    The discovery feed's ``DiscoveryEvent`` is a thin projection with no
    description, end date, or creator. ``fetchEventBySlug`` returns the real
    ``Event``, which carries all three — the only route to the Experience
    fields the feed cannot answer.
    """
    if not (slug or "").strip():
        return None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.tix.africa",
        "Referer": f"https://www.tix.africa/{slug}",
        "User-Agent": "validds-instagram-ingest/1.0 (event discovery)",
    }
    try:
        with httpx.Client(timeout=30.0, headers=headers) as client:
            resp = client.post(
                _GQL,
                json={
                    "operationName": "fetchEventBySlug",
                    "query": _DETAIL_QUERY,
                    "variables": {"slug": slug},
                },
            )
        if resp.status_code >= 400:
            log.debug("[tix] detail HTTP %s for %s", resp.status_code, slug)
            return None
        data = resp.json()
        if data.get("errors"):
            log.debug("[tix] detail errors for %s: %s", slug, data["errors"][:1])
            return None
        node = (data.get("data") or {}).get("fetchEventBySlug")
    except Exception as exc:  # noqa: BLE001
        log.debug("[tix] detail failed for %s: %s", slug, exc)
        return None
    if not isinstance(node, dict):
        return None

    # Descriptions are stored as HTML fragments.
    raw_desc = node.get("description") or ""
    text = _TAG_STRIP.sub(" ", raw_desc)
    text = re.sub(r"\s+", " ", text).replace("&amp;", "&").replace("&nbsp;", " ").strip()

    ends = node.get("endDate")
    ends_at = (
        datetime.fromtimestamp(ends, tz=timezone.utc)
        if isinstance(ends, (int, float)) and ends > 0
        else None
    )
    user = node.get("user") or {}
    person = " ".join(
        part for part in [user.get("firstName"), user.get("lastName")] if part
    ).strip()
    # displayName is the brand ('Euphoria Restaurant'); first/last is the human
    # who owns the account and is only a fallback.
    organizer = (user.get("displayName") or "").strip() or person or None

    return {
        "description": text or None,
        "endsAt": ends_at,
        "timezone": node.get("timezone"),
        "organizerId": user.get("id"),
        "organizerName": organizer,
        "organizer": {
            "id": user.get("id"),
            "name": organizer,
            "personName": person or None,
            "username": (user.get("username") or "").strip() or None,
            "bio": (user.get("bio") or "").strip() or None,
            "website": (user.get("website") or "").strip() or None,
            "email": (user.get("email") or "").strip() or None,
            "phone": (user.get("phone") or "").strip() or None,
            "country": user.get("country"),
        } if user.get("id") else None,
    }


def fetch_tix_events(
    *,
    country: str = "NG",
    keyword: str | None = None,
    limit: int = 100,
    per_page: int = 20,
    lagos_only: bool = True,
) -> list[dict[str, Any]]:
    """
    Public discovery feed. ``startDate`` is a unix timestamp (seconds).
    """
    events: list[dict[str, Any]] = []
    page = 1
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.tix.africa",
        "Referer": "https://www.tix.africa/discover",
        "User-Agent": "validds-instagram-ingest/1.0 (event discovery)",
    }
    lagos_tokens = (
        "lagos",
        "lekki",
        "ikoyi",
        "yaba",
        "vi,",
        "victoria island",
        "ikeja",
        "surulere",
        "ajah",
        "oniru",
        "marwa",
        "banana island",
    )
    with httpx.Client(timeout=45.0, headers=headers) as client:
        while len(events) < limit and page <= 25:
            body = {
                "operationName": "fetchDiscoveryEvents",
                "query": _QUERY,
                "variables": {
                    "keyword": keyword,
                    "page": page,
                    "per": per_page,
                    "country": country,
                },
            }
            resp = client.post(_GQL, json=body)
            if resp.status_code >= 400:
                log.warning("[tix] HTTP %s page=%s", resp.status_code, page)
                break
            data = resp.json()
            if data.get("errors"):
                log.warning("[tix] graphql errors: %s", data["errors"][:2])
                break
            edges = (
                ((data.get("data") or {}).get("fetchDiscoveryEvents") or {})
                .get("events", {})
                .get("edges")
                or []
            )
            if not edges:
                break
            for edge in edges:
                node = (edge or {}).get("node") or {}
                title = (node.get("title") or node.get("customName") or "").strip()
                eid = node.get("id") or node.get("slug")
                if not title or not eid:
                    continue
                blob = " ".join(
                    str(node.get(k) or "")
                    for k in ("title", "address", "locationName", "slug")
                ).lower()
                if lagos_only and not any(tok in blob for tok in lagos_tokens):
                    continue
                ts = node.get("startDate")
                starts = None
                if isinstance(ts, (int, float)) and ts > 0:
                    starts = datetime.fromtimestamp(ts, tz=timezone.utc)
                slug = node.get("slug")
                # Active, publicly-sellable tiers only — invite-only rows are
                # not price points a browsing user can act on.
                tiers = []
                for t_edge in ((node.get("tickets") or {}).get("edges") or []):
                    t = (t_edge or {}).get("node") or {}
                    if t.get("status") != "active" or t.get("inviteOnly"):
                        continue
                    price = t.get("priceWithFees")
                    if price is None:
                        price = t.get("price")
                    if price is None:
                        continue
                    # DiscoveryTicket carries no tier name — only the amount.
                    tiers.append({"name": None, "price": float(price)})
                events.append(
                    {
                        "_id": f"tix:{eid}",
                        "source": "tix",
                        "sourceId": str(eid),
                        "name": title,
                        "city": "Lagos",
                        "country": "Nigeria",
                        "address": node.get("address") or node.get("locationName"),
                        "locationName": node.get("locationName"),
                        "startsAt": starts,
                        "imageUrl": node.get("discoveryImage") or node.get("headerImage"),
                        "url": f"https://www.tix.africa/{slug}" if slug else None,
                        "slug": slug,
                        "eventType": node.get("eventType"),
                        "currency": node.get("currency"),
                        "repeats": node.get("repeats"),
                        "tickets": tiers,
                        "organizerType": "organizer",
                    }
                )
                if len(events) >= limit:
                    break
            if len(edges) < per_page:
                break
            page += 1

    log.info("[tix] events → %d (keyword=%r lagos_only=%s)", len(events), keyword, lagos_only)
    return events[:limit]

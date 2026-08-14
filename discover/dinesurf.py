"""
DineSurf venue lookup — enrichment only, not discovery.

`app.dinesurf.com/restaurants/<slug>` server-renders a 110-field venue object
into `__NEXT_DATA__`. The slug is the venue name slugified, so a venue we
already know can be looked up directly without enumerating the site.

Enumeration is not possible anyway: their `/api/restaurants` returns a 500 from
a `type` column missing in their own schema, and the sitemap is a two-URL stub.
`robots.txt` explicitly allows `/restaurants/`.

Read via the rendered page rather than `_next/data/<buildId>/…`. Both work, but
the buildId changes on every deploy and hardcoding it would break silently.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("ig.dinesurf")

_BASE = "https://app.dinesurf.com/restaurants"
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
# Their own placeholder for venues that have not claimed their listing.
_UNCLAIMED = "unclaimed@dinesurf.com"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def normalise_phone(raw: Any) -> str | None:
    """
    DineSurf stores WhatsApp numbers in four different shapes.

    Seen in one sample of five venues: '9132739111', '2349013133300',
    '+2348181777666', '09085614815'. All are the same country.
    """
    digits = re.sub(r"[^\d]", "", str(raw or ""))
    if not digits:
        return None
    if digits.startswith("234"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    # Nigerian subscriber numbers are 10 digits after the country code.
    if len(digits) != 10:
        return None
    return f"+234{digits}"


def open_days(days: list[dict[str, Any]] | None) -> list[str]:
    """
    Which days the venue opens. Not opening *times*.

    `days[]` holds only day references ({id, name, ...}) with no clock fields,
    and the venue-level open_time/close_time were null on every venue checked.
    So DineSurf cannot fill `openingTimes`; Google remains the source for that.
    """
    out: list[str] = []
    for row in days or []:
        name = (row.get("name") or "").strip().title() if isinstance(row, dict) else ""
        if name:
            out.append(name)
    return out


def fetch_venue(
    name: str,
    *,
    slug: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """One venue, normalised to the shape the mapper's providers use."""
    target = slug or slugify(name)
    if not target:
        return None
    own = client is None
    if own:
        client = httpx.Client(timeout=30.0, follow_redirects=True)
    assert client is not None
    try:
        resp = client.get(
            f"{_BASE}/{target}",
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
        )
        if resp.status_code != 200:
            return None
        match = _NEXT_DATA.search(resp.text)
        if not match:
            return None
        payload = json.loads(match.group(1))
        venue = ((payload.get("props") or {}).get("pageProps") or {}).get("restaurant")
        if not isinstance(venue, dict) or not venue.get("company_name"):
            return None
    except Exception as exc:  # noqa: BLE001
        log.debug("[dinesurf] %s failed: %s", target, exc)
        return None
    finally:
        if own:
            client.close()

    email = (venue.get("email") or venue.get("company_email") or "").strip()
    if email == _UNCLAIMED:
        email = ""

    # Each gallery row carries both `path` (relative) and `url` (absolute S3).
    # Only `url` is usable; `path` matched first here and silently emptied the
    # list down to just the banner.
    gallery = [
        img.get("url")
        for img in (venue.get("gallery") or [])
        if isinstance(img, dict)
        and img.get("type") == "image"
        and str(img.get("url") or "").startswith("http")
    ]
    if venue.get("banner"):
        gallery.insert(0, venue["banner"])

    avg = venue.get("average_menu_price")
    try:
        avg_budget = float(avg) if avg not in (None, "", "0.00") else None
    except (TypeError, ValueError):
        avg_budget = None

    return {
        "_id": f"dinesurf:{venue.get('id')}",
        "source": "dinesurf",
        "sourceId": str(venue.get("id") or target),
        "name": (venue.get("company_name") or name).strip(),
        "city": "Lagos",
        "country": "Nigeria",
        "slug": venue.get("slug") or target,
        "address": (venue.get("company_address") or "").strip() or None,
        "description": (venue.get("description") or "").strip() or None,
        "phone": venue.get("company_phone") or venue.get("phone_number") or None,
        "email": email or None,
        "website": (venue.get("website_url") or "").strip() or None,
        "whatsApp": normalise_phone(
            venue.get("whatsapp_number") or venue.get("extra_whatsapp")
        ),
        "hours": None,  # see open_days: DineSurf carries no clock times
        "openDays": open_days(venue.get("days")),
        "cuisines": [
            c.get("name") for c in (venue.get("cuisines") or [])
            if isinstance(c, dict) and c.get("name")
        ],
        # Venue photographs, free and unmetered, unlike Google's per-photo cost.
        "photos": gallery[:8],
        "rating": venue.get("overall_rating"),
        "ratingCount": venue.get("total_reviews"),
        "avgBudget": avg_budget,
        # These columns exist in their schema but were empty on every venue
        # checked. Carried through in case that changes.
        "dressCode": venue.get("dress_code") or None,
        "seatingPreferences": [
            s.get("name") for s in (venue.get("seating_preferences") or [])
            if isinstance(s, dict) and s.get("name")
        ],
        "menuUrl": (venue.get("menu_url") or "").strip() or None,
        "instagramHint": None,
    }


def fetch_many(
    names: list[str], *, client: httpx.Client | None = None
) -> dict[str, dict[str, Any]]:
    """Look up several venues, keyed by the name asked for."""
    own = client is None
    if own:
        client = httpx.Client(timeout=30.0, follow_redirects=True)
    assert client is not None
    found: dict[str, dict[str, Any]] = {}
    try:
        for name in names:
            venue = fetch_venue(name, client=client)
            if venue:
                found[name] = venue
    finally:
        if own:
            client.close()
    log.info("[dinesurf] %d/%d venues resolved", len(found), len(names))
    return found

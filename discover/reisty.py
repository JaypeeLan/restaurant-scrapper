"""Reisty guest API — restaurants + restaurant events."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from config import settings
from discover.places import CITY_PRESETS, extract_instagram_handle

log = logging.getLogger("ig.reisty")

_BASE = "https://reistyapp-production.up.railway.app/api/3.0"


def _headers() -> dict[str, str]:
    key = (settings.REISTY_API_KEY or "").strip()
    if not key:
        raise RuntimeError("REISTY_API_KEY is not set")
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "validds-instagram-ingest/1.0 (venue discovery)",
        "Origin": "https://www.reisty.com",
        "Referer": "https://www.reisty.com/",
        "apiKey": key,
    }


def _get(client: httpx.Client, path: str) -> dict[str, Any]:
    resp = client.get(f"{_BASE}{path}")
    if resp.status_code >= 400:
        raise RuntimeError(f"reisty {path} HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    data = resp.json()
    if isinstance(data, dict) and data.get("status") is False:
        raise RuntimeError(f"reisty {path}: {data.get('error_message') or data}")
    return data if isinstance(data, dict) else {"result": data}


def _lagos_only(state: str | None, city_key: str) -> bool:
    if city_key.lower() != "lagos":
        return True
    return not state or "lagos" in state.lower()


def list_restaurant_ids(*, limit: int = 300) -> list[dict[str, Any]]:
    """Thin search index (~69 venues) — Id/Name/State only."""
    with httpx.Client(timeout=60.0, headers=_headers()) as client:
        data = _get(client, "/guestservice/search_restaurants?Text=")
    result = data.get("result") or {}
    items = result.get("Items") or []
    out = []
    for row in items:
        rid = row.get("Id")
        name = (row.get("Name") or "").strip()
        if not rid or not name:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def about_restaurant(restaurant_id: str, *, client: httpx.Client | None = None) -> dict[str, Any] | None:
    own = client is None
    if own:
        client = httpx.Client(timeout=45.0, headers=_headers())
    assert client is not None
    try:
        data = _get(client, f"/guestservice/about_restaurant/{restaurant_id}")
        rows = data.get("result") or []
        if isinstance(rows, list) and rows:
            return rows[0] if isinstance(rows[0], dict) else None
        return rows if isinstance(rows, dict) else None
    finally:
        if own:
            client.close()


def search_reisty(
    city_key: str = "lagos",
    *,
    limit: int = 100,
    detail: bool = True,
) -> list[dict[str, Any]]:
    """
    Reisty directory → optional ``about_restaurant`` enrich.

    Detail pass is what yields email / phone / website (often IG) / hours /
    address / menu link — the list endpoints leave those null.
    """
    preset = CITY_PRESETS.get(city_key.lower())
    if not preset:
        raise ValueError(f"unknown city {city_key!r}")

    index = list_restaurant_ids(limit=max(limit * 2, limit))
    places: list[dict[str, Any]] = []

    with httpx.Client(timeout=45.0, headers=_headers()) as client:
        for row in index:
            if len(places) >= limit:
                break
            if not _lagos_only(row.get("State"), city_key):
                continue
            rid = str(row["Id"])
            name = (row.get("Name") or "").strip()
            detail_row: dict[str, Any] | None = None
            if detail:
                try:
                    detail_row = about_restaurant(rid, client=client)
                except Exception as exc:  # noqa: BLE001
                    log.debug("[reisty] about %s failed: %s", rid, exc)

            src = detail_row or {}
            website_raw = (src.get("Restaurantwebsite") or "").strip()
            website = website_raw if website_raw and website_raw not in {"_", "-"} else None
            if website and "reisty.com" in website.lower() and "instagram.com" not in website.lower():
                # Placeholder / booking deep-link, not the venue site.
                website = None
            phone = src.get("PhoneNumaber") or src.get("PhoneNumber") or None
            email = (src.get("EmailAddress") or "").strip() or None
            address = src.get("Address") or None
            menu_raw = (src.get("MenuLink") or "").strip() or None
            menu_url = None
            if menu_raw and "reisty.com" not in menu_raw.lower():
                menu_url = menu_raw if menu_raw.startswith("http") else f"https://{menu_raw}"
            # Reisty swaps lat/lng on some rows — keep both, prefer plausible Lagos.
            lat = src.get("Longitude")
            lng = src.get("Latitude")
            try:
                lat_f = float(lat) if lat is not None else None
                lng_f = float(lng) if lng is not None else None
            except (TypeError, ValueError):
                lat_f = lng_f = None
            # Reisty populates Latitude/Longitude as 0.0 on every row today.
            # Null island is not a location — drop it so downstream geocoding
            # can tell "unknown" from "off the coast of Ghana".
            if lat_f is not None and lng_f is not None:
                if abs(lat_f) < 0.01 and abs(lng_f) < 0.01:
                    lat_f = lng_f = None
                # Lagos is ~6.4N, 3.3E. If values look swapped, fix.
                elif 2.5 <= lat_f <= 4.5 and 6.0 <= lng_f <= 7.0:
                    lat_f, lng_f = lng_f, lat_f

            places.append(
                {
                    "_id": f"reisty:{rid}",
                    "source": "reisty",
                    "sourceId": rid,
                    "name": (src.get("RestaurantName") or name).strip(),
                    "city": preset["name"],
                    "country": preset["country"],
                    "amenity": src.get("RestaurantType")
                    or row.get("RestaurantType")
                    or "restaurant",
                    "address": address,
                    "website": website,
                    "phone": phone,
                    "email": email,
                    "hours": (src.get("OpenFrom") or "").strip() or None,
                    "description": (src.get("Description") or "").strip() or None,
                    "lat": lat_f,
                    "lng": lng_f,
                    "instagramHint": extract_instagram_handle(website),
                    "menuUrl": menu_url,
                    "rating": src.get("ReviewDetails", {}).get("Rating")
                    if isinstance(src.get("ReviewDetails"), dict)
                    else row.get("Rating"),
                    "avgBudget": src.get("AverageCost") or None,
                    "cuisines": src.get("OtherCuisines") or [],
                    "lga": src.get("LGA") or row.get("LGA"),
                    "dressCode": src.get("DressCode"),
                    "parking": src.get("ParkingOption"),
                    "slug": src.get("QuerySlot") or row.get("QuerySlot"),
                }
            )

    log.info("[reisty] %s → %d places (detail=%s)", city_key, len(places), detail)
    return places


def fetch_reisty_events(*, limit: int = 200) -> list[dict[str, Any]]:
    """Live restaurant events from ``/restaurantevent/all``."""
    with httpx.Client(timeout=45.0, headers=_headers()) as client:
        data = _get(client, "/restaurantevent/all")
    rows = data.get("result") or []
    events: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (row.get("EventName") or "").strip()
        date_s = (row.get("EventDate") or "").strip()
        if not name or not date_s:
            continue
        rid = row.get("RestaurantId") or "unknown"
        time_s = (row.get("EventTime") or "").strip()
        start_at = None
        try:
            # EventDate is YYYY-MM-DD; EventTime like "20:00 - 23:00"
            start_clock = time_s.split("-")[0].strip() if time_s else "00:00"
            if len(start_clock) == 5:
                start_clock += ":00"
            start_at = datetime.fromisoformat(f"{date_s}T{start_clock}").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            start_at = None
        events.append(
            {
                "_id": f"reisty:{rid}:{date_s}:{name.lower()[:40]}",
                "source": "reisty",
                "sourceId": f"{rid}:{date_s}:{name}",
                "name": name,
                "city": "Lagos",
                "country": "Nigeria",
                "address": row.get("EventAddress"),
                "startsAt": start_at,
                "timeText": time_s or None,
                "dateText": date_s,
                "imageUrl": row.get("EventImage"),
                "restaurantId": rid,
                "avgBudget": row.get("RestaurantAverageCost"),
                "organizerType": "restaurant",
            }
        )
        if len(events) >= limit:
            break
    log.info("[reisty] events → %d", len(events))
    return events

"""City presets + place discovery backends (OSM free, Google Places optional)."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from config import settings

log = logging.getLogger("ig.places")

# Lagos-first. Expand as needed.
CITY_PRESETS: dict[str, dict[str, Any]] = {
    "lagos": {
        "name": "Lagos",
        "country": "Nigeria",
        "query_suffix": "Lagos Nigeria",
        # south, west, north, east
        "bbox": (6.35, 3.05, 6.72, 3.70),
        "lat": 6.5244,
        "lng": 3.3792,
    },
    "abuja": {
        "name": "Abuja",
        "country": "Nigeria",
        "query_suffix": "Abuja Nigeria",
        "bbox": (8.90, 7.30, 9.20, 7.60),
        "lat": 9.0765,
        "lng": 7.3986,
    },
}

_IG_URL = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?",
    re.I,
)


def extract_instagram_handle(text: str | None) -> str | None:
    if not text:
        return None
    m = _IG_URL.search(text)
    if not m:
        return None
    handle = m.group(1).lower().rstrip(".")
    if handle in {"p", "reel", "reels", "stories", "explore", "accounts"}:
        return None
    return handle


def place_search_query(place: dict[str, Any]) -> str:
    """Query string for Instagram topsearch."""
    name = (place.get("name") or "").strip()
    city = (place.get("city") or "").strip()
    if city and city.lower() not in name.lower():
        return f"{name} {city}".strip()
    return name


_OVERPASS_FALLBACKS = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def search_osm(city_key: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """
    Free venue discovery via OpenStreetMap Overpass.

    No Instagram links usually — resolve handles separately with logged-in search.
    """
    preset = CITY_PRESETS.get(city_key.lower())
    if not preset:
        raise ValueError(f"unknown city {city_key!r}; known: {sorted(CITY_PRESETS)}")

    s, w, n, e = preset["bbox"]
    query = f"""
    [out:json][timeout:90];
    (
      node["amenity"~"restaurant|bar|cafe|fast_food|nightclub|pub|biergarten"]({s},{w},{n},{e});
      way["amenity"~"restaurant|bar|cafe|fast_food|nightclub|pub|biergarten"]({s},{w},{n},{e});
      node["tourism"~"hotel|hostel"]({s},{w},{n},{e});
      way["tourism"~"hotel|hostel"]({s},{w},{n},{e});
    );
    out center tags;
    """
    headers = {
        "Accept": "*/*",
        "User-Agent": "validds-instagram-ingest/1.0 (venue discovery)",
    }
    urls = []
    primary = settings.OVERPASS_URL
    if primary:
        urls.append(primary)
    for u in _OVERPASS_FALLBACKS:
        if u not in urls:
            urls.append(u)

    payload: dict[str, Any] | None = None
    last_err: Exception | None = None
    with httpx.Client(timeout=120.0, headers=headers) as client:
        for url in urls:
            try:
                resp = client.post(url, data={"data": query})
                if resp.status_code >= 400:
                    log.warning("[osm] %s → HTTP %s", url, resp.status_code)
                    continue
                payload = resp.json()
                log.info("[osm] using %s", url)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.warning("[osm] %s failed: %s", url, exc)
    if payload is None:
        raise RuntimeError(f"all Overpass endpoints failed: {last_err}")

    places: list[dict[str, Any]] = []
    for el in payload.get("elements") or []:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name or len(name) < 2:
            continue
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lng = el.get("lon") or (el.get("center") or {}).get("lon")
        website = tags.get("website") or tags.get("contact:website") or tags.get("url")
        ig = (
            extract_instagram_handle(tags.get("contact:instagram"))
            or extract_instagram_handle(website)
        )
        osm_id = f"{el.get('type')}/{el.get('id')}"
        places.append({
            "_id": f"osm:{osm_id}",
            "source": "osm",
            "sourceId": osm_id,
            "name": name,
            "city": preset["name"],
            "country": preset["country"],
            "amenity": tags.get("amenity") or tags.get("tourism"),
            "website": website,
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "lat": lat,
            "lng": lng,
            "instagramHint": ig,
            "rawTags": tags,
        })
        if len(places) >= limit:
            break

    log.info("[osm] %s → %d places", city_key, len(places))
    return places


def search_google_places(city_key: str, *, limit: int = 60) -> list[dict[str, Any]]:
    """
    Google Places API (New) text search. Requires GOOGLE_PLACES_API_KEY.
    """
    key = settings.GOOGLE_PLACES_API_KEY
    if not key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not set")

    preset = CITY_PRESETS.get(city_key.lower())
    if not preset:
        raise ValueError(f"unknown city {city_key!r}")

    types = [
        "restaurant",
        "bar",
        "cafe",
        "night_club",
        "meal_takeaway",
    ]
    places: list[dict[str, Any]] = []
    seen: set[str] = set()

    with httpx.Client(timeout=45.0) as client:
        for place_type in types:
            if len(places) >= limit:
                break
            body = {
                "textQuery": f"{place_type.replace('_', ' ')} in {preset['query_suffix']}",
                "pageSize": min(20, limit - len(places)),
                "locationBias": {
                    "circle": {
                        "center": {
                            "latitude": preset["lat"],
                            "longitude": preset["lng"],
                        },
                        "radius": 25000.0,
                    }
                },
            }
            page_token = None
            pages = 0
            while len(places) < limit and pages < 3:
                if page_token:
                    body["pageToken"] = page_token
                resp = client.post(
                    "https://places.googleapis.com/v1/places:searchText",
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": key,
                        "X-Goog-FieldMask": (
                            "places.id,places.displayName,places.formattedAddress,"
                            "places.websiteUri,places.nationalPhoneNumber,"
                            "places.location,places.types,places.googleMapsUri,"
                            "nextPageToken"
                        ),
                    },
                    json=body,
                )
                if resp.status_code >= 400:
                    log.warning(
                        "[google] places HTTP %s: %s",
                        resp.status_code,
                        (resp.text or "")[:200],
                    )
                    break
                data = resp.json()
                for p in data.get("places") or []:
                    pid = p.get("id") or ""
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    name = ((p.get("displayName") or {}).get("text") or "").strip()
                    if not name:
                        continue
                    website = p.get("websiteUri")
                    loc = p.get("location") or {}
                    places.append({
                        "_id": f"google:{pid}",
                        "source": "google",
                        "sourceId": pid,
                        "name": name,
                        "city": preset["name"],
                        "country": preset["country"],
                        "amenity": place_type,
                        "address": p.get("formattedAddress"),
                        "website": website,
                        "phone": p.get("nationalPhoneNumber"),
                        "lat": loc.get("latitude"),
                        "lng": loc.get("longitude"),
                        "instagramHint": extract_instagram_handle(website),
                        "mapsUri": p.get("googleMapsUri"),
                        "types": p.get("types") or [],
                    })
                    if len(places) >= limit:
                        break
                page_token = data.get("nextPageToken")
                pages += 1
                if not page_token:
                    break

    log.info("[google] %s → %d places", city_key, len(places))
    return places


def discover_places(
    city_key: str,
    *,
    limit: int = 200,
    backend: str | None = None,
) -> list[dict[str, Any]]:
    """
    backend: auto | osm | google
    auto = google if API key set, else osm
    """
    mode = (backend or settings.PLACES_BACKEND or "auto").lower()
    if mode == "auto":
        mode = "google" if settings.GOOGLE_PLACES_API_KEY else "osm"
    if mode == "google":
        return search_google_places(city_key, limit=limit)
    if mode == "osm":
        return search_osm(city_key, limit=limit)
    raise ValueError(f"unknown places backend {mode!r}")

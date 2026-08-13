"""FlavorQueste restaurant directory (public tRPC)."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from discover.places import CITY_PRESETS, extract_instagram_handle

log = logging.getLogger("ig.flavorqueste")

_BASE = "https://flavorqueste.com/api/trpc/restaurants.fetchAll"
_POINT = re.compile(r"POINT\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", re.I)


def _parse_geom(geom: str | None) -> tuple[float | None, float | None]:
    if not geom:
        return None, None
    m = _POINT.search(str(geom))
    if not m:
        return None, None
    # GeoJSON-ish WKT: POINT(lng lat)
    return float(m.group(2)), float(m.group(1))


def _hours_summary(rows: list[dict[str, Any]] | None) -> str | None:
    if not rows:
        return None
    parts: list[str] = []
    for row in rows:
        day = (row.get("dayOfWeek") or "").strip()
        open_t = (row.get("openTime") or "").strip()
        close_t = (row.get("closeTime") or "").strip()
        if day and open_t and close_t:
            parts.append(f"{day[:3]} {open_t}-{close_t}")
    return "; ".join(parts[:7]) if parts else None


def search_flavorqueste(
    city_key: str = "lagos",
    *,
    limit: int = 200,
    max_pages: int = 60,
) -> list[dict[str, Any]]:
    """
    Paginate FlavorQueste ``restaurants.fetchAll``.

    Richer than Google Places text search: description, structured hours,
    categories/tags, ratings, budget, geo. No email field on this API.
    """
    preset = CITY_PRESETS.get(city_key.lower())
    if not preset:
        raise ValueError(f"unknown city {city_key!r}")

    places: list[dict[str, Any]] = []
    page = 1
    with httpx.Client(timeout=45.0, headers={"Accept": "application/json"}) as client:
        while len(places) < limit and page <= max_pages:
            inp = {
                "0": {
                    "json": {
                        "coords": None,
                        "filters": {
                            "query": "",
                            "categories": [],
                            "tags": [],
                            "ambienceRating": None,
                            "foodRating": None,
                            "serviceRating": None,
                            "valueRating": None,
                            "overallRating": None,
                            "radius": None,
                        },
                        "page": page,
                        "sortBy": "top_rated",
                    }
                }
            }
            # Minimal JSON for tRPC batch input (no spaces).
            import json as _json

            url = f"{_BASE}?batch=1&input={quote(_json.dumps(inp, separators=(',', ':')))}"
            resp = client.get(url)
            if resp.status_code >= 400:
                log.warning("[flavorqueste] HTTP %s page=%s", resp.status_code, page)
                break
            batch = resp.json()
            try:
                payload = batch[0]["result"]["data"]["json"]
            except (KeyError, IndexError, TypeError) as exc:
                log.warning("[flavorqueste] unexpected payload: %s", exc)
                break
            rows = payload.get("restaurants") or []
            if not rows:
                break
            for row in rows:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                rid = row.get("id") or row.get("slug")
                if not rid:
                    continue
                website = (row.get("website") or "").strip() or None
                lat, lng = _parse_geom(row.get("geom"))
                cats = [
                    c.get("name")
                    for c in (row.get("categories") or [])
                    if isinstance(c, dict) and c.get("name")
                ]
                tags = [
                    t.get("name")
                    for t in (row.get("tags") or [])
                    if isinstance(t, dict) and t.get("name")
                ]
                places.append(
                    {
                        "_id": f"fq:{rid}",
                        "source": "flavorqueste",
                        "sourceId": str(rid),
                        "name": name,
                        "city": preset["name"],
                        "country": preset["country"],
                        "amenity": (row.get("primaryCategory") or {}).get("name")
                        if isinstance(row.get("primaryCategory"), dict)
                        else (cats[0] if cats else "restaurant"),
                        "address": row.get("address"),
                        "website": website,
                        "phone": row.get("phone") or None,
                        "email": None,
                        "hours": _hours_summary(row.get("businessHours")),
                        "description": (row.get("description") or "").strip() or None,
                        "lat": lat,
                        "lng": lng,
                        "instagramHint": extract_instagram_handle(website),
                        "categories": cats,
                        "tags": tags,
                        "rating": float(row["avg_overall_rating"])
                        if row.get("avg_overall_rating") not in (None, "")
                        else None,
                        "avgBudget": row.get("avg_budget") or row.get("averageBudget"),
                        "slug": row.get("slug"),
                        "logo": row.get("logo"),
                        # Per-axis review scores — the closest local analogue
                        # to Yelp's ambience/service attributes.
                        "reviewsStat": row.get("reviewsStat") or {},
                        "reviewCount": row.get("reviews_count"),
                        # S3-hosted and free, unlike Google's billed photos.
                        "photos": [
                            m.get("url")
                            for m in (row.get("media") or [])
                            if isinstance(m, dict)
                            and m.get("type") == "image"
                            and m.get("url")
                        ][:6],
                    }
                )
                if len(places) >= limit:
                    break
            info = payload.get("pageInfo") or {}
            total_pages = int(info.get("totalPages") or page)
            if page >= total_pages:
                break
            page += 1

    log.info("[flavorqueste] %s → %d places (pages≤%d)", city_key, len(places), page)
    return places[:limit]

"""
Automated venue discovery:

  Places (Google / FlavorQueste / OSM) → Serper Instagram handle lookup → seed ig_accounts

Handle resolution uses Serper ``site:instagram.com`` search — not Instagram
topsearch — so discover no longer needs logged-in cookies (those trigger
scraping warnings / checkpoints).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from config import settings
from discover.places import discover_places, handle_from_search, place_search_query
from pipeline import store

log = logging.getLogger("ig.discover")


def _website_handle(place: dict[str, Any]) -> str | None:
    hint = place.get("instagramHint")
    if hint:
        return str(hint).lower().lstrip("@")
    return None


def _city_label(city: str) -> str:
    return (city or "lagos").strip().title() or "Lagos"


def run_discover(
    *,
    city: str = "lagos",
    limit_places: int = 100,
    resolve_limit: int = 40,
    min_score: float | None = None,  # kept for CLI compat; unused with Serper
    seed: bool = True,
    backend: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    One discovery cycle.

    - Upserts places into Mongo
    - Resolves IG handles for places that don't have one yet (capped per run)
    - Optionally seeds ig_accounts
    """
    _ = min_score  # CLI still passes this; Serper path has its own name-match gate
    if not settings.SERPER_API_KEY:
        raise RuntimeError(
            "SERPER_API_KEY required to resolve Instagram handles "
            "(discover no longer uses logged-in Instagram cookies)"
        )

    started = datetime.now(timezone.utc)
    db = store.get_db()
    store.ensure_indexes(db)

    places = discover_places(city, limit=limit_places, backend=backend)
    if dry_run:
        return {
            "city": city,
            "placesFound": len(places),
            "dryRun": True,
            "sample": [
                {"name": p["name"], "ig": p.get("instagramHint")} for p in places[:10]
            ],
        }

    upserted_places = store.upsert_places(db, places)

    # Prefer places never searched for a handle.
    pending = store.places_needing_handle(db, city=city, limit=resolve_limit)
    resolved: list[dict[str, Any]] = []
    seeded_handles: list[str] = []
    skipped = 0
    city_label = _city_label(city)

    with httpx.Client(timeout=25.0, trust_env=False) as client:
        for place in pending:
            # Website already had an IG link — accept without search.
            hint = _website_handle(place)
            if hint:
                hit = {
                    "handle": hint,
                    "score": 1.0,
                    "query": place_search_query(place),
                    "fullName": place.get("name"),
                    "placeId": place.get("_id"),
                    "source": "website",
                }
                resolved.append(hit)
                store.mark_place_handle(
                    db,
                    place_id=str(place["_id"]),
                    handle=hint,
                    score=1.0,
                    status="resolved",
                )
                continue

            name = (place.get("name") or "").strip()
            q = place_search_query(place)
            if not name or not q or len(q) < 3:
                store.mark_place_handle(
                    db,
                    place_id=str(place["_id"]),
                    handle=None,
                    score=0,
                    status="skipped",
                )
                skipped += 1
                continue

            handle = handle_from_search(name, city=city_label, client=client)
            # Light pacing so Serper stays under burst limits.
            time.sleep(0.35)

            if not handle:
                store.mark_place_handle(
                    db,
                    place_id=str(place["_id"]),
                    handle=None,
                    score=0,
                    status="unresolved",
                )
                skipped += 1
                continue

            hit = {
                "handle": handle,
                "score": 0.85,
                "query": q,
                "fullName": name,
                "placeId": place.get("_id"),
                "source": "serper",
            }
            resolved.append(hit)
            store.mark_place_handle(
                db,
                place_id=str(place["_id"]),
                handle=handle,
                score=0.85,
                status="resolved",
            )

    if resolved:
        store.upsert_handle_candidates(db, resolved)

    if seed and resolved:
        handles = sorted({h["handle"] for h in resolved if h.get("handle")})
        store.upsert_accounts(db, handles)
        store.mark_candidates_seeded(db, handles)
        seeded_handles = handles

    summary = {
        "kind": "discover",
        "city": city,
        "placesFound": len(places),
        "placesUpserted": upserted_places,
        "resolveAttempted": len(pending),
        "resolved": len(resolved),
        "skipped": skipped,
        "seeded": len(seeded_handles),
        "handles": seeded_handles[:30],
        "resolver": "serper",
        "startedAt": started,
        "durationS": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
    }
    if not dry_run:
        store.record_run(db, summary)
    log.info("[discover] %s", summary)
    return summary

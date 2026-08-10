"""
Automated venue discovery:

  Places (OSM / Google) → Instagram topsearch → seed ig_accounts
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from discover.places import discover_places, place_search_query
from ig import logged_in_search
from pipeline import store

log = logging.getLogger("ig.discover")


def _website_handle(place: dict[str, Any]) -> str | None:
    hint = place.get("instagramHint")
    if hint:
        return str(hint).lower().lstrip("@")
    return None


def run_discover(
    *,
    city: str = "lagos",
    limit_places: int = 100,
    resolve_limit: int = 40,
    min_score: float | None = None,
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
    if not logged_in_search.session_configured():
        raise logged_in_search.LoggedInAuthError(
            "logged-in cookies required (cookies.txt / IG_COOKIES) to resolve handles"
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

    queries: list[str] = []
    place_by_query: dict[str, dict[str, Any]] = {}
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
        q = place_search_query(place)
        if not q or len(q) < 3:
            store.mark_place_handle(
                db, place_id=str(place["_id"]), handle=None, score=0, status="skipped"
            )
            skipped += 1
            continue
        queries.append(q)
        place_by_query[q] = place

    if queries:
        try:
            hits = logged_in_search.search_many(queries, min_score=min_score)
        except logged_in_search.LoggedInAuthError:
            raise

        hit_by_query = {h["query"]: h for h in hits}
        for q, place in place_by_query.items():
            hit = hit_by_query.get(q)
            if not hit:
                store.mark_place_handle(
                    db,
                    place_id=str(place["_id"]),
                    handle=None,
                    score=0,
                    status="unresolved",
                )
                skipped += 1
                continue
            hit = dict(hit)
            hit["placeId"] = place.get("_id")
            resolved.append(hit)
            store.mark_place_handle(
                db,
                place_id=str(place["_id"]),
                handle=hit["handle"],
                score=float(hit.get("score") or 0),
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
        "startedAt": started,
        "durationS": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
    }
    if not dry_run:
        store.record_run(db, summary)
    log.info("[discover] %s", summary)
    return summary

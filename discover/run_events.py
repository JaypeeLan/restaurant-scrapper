"""Pull organizer / restaurant events from public Lagos sources."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from config import settings
from pipeline import store

log = logging.getLogger("ig.discover_events")


def run_discover_events(
    *,
    sources: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Fetch external events and upsert into ``external_events``.

    Sources: tix | all
    """
    wanted = {s.strip().lower() for s in (sources or ["tix"]) if s.strip()}
    if "all" in wanted:
        wanted = {"tix"}
    cap = limit if limit is not None else settings.DISCOVER_EVENTS_LIMIT
    started = datetime.now(timezone.utc)
    events: list[dict[str, Any]] = []
    errors: list[str] = []

    if "tix" in wanted:
        try:
            from discover.tix import fetch_tix_events

            events.extend(fetch_tix_events(keyword=None, limit=cap, lagos_only=True))
        except Exception as exc:  # noqa: BLE001
            log.warning("tix events failed: %s", exc)
            errors.append(f"tix: {exc}")

    # De-dupe by _id (last write wins within this batch).
    by_id: dict[str, dict[str, Any]] = {}
    for ev in events:
        eid = ev.get("_id")
        if eid:
            by_id[str(eid)] = ev
    merged = list(by_id.values())[: cap * 2]

    if dry_run:
        return {
            "dryRun": True,
            "sources": sorted(wanted),
            "eventsFound": len(merged),
            "errors": errors,
            "sample": [
                {
                    "name": e.get("name"),
                    "source": e.get("source"),
                    "startsAt": e.get("startsAt"),
                    "address": e.get("address"),
                }
                for e in merged[:10]
            ],
        }

    db = store.get_db()
    store.ensure_indexes(db)
    upserted = store.upsert_external_events(db, merged)
    return {
        "sources": sorted(wanted),
        "eventsFound": len(merged),
        "upserted": upserted,
        "errors": errors,
        "startedAt": started.isoformat(),
        "finishedAt": datetime.now(timezone.utc).isoformat(),
    }

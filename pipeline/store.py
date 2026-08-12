"""
Mongo raw store + change detection.

The change-detection layer is the whole reason a 1,000-account cycle is cheap:
on any given day the large majority of restaurants have posted nothing, and the
newest-post-id watermark turns those accounts into a single call that returns
early. Without it you re-download the same grid every cycle forever.

Collections
    ig_accounts       one doc per restaurant: watermark, tier, health, source
    ig_posts_raw      one doc per post, _id = "{handle}:{shortcode}"
    ig_highlights_raw one doc per highlight tray, _id = "{handle}:{trayId}"
    ig_runs           per-run summaries and the daily counters
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne
from pymongo.errors import BulkWriteError

from config import settings

log = logging.getLogger("ig.store")

_client: MongoClient | None = None


def get_db(uri: str | None = None, db_name: str | None = None):
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {"serverSelectionTimeoutMS": 15_000}
        try:
            import certifi

            kwargs["tlsCAFile"] = certifi.where()
        except ImportError:
            pass
        _client = MongoClient(uri or settings.MONGODB_URI, **kwargs)
    return _client[db_name or settings.MONGODB_DB_NAME]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_indexes(db) -> None:
    """Idempotent. Call once at startup."""
    db[settings.COL_ACCOUNTS].create_index([("handle", ASCENDING)], unique=True)
    db[settings.COL_ACCOUNTS].create_index([("nextFetchAt", ASCENDING)])
    db[settings.COL_ACCOUNTS].create_index([("tier", ASCENDING), ("nextFetchAt", ASCENDING)])

    db[settings.COL_POSTS].create_index([("handle", ASCENDING), ("postedAt", DESCENDING)])
    db[settings.COL_POSTS].create_index([("postedAt", DESCENDING)])
    db[settings.COL_POSTS].create_index([("shortcode", ASCENDING)])
    # Supports the not-yet-built event extraction pass.
    db[settings.COL_POSTS].create_index([("extractedAt", ASCENDING)], sparse=True)
    db[settings.COL_POSTS].create_index([("ocrAt", ASCENDING)], sparse=True)
    db[settings.COL_POSTS].create_index([("ocrStatus", ASCENDING)], sparse=True)

    db[settings.COL_HIGHLIGHTS].create_index([("handle", ASCENDING)])
    db[settings.COL_HANDLE_CANDIDATES].create_index([("handle", ASCENDING)], unique=True)
    db[settings.COL_HANDLE_CANDIDATES].create_index([("query", ASCENDING)])
    db[settings.COL_HANDLE_CANDIDATES].create_index([("score", DESCENDING)])

    db[settings.COL_PLACES].create_index([("city", ASCENDING), ("handleStatus", ASCENDING)])
    db[settings.COL_PLACES].create_index([("name", ASCENDING)])
    db[settings.COL_PLACES].create_index([("instagramHandle", ASCENDING)], sparse=True)


def upsert_places(db, places: list[dict[str, Any]]) -> int:
    """Upsert discovered venues. Returns upserted count."""
    if not places:
        return 0
    ops: list[UpdateOne] = []
    for p in places:
        pid = p.get("_id")
        if not pid:
            continue
        payload = {k: v for k, v in p.items() if k != "_id"}
        payload["updatedAt"] = _now()
        ops.append(
            UpdateOne(
                {"_id": pid},
                {
                    "$set": payload,
                    "$setOnInsert": {
                        "firstSeenAt": _now(),
                        "handleStatus": "pending",
                        "instagramHandle": p.get("instagramHint"),
                    },
                },
                upsert=True,
            )
        )
    if not ops:
        return 0
    result = db[settings.COL_PLACES].bulk_write(ops, ordered=False)
    return result.upserted_count


def places_needing_handle(
    db,
    *,
    city: str | None = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Places not yet resolved/skipped for an Instagram handle."""
    query: dict[str, Any] = {
        "handleStatus": {"$nin": ["resolved", "skipped"]},
    }
    if city:
        # Accept preset key ("lagos") or display name ("Lagos")
        from discover.places import CITY_PRESETS

        preset = CITY_PRESETS.get(city.lower())
        query["city"] = preset["name"] if preset else city
    return list(
        db[settings.COL_PLACES]
        .find(query)
        .sort([("firstSeenAt", ASCENDING)])
        .limit(limit)
    )


def mark_place_handle(
    db,
    *,
    place_id: str,
    handle: str | None,
    score: float,
    status: str,
) -> None:
    update: dict[str, Any] = {
        "handleStatus": status,
        "handleScore": score,
        "handleCheckedAt": _now(),
    }
    if handle:
        update["instagramHandle"] = handle.strip().lstrip("@").lower()
    db[settings.COL_PLACES].update_one({"_id": place_id}, {"$set": update})


def upsert_handle_candidates(db, candidates: list[dict[str, Any]]) -> int:
    """Store / refresh logged-in search hits. Returns upserted count."""
    if not candidates:
        return 0
    ops: list[UpdateOne] = []
    for c in candidates:
        handle = (c.get("handle") or "").strip().lstrip("@").lower()
        if not handle:
            continue
        ops.append(
            UpdateOne(
                {"handle": handle},
                {
                    "$set": {
                        "handle": handle,
                        "igUserId": c.get("igUserId"),
                        "fullName": c.get("fullName"),
                        "isPrivate": c.get("isPrivate"),
                        "isVerified": c.get("isVerified"),
                        "followerCount": c.get("followerCount"),
                        "profilePicUrl": c.get("profilePicUrl"),
                        "score": c.get("score"),
                        "query": c.get("query"),
                        "source": "logged_in_topsearch",
                        "updatedAt": _now(),
                    },
                    "$setOnInsert": {"firstSeenAt": _now(), "seeded": False},
                },
                upsert=True,
            )
        )
    if not ops:
        return 0
    result = db[settings.COL_HANDLE_CANDIDATES].bulk_write(ops, ordered=False)
    return result.upserted_count


def mark_candidates_seeded(db, handles: Iterable[str]) -> None:
    hs = [h.strip().lstrip("@").lower() for h in handles if h and h.strip()]
    if not hs:
        return
    db[settings.COL_HANDLE_CANDIDATES].update_many(
        {"handle": {"$in": hs}},
        {"$set": {"seeded": True, "seededAt": _now()}},
    )


# ── accounts ──────────────────────────────────────────────────────────────────


def upsert_accounts(db, handles: Iterable[str]) -> int:
    """Seed the account list. Existing docs keep their watermark and tier."""
    ops = [
        UpdateOne(
            {"handle": h.strip().lstrip("@").lower()},
            {
                "$setOnInsert": {
                    "handle": h.strip().lstrip("@").lower(),
                    "tier": "warm",
                    "createdAt": _now(),
                    "newestPostId": None,
                    "newestPostedAt": None,
                    "consecutiveFailures": 0,
                    "nextFetchAt": _now(),
                    "backfilled": False,
                }
            },
            upsert=True,
        )
        for h in handles
        if h and h.strip()
    ]
    if not ops:
        return 0
    result = db[settings.COL_ACCOUNTS].bulk_write(ops, ordered=False)
    return result.upserted_count


def due_accounts(db, *, limit: int = 500, tier: str | None = None) -> list[dict[str, Any]]:
    """Accounts whose nextFetchAt has passed, oldest first."""
    query: dict[str, Any] = {"nextFetchAt": {"$lte": _now()}}
    if tier:
        query["tier"] = tier
    return list(
        db[settings.COL_ACCOUNTS]
        .find(query)
        .sort([("nextFetchAt", ASCENDING)])
        .limit(limit)
    )


def record_success(
    db,
    handle: str,
    *,
    profile: dict[str, Any] | None,
    newest_post_id: str | None,
    newest_posted_at: datetime | None,
    new_post_count: int,
    tier: str,
    next_fetch_at: datetime,
    source: str,
) -> None:
    update: dict[str, Any] = {
        "$set": {
            "lastFetchedAt": _now(),
            "lastSuccessAt": _now(),
            "consecutiveFailures": 0,
            "tier": tier,
            "nextFetchAt": next_fetch_at,
            "lastSource": source,
            "backfilled": True,
        },
        "$inc": {"totalPostsSeen": int(new_post_count)},
    }
    if newest_post_id:
        update["$set"]["newestPostId"] = str(newest_post_id)
    if newest_posted_at:
        update["$set"]["newestPostedAt"] = newest_posted_at
    if profile:
        update["$set"]["profile"] = profile
        if profile.get("igUserId"):
            update["$set"]["igUserId"] = profile["igUserId"]
    db[settings.COL_ACCOUNTS].update_one({"handle": handle}, update, upsert=True)


def record_failure(db, handle: str, reason: str, *, backoff_hours: int = 6) -> int:
    """
    Exponential backoff per account.

    A handle that 404s shouldn't be retried on the normal cadence forever —
    each retry is a request you can't spend on an account that actually posts.
    """
    doc = db[settings.COL_ACCOUNTS].find_one_and_update(
        {"handle": handle},
        {
            "$inc": {"consecutiveFailures": 1},
            "$set": {"lastFetchedAt": _now(), "lastError": reason[:400]},
        },
        upsert=True,
        return_document=True,
    ) or {}
    failures = int(doc.get("consecutiveFailures") or 1)
    delay = min(backoff_hours * (2 ** min(failures - 1, 5)), 24 * 14)
    db[settings.COL_ACCOUNTS].update_one(
        {"handle": handle},
        {"$set": {"nextFetchAt": _now() + timedelta(hours=delay)}},
    )
    return failures


# ── posts ─────────────────────────────────────────────────────────────────────


def upsert_posts(db, docs: list[dict[str, Any]]) -> dict[str, int]:
    """
    Bulk upsert. Returns {"new": n, "changed": n, "unchanged": n, "ocrFilled": n}.

    A post whose contentHash is unchanged is left alone for caption fields —
    except OCR fields may still be written when the doc carries fresh OCR and
    Mongo does not yet have ``ocrAt``.
    """
    if not docs:
        return {"new": 0, "changed": 0, "unchanged": 0, "ocrFilled": 0}

    ids = [d["_id"] for d in docs]
    existing = {
        d["_id"]: d
        for d in db[settings.COL_POSTS].find(
            {"_id": {"$in": ids}}, {"contentHash": 1, "ocrAt": 1}
        )
    }

    ops: list[UpdateOne] = []
    stats = {"new": 0, "changed": 0, "unchanged": 0, "ocrFilled": 0}
    ocr_keys = ("ocrText", "ocrTitle", "ocrStatus", "ocrAt", "ocrError")

    for doc in docs:
        prior = existing.get(doc["_id"])
        prior_hash = (prior or {}).get("contentHash", "__missing__")
        if prior_hash == doc["contentHash"]:
            if doc.get("ocrAt") and not (prior or {}).get("ocrAt"):
                ocr_payload = {k: doc[k] for k in ocr_keys if k in doc}
                if ocr_payload:
                    ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": ocr_payload}))
                    stats["ocrFilled"] += 1
                    continue
            stats["unchanged"] += 1
            continue

        is_new = prior is None
        stats["new" if is_new else "changed"] += 1

        payload = dict(doc)
        payload.pop("_id", None)
        payload["updatedAt"] = _now()

        set_on_insert = {"firstSeenAt": _now()}
        update: dict[str, Any] = {"$set": payload, "$setOnInsert": set_on_insert}
        if not is_new:
            # Caption changed → the post needs re-extraction later.
            update["$unset"] = {"extractedAt": ""}

        ops.append(UpdateOne({"_id": doc["_id"]}, update, upsert=True))

    if ops:
        try:
            db[settings.COL_POSTS].bulk_write(ops, ordered=False)
        except BulkWriteError as exc:
            log.error("[store] bulk write partial failure: %s", exc.details)

    return stats


def newest_watermark(docs: list[dict[str, Any]]) -> tuple[str | None, datetime | None]:
    """Newest (postId, postedAt) in a batch, ignoring undated items."""
    dated = [d for d in docs if d.get("postedAt")]
    if not dated:
        return (None, None)
    newest = max(dated, key=lambda d: d["postedAt"])
    return (newest.get("postId") or newest.get("shortcode"), newest["postedAt"])


# ── highlights ────────────────────────────────────────────────────────────────


def upsert_highlights(db, handle: str, trays: list[dict[str, Any]]) -> int:
    if not trays:
        return 0
    ops = [
        UpdateOne(
            {"_id": f"{handle}:{t.get('id')}"},
            {
                "$set": {
                    "handle": handle,
                    "trayId": t.get("id"),
                    "title": t.get("title"),
                    "coverUrl": t.get("coverUrl"),
                    "mediaCount": t.get("mediaCount"),
                    "updatedAt": _now(),
                },
                "$setOnInsert": {"firstSeenAt": _now()},
            },
            upsert=True,
        )
        for t in trays
        if t.get("id")
    ]
    if not ops:
        return 0
    result = db[settings.COL_HIGHLIGHTS].bulk_write(ops, ordered=False)
    return result.upserted_count


def upsert_highlight_menu(db, doc_id: str, payload: dict[str, Any]) -> None:
    """Persist slides + MenuType drafts onto an existing highlight tray doc."""
    data = dict(payload)
    data["updatedAt"] = _now()
    db[settings.COL_HIGHLIGHTS].update_one(
        {"_id": doc_id},
        {"$set": data, "$setOnInsert": {"firstSeenAt": _now()}},
        upsert=True,
    )


# ── run bookkeeping ───────────────────────────────────────────────────────────


def record_run(db, summary: dict[str, Any]) -> None:
    db[settings.COL_RUNS].insert_one({**summary, "finishedAt": _now()})

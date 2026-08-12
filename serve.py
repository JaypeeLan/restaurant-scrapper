"""
Read API over the ingest collections.

    uvicorn serve:app --reload --port 8000

Read-only for the dashboard. DeepSeek results are read from stored
``llmName`` / ``llmExtract`` on posts (written by ingest / backfill-llm).

Mirrors the FastAPI conventions in validds/scraper/serve.py.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from pipeline import event_extract, store, tiers
from pipeline.menu_merge import merge_menu_trays

log = logging.getLogger("ig.serve")

app = FastAPI(title="Instagram ingest API", version="1.0.0")

# Short in-process TTL for expensive read endpoints. Ingest runs on a ~30m
# cadence, so a minute or two of staleness is fine and avoids re-extracting
# the same ~1000 posts on every tab switch / SummaryBar remount.
_API_CACHE_TTL_S = float(os.getenv("API_CACHE_TTL_S", "60"))
_HIGHLIGHTS_CACHE_TTL_S = float(os.getenv("API_HIGHLIGHTS_CACHE_TTL_S", "180"))


class _TtlCache:
    """Thread-safe deepcopy TTL map. Values are copied on get/set so callers
    can mutate responses without poisoning the store."""

    def __init__(self, ttl_s: float, *, max_entries: int = 128) -> None:
        self.ttl_s = max(0.0, ttl_s)
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._store: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        if self.ttl_s <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            hit = self._store.get(key)
            if hit is None:
                return None
            expires, value = hit
            if now >= expires:
                del self._store[key]
                return None
            return copy.deepcopy(value)

    def set(self, key: Any, value: Any) -> None:
        if self.ttl_s <= 0:
            return
        expires = time.monotonic() + self.ttl_s
        payload = copy.deepcopy(value)
        with self._lock:
            if len(self._store) >= self.max_entries:
                # Drop expired first, then oldest insert order.
                now = time.monotonic()
                expired = [k for k, (exp, _) in self._store.items() if now >= exp]
                for k in expired:
                    del self._store[k]
                while len(self._store) >= self.max_entries:
                    self._store.pop(next(iter(self._store)), None)
            self._store[key] = (expires, payload)


_drafts_cache = _TtlCache(_API_CACHE_TTL_S)
_summary_cache = _TtlCache(_API_CACHE_TTL_S, max_entries=4)
_capacity_cache = _TtlCache(_API_CACHE_TTL_S, max_entries=4)
_highlights_cache = _TtlCache(_HIGHLIGHTS_CACHE_TTL_S)
_drafts_inflight: dict[Any, threading.Event] = {}
_drafts_inflight_lock = threading.Lock()

_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "IG_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if o.strip()
]
# Vercel production + preview deployments (*.vercel.app).
_CORS_ORIGIN_REGEX = os.getenv(
    "IG_CORS_ORIGIN_REGEX",
    r"https://([a-z0-9-]+\.)*vercel\.app",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_origin_regex=_CORS_ORIGIN_REGEX or None,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_ingest_at(now: datetime | None = None) -> datetime:
    """Next UTC ingest slot on the */N clock grid (matches Render cron)."""
    now = now or _now()
    every = max(1, settings.INGEST_EVERY_MINUTES)
    # Floor current UTC minutes since epoch to the cadence grid, then step once.
    minute_epoch = int(now.timestamp()) // 60
    next_minute = ((minute_epoch // every) + 1) * every
    return datetime.fromtimestamp(next_minute * 60, tz=timezone.utc)


def _next_discover_at(now: datetime | None = None) -> datetime | None:
    """Next discover fire time in UTC (matches ``M */H * * *`` style cron)."""
    if settings.DISCOVER_EVERY_HOURS <= 0:
        return None
    now = now or _now()
    every_h = max(1, settings.DISCOVER_EVERY_HOURS)
    minute = max(0, min(59, settings.DISCOVER_CRON_MINUTE))
    if every_h >= 24:
        hour = max(0, min(23, settings.DISCOVER_CRON_HOUR))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    for day_offset in (0, 1):
        day = (now + timedelta(days=day_offset)).date()
        for hour in range(0, 24, every_h):
            candidate = datetime(
                day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc
            )
            if candidate > now:
                return candidate
    return None


def _discover_cron_expr() -> str | None:
    if settings.DISCOVER_EVERY_HOURS <= 0:
        return None
    minute = max(0, min(59, settings.DISCOVER_CRON_MINUTE))
    every_h = settings.DISCOVER_EVERY_HOURS
    if every_h >= 24:
        hour = max(0, min(23, settings.DISCOVER_CRON_HOUR))
        return f"{minute} {hour} * * *"
    return f"{minute} */{every_h} * * *"


def _next_menu_at(now: datetime) -> datetime | None:
    """
    Next Sunday 03:30 UTC (matches default MENU_CRON ``30 3 * * 0``).
    Falls back to parsing is deferred — cron string is informational in the UI.
    """
    # weekday: Mon=0 … Sun=6
    days_ahead = (6 - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=3, minute=30, second=0, microsecond=0
    )
    if candidate <= now:
        candidate = candidate + timedelta(days=7)
    return candidate


def _schedule_next() -> dict[str, Any]:
    now = _now()
    nxt_ingest = _next_ingest_at(now)
    nxt_discover = _next_discover_at(now)
    nxt_menu = _next_menu_at(now) if settings.MENU_EVERY_DAYS > 0 else None
    return {
        "now": _iso(now),
        "nextIngestAt": _iso(nxt_ingest),
        "nextIngestInSeconds": max(0, int((nxt_ingest - now).total_seconds())),
        "nextDiscoverAt": _iso(nxt_discover) if nxt_discover else None,
        "nextDiscoverInSeconds": (
            max(0, int((nxt_discover - now).total_seconds())) if nxt_discover else None
        ),
        "nextMenuAt": _iso(nxt_menu) if nxt_menu else None,
        "nextMenuInSeconds": (
            max(0, int((nxt_menu - now).total_seconds())) if nxt_menu else None
        ),
        "ingestCron": f"*/{max(1, settings.INGEST_EVERY_MINUTES)} * * * *",
        "discoverCron": _discover_cron_expr(),
        "menuCron": settings.MENU_CRON if settings.MENU_EVERY_DAYS > 0 else None,
    }


def _db():
    try:
        db = store.get_db()
        # Force a round-trip so SSL / allowlist failures surface here, not mid-query.
        db.command("ping")
        return db
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "TLSV1_ALERT_INTERNAL_ERROR" in msg or "SSL handshake failed" in msg:
            raise HTTPException(
                status_code=503,
                detail=(
                    "mongo unavailable: Atlas TLS handshake failed. "
                    "Usually your current public IP is not on Network Access allowlist "
                    "(Atlas → Network Access → Add IP Address). "
                    "Allowlisting 0.0.0.0/0 works for local dev."
                ),
            ) from exc
        raise HTTPException(status_code=503, detail=f"mongo unavailable: {exc}") from exc


def _iso(value: Any) -> Any:
    """Datetimes → ISO strings so the JSON is directly usable in TS."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


_EVENT_POST_LIMIT = 1000
_EVENT_POST_PROJECTION = {
    "source.raw": 0,
    "contentHash": 0,
}

# Instagram highlight tray titles that usually mean a menu board.
_MENU_HIGHLIGHT_RE = re.compile(
    r"menu|food|drink|beverage|wine|cocktail|kitchen|lunch|dinner|brunch|"
    r"pastr(?:y|ies)|takeaway|take\s*away|how\s+to\s+order|specials?|"
    r"\bbar\b|dessert|sushi|dim\s*sum|cigar",
    re.I,
)


def _menu_tray_filter() -> dict[str, Any]:
    """IG highlight trays + website/Linktree menu sources."""
    return {
        "$or": [
            {"sourceType": "web"},
            {"title": {"$regex": _MENU_HIGHLIGHT_RE.pattern, "$options": "i"}},
        ]
    }


def _experience_drafts(
    db: Any,
    *,
    handle: str | None = None,
    min_score: int = 2,
    ocr_fetch: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """
    Shared experience extraction for summary + /api/events.

    Uses stored ``llmName`` / ``llmExtract`` only — never calls DeepSeek live.
    """
    handle_key = handle.strip().lstrip("@").lower() if handle else ""
    cache_key = (handle_key, int(min_score), bool(ocr_fetch))
    cached = _drafts_cache.get(cache_key)
    if cached is not None:
        return cached

    leader = False
    done: threading.Event | None = None
    with _drafts_inflight_lock:
        done = _drafts_inflight.get(cache_key)
        if done is None:
            done = threading.Event()
            _drafts_inflight[cache_key] = done
            leader = True

    if not leader:
        done.wait(timeout=120)
        cached = _drafts_cache.get(cache_key)
        if cached is not None:
            return cached
        # Leader failed or timed out — fall through and try ourselves.

    try:
        query: dict[str, Any] = {}
        if handle_key:
            query["handle"] = handle_key

        posts = list(
            db[settings.COL_POSTS]
            .find(query, _EVENT_POST_PROJECTION)
            .sort([("postedAt", -1)])
            .limit(_EVENT_POST_LIMIT)
        )
        handles = list({(p.get("handle") or "").lower() for p in posts if p.get("handle")})
        profiles: dict[str, dict[str, Any]] = {}
        if handles:
            for a in db[settings.COL_ACCOUNTS].find(
                {"handle": {"$in": handles}},
                {"handle": 1, "profile.name": 1, "profile.website": 1},
            ):
                prof = a.get("profile") or {}
                profiles[a["handle"]] = {
                    "name": prof.get("name"),
                    "website": prof.get("website"),
                }

        events = event_extract.extract_events(
            posts,
            min_score=min_score,
            profiles=profiles,
            use_llm=False,
            ocr_allow_fetch=ocr_fetch,
        )

        result = (events, profiles)
        _drafts_cache.set(cache_key, result)
        return result
    finally:
        if leader and done is not None:
            with _drafts_inflight_lock:
                _drafts_inflight.pop(cache_key, None)
            done.set()


def _clean(doc: dict[str, Any], *, drop_raw: bool = True) -> dict[str, Any]:
    """
    Serialize a Mongo doc for the wire.

    `source.raw` is the full upstream payload — often 20-40 KB per post. Sending
    it to a browser grid of 50 posts is megabytes of nothing anyone looks at, so
    it's dropped unless explicitly requested.
    """
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if key == "_id":
            out["id"] = str(value)
            continue
        if key == "source" and isinstance(value, dict):
            out["source"] = (
                {"name": value.get("name")}
                if drop_raw
                else {"name": value.get("name"), "raw": value.get("raw")}
            )
            continue
        out[key] = _iso(value)
    return out


# ── health ────────────────────────────────────────────────────────────────────


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        db = store.get_db()
        db.command("ping")
        return {"ok": True, "db": settings.MONGODB_DB_NAME}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ── summary ───────────────────────────────────────────────────────────────────


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    """Headline counters for the dashboard strip."""
    cached = _summary_cache.get("summary")
    if cached is not None:
        # Refresh schedule countdowns even on a cache hit — absolute next*At
        # stays valid; the seconds-left fields should track wall clock.
        cached.update(_schedule_next())
        return cached

    db = _db()
    accounts = db[settings.COL_ACCOUNTS]
    posts = db[settings.COL_POSTS]

    day_ago = _now() - timedelta(days=1)
    week_ago = _now() - timedelta(days=7)

    events, _profiles = _experience_drafts(db)

    payload = {
        "accounts": accounts.count_documents({}),
        "accountsDue": accounts.count_documents({"nextFetchAt": {"$lte": _now()}}),
        "accountsFailing": accounts.count_documents({"consecutiveFailures": {"$gte": 3}}),
        "posts": posts.count_documents({}),
        "postsLast24h": posts.count_documents({"firstSeenAt": {"$gte": day_ago}}),
        "postsLast7d": posts.count_documents({"firstSeenAt": {"$gte": week_ago}}),
        "highlights": db[settings.COL_HIGHLIGHTS].count_documents({}),
        "menus": db[settings.COL_HIGHLIGHTS].count_documents(_menu_tray_filter()),
        "menuItems": int(
            (
                next(
                    db[settings.COL_HIGHLIGHTS].aggregate(
                        [
                            {"$match": {"menuItemCount": {"$gt": 0}}},
                            {"$group": {"_id": None, "n": {"$sum": "$menuItemCount"}}},
                        ]
                    ),
                    {"n": 0},
                )
            ).get("n")
            or 0
        ),
        "events": len(events),
        "generatedAt": _iso(_now()),
        **_schedule_next(),
    }
    _summary_cache.set("summary", payload)
    return payload


# ── posts ─────────────────────────────────────────────────────────────────────


@app.get("/api/posts")
def list_posts(
    handle: str | None = Query(None, description="exact handle"),
    q: str | None = Query(None, description="caption substring, case-insensitive"),
    since: str | None = Query(None, description="ISO date lower bound on postedAt"),
    until: str | None = Query(None, description="ISO date upper bound on postedAt"),
    media_type: str | None = Query(None),
    source: str | None = Query(None, description="graph | web_json | embed"),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    sort: str = Query("postedAt", pattern="^(postedAt|firstSeenAt|likeCount)$"),
) -> dict[str, Any]:
    db = _db()
    query: dict[str, Any] = {}

    if handle:
        query["handle"] = handle.strip().lstrip("@").lower()
    if q:
        # Escaped so a caption search for "$5 (special)" can't blow up the regex.
        query["caption"] = {"$regex": re.escape(q), "$options": "i"}
    if media_type:
        query["mediaType"] = media_type.upper()
    if source:
        query["source.name"] = source

    date_filter: dict[str, Any] = {}
    for key, raw in (("$gte", since), ("$lte", until)):
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            date_filter[key] = parsed
        except ValueError as exc:
            raise HTTPException(400, f"bad date {raw!r}: expected ISO-8601") from exc
    if date_filter:
        query["postedAt"] = date_filter

    cursor = (
        db[settings.COL_POSTS]
        .find(query, {"source.raw": 0})
        .sort([(sort, -1)])
        .skip(skip)
        .limit(limit)
    )
    items = [_clean(d) for d in cursor]

    return {
        "items": items,
        "total": db[settings.COL_POSTS].count_documents(query),
        "limit": limit,
        "skip": skip,
    }


@app.get("/api/posts/{post_id:path}")
def get_post(post_id: str) -> dict[str, Any]:
    """Single post including the full raw payload."""
    db = _db()
    doc = db[settings.COL_POSTS].find_one({"_id": post_id})
    if not doc:
        raise HTTPException(404, "post not found")
    return _clean(doc, drop_raw=False)


# ── accounts ──────────────────────────────────────────────────────────────────


@app.get("/api/accounts")
def list_accounts(
    tier: str | None = Query(None),
    failing: bool = Query(False, description="only accounts with 3+ failures"),
    q: str | None = Query(None, description="handle substring"),
    limit: int = Query(100, ge=1, le=200),
    skip: int = Query(0, ge=0),
    sort: str = Query(
        "nextFetchAt", pattern="^(nextFetchAt|lastFetchedAt|handle|consecutiveFailures|newestPostedAt)$"
    ),
) -> dict[str, Any]:
    db = _db()
    query: dict[str, Any] = {}
    if tier:
        query["tier"] = tier
    if failing:
        query["consecutiveFailures"] = {"$gte": 3}
    if q:
        query["handle"] = {"$regex": re.escape(q), "$options": "i"}

    direction = 1 if sort in ("handle", "nextFetchAt") else -1
    cursor = (
        db[settings.COL_ACCOUNTS]
        .find(query)
        .sort([(sort, direction)])
        .skip(skip)
        .limit(limit)
    )

    return {
        "items": [_clean(d) for d in cursor],
        "total": db[settings.COL_ACCOUNTS].count_documents(query),
        "limit": limit,
        "skip": skip,
    }


# ── runs ──────────────────────────────────────────────────────────────────────


def _schedule_meta() -> dict[str, Any]:
    """Configured cadences — what operators expect between runs."""
    graph_ok = bool(settings.IG_GRAPH_ACCESS_TOKEN and settings.IG_GRAPH_USER_ID)
    return {
        "ingestEveryMinutes": settings.INGEST_EVERY_MINUTES,
        "ingestLimit": settings.INGEST_LIMIT,
        "discoverEveryHours": settings.DISCOVER_EVERY_HOURS,
        "discoverCity": settings.DISCOVER_CITY,
        "discoverPlaceLimit": settings.DISCOVER_PLACE_LIMIT,
        "discoverResolveLimit": settings.DISCOVER_RESOLVE_LIMIT,
        "menuEveryDays": settings.MENU_EVERY_DAYS,
        "menuBackfillLimit": settings.MENU_BACKFILL_LIMIT,
        "menuCron": settings.MENU_CRON if settings.MENU_EVERY_DAYS > 0 else None,
        "tierIntervalsHours": settings.TIER_INTERVALS_HOURS,
        "fallbackMaxPerRun": settings.IG_FALLBACK_MAX_PER_RUN,
        "fallbackEnabled": settings.IG_FALLBACK_ENABLED,
        "graphConfigured": graph_ok,
        # playwright | graph — which path actually drains due accounts today
        "primarySource": "graph" if graph_ok else "playwright",
    }


def _observed_gap_minutes(docs: list[dict[str, Any]], *, kind: str) -> float | None:
    """Minutes between the two most recent finished runs of `kind`."""
    times: list[datetime] = []
    for d in docs:
        if (d.get("kind") or "ingest") != kind:
            continue
        finished = d.get("finishedAt") or d.get("startedAt")
        if isinstance(finished, datetime):
            times.append(finished)
        if len(times) >= 2:
            break
    if len(times) < 2:
        return None
    delta = abs((times[0] - times[1]).total_seconds()) / 60.0
    return round(delta, 1)


@app.get("/api/runs")
def list_runs(
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    kind: str | None = Query(None, description="ingest | discover (omit = both)"),
) -> dict[str, Any]:
    """
    Recent runs, oldest-first within the page so charts read left→right.

    Includes configured schedule intervals plus the observed gap between the
    latest two ingest/discover runs. In Playwright-only mode, healthy runs are
    mostly fallbackOk; with Graph configured, mostly graphOk.
    """
    db = _db()
    query: dict[str, Any] = {"startedAt": {"$exists": True}}
    if kind in ("ingest", "discover"):
        # Legacy ingest rows may omit kind — treat missing as ingest.
        if kind == "ingest":
            query["$or"] = [{"kind": "ingest"}, {"kind": {"$exists": False}}]
        else:
            query["kind"] = "discover"

    total = db[settings.COL_RUNS].count_documents(query)
    docs = list(
        db[settings.COL_RUNS]
        .find(query)
        .sort([("finishedAt", -1)])
        .skip(skip)
        .limit(limit)
    )

    # Gap stats from the newest runs regardless of page (cheap two-doc lookback).
    recent = list(
        db[settings.COL_RUNS]
        .find({"startedAt": {"$exists": True}})
        .sort([("finishedAt", -1)])
        .limit(40)
    )
    last_ingest = next(
        (d for d in recent if (d.get("kind") or "ingest") == "ingest"),
        None,
    )
    last_discover = next((d for d in recent if d.get("kind") == "discover"), None)

    return {
        "items": [_clean(d) for d in reversed(docs)],
        "total": total,
        "limit": limit,
        "skip": skip,
        "schedule": _schedule_meta(),
        "lastIngestAt": _iso(last_ingest.get("finishedAt")) if last_ingest else None,
        "lastDiscoverAt": _iso(last_discover.get("finishedAt")) if last_discover else None,
        "observedIngestGapMinutes": _observed_gap_minutes(recent, kind="ingest"),
        "observedDiscoverGapHours": (
            round(g / 60.0, 2)
            if (g := _observed_gap_minutes(recent, kind="discover")) is not None
            else None
        ),
        **_schedule_next(),
    }


# ── highlights / menus ────────────────────────────────────────────────────────


@app.get("/api/highlights")
def list_highlights(
    handle: str | None = Query(None, description="exact handle"),
    q: str | None = Query(None, description="title substring"),
    menus_only: bool = Query(
        True,
        description="Only trays whose title looks like a menu (food/drinks/etc.)",
    ),
    grouped: bool = Query(True, description="group by handle"),
    include_slides: bool = Query(
        False,
        description="Include slide image URLs + OCR (heavy). Default omits slides.",
    ),
    limit: int = Query(100, ge=1, le=500),
    skip: int = Query(0, ge=0),
) -> dict[str, Any]:
    """
    Instagram highlight trays. Menu trays may include extracted ``menuItems``
    shaped like product MenuType drafts (itemName, price, category, type, section).
    """
    handle_key = handle.strip().lstrip("@").lower() if handle else ""
    cache_key = (
        handle_key,
        q or "",
        bool(menus_only),
        bool(grouped),
        bool(include_slides),
        int(limit),
        int(skip),
    )
    cached = _highlights_cache.get(cache_key)
    if cached is not None:
        return cached

    db = _db()
    query: dict[str, Any] = {}
    if handle_key:
        query["handle"] = handle_key

    title_clauses: list[dict[str, Any]] = []
    if menus_only:
        title_clauses.append(_menu_tray_filter())
    if q:
        title_clauses.append({"title": {"$regex": re.escape(q), "$options": "i"}})
    if len(title_clauses) == 1:
        query.update(title_clauses[0])
    elif title_clauses:
        query["$and"] = title_clauses

    total = db[settings.COL_HIGHLIGHTS].count_documents(query)
    projection: dict[str, int] | None = None
    if not include_slides:
        projection = {"slides": 0}
    cursor = (
        db[settings.COL_HIGHLIGHTS]
        .find(query, projection)
        .sort([("handle", 1), ("title", 1)])
        .skip(skip)
        .limit(limit)
    )
    items = [_clean(doc) for doc in cursor]
    for item in items:
        is_web = item.get("sourceType") == "web"
        tray_id = item.get("trayId")
        if is_web:
            item["permalink"] = item.get("menuUrl") or item.get("sourceUrl")
        elif tray_id:
            item["permalink"] = f"https://www.instagram.com/stories/highlights/{tray_id}/"
        item["kind"] = (
            "menu"
            if is_web or _MENU_HIGHLIGHT_RE.search(str(item.get("title") or ""))
            else "highlight"
        )
        items_list = item.get("menuItems")
        if not isinstance(items_list, list):
            item["menuItems"] = []
            item["menuItemCount"] = int(item.get("menuItemCount") or 0)
        else:
            item["menuItemCount"] = int(item.get("menuItemCount") or len(items_list))

    if not grouped:
        payload = {
            "grouped": False,
            "total": total,
            "limit": limit,
            "skip": skip,
            "menusOnly": menus_only,
            "items": items,
        }
        _highlights_cache.set(cache_key, payload)
        return payload

    by_handle: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        h = (item.get("handle") or "").lower() or "unknown"
        by_handle.setdefault(h, []).append(item)

    profiles: list[dict[str, Any]] = []
    for h, trays in by_handle.items():
        merged_trays = merge_menu_trays(trays)
        profiles.append(
            {
                "handle": h,
                "menuCount": sum(1 for t in merged_trays if t.get("kind") == "menu"),
                "highlightCount": len(merged_trays),
                "menuItemCount": sum(int(t.get("menuItemCount") or 0) for t in merged_trays),
                "highlights": merged_trays,
            }
        )
    profiles.sort(
        key=lambda p: (-p["menuItemCount"], -p["menuCount"], -p["highlightCount"], p["handle"])
    )

    payload = {
        "grouped": True,
        "total": total,
        "profileTotal": len(profiles),
        "limit": limit,
        "skip": skip,
        "menusOnly": menus_only,
        "profiles": profiles,
    }
    _highlights_cache.set(cache_key, payload)
    return payload


# ── events (heuristic + optional DeepSeek refine) ─────────────────────────────


@app.get("/api/events")
def list_events(
    handle: str | None = Query(None, description="exact handle"),
    min_score: int = Query(2, ge=1, le=10),
    grouped: bool = Query(True, description="group by handle"),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    ocr_fetch: bool = Query(
        False,
        description="Download + OCR image flyers live (off by default; uses stored ocrText)",
    ),
) -> dict[str, Any]:
    """
    Experience drafts from captions / flyer OCR / stored DeepSeek, shaped like ExperienceType.

    Name priority: stored DeepSeek → usable flyer OCR → caption heuristics.
    Live DeepSeek is never run here; use ingest / backfill-llm in the background.
    Paginated: `limit`/`skip` apply to profile groups when `grouped=true`, else
    to flat experience items. `total` is profile groups (grouped) or experience
    drafts (flat). `experienceTotal` is always the full deduped experience count.
    """
    db = _db()
    events, profiles = _experience_drafts(
        db,
        handle=handle,
        min_score=min_score,
        ocr_fetch=ocr_fetch,
    )
    for event in events:
        event["postedAt"] = _iso(event.get("postedAt"))
        for src in event.get("sourcePosts") or []:
            if isinstance(src, dict) and "postedAt" in src:
                src["postedAt"] = _iso(src.get("postedAt"))

    llm_meta = {
        "enabled": bool(settings.DEEPSEEK_ENABLED and settings.DEEPSEEK_API_KEY),
        "live": False,
        "ocrFetch": ocr_fetch,
        "model": settings.DEEPSEEK_MODEL if settings.DEEPSEEK_API_KEY else None,
        "refined": sum(1 for e in events if e.get("nameSource") == "deepseek"),
    }
    experience_total = len(events)

    if grouped:
        groups = event_extract.group_by_handle(events)
        for group in groups:
            group["profileName"] = (profiles.get(group["handle"]) or {}).get("name")
        total = len(groups)
        page = groups[skip : skip + limit]
        return {
            "grouped": True,
            "total": total,
            "experienceTotal": experience_total,
            "limit": limit,
            "skip": skip,
            "llm": llm_meta,
            "profiles": page,
        }

    return {
        "grouped": False,
        "total": experience_total,
        "experienceTotal": experience_total,
        "limit": limit,
        "skip": skip,
        "llm": llm_meta,
        "items": events[skip : skip + limit],
    }


# ── capacity ──────────────────────────────────────────────────────────────────


def _playwright_capacity() -> dict[str, Any]:
    """Rough daily fetch budget from cron cadence × per-run cap (not Graph)."""
    every = max(1, settings.INGEST_EVERY_MINUTES)
    runs_per_day = (24 * 60) / every
    per_run = settings.IG_FALLBACK_MAX_PER_RUN
    avg_gap = (settings.IG_FALLBACK_MIN_GAP_S + settings.IG_FALLBACK_MAX_GAP_S) / 2
    gap_per_hour = 3600 / max(1.0, avg_gap)
    # Cap by both the cron batch size and the gap-limited hourly rate.
    daily = min(runs_per_day * per_run, gap_per_hour * 24)
    return {
        "dailyCapacity": round(daily, 1),
        "callsPerHour": round(min(per_run * (60 / every), gap_per_hour), 1),
        "runsPerDay": round(runs_per_day, 1),
        "maxPerRun": per_run,
        "avgGapS": round(avg_gap, 1),
    }


@app.get("/api/capacity")
def capacity() -> dict[str, Any]:
    """Live version of `python main.py capacity`."""
    cached = _capacity_cache.get("capacity")
    if cached is not None:
        return cached

    db = _db()
    counts = {
        t: db[settings.COL_ACCOUNTS].count_documents({"tier": t}) for t in tiers.TIER_ORDER
    }
    plan = tiers.plan_capacity(counts)
    graph_ok = bool(settings.IG_GRAPH_ACCESS_TOKEN and settings.IG_GRAPH_USER_ID)
    pw = _playwright_capacity()

    # When Graph is not configured, budget against Playwright pacing instead of
    # the unused Meta ceiling so Capacity matches how ingest actually runs.
    if not graph_ok and settings.IG_FALLBACK_ENABLED:
        daily_cap = pw["dailyCapacity"]
        demand = plan["dailyDemand"]
        util = (demand / daily_cap) if daily_cap else None
        payload = {
            **plan,
            "dailyCapacity": daily_cap,
            "utilization": round(util, 3) if util is not None else None,
            "withinBudget": bool(util is not None and util <= 1.0),
            "tierCounts": counts,
            "callsPerHour": pw["callsPerHour"],
            "tierIntervalsHours": settings.TIER_INTERVALS_HOURS,
            "graphConfigured": False,
            "fallbackEnabled": True,
            "primarySource": "playwright",
            "playwright": pw,
        }
    else:
        payload = {
            **plan,
            "tierCounts": counts,
            "callsPerHour": settings.IG_GRAPH_CALLS_PER_HOUR,
            "tierIntervalsHours": settings.TIER_INTERVALS_HOURS,
            "graphConfigured": graph_ok,
            "fallbackEnabled": settings.IG_FALLBACK_ENABLED,
            "primarySource": "graph" if graph_ok else "none",
            "playwright": pw,
        }

    _capacity_cache.set("capacity", payload)
    return payload


# ── static frontend (after `npm run build` in web/) ───────────────────────────

_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dist")

if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):  # noqa: ANN201
        """Serve the SPA for any non-API route."""
        if full_path.startswith("api/"):
            raise HTTPException(404, "not found")
        return FileResponse(os.path.join(_DIST, "index.html"))

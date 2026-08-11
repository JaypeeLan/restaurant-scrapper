"""
Read-only API over the ingest collections.

    uvicorn serve:app --reload --port 8000

Read-only on purpose: this exists to look at what the pipeline collected, not
to trigger it. Nothing here writes, so it's safe to expose to a dashboard.

Mirrors the FastAPI conventions in validds/scraper/serve.py.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from pipeline import event_extract, store, tiers

log = logging.getLogger("ig.serve")

app = FastAPI(title="Instagram ingest API", version="1.0.0")

_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "IG_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
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


def _schedule_next() -> dict[str, Any]:
    now = _now()
    nxt_ingest = _next_ingest_at(now)
    nxt_discover = _next_discover_at(now)
    return {
        "now": _iso(now),
        "nextIngestAt": _iso(nxt_ingest),
        "nextIngestInSeconds": max(0, int((nxt_ingest - now).total_seconds())),
        "nextDiscoverAt": _iso(nxt_discover) if nxt_discover else None,
        "nextDiscoverInSeconds": (
            max(0, int((nxt_discover - now).total_seconds())) if nxt_discover else None
        ),
        "ingestCron": f"*/{max(1, settings.INGEST_EVERY_MINUTES)} * * * *",
        "discoverCron": _discover_cron_expr(),
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
    db = _db()
    accounts = db[settings.COL_ACCOUNTS]
    posts = db[settings.COL_POSTS]

    day_ago = _now() - timedelta(days=1)
    week_ago = _now() - timedelta(days=7)

    return {
        "accounts": accounts.count_documents({}),
        "accountsDue": accounts.count_documents({"nextFetchAt": {"$lte": _now()}}),
        "accountsFailing": accounts.count_documents({"consecutiveFailures": {"$gte": 3}}),
        "posts": posts.count_documents({}),
        "postsLast24h": posts.count_documents({"firstSeenAt": {"$gte": day_ago}}),
        "postsLast7d": posts.count_documents({"firstSeenAt": {"$gte": week_ago}}),
        "highlights": db[settings.COL_HIGHLIGHTS].count_documents({}),
        "events": len(
            event_extract.extract_events(
                list(
                    posts.find({}, {"caption": 1, "handle": 1, "_id": 1})
                    .sort([("postedAt", -1)])
                    .limit(2000)
                )
            )
        ),
        "generatedAt": _iso(_now()),
        **_schedule_next(),
    }


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


# ── events (heuristic + optional DeepSeek refine) ─────────────────────────────


@app.get("/api/events")
def list_events(
    handle: str | None = Query(None, description="exact handle"),
    min_score: int = Query(2, ge=1, le=10),
    grouped: bool = Query(True, description="group by handle"),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    llm: bool = Query(
        False,
        description="Refine with DeepSeek (off by default — slow on cold cache)",
    ),
    ocr_fetch: bool = Query(
        False,
        description="Download + OCR image flyers live (off by default; uses disk cache only)",
    ),
) -> dict[str, Any]:
    """
    Experience drafts from captions, shaped like the main product ExperienceType.

    Derived on read from `ig_posts_raw`. Incomplete fields listed per item under
    `missing`. Live OCR/DeepSeek are opt-in so the dashboard stays responsive;
    cached OCR/LLM results are still applied when present under `.cache/`.

    Paginated: `limit`/`skip` apply to profile groups when `grouped=true`, else
    to flat experience items. `total` is the full count before slicing.
    """
    db = _db()
    query: dict[str, Any] = {}
    if handle:
        query["handle"] = handle.strip().lstrip("@").lower()

    posts = list(
        db[settings.COL_POSTS]
        .find(
            query,
            {
                "source.raw": 0,
                "contentHash": 0,
            },
        )
        .sort([("postedAt", -1)])
        .limit(1000)
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
        use_llm=llm,
        ocr_allow_fetch=ocr_fetch,
    )
    for event in events:
        event["postedAt"] = _iso(event.get("postedAt"))
        for src in event.get("sourcePosts") or []:
            if isinstance(src, dict) and "postedAt" in src:
                src["postedAt"] = _iso(src.get("postedAt"))

    llm_meta = {
        "enabled": bool(settings.DEEPSEEK_ENABLED and settings.DEEPSEEK_API_KEY),
        "requested": llm,
        "ocrFetch": ocr_fetch,
        "model": settings.DEEPSEEK_MODEL if settings.DEEPSEEK_API_KEY else None,
        "refined": sum(1 for e in events if e.get("nameSource") == "deepseek"),
    }

    if grouped:
        groups = event_extract.group_by_handle(events)
        for group in groups:
            group["profileName"] = (profiles.get(group["handle"]) or {}).get("name")
        total = len(groups)
        page = groups[skip : skip + limit]
        return {
            "grouped": True,
            "total": total,
            "limit": limit,
            "skip": skip,
            "llm": llm_meta,
            "profiles": page,
        }

    total = len(events)
    return {
        "grouped": False,
        "total": total,
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
        return {
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

    return {
        **plan,
        "tierCounts": counts,
        "callsPerHour": settings.IG_GRAPH_CALLS_PER_HOUR,
        "tierIntervalsHours": settings.TIER_INTERVALS_HOURS,
        "graphConfigured": graph_ok,
        "fallbackEnabled": settings.IG_FALLBACK_ENABLED,
        "primarySource": "graph" if graph_ok else "none",
        "playwright": pw,
    }


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

"""
Map discovered Lagos events into the exploree-api ``Experience`` create shape.

Same flow as the restaurant mapper: pull every provider, enrich each event with
whatever the feeds omit (Tix detail query, geocoding, venue join), infer the
residue with DeepSeek, and emit a bare array of records. Fields nobody can fill
stay ``null``.

    python scripts/map_exploree_experiences.py --count 5 --primary tix

Joi-forbidden create keys (coverImage, slug, active, experienceId) are omitted
rather than nulled. `ownerRef` is the external id the owner must resolve from.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.map_exploree_restaurants import _hhmm  # noqa: E402

# exploree-api: src/app/services/experience/experience.types.ts
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Food": ("brunch", "dinner", "buka", "food", "tasting", "buffet", "grill", "lunch"),
    "Drinks": ("happy hour", "cocktail", "wine", "bar night", "mixology", "sip"),
    "Music": ("live band", "concert", "dj", "acoustic", "sounds", "afrobeat"),
    "Dance": ("dance", "salsa"),
    "Rave": ("rave", "night party", "club night"),
    "Games": ("karaoke", "trivia", "quiz", "games night", "bingo"),
    "Movies": ("movie", "cinema", "screening", "film"),
    "Art": ("art", "gallery", "painting", "exhibit", "paint"),
    "Workshop": ("workshop", "masterclass", "class"),
    "Festival": ("festival", "fest", "carnival"),
    "Networking": ("networking", "mixer", "meetup"),
    "Wellness": ("yoga", "wellness", "spa", "retreat", "breathe"),
    "Fitness": ("fitness", "run", "bootcamp", "workout"),
    "Outdoors": ("yacht", "boat", "beach", "picnic", "cruise", "outdoor"),
    "Theater": ("theatre", "theater", "play", "stage"),
    "Conference": ("conference", "summit"),
    "Kids": ("children", "kids", "camp"),
}
VALID_CATEGORIES = set(CATEGORY_KEYWORDS) | {
    "Exhibition", "Tour", "Family", "Charity", "Educational", "Business",
    "Technology", "Fashion", "Beauty", "Cultural", "Social", "Seminar", "Sports",
}
AGE_LIMITS = {"All Ages", "18+", "21+"}
WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]

_TIME_RANGE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})")


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _categories(text: str) -> list[str] | None:
    lowered = (text or "").lower()
    hits = [
        cat
        for cat, words in CATEGORY_KEYWORDS.items()
        if any(w in lowered for w in words)
    ]
    return hits or None


def _schedule(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Build `schedule`, honouring the Joi one-time/recurring exclusivity:
    `date` is forbidden when recurring, `recurrence` forbidden when one-time.
    """
    notes: dict[str, Any] = {}
    starts = _as_dt(event.get("startsAt"))
    ends = _as_dt(event.get("endsAt"))

    start_time = end_time = None
    span = _TIME_RANGE.search(str(event.get("timeText") or ""))
    if span:
        start_time, end_time = _hhmm(span.group(1)), _hhmm(span.group(2))
    if not start_time and starts:
        start_time = f"{starts.hour:02d}:{starts.minute:02d}"
    if not end_time and ends:
        # Tix endDate is the run's final day, so only its clock is meaningful.
        end_time = f"{ends.hour:02d}:{ends.minute:02d}"
        notes["endTimeFrom"] = "tix-detail"

    # Tix `repeats` is an occurrence count, not a flag: >0 means the event
    # recurs, but the cadence itself is never exposed.
    repeats = event.get("repeats")
    recurring = isinstance(repeats, (int, float)) and repeats > 0

    schedule: dict[str, Any] = {
        "eventType": "recurring" if recurring else "one-time",
        "startTime": start_time,
        "endTime": end_time,
    }
    if recurring:
        if starts:
            schedule["recurrence"] = {
                "days": [WEEKDAYS[starts.weekday()]],
                "startDate": starts.date().isoformat(),
                "endDate": ends.date().isoformat() if ends else None,
            }
            notes["recurrenceInferred"] = True
        else:
            schedule["recurrence"] = None
    else:
        schedule["date"] = starts.date().isoformat() if starts else None
    return schedule, notes


def _price_points(event: dict[str, Any]) -> list[dict[str, Any]] | None:
    tiers = event.get("tickets") or []
    points: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for idx, tier in enumerate(tiers, start=1):
        price = tier.get("price")
        if price is None or price in seen:
            continue
        seen.add(price)
        points.append(
            {
                "type": tier.get("name") or ("Free" if price == 0 else f"Tier {idx}"),
                "description": None,
                "price": price,
            }
        )
    return points or None


# ── enrichment ────────────────────────────────────────────────────────────────

_ENRICH_SYSTEM = """You classify Lagos events for a listings product. JSON only.

Given an event name and description, return:
  categories: array from this exact list, 1-3 best fits, [] if unclear —
    Food, Drinks, Dance, Rave, Art, Games, Music, Movies, Theater, Festival,
    Workshop, Seminar, Conference, Networking, Sports, Fitness, Wellness,
    Exhibition, Tour, Outdoors, Family, Kids, Charity, Educational, Business,
    Technology, Fashion, Beauty, Cultural, Social
  tags: 3-6 lowercase keyword tags, no '#'
  ageLimit: "All Ages" | "18+" | "21+" | null  (null unless stated)
  dressCode: short phrase or null (null unless stated)

Use null/[] rather than guessing. Do not invent facts absent from the text.
"""


def enrich_event(event: dict[str, Any], *, tix_detail: bool, geocode: bool, client: Any) -> dict[str, Any]:
    """Fill what the discovery feeds omit. Mutates `event`, returns notes."""
    from discover.places import geocode_address

    notes: dict[str, Any] = {}

    if tix_detail and event.get("source") == "tix" and event.get("slug"):
        from discover.tix import fetch_tix_event_detail

        detail = fetch_tix_event_detail(str(event["slug"]))
        if detail:
            for key in ("description", "endsAt", "organizerId", "organizerName"):
                if detail.get(key) is not None:
                    event[key] = detail[key]
            notes["tixDetail"] = True

    if geocode and event.get("address"):
        hit = geocode_address(str(event["address"]), client=client)
        if hit:
            event["lat"], event["lng"] = hit["lat"], hit["lng"]
            notes["geocoded"] = hit.get("precision")
    return notes


def infer_taxonomy(event: dict[str, Any]) -> dict[str, Any]:
    """DeepSeek categories/tags/ageLimit/dressCode from name + description."""
    from pipeline import deepseek_extract as ds

    if not ds.enabled():
        return {}
    name = (event.get("name") or "").strip()
    description = (event.get("description") or "").strip()
    if not name and not description:
        return {}

    key = ds._cache_key(
        handle="experience-taxonomy", post_id=name,
        caption=description[:1500], ocr_text="",
    )
    cached = ds.read_cached(key)
    if cached is not None:
        return cached

    user = json.dumps({"name": name, "description": description[:1500]}, ensure_ascii=False)
    original = ds._SYSTEM
    try:
        ds._SYSTEM = _ENRICH_SYSTEM
        result = ds._call_api(user) or {}
    finally:
        ds._SYSTEM = original

    clean: dict[str, Any] = {}
    cats = [c for c in (result.get("categories") or []) if c in VALID_CATEGORIES]
    if cats:
        clean["categories"] = cats[:3]
    tags = [
        str(t).strip().lstrip("#").lower()
        for t in (result.get("tags") or [])
        if str(t).strip()
    ]
    if tags:
        clean["tags"] = tags[:6]
    if result.get("ageLimit") in AGE_LIMITS:
        clean["ageLimit"] = result["ageLimit"]
    if isinstance(result.get("dressCode"), str) and result["dressCode"].strip():
        clean["dressCode"] = result["dressCode"].strip()[:60]

    ds.write_cached(key, clean)
    return clean


def to_experience_record(
    event: dict[str, Any], *, taxonomy: dict[str, Any] | None = None
) -> dict[str, Any]:
    taxonomy = taxonomy or {}
    source = event.get("source")
    name = (event.get("name") or "").strip() or None
    schedule, sched_notes = _schedule(event)
    label = event.get("address") or event.get("locationName")
    coords = (
        [float(event["lng"]), float(event["lat"])]
        if event.get("lat") is not None and event.get("lng") is not None
        else None
    )

    categories = taxonomy.get("categories") or _categories(
        f"{name or ''} {event.get('description') or ''}"
    )

    # Reisty events hang off a restaurant; Tix events off a ticketing account.
    source_type = "Restaurant" if source == "reisty" else "Organizer"
    owner_ref = (
        event.get("restaurantId") if source == "reisty" else event.get("organizerId")
    )

    record = {
        "name": name,
        "description": event.get("description"),
        "tags": taxonomy.get("tags"),
        "categories": categories,
        "ageLimit": taxonomy.get("ageLimit"),
        "dressCode": taxonomy.get("dressCode"),
        "schedule": schedule,
        "pricePoints": _price_points(event),
        "sourceType": source_type,
        "owner": None,  # resolved after the Restaurant/Organizer import
        "salesStart": None,
        "salesEnd": None,
        "location": {"label": label, "coordinates": coords} if label or coords else None,
        "appearances": None,
        "offers": None,
        # Not part of addExperienceValidation — strip before POSTing.
        "ownerRef": {
            "provider": source,
            "externalId": owner_ref,
            "name": event.get("organizerName"),
        } if owner_ref or event.get("organizerName") else None,
        "imageUrl": event.get("imageUrl"),
        "sourceUrl": event.get("url"),
    }

    required = [
        "name", "description", "tags", "categories",
        "pricePoints", "sourceType", "location",
    ]
    missing = [k for k in required if record.get(k) in (None, [], {})]
    missing.append("owner")  # always — needs a prior Restaurant/Organizer write
    if record["location"] and not record["location"].get("coordinates"):
        missing.append("location.coordinates")
    for key in ("startTime", "endTime"):
        if not schedule.get(key):
            missing.append(f"schedule.{key}")
    if schedule.get("eventType") == "one-time" and not schedule.get("date"):
        missing.append("schedule.date")
    if schedule.get("eventType") == "recurring" and not schedule.get("recurrence"):
        missing.append("schedule.recurrence")

    filled = [k for k, v in record.items() if v not in (None, [], {})]
    return {
        "record": record,
        "_meta": {
            "source": source,
            "filledCount": len(filled),
            "fieldCount": len(record),
            "missingRequired": missing,
            **sched_notes,
        },
    }


def collect(limit: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    from discover.reisty import fetch_reisty_events
    from discover.tix import fetch_tix_events

    for label, fn in (
        ("reisty", lambda: fetch_reisty_events(limit=limit)),
        ("tix", lambda: fetch_tix_events(limit=limit, lagos_only=True)),
    ):
        try:
            rows = fn()
            print(f"  {label}: {len(rows)} events", file=sys.stderr)
            events.extend(rows)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label}: FAILED — {exc}", file=sys.stderr)
    return events


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--per-source", type=int, default=30)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument(
        "--primary", default="tix",
        help="lead provider for the sample (empty = both)",
    )
    ap.add_argument("--no-detail", action="store_true", help="skip Tix detail query")
    ap.add_argument("--no-geocode", action="store_true", help="skip coordinate backfill")
    ap.add_argument("--no-llm", action="store_true", help="skip DeepSeek taxonomy")
    ap.add_argument("--exclude", default=None, help="prior output file to skip")
    ap.add_argument("--out", default="docs/sample_payloads/exploree_experiences_sample.json")
    args = ap.parse_args()

    print("fetching Lagos events…", file=sys.stderr)
    events = collect(args.per_source)
    if not events:
        print("no events fetched", file=sys.stderr)
        return 1

    pool = events
    if args.primary:
        led = [e for e in pool if e.get("source") == args.primary]
        if led:
            pool = led
        else:
            print(f"  ! no {args.primary} events; using all", file=sys.stderr)
    if args.exclude:
        prior = json.loads(Path(args.exclude).read_text())
        skip = {(r.get("name") or "").strip().lower() for r in prior}
        fresh = [e for e in pool if (e.get("name") or "").strip().lower() not in skip]
        print(f"  excluding {len(pool) - len(fresh)} already-sampled", file=sys.stderr)
        pool = fresh or pool

    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    rng = random.Random(seed)
    sample = rng.sample(pool, min(args.count, len(pool)))

    import httpx

    notes: list[dict[str, Any]] = []
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        for event in sample:
            notes.append(
                enrich_event(
                    event,
                    tix_detail=not args.no_detail,
                    geocode=not args.no_geocode,
                    client=client,
                )
            )
    print(
        f"  enriched: {sum(1 for n in notes if n.get('tixDetail'))} tix-detail, "
        f"{sum(1 for n in notes if n.get('geocoded'))} geocoded",
        file=sys.stderr,
    )

    taxo = [{} if args.no_llm else infer_taxonomy(e) for e in sample]
    print(f"  deepseek: {sum(1 for t in taxo if t)} classified", file=sys.stderr)

    mapped = [to_experience_record(e, taxonomy=t) for e, t in zip(sample, taxo)]
    for row in mapped:
        print(
            f"  {row['record']['name'][:40]}: {row['_meta']['filledCount']}"
            f"/{row['_meta']['fieldCount']} | missing {row['_meta']['missingRequired']}",
            file=sys.stderr,
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([m["record"] for m in mapped], indent=2, ensure_ascii=False, default=str)
    )
    print(f"wrote {out} (seed={seed})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

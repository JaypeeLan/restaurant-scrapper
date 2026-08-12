"""
Map discovered Lagos venues into the exploree-api ``Restaurant`` create shape.

Every provider (Reisty, FlavorQueste, Google Places / OSM) is pulled, venues
are matched across providers, and each target field takes the best available
value — so one output row is the union of what all sources know, not one
source's slice. Fields nobody fills stay ``null``.

    python scripts/map_exploree_restaurants.py --count 5 --out out.json

The ``payload`` block is what would POST to /restaurant: only keys the Joi
schema allows (`.unknown(false)` rejects anything else). Provenance, photos,
and per-field attribution live in the sibling ``_meta`` block.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import unicodedata
from math import cos, radians
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# exploree-api: src/app/services/restaurant/restaurant/restaurant.types.ts
CUISINE_ENUM = {
    "italian": "Italian", "chinese": "Chinese", "indian": "Indian",
    "american": "American", "mexican": "Mexican", "french": "French",
    "japanese": "Japanese", "thai": "Thai", "mediterranean": "Mediterranean",
    "greek": "Greek", "spanish": "Spanish", "korean": "Korean",
    "vietnamese": "Vietnamese", "lebanese": "Lebanese", "brazilian": "Brazilian",
    "caribbean": "Caribbean", "moroccan": "Moroccan", "ethiopian": "Ethiopian",
    "turkish": "Turkish", "persian": "Persian", "nigerian": "Nigerian",
    "south african": "South African", "ghanaian": "Ghanaian",
    "kenyan": "Kenyan", "egyptian": "Egyptian", "tanzanian": "Tanzanian",
}

DAY_FULL = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}
ALL_DAYS = list(DAY_FULL.values())

# Provider precedence per field — first provider that has a usable value wins.
# Ordered by how trustworthy that provider is *for that specific field*.
FIELD_PRECEDENCE: dict[str, tuple[str, ...]] = {
    "coordinates":  ("google", "flavorqueste", "osm", "reisty"),
    "openingTimes": ("google", "flavorqueste", "reisty"),
    "description":  ("reisty", "flavorqueste", "google"),
    "phone":        ("reisty", "flavorqueste", "google", "osm"),
    "email":        ("reisty", "flavorqueste", "google"),
    "website":      ("reisty", "flavorqueste", "google", "osm"),
    "instagram":    ("reisty", "flavorqueste", "google"),
    "address":      ("google", "flavorqueste", "reisty", "osm"),
    # Google's priceRange is a real NGN band; the directories only have an
    # averaged spend, so Google leads even though both can answer.
    "minimumSpend": ("google", "reisty", "flavorqueste"),
    "dressCode":    ("reisty",),
    "cuisine":      ("flavorqueste", "reisty", "google"),
    # Google aggregates far more reviews than the local apps, so its score is
    # the trustworthy one even where a directory also has a rating.
    "rating":       ("google", "flavorqueste", "reisty"),
}

# Fields where the primary provider must NOT be promoted, because it has no
# such data at all and promoting it would just push a real value down.
_PRIMARY_EXEMPT = {"email", "dressCode", "description"}


def set_primary(provider: str) -> None:
    """Move `provider` to the front of every precedence list it can serve."""
    for field, order in list(FIELD_PRECEDENCE.items()):
        if field in _PRIMARY_EXEMPT or provider not in order:
            continue
        FIELD_PRECEDENCE[field] = (provider,) + tuple(
            p for p in order if p != provider
        )

# Joi: ^([01]\d|2[0-3]):([0-5]\d)$ — zero-padded, 24h, no 24:00.
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp])?\.?[Mm]?\.?")
_FQ_DAY = re.compile(r"([A-Za-z]{3})\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})")
# Google: 'Monday: 7:30 AM – 10:00 PM' with U+202F / U+2009 spaces, en dash.
_G_DAY = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*:\s*([^;]+)",
    re.I,
)
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_NOISE_WORDS = {"the", "lagos", "restaurant", "cafe", "bar", "lounge", "nigeria", "ng"}


def _clean_spaces(text: str) -> str:
    """Google pads times with U+202F / U+2009; normalise before parsing."""
    return unicodedata.normalize("NFKC", str(text)).replace(" ", " ")


def _hhmm(raw: str | None, meridiem: str | None = None) -> str | None:
    """Coerce to strict HH:MM, honouring AM/PM. None if unparseable."""
    if not raw:
        return None
    m = _CLOCK.search(_clean_spaces(raw))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = (meridiem or m.group(3) or "").lower()
    if ampm == "p" and hour != 12:
        hour += 12
    elif ampm == "a" and hour == 12:
        hour = 0
    if hour == 24:
        hour, minute = 23, 59
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _split_range(text: str) -> tuple[str | None, str | None]:
    """'7:30 AM – 10:00 PM' / '11:00-23:59' / '10:00 till 23:59' → (open, close)."""
    cleaned = _clean_spaces(text)
    if "closed" in cleaned.lower():
        return None, None
    if "24 hours" in cleaned.lower():
        return "00:00", "23:59"
    parts = re.split(r"[-–—]|\btill\b|\bto\b", cleaned, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None, None
    return _hhmm(parts[0]), _hhmm(parts[1])


def _parse_hours(hours: str | None) -> tuple[dict[str, Any] | None, bool]:
    """
    → ({Day: {open, close}}, inferred?)

    Google and FlavorQueste carry real per-day rows. Reisty gives one blob for
    the whole week, so it is expanded across all 7 days and flagged inferred.
    """
    if not hours:
        return None, False
    text = _clean_spaces(hours)

    google_days = _G_DAY.findall(text)
    if google_days:
        out: dict[str, Any] = {}
        for day, span in google_days:
            open_t, close_t = _split_range(span)
            if open_t and close_t:
                out[day.capitalize()] = {"open": open_t, "close": close_t}
        return (out or None), False

    fq_days = _FQ_DAY.findall(text)
    if fq_days:
        out = {}
        for abbr, open_t, close_t in fq_days:
            day = DAY_FULL.get(abbr.lower()[:3])
            o, c = _hhmm(open_t), _hhmm(close_t)
            if day and o and c:
                out[day] = {"open": o, "close": c}
        return (out or None), False

    open_t, close_t = _split_range(text)
    if open_t and close_t:
        return {d: {"open": open_t, "close": close_t} for d in ALL_DAYS}, True
    return None, False


# ── cross-provider venue matching ─────────────────────────────────────────────

def _norm_name(name: str) -> str:
    text = _PUNCT.sub(" ", _clean_spaces(name).lower())
    tokens = [t for t in text.split() if t and t not in _NOISE_WORDS]
    return " ".join(tokens)


def _near(a: dict[str, Any], b: dict[str, Any], *, metres: int = 400) -> bool | None:
    """None when either side has no coords — 'unknown', not 'far apart'."""
    try:
        alat, alng = float(a["lat"]), float(a["lng"])
        blat, blng = float(b["lat"]), float(b["lng"])
    except (TypeError, ValueError, KeyError):
        return None
    dlat = (alat - blat) * 111_320
    dlng = (alng - blng) * 111_320 * cos(radians(alat))
    return (dlat**2 + dlng**2) ** 0.5 <= metres


def group_venues(places: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """
    Cluster the same venue across providers.

    Normalised-name equality is the key; coordinates only *veto* a match when
    both sides have them and disagree, so Reisty's missing geo never blocks a
    merge it should have joined.
    """
    clusters: list[list[dict[str, Any]]] = []
    by_name: dict[str, list[int]] = {}
    for place in places:
        key = _norm_name(place.get("name") or "")
        if not key:
            clusters.append([place])
            continue
        placed = False
        for idx in by_name.get(key, []):
            if any(_near(place, other) is False for other in clusters[idx]):
                continue
            clusters[idx].append(place)
            placed = True
            break
        if not placed:
            clusters.append([place])
            by_name.setdefault(key, []).append(len(clusters) - 1)
    return clusters


def _pick(
    cluster: list[dict[str, Any]],
    field: str,
    extract: Callable[[dict[str, Any]], Any],
) -> tuple[Any, str | None]:
    """Best value for `field` across the cluster → (value, winning provider)."""
    order = FIELD_PRECEDENCE.get(field, ())
    ranked = sorted(
        cluster,
        key=lambda p: order.index(p.get("source"))
        if p.get("source") in order
        else len(order),
    )
    for place in ranked:
        value = extract(place)
        if value not in (None, "", [], {}):
            return value, place.get("source")
    return None, None


# ── field extractors ──────────────────────────────────────────────────────────

_CUISINE_NOISE_FLOOR = 6


def _cuisine_of(place: dict[str, Any]) -> list[str] | None:
    """Only exact Cuisine enum members survive — the rest are unmappable."""
    raw: list[str] = []
    if place.get("amenity"):
        raw.append(str(place["amenity"]))
    secondary = list(place.get("categories") or []) + list(place.get("cuisines") or [])
    # A venue self-tagging into 14 buckets (one buka claims Chinese) is noise;
    # only the primary type is trustworthy at that point.
    if len(secondary) < _CUISINE_NOISE_FLOOR:
        raw.extend(secondary)
    hits: list[str] = []
    for value in raw:
        key = str(value).strip().lower()
        if key in CUISINE_ENUM and CUISINE_ENUM[key] not in hits:
            hits.append(CUISINE_ENUM[key])
    return hits or None


def _spend_of(place: dict[str, Any]) -> int | None:
    """FlavorQueste avg_budget is a float ('26666.67'); Reisty AverageCost too."""
    raw = place.get("avgBudget")
    if raw in (None, "", 0, "0"):
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    try:
        return round(float(cleaned)) or None
    except ValueError:
        return None


def _dress_of(place: dict[str, Any]) -> bool | None:
    raw = place.get("dressCode")
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text or text in {"none", "no", "n/a", "nil", "-", "any"}:
        return False
    return True


def _coords_of(place: dict[str, Any]) -> list[float] | None:
    """
    exploree Address.coordinates is [longitude, latitude] — flip ours.

    Reisty ships Latitude/Longitude as a literal 0.0 on every row, so null
    island has to be rejected rather than posted as a real fix.
    """
    lat, lng = place.get("lat"), place.get("lng")
    if lat is None or lng is None:
        return None
    lat, lng = float(lat), float(lng)
    if abs(lat) < 0.01 and abs(lng) < 0.01:
        return None
    return [lng, lat]


def _hours_of(place: dict[str, Any]) -> dict[str, Any] | None:
    return _parse_hours(place.get("hours"))[0]


def _attrs(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for place in cluster:
        for key, value in (place.get("attrs") or {}).items():
            merged.setdefault(key, value)
    return merged


def _meal_of(cluster: list[dict[str, Any]]) -> list[str] | None:
    """Google `serves*` booleans → Meal[]. Supper has no Google equivalent."""
    attrs = _attrs(cluster)
    pairs = (
        ("servesBreakfast", "Breakfast"),
        ("servesBrunch", "Brunch"),
        ("servesLunch", "Lunch"),
        ("servesDinner", "Dinner"),
    )
    hits = [meal for key, meal in pairs if attrs.get(key) is True]
    return hits or None


def _service_of(cluster: list[dict[str, Any]]) -> list[str] | None:
    """Service enum is only Walk-In / Order."""
    attrs = _attrs(cluster)
    hits: list[str] = []
    if attrs.get("dineIn") is True:
        hits.append("Walk-In")
    if attrs.get("takeout") is True or attrs.get("delivery") is True:
        hits.append("Order")
    return hits or None


def _seating_of(cluster: list[dict[str, Any]]) -> list[str] | None:
    """Google only distinguishes outdoor seating; the other 14 enum values
    have no structured source."""
    attrs = _attrs(cluster)
    return ["Outdoor Seating"] if attrs.get("outdoorSeating") is True else None


def _dietary_of(cluster: list[dict[str, Any]]) -> list[str] | None:
    attrs = _attrs(cluster)
    return ["Vegetarian"] if attrs.get("servesVegetarianFood") is True else None


def _suitable_of(cluster: list[dict[str, Any]]) -> list[str] | None:
    attrs = _attrs(cluster)
    hits: list[str] = []
    if attrs.get("goodForGroups") is True:
        hits.append("Groups")
    if attrs.get("reservable") is True and "Events" not in hits:
        hits.append("Events")
    return hits or None


def _price_range_spend(place: dict[str, Any]) -> int | None:
    """Google priceRange.startPrice is already NGN — better than an average."""
    rng = place.get("priceRange") or {}
    start = (rng.get("startPrice") or {}).get("units")
    if start is None:
        return None
    try:
        return int(str(start))
    except ValueError:
        return None


def _bool_attr(cluster: list[dict[str, Any]], key: str) -> bool | None:
    attrs = _attrs(cluster)
    value = attrs.get(key)
    return bool(value) if value is not None else None


def _wheelchair_of(cluster: list[dict[str, Any]]) -> bool | None:
    for place in cluster:
        access = place.get("accessibility") or {}
        if access:
            return bool(access.get("wheelchairAccessibleEntrance"))
    return None


_VIBE_SYSTEM = """You infer venue ambience attributes for a Lagos restaurant directory.
You are given a venue description and real customer reviews. Return JSON only.

Keys, each either an allowed value or null. Use null whenever the text does not
clearly support a value — do NOT guess. Most venues should have several nulls.

  lighting:      "Soft Lights" | "Bright Lights"
  coziness:      "Cozy" | "Spacious"
  music:         "Loud Music" | "Soft Music" | "No Music"
  picturesque:   "Great" | "Decent" | "Bad"
  bathroom:      "Great" | "Decent" | "Bad"
  serviceSpeed:  "Fast" | "Normal" | "Slow"
  decorType:     short phrase, max 4 words
  rooftop:       true | false
  evidence:      object mapping each non-null key to the phrase that justified it
"""

_VIBE_ALLOWED = {
    "lighting": {"Soft Lights", "Bright Lights"},
    "coziness": {"Cozy", "Spacious"},
    "music": {"Loud Music", "Soft Music", "No Music"},
    "picturesque": {"Great", "Decent", "Bad"},
    "bathroom": {"Great", "Decent", "Bad"},
    "serviceSpeed": {"Fast", "Normal", "Slow"},
}


def infer_vibe(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """
    DeepSeek over description + Google reviews for the fields with no
    structured source. Returns {} when disabled or unsupported by the text.
    """
    from pipeline import deepseek_extract as ds

    if not ds.enabled():
        return {}
    reviews: list[str] = []
    for place in cluster:
        reviews.extend(place.get("reviews") or [])
    description = next((p.get("description") for p in cluster if p.get("description")), "")
    if not reviews and not description:
        return {}

    name = next((p.get("name") for p in cluster if p.get("name")), "")
    key = ds._cache_key(
        handle="vibe", post_id=name, caption=(description or "")[:400],
        ocr_text=" || ".join(reviews)[:2000],
    )
    cached = ds.read_cached(key)
    if cached is not None:
        return cached

    user = json.dumps(
        {
            "name": name,
            "description": (description or "")[:800],
            "reviews": [r[:600] for r in reviews[:5]],
        },
        ensure_ascii=False,
    )
    # Reuse the shared client but swap in the ambience system prompt.
    original = ds._SYSTEM
    try:
        ds._SYSTEM = _VIBE_SYSTEM
        result = ds._call_api(user) or {}
    finally:
        ds._SYSTEM = original

    clean: dict[str, Any] = {}
    for field, allowed in _VIBE_ALLOWED.items():
        value = result.get(field)
        if isinstance(value, str) and value in allowed:
            clean[field] = value
    if isinstance(result.get("decorType"), str) and result["decorType"].strip():
        clean["decorType"] = result["decorType"].strip()[:60]
    if isinstance(result.get("rooftop"), bool):
        clean["rooftop"] = result["rooftop"]
    if isinstance(result.get("evidence"), dict):
        clean["_evidence"] = result["evidence"]

    ds.write_cached(key, clean)
    return clean


def enrich_cluster(
    cluster: list[dict[str, Any]],
    *,
    geocode: bool,
    resolve_ig: bool,
    client: Any = None,
) -> dict[str, Any]:
    """
    Fill gaps the providers left, in place. Returns a notes dict describing
    what was backfilled and how trustworthy it is.
    """
    from discover.places import geocode_address, handle_from_website

    notes: dict[str, Any] = {}

    if geocode and not any(_coords_of(p) for p in cluster):
        label, _ = _pick(cluster, "address", lambda p: p.get("address"))
        if label:
            hit = geocode_address(label, client=client)
            if hit:
                cluster.append({
                    "source": "geocode",
                    "name": cluster[0].get("name"),
                    "lat": hit["lat"],
                    "lng": hit["lng"],
                })
                notes["geocoded"] = hit.get("precision")

    if resolve_ig and not any(p.get("instagramHint") for p in cluster):
        website, _ = _pick(cluster, "website", lambda p: p.get("website"))
        if website:
            handle = handle_from_website(
                website, name=cluster[0].get("name") or "", client=client
            )
            if handle:
                cluster.append({
                    "source": "website",
                    "name": cluster[0].get("name"),
                    "instagramHint": handle,
                })
                notes["instagramFrom"] = "website"
    return notes


def to_restaurant_payload(
    cluster: list[dict[str, Any]], *, vibe: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One cross-provider venue cluster → exploree `addRestaurantValidation`."""
    attribution: dict[str, str] = {}
    vibe = vibe or {}

    def pick(field: str, extract: Callable[[dict[str, Any]], Any]) -> Any:
        value, provider = _pick(cluster, field, extract)
        if provider:
            attribution[field] = provider
        return value

    name = next(
        ((p.get("name") or "").strip() for p in cluster if (p.get("name") or "").strip()),
        None,
    )
    coords = pick("coordinates", _coords_of)
    label = pick("address", lambda p: p.get("address"))
    hours = pick("openingTimes", _hours_of)
    email = pick("email", lambda p: p.get("email"))
    phone = pick("phone", lambda p: p.get("phone"))
    ig = pick("instagram", lambda p: p.get("instagramHint"))

    hours_inferred = False
    for place in cluster:
        parsed, inferred = _parse_hours(place.get("hours"))
        if parsed == hours:
            hours_inferred = inferred
            break

    payload = {
        "name": name,
        "description": pick("description", lambda p: p.get("description")),
        "socialMedia": {"ig": ig, "twitter": None} if ig else None,
        "address": {"label": label, "coordinates": coords} if label or coords else None,
        "emails": [email] if email else None,
        "phones": [phone] if phone else None,
        "website": pick("website", lambda p: p.get("website")),
        "whatsApp": None,             # no provider exposes a WhatsApp number
        "openingTimes": hours,
        "dateEstablished": None,      # no provider exposes a founding date
        "cuisine": pick("cuisine", _cuisine_of),
        "meal": _meal_of(cluster),          # google serves* booleans
        "service": _service_of(cluster),    # google dineIn / takeout / delivery
        "lighting": vibe.get("lighting"),   # inferred — no structured source
        "bathroom": vibe.get("bathroom"),
        "picturesque": vibe.get("picturesque"),
        "coziness": vibe.get("coziness"),
        "music": vibe.get("music"),
        "photographyAllowed": None,         # no source, structured or textual
        "suitableFor": _suitable_of(cluster),
        # Google's NGN priceRange beats a directory's average spend.
        "minimumSpend": pick(
            "minimumSpend", lambda p: _price_range_spend(p) or _spend_of(p)
        ),
        "dressCode": pick("dressCode", _dress_of),
        "dietaryOptions": _dietary_of(cluster),
        "serviceSpeed": vibe.get("serviceSpeed"),
        "decorType": vibe.get("decorType"),
        "seatingOptions": _seating_of(cluster),
        "wheelchair": _wheelchair_of(cluster),
        "outdoor": _bool_attr(cluster, "outdoorSeating"),
        "rooftop": vibe.get("rooftop"),
    }
    for field in (
        "lighting", "bathroom", "picturesque", "coziness",
        "music", "serviceSpeed", "decorType", "rooftop",
    ):
        if payload.get(field) is not None:
            attribution[field] = "deepseek"
    for field, fn in (
        ("meal", _meal_of), ("service", _service_of), ("seatingOptions", _seating_of),
        ("dietaryOptions", _dietary_of), ("suitableFor", _suitable_of),
    ):
        if fn(cluster):
            attribution[field] = "google"

    required = [
        "name", "address", "openingTimes", "meal", "service",
        "lighting", "minimumSpend", "dressCode", "seatingOptions",
    ]
    missing = [k for k in required if payload.get(k) in (None, [], {})]
    if payload["address"] and not payload["address"].get("coordinates"):
        missing.append("address.coordinates")
    if payload["address"] and not payload["address"].get("label"):
        missing.append("address.label")

    filled = [k for k, v in payload.items() if v not in (None, [], {})]

    # coverImage / imageUrl are Joi.forbidden on create — photos go up through
    # the separate upload endpoints after the restaurant exists.
    photos: list[str] = []
    for place in cluster:
        photos.extend(place.get("photos") or [])

    rating = pick("rating", lambda p: p.get("rating"))
    record = dict(payload)
    # Not part of addRestaurantValidation — `rating` has no Restaurant field at
    # all (reviews are their own model) and photos upload separately. Kept here
    # because they are venue data; strip both before POSTing.
    record["rating"] = round(float(rating), 2) if rating is not None else None
    record["photos"] = photos[:6] or None
    record["menuUrl"] = next((p.get("menuUrl") for p in cluster if p.get("menuUrl")), None)

    return {
        "record": record,
        "_meta": {
            "matchedProviders": sorted({str(p.get("source")) for p in cluster}),
            "fieldAttribution": attribution,
            "openingTimesInferred": hours_inferred,
            "filledCount": len(filled),
            "fieldCount": len(payload),
            "missingRequired": missing,
            "postable": not missing,
        },
    }


def collect(
    city: str, per_source: int, photo_limit: int, rich: bool = True
) -> list[dict[str, Any]]:
    """Every configured provider — Google only when a key is present."""
    from config import settings
    from discover.flavorqueste import search_flavorqueste
    from discover.places import search_google_places, search_osm
    from discover.reisty import search_reisty

    providers: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
        ("reisty", lambda: search_reisty(city, limit=per_source, detail=True)),
        ("flavorqueste", lambda: search_flavorqueste(city, limit=per_source)),
    ]
    if settings.GOOGLE_PLACES_API_KEY:
        providers.append(
            ("google", lambda: search_google_places(
                city, limit=per_source, photo_limit=photo_limit, rich=rich
            ))
        )
    else:
        providers.append(("osm", lambda: search_osm(city, limit=per_source)))

    places: list[dict[str, Any]] = []
    for label, fn in providers:
        try:
            rows = fn()
            print(f"  {label}: {len(rows)} places", file=sys.stderr)
            places.extend(rows)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label}: FAILED — {exc}", file=sys.stderr)
    return places


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="lagos")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--per-source", type=int, default=40)
    ap.add_argument("--photo-limit", type=int, default=3)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument(
        "--no-rich", action="store_true",
        help="skip Google's atmosphere fields (cheaper SKU, loses meal/service)",
    )
    ap.add_argument("--no-geocode", action="store_true", help="skip coordinate backfill")
    ap.add_argument("--no-ig", action="store_true", help="skip website→IG handle lookup")
    ap.add_argument("--no-llm", action="store_true", help="skip DeepSeek ambience inference")
    ap.add_argument(
        "--primary", default="google",
        help="lead provider: venues come from it and it wins field precedence "
             "(empty string = union of all providers, no promotion)",
    )
    ap.add_argument(
        "--exclude", default=None,
        help="path to a previous output file whose venues should be skipped",
    )
    ap.add_argument("--out", default="docs/sample_payloads/exploree_restaurants_sample.json")
    args = ap.parse_args()

    if args.primary:
        set_primary(args.primary)
        print(f"primary provider: {args.primary}", file=sys.stderr)

    print(f"fetching {args.city} venues…", file=sys.stderr)
    places = collect(args.city, args.per_source, args.photo_limit, rich=not args.no_rich)
    if not places:
        print("no places fetched", file=sys.stderr)
        return 1

    clusters = group_venues(places)
    multi = [c for c in clusters if len({p.get("source") for p in c}) > 1]
    print(
        f"  {len(places)} rows → {len(clusters)} venues "
        f"({len(multi)} matched across >1 provider)",
        file=sys.stderr,
    )

    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    rng = random.Random(seed)
    # Prefer clusters that actually exercise the merge.
    pool = multi if len(multi) >= args.count else multi + [c for c in clusters if c not in multi]
    # Google-led: the venue set is Google's, enriched by the directories —
    # rather than the union of every provider's catalogue.
    if args.primary:
        led = [c for c in pool if any(p.get("source") == args.primary for p in c)]
        if led:
            pool = led
        else:
            print(
                f"  ! no clusters contain {args.primary}; falling back to all",
                file=sys.stderr,
            )

    if args.exclude:
        prior = json.loads(Path(args.exclude).read_text())
        skip = {_norm_name(r.get("name") or "") for r in prior}
        fresh = [
            c for c in pool
            if _norm_name(next((p.get("name") or "") for p in c)) not in skip
        ]
        print(f"  excluding {len(pool) - len(fresh)} already-sampled venues", file=sys.stderr)
        pool = fresh or pool

    sample = rng.sample(pool, min(args.count, len(pool)))

    # Enrichment and inference are per-venue and billed, so only the sampled
    # venues get them — never the whole 100+ sweep.
    import httpx

    enrich_notes: list[dict[str, Any]] = []
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        for cluster in sample:
            enrich_notes.append(
                enrich_cluster(
                    cluster,
                    geocode=not args.no_geocode,
                    resolve_ig=not args.no_ig,
                    client=client,
                )
            )
    print(
        f"  backfilled: {sum(1 for n in enrich_notes if n.get('geocoded'))} geocoded, "
        f"{sum(1 for n in enrich_notes if n.get('instagramFrom'))} ig-from-website",
        file=sys.stderr,
    )

    vibes = [{} if args.no_llm else infer_vibe(c) for c in sample]
    print(
        f"  deepseek: {sum(1 for v in vibes if v)} venues returned ambience fields",
        file=sys.stderr,
    )

    mapped = [to_restaurant_payload(c, vibe=v) for c, v in zip(sample, vibes)]
    for row, notes in zip(mapped, enrich_notes):
        row["_meta"].update(notes)

    # The file is the venue data itself — a bare array of restaurants. Coverage
    # stats go to stderr so they stay visible without polluting the output.
    records = [m["record"] for m in mapped]

    population = [to_restaurant_payload(c) for c in clusters]
    gaps: dict[str, int] = {}
    for row in population:
        for key in row["_meta"]["missingRequired"]:
            gaps[key] = gaps.get(key, 0) + 1
    for key, count in sorted(gaps.items(), key=lambda kv: -kv[1]):
        print(f"  gap {key}: {count}/{len(population)} missing", file=sys.stderr)
    for row in mapped:
        print(
            f"  {row['record']['name']}: {row['_meta']['filledCount']}"
            f"/{row['_meta']['fieldCount']} via {'+'.join(row['_meta']['matchedProviders'])}",
            file=sys.stderr,
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, indent=2, ensure_ascii=False, default=str))
    print(f"wrote {out} (seed={seed})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

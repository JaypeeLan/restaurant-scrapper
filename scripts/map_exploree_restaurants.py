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
    # "Casual" is the absence of a dress code, not one. Mapping any non-empty
    # string to true marked casual venues as enforcing a policy.
    if not text or text in {
        "none", "no", "n/a", "nil", "-", "any", "casual", "none required",
        "no dress code", "relaxed", "informal",
    }:
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
    """Reisty floor plans name real areas; Google only flags outdoor."""
    hits = _floorplan_seating(cluster)
    if _attrs(cluster).get("outdoorSeating") is True and "Outdoor Seating" not in hits:
        hits.append("Outdoor Seating")
    return hits or None


def _dietary_of(cluster: list[dict[str, Any]]) -> list[str] | None:
    attrs = _attrs(cluster)
    return ["Vegetarian"] if attrs.get("servesVegetarianFood") is True else None


def _suitable_of(cluster: list[dict[str, Any]]) -> list[str] | None:
    attrs = _attrs(cluster)
    hits: list[str] = []
    max_party = _party_size(cluster)
    if attrs.get("goodForGroups") is True or (max_party or 0) >= 20:
        hits.append("Groups")
    # A venue seating 50+ is taking event bookings, not just large tables.
    if attrs.get("reservable") is True or (max_party or 0) >= 50:
        if "Events" not in hits:
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


# Reisty floor-plan names → SeatingOption enum. Names with no enum equivalent
# ('INDOOR', 'Gallery') are intentionally dropped rather than approximated.
_FLOORPLAN_MAP = (
    (("outdoor", "garden", "terrace", "patio", "poolside"), "Outdoor Seating"),
    (("bar",), "Bar Seating"),
    (("booth",), "Booths"),
    (("private", "vip"), "Private Dining Rooms"),
    (("lounge", "sofa"), "Sofas"),
    (("window",), "Window Seats"),
    (("high table", "hightable"), "High Tables"),
    (("counter",), "Counter Seating"),
    (("family",), "Family Tables"),
)


def _rating_band(value: Any, good: float, ok: float) -> str | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score >= good:
        return "Great"
    if score >= ok:
        return "Decent"
    return "Bad"


def _fq_stat(cluster: list[dict[str, Any]], key: str) -> Any:
    for place in cluster:
        stat = place.get("reviewsStat") or {}
        if isinstance(stat, dict) and stat.get(key) is not None:
            return stat[key]
    return None


def _tags(cluster: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for place in cluster:
        out.extend(str(t) for t in (place.get("tags") or []))
    return out


def _reisty_category(cluster: list[dict[str, Any]], key: str) -> Any:
    for place in cluster:
        cat = place.get("categoryRating") or {}
        if isinstance(cat, dict) and cat.get(key) is not None:
            return cat[key]
    return None


def _picturesque_of(cluster: list[dict[str, Any]]) -> str | None:
    """FlavorQueste and Reisty both score ambience; 'Snapworthy' is explicit."""
    if any("snapworthy" in t.lower() for t in _tags(cluster)):
        return "Great"
    return _rating_band(
        _fq_stat(cluster, "ambience") or _reisty_category(cluster, "Ambience"),
        4.5, 3.5,
    )


def _service_speed_of(cluster: list[dict[str, Any]]) -> str | None:
    """
    Proxy only: FlavorQueste rates service *quality*, not speed. A well-rated
    venue is rarely described as slow, but this is inference, not measurement.
    """
    band = _rating_band(_fq_stat(cluster, "service"), 4.5, 3.5)
    return {"Great": "Fast", "Decent": "Normal", "Bad": "Slow"}.get(band or "")


# Review vocabulary → Music enum. Loud wins ties: a venue with both a DJ and
# "chilled background music" mentioned is the louder of the two experiences.
_MUSIC_LOUD = re.compile(
    r"\b(dj|d\.j\.|live band|live music|afrobeat|amapiano|turn ?up|"
    r"dance ?floor|clubbing|nightclub|loud music|blasting|party vibe)\b", re.I
)
_MUSIC_SOFT = re.compile(
    r"\b(acoustic|saxophon|jazz|piano|soft music|background music|"
    r"ambient music|soothing music|mellow|chill(ed)? music)\b", re.I
)
_MUSIC_NONE = re.compile(r"\b(no music|music.{0,12}(absent|missing)|very quiet)\b", re.I)


def _reviews_text(cluster: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for place in cluster:
        parts.extend(r for r in (place.get("reviews") or []) if r)
        if place.get("description"):
            parts.append(str(place["description"]))
    return " ".join(parts)


def _music_of(cluster: list[dict[str, Any]]) -> str | None:
    """Structured tags first, then review vocabulary."""
    if any("live event" in t.lower() or "live music" in t.lower() for t in _tags(cluster)):
        return "Loud Music"
    if _bool_attr(cluster, "liveMusic"):
        return "Loud Music"
    text = _reviews_text(cluster)
    if not text:
        return None
    if _MUSIC_LOUD.search(text):
        return "Loud Music"
    if _MUSIC_SOFT.search(text):
        return "Soft Music"
    if _MUSIC_NONE.search(text):
        return "No Music"
    return None


def _floorplan_seating(cluster: list[dict[str, Any]]) -> list[str]:
    names = [
        str(n).lower()
        for place in cluster
        for n in (place.get("floorPlans") or [])
    ]
    hits: list[str] = []
    for name in names:
        for needles, enum_value in _FLOORPLAN_MAP:
            if any(needle in name for needle in needles) and enum_value not in hits:
                hits.append(enum_value)
    return hits


def _party_size(cluster: list[dict[str, Any]]) -> int | None:
    for place in cluster:
        size = place.get("partySize") or {}
        if isinstance(size, dict) and size.get("Max"):
            try:
                return int(size["Max"])
            except (TypeError, ValueError):
                continue
    return None


def _house_rules_dress(cluster: list[dict[str, Any]]) -> bool | None:
    """Reisty buries dress policy in an HTML house-rules blob."""
    for place in cluster:
        rules = place.get("houseRules")
        if rules and "dress" in str(rules).lower():
            return True
    return None


def _wheelchair_of(cluster: list[dict[str, Any]]) -> bool | None:
    for place in cluster:
        access = place.get("accessibility") or {}
        if access:
            return bool(access.get("wheelchairAccessibleEntrance"))
    return None


_VIBE_SYSTEM = """You infer venue ambience attributes for a Lagos restaurant directory
from its description and real customer reviews. Return JSON only.

Output values MUST be exactly one of the allowed strings below — these are
database enums, not free text. Never invent variants ("Dim", "Bright", "High",
"Moderate", "Low" are all INVALID). Use null when the text does not clearly
support a value; do NOT guess. Most venues should have several nulls.

  lighting:      "Soft Lights" | "Bright Lights"
                 Soft Lights  <- dim, moody, candlelit, low/mood lighting,
                                 intimate, romantic, dark, warm glow
                 Bright Lights <- bright, airy, well-lit, natural light,
                                 sunny, big windows, daylight
  coziness:      "Cozy" | "Spacious"
                 Cozy      <- intimate, snug, small, tucked away, warm
                 Spacious  <- large, open, roomy, airy, huge, expansive
  music:         "Loud Music" | "Soft Music" | "No Music"
  picturesque:   "Great" | "Decent" | "Bad"     (photogenic / aesthetic)
  bathroom:      "Great" | "Decent" | "Bad"
  serviceSpeed:  "Fast" | "Normal" | "Slow"
  decorType:     short phrase, max 4 words
  rooftop:       true | false
  seatingOptions: array of seating the reviews explicitly describe, from
                 EXACTLY these strings, [] if none are mentioned —
                 "Long Chairs", "Booths", "Paired Sitting", "Bar Seating",
                 "Outdoor Seating", "Private Dining Rooms", "Window Seats",
                 "High Tables", "Sofas", "Floor Seating", "Picnic Tables",
                 "Family Tables", "Bench Seating", "Counter Seating",
                 "Shared Tables"
  evidence:      object mapping each non-null key to the exact quoted phrase
                 from the reviews that justified it

A venue can be both dim and spacious, or bright and cozy — judge the two
independently rather than assuming they correlate.
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
    # Google caps reviews at 5 per venue, so pool every provider's text to get
    # closer to the 20-30 an ambience judgement really wants.
    reviews: list[str] = []
    for place in cluster:
        reviews.extend(r for r in (place.get("reviews") or []) if r)
    seen: set[str] = set()
    reviews = [r for r in reviews if not (r in seen or seen.add(r))][:30]

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
            "reviews": [r[:600] for r in reviews],
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
    if not isinstance(result, dict):
        return {}

    clean: dict[str, Any] = {}
    for field, allowed in _VIBE_ALLOWED.items():
        value = result.get(field)
        if isinstance(value, str) and value in allowed:
            clean[field] = value
    if isinstance(result.get("decorType"), str) and result["decorType"].strip():
        clean["decorType"] = result["decorType"].strip()[:60]
    if isinstance(result.get("rooftop"), bool):
        clean["rooftop"] = result["rooftop"]
    seats = [x for x in (result.get("seatingOptions") or []) if x in _SEATING_ENUM]
    if seats:
        clean["seatingOptions"] = seats[:6]
    if isinstance(result.get("evidence"), dict):
        clean["_evidence"] = result["evidence"]

    ds.write_cached(key, clean)
    return clean


_VISION_PROMPT = """You are looking at interior photos of one Lagos restaurant.
Judge the venue's physical atmosphere. Reply with JSON only.

Values MUST be exactly one of the allowed strings — these are database enums.
Never output variants like "Dim", "Bright", "High", "Medium", "Luxury".
Use null when the photos genuinely do not show enough to judge.

  lighting:    "Soft Lights" | "Bright Lights" | null
               Soft Lights   = warm/dim/moody, lamps, low ambient light
               Bright Lights = daylight, large windows, bright white fittings
  coziness:    "Cozy" | "Spacious" | null
               Cozy     = enclosed, intimate, low ceilings, tight seating
               Spacious = open, high ceilings, wide floor, airy
  picturesque: "Great" | "Decent" | "Bad" | null   (how photogenic the room is)
  rooftop:     true | false | null   (true only if clearly open-air and elevated)
  decorType:   short phrase, max 4 words, or null
  seatingOptions: array of any you can actually see, from EXACTLY these —
      "Long Chairs", "Booths", "Paired Sitting", "Bar Seating",
      "Outdoor Seating", "Private Dining Rooms", "Window Seats",
      "High Tables", "Sofas", "Floor Seating", "Picnic Tables",
      "Family Tables", "Bench Seating", "Counter Seating", "Shared Tables"
      ([] if no seating is visible)
  evidence:    one sentence describing what you actually saw

Judge lighting and coziness independently — a room can be dim and spacious,
or bright and cozy.

The images are mixed: some show the room, others are plated food, flyers,
menus or portraits. Work in two steps.

1. Decide which images actually show the venue's interior or seating area.
2. Judge lighting, coziness, picturesque, rooftop, decor and seating ONLY
   from those. Ignore the rest entirely — a dark photo of a coffee cup says
   nothing about the room's lighting.

Report how many images you used in `interiorFrames`. Only answer null when
none of the images show the space.
"""

# exploree-api SeatingOption enum, verbatim.
_SEATING_ENUM = {
    "Long Chairs", "Booths", "Paired Sitting", "Bar Seating", "Outdoor Seating",
    "Private Dining Rooms", "Window Seats", "High Tables", "Sofas",
    "Floor Seating", "Picnic Tables", "Family Tables", "Bench Seating",
    "Counter Seating", "Shared Tables",
}

_VISION_ALLOWED = {
    "lighting": {"Soft Lights", "Bright Lights"},
    "coziness": {"Cozy", "Spacious"},
    "picturesque": {"Great", "Decent", "Bad"},
}


def infer_vibe_vision(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Gemini over the venue photos we already store.

    Lighting and coziness are visual facts. Review text mentions lighting for
    only ~20% of Lagos venues, but most venues have photos — so the image is
    the higher-coverage source by a wide margin.
    """
    import base64
    import hashlib

    from config import settings

    if not settings.GEMINI_ENABLED or not settings.GEMINI_API_KEY:
        return {}

    urls: list[str] = []
    # An interior-targeted image search beats whatever the venue last posted;
    # directory and grid photos are the fallback when it returns nothing.
    from discover.places import venue_interior_images

    name_for_search = next((p.get("name") for p in cluster if p.get("name")), "")
    urls.extend(venue_interior_images(name_for_search, limit=6))
    for provider in ("flavorqueste", "google", "reisty", "instagram"):
        for place in cluster:
            if place.get("source") == provider:
                urls.extend(u for u in (place.get("photos") or []) if u)
    urls = list(dict.fromkeys(urls))[: settings.GEMINI_MAX_PHOTOS]
    if not urls:
        return {}

    cache_dir = Path(__file__).resolve().parent.parent / ".cache" / "vision"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256("|".join(urls).encode()).hexdigest()[:32]
    cached = cache_dir / f"{key}.json"
    if cached.exists():
        try:
            return json.loads(cached.read_text())
        except json.JSONDecodeError:
            pass

    import httpx

    parts: list[dict[str, Any]] = [{"text": _VISION_PROMPT}]
    with httpx.Client(timeout=45.0, follow_redirects=True) as img_client:
        for url in urls:
            try:
                resp = img_client.get(url)
                if resp.status_code >= 400 or not resp.content:
                    continue
                mime = resp.headers.get("content-type", "").split(";")[0].strip()
                # A 404 page is HTML. Previously this was relabelled as JPEG
                # and posted anyway, which is what "Unable to process input
                # image" was: the model being handed a web page.
                if not mime.startswith("image/"):
                    continue
                if len(resp.content) < 2_000:
                    continue  # tracking pixels and placeholder thumbnails
                parts.append({
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(resp.content).decode(),
                    }
                })
            except Exception as exc:  # noqa: BLE001
                print(f"    photo fetch failed: {exc}", file=sys.stderr)
    if len(parts) == 1:  # prompt only, every image failed
        return {}

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.GEMINI_MODEL}:generateContent"
    )
    import time

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    # On the free tier one model can be "experiencing high demand" for minutes
    # while a sibling answers immediately, so exhaust models before giving up.
    models = [settings.GEMINI_MODEL] + [
        m for m in settings.GEMINI_FALLBACK_MODELS if m != settings.GEMINI_MODEL
    ]
    result: dict[str, Any] | None = None
    last_message = ""
    try:
        with httpx.Client(timeout=settings.GEMINI_TIMEOUT_S) as client:
            for model in models:
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent"
                )
                for attempt in range(2):
                    resp = client.post(
                        url,
                        headers={
                            "x-goog-api-key": settings.GEMINI_API_KEY,
                            "Content-Type": "application/json",
                        },
                        json=body,
                    )
                    payload = resp.json()
                    error = payload.get("error") or {}
                    if not error:
                        result = json.loads(
                            payload["candidates"][0]["content"]["parts"][0]["text"]
                        )
                        break
                    last_message = str(error.get("message", ""))
                    transient = (
                        "high demand" in last_message
                        or "overloaded" in last_message
                        or resp.status_code in (500, 503)
                    )
                    if not transient:
                        break  # quota or bad request: another model will not help
                    if attempt == 0:
                        time.sleep(3)
                if result is not None:
                    break
    except Exception as exc:  # noqa: BLE001
        print(f"    gemini failed: {exc}", file=sys.stderr)
        return {}
    if result is None:
        print(f"    gemini: {last_message[:90]}", file=sys.stderr)
        return {}
    # responseMimeType asks for JSON, not for an object. The model sometimes
    # answers with a single-element array, which used to crash the parse.
    if isinstance(result, list):
        result = next((r for r in result if isinstance(r, dict)), None)
    if not isinstance(result, dict):
        return {}

    clean: dict[str, Any] = {}
    for field, allowed in _VISION_ALLOWED.items():
        if result.get(field) in allowed:
            clean[field] = result[field]
    if isinstance(result.get("rooftop"), bool):
        clean["rooftop"] = result["rooftop"]
    if isinstance(result.get("decorType"), str) and result["decorType"].strip():
        clean["decorType"] = result["decorType"].strip()[:60]
    seats = [
        s for s in (result.get("seatingOptions") or [])
        if s in _SEATING_ENUM
    ]
    if seats:
        clean["seatingOptions"] = seats[:6]
    if isinstance(result.get("evidence"), str):
        clean["_evidence"] = result["evidence"][:300]
    if isinstance(result.get("interiorFrames"), int):
        clean["_interiorFrames"] = result["interiorFrames"]
    clean["_photos"] = len(parts) - 1

    cached.write_text(json.dumps(clean))
    return clean


_MENU_SYSTEM = """You read a restaurant menu and infer what the venue serves.
Reply with JSON only.

Judge from the DISHES, not from marketing words. "Upscale Dining" is not a
cuisine; jollof rice and egusi are. A menu may support several cuisines.

  cuisine: array of 0-4 values, each EXACTLY one of —
    Italian, Chinese, Indian, American, Mexican, French, Japanese, Thai,
    Mediterranean, Greek, Spanish, Korean, Vietnamese, Lebanese, Brazilian,
    Caribbean, Moroccan, Ethiopian, Turkish, Persian, Nigerian,
    South African, Ghanaian, Kenyan, Egyptian, Tanzanian
    (Return [] if the dishes do not clearly indicate any of these. Do NOT
     substitute near-misses: "Continental" and "Asian" are not on the list.)

  meal: array of 0-5 values, each EXACTLY one of —
    Breakfast, Brunch, Lunch, Dinner, Supper
    Infer from the dishes and any section headings: eggs/pastries -> Breakfast,
    a stated brunch section -> Brunch, mains/steaks -> Lunch and Dinner,
    late-night small plates -> Supper.

  evidence: object mapping each returned value to a dish that justified it
"""

_MEAL_ENUM = {"Breakfast", "Brunch", "Lunch", "Dinner", "Supper"}


def infer_menu_taxonomy(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Read the venue's actual menu to derive cuisine and meal service.

    A self-applied directory tag says "Upscale Dining"; the menu says whether
    the kitchen cooks Nigerian or Italian food, and whether it serves
    breakfast. This is the only route that works for bars and nightclubs,
    where Google populates no `serves*` attributes at all.
    """
    from pipeline import deepseek_extract as ds
    from pipeline.web_menu import WebMenuSource, url_to_menu_text

    if not ds.enabled():
        return {}
    menu_url = next((p.get("menuUrl") for p in cluster if p.get("menuUrl")), None)

    name = next((p.get("name") for p in cluster if p.get("name")), "")
    text = ""
    if menu_url:
        source = WebMenuSource(
            title="menu", url=str(menu_url),
            kind="pdf" if str(menu_url).lower().endswith(".pdf") else "page",
            aggregator="website",
        )
        try:
            text = url_to_menu_text(source)
        except Exception as exc:  # noqa: BLE001
            print(f"    menu fetch failed: {exc}", file=sys.stderr)
        text = re.sub(r"\s+", " ", text or "").strip()
        # Nav chrome and cookie banners come back as a few hundred chars with
        # no dishes in them; that is not a menu.
        if len(text) < 400:
            text = ""

    # Plenty of Lagos cafes publish no menu anywhere. Their description, their
    # categories and what reviewers say they ate are weaker evidence than a
    # menu, but they are evidence.
    from_menu = bool(text)
    if not text:
        parts: list[str] = []
        for place in cluster:
            if place.get("description"):
                parts.append(str(place["description"]))
            parts.extend(str(t) for t in (place.get("tags") or []))
            parts.extend(str(c) for c in (place.get("categories") or []))
            if place.get("amenity"):
                parts.append(str(place["amenity"]))
            parts.extend(r for r in (place.get("reviews") or []) if r)
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        if len(text) < 200:
            return {}

    key = ds._cache_key(
        handle="menu-taxonomy-v2", post_id=name, caption=text[:4000], ocr_text="",
    )
    cached = ds.read_cached(key)
    if cached is not None:
        return cached

    user = json.dumps(
        {
            "venue": name,
            "evidenceType": "menu" if from_menu else "description_and_reviews",
            "text": text[:6000],
        },
        ensure_ascii=False,
    )
    original = ds._SYSTEM
    try:
        ds._SYSTEM = _MENU_SYSTEM
        result = ds._call_api(user) or {}
    finally:
        ds._SYSTEM = original

    clean: dict[str, Any] = {}
    cuisines = [
        c for c in (result.get("cuisine") or [])
        if isinstance(c, str) and c in set(CUISINE_ENUM.values())
    ]
    if cuisines:
        clean["cuisine"] = cuisines[:4]
    meals = [m for m in (result.get("meal") or []) if m in _MEAL_ENUM]
    if meals:
        clean["meal"] = meals
    if isinstance(result.get("evidence"), dict):
        clean["_evidence"] = result["evidence"]
    clean["_menuChars"] = len(text)
    clean["_fromMenu"] = from_menu

    ds.write_cached(key, clean)
    return clean


_SERP_SYSTEM = """You read Google result snippets about a specific Lagos venue
and extract only facts that are definitely about THAT venue. JSON only.

The danger is same-name entities: "Tiffany Amber Cafe" shares a name with a
Nigerian fashion house founded in 1998, which is NOT the cafe. If a snippet is
about a different business — a clothing brand, a chain in another country, a
person — ignore it entirely.

  dateEstablished: "YYYY" or null
      Only when a snippet states this venue/restaurant/cafe was founded,
      established, or opened in that year. A parent fashion brand's founding
      year is not the cafe's.
  whatsApp: digits only, or null
      Only when a snippet ties the number to WhatsApp specifically.
  dressCode: true | false | null
      true  only when a snippet describes THIS venue enforcing a dress code
            or door policy ("smart casual required", "no shorts or slippers").
      false when a snippet describes it as casual / no dress code.
      null  when nothing addresses it. A dress code quoted for a venue in
            another city or country is not this venue's.
  bathroom: "Great" | "Decent" | "Bad" | null
      Judge only from reviewers describing the restroom/toilet of THIS venue.
      Clean/well-kept -> Great, usable/unremarkable -> Decent,
      dirty/broken/no water -> Bad.
  evidence: the exact snippet phrase you used for each non-null value, or {}

Return null rather than a plausible guess. Most venues will have nulls.
"""

_BATHROOM_ENUM = {"Great", "Decent", "Bad"}


def infer_from_serp(cluster: list[dict[str, Any]], *, city: str = "Lagos") -> dict[str, Any]:
    """
    Google result snippets → founding year / WhatsApp, disambiguated by LLM.

    The facts exist in search (a venue's own hiring post states its founding
    year), but a regex over snippets confidently returns the wrong entity's
    data when names collide.
    """
    from config import settings
    from pipeline import deepseek_extract as ds

    if not settings.SERPER_API_KEY or not ds.enabled():
        return {}
    name = next((p.get("name") for p in cluster if p.get("name")), "")
    if not name:
        return {}

    import httpx

    # One query per fact — a single blended query buries the dress-code and
    # restroom answers under generic listing pages.
    queries = [
        f"{name} {city} restaurant founded OR established year",
        f"does {name} {city} have a dress code",
        f"{name} {city} review restroom toilet clean",
        f"{name} {city} whatsapp contact",
    ]
    snippets: list[str] = []
    try:
        with httpx.Client(timeout=30.0) as client:
            for query in queries:
                resp = client.post(
                    settings.SERPER_ENDPOINT,
                    headers={
                        "X-API-KEY": settings.SERPER_API_KEY,
                        "Content-Type": "application/json",
                    },
                    json={"q": query, "num": 8, "gl": settings.SERPER_COUNTRY},
                )
                if resp.status_code >= 400:
                    continue
                for item in (resp.json().get("organic") or []):
                    text = f"{item.get('title','')} — {item.get('snippet','')}".strip(" —")
                    if text and text not in snippets:
                        snippets.append(text[:300])
    except Exception as exc:  # noqa: BLE001
        print(f"    serp lookup failed: {exc}", file=sys.stderr)
        return {}
    if not snippets:
        return {}

    address = next((p.get("address") for p in cluster if p.get("address")), "")
    key = ds._cache_key(
        handle="serp-facts-v2", post_id=name,
        caption=" || ".join(snippets)[:6000], ocr_text=str(address)[:200],
    )
    cached = ds.read_cached(key)
    if cached is not None:
        return cached

    user = json.dumps(
        {"venue": name, "city": city, "address": address, "snippets": snippets[:30]},
        ensure_ascii=False,
    )
    original = ds._SYSTEM
    try:
        ds._SYSTEM = _SERP_SYSTEM
        result = ds._call_api(user) or {}
    finally:
        ds._SYSTEM = original

    clean: dict[str, Any] = {}
    year = str(result.get("dateEstablished") or "").strip()
    if re.fullmatch(r"(?:19|20)\d{2}", year):
        clean["dateEstablished"] = f"{year}-01-01"
    wa = re.sub(r"[^\d+]", "", str(result.get("whatsApp") or ""))
    if len(wa) >= 10:
        clean["whatsApp"] = f"+234{wa[1:]}" if wa.startswith("0") else wa
    if isinstance(result.get("dressCode"), bool):
        clean["dressCode"] = result["dressCode"]
    if result.get("bathroom") in _BATHROOM_ENUM:
        clean["bathroom"] = result["bathroom"]
    if isinstance(result.get("evidence"), dict) and clean:
        clean["_evidence"] = result["evidence"]

    ds.write_cached(key, clean)
    return clean


def enrich_cluster(
    cluster: list[dict[str, Any]],
    *,
    geocode: bool,
    resolve_ig: bool,
    find_menu: bool = True,
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
        from discover.places import handle_from_search

        name = cluster[0].get("name") or ""
        website, _ = _pick(cluster, "website", lambda p: p.get("website"))
        handle = None
        origin = None
        # The venue's own site is authoritative when it links Instagram;
        # search is the fallback for venues with no reachable site.
        if website:
            handle = handle_from_website(website, name=name, client=client)
            origin = "website" if handle else None
        if not handle:
            handle = handle_from_search(name, client=client)
            origin = "serper" if handle else None
        if handle:
            cluster.append({
                "source": origin,
                "name": name,
                "instagramHint": handle,
            })
            notes["instagramFrom"] = origin

    if resolve_ig:
        from discover.places import harvest_contacts

        site, _ = _pick(cluster, "website", lambda p: p.get("website"))
        need_email = not any(p.get("email") for p in cluster)
        need_wa = not any(p.get("whatsApp") for p in cluster)
        if site and (need_email or need_wa):
            found = harvest_contacts(site, client=client)
            if found:
                row = {"source": "ownsite", "name": cluster[0].get("name")}
                if need_email and found.get("emails"):
                    row["email"] = found["emails"][0]
                if need_wa and found.get("whatsApp"):
                    row["whatsApp"] = found["whatsApp"]
                if len(row) > 2:
                    cluster.append(row)
                    notes["contactsFrom"] = "ownsite"

    if find_menu and not any(p.get("menuUrl") for p in cluster):
        # Lagos venues publish menus behind link-in-bio pages far more often
        # than on their own site, so follow the aggregator's outbound links.
        website, _ = _pick(cluster, "website", lambda p: p.get("website"))
        if website:
            try:
                from pipeline.web_menu import discover_menu_sources, pick_best_source

                best = pick_best_source(discover_menu_sources(website))
                if best and best.url:
                    cluster.append({
                        "source": "website",
                        "name": cluster[0].get("name"),
                        "menuUrl": best.url,
                    })
                    notes["menuFrom"] = best.aggregator
            except Exception as exc:  # noqa: BLE001
                print(f"    menu discovery failed: {exc}", file=sys.stderr)
    return notes


def _resolve_dress_code(cluster, pick, house_rules) -> bool:
    explicit = pick("dressCode", _dress_of)
    if explicit is not None:
        return bool(explicit)
    for place in cluster:
        if place.get("source") == "serp" and place.get("dressCode") is not None:
            return bool(place["dressCode"])
    return bool(house_rules(cluster))


def to_restaurant_payload(
    cluster: list[dict[str, Any]], *, vibe: dict[str, Any] | None = None,
    menu: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One cross-provider venue cluster → exploree `addRestaurantValidation`."""
    attribution: dict[str, str] = {}
    vibe = vibe or {}
    menu = menu or {}

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
        "whatsApp": next((p.get("whatsApp") for p in cluster if p.get("whatsApp")), None),
        "openingTimes": hours,
        "dateEstablished": next(
            (p.get("dateEstablished") for p in cluster if p.get("dateEstablished")), None
        ),
        # The menu beats a self-applied "Upscale Dining" tag.
        "cuisine": (menu.get("cuisine") or pick("cuisine", _cuisine_of)),
        # Google populates no serves* for bars/nightclubs; menus do.
        "meal": (_meal_of(cluster) or menu.get("meal")),
        "service": _service_of(cluster),    # google dineIn / takeout / delivery
        "lighting": vibe.get("lighting"),   # inferred — no structured source
        "bathroom": next(
            (p.get("bathroom") for p in cluster if p.get("bathroom")), None
        ) or vibe.get("bathroom"),
        # Structured signals beat inference; DeepSeek only fills the gap.
        "picturesque": _picturesque_of(cluster) or vibe.get("picturesque"),
        # rooftop/decorType now come from the photo pass when present.
        "coziness": vibe.get("coziness"),
        "music": _music_of(cluster) or vibe.get("music"),
        "photographyAllowed": next(
            (p.get("photographyAllowed") for p in cluster if p.get("photographyAllowed")),
            None,
        ),
        "suitableFor": _suitable_of(cluster),
        # Google's NGN priceRange beats a directory's average spend.
        "minimumSpend": pick(
            "minimumSpend", lambda p: _price_range_spend(p) or _spend_of(p)
        ),
        # Evidence sets true/false; silence means no stated policy, which for
        # a boolean is false rather than unknown.
        "dressCode": _resolve_dress_code(cluster, pick, _house_rules_dress),
        "dietaryOptions": _dietary_of(cluster),
        "serviceSpeed": _service_speed_of(cluster) or vibe.get("serviceSpeed"),
        "decorType": vibe.get("decorType"),
        "seatingOptions": (_seating_of(cluster) or vibe.get("seatingOptions")),
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
        ("meal", _meal_of), ("service", _service_of),
        ("dietaryOptions", _dietary_of), ("suitableFor", _suitable_of),
    ):
        if fn(cluster):
            attribution[field] = "google"
    if _floorplan_seating(cluster):
        attribution["seatingOptions"] = "reisty-floorplans"
    elif _seating_of(cluster):
        attribution["seatingOptions"] = "google"
    if menu.get("cuisine"):
        attribution["cuisine"] = "menu-llm"
    if not _meal_of(cluster) and menu.get("meal"):
        attribution["meal"] = "menu-llm"
    if _picturesque_of(cluster):
        attribution["picturesque"] = "flavorqueste-reviews"
    if _service_speed_of(cluster):
        attribution["serviceSpeed"] = "flavorqueste-reviews (proxy)"
    if _music_of(cluster):
        attribution["music"] = "tags/liveMusic"
    if payload.get("dressCode") is not None and "dressCode" not in attribution:
        attribution["dressCode"] = "reisty-houserules"

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
    # FlavorQueste (S3) and Reisty (Cloudinary) photos are free and stable;
    # Google's cost a billed request each, so they go last.
    photos: list[str] = []
    for provider in ("flavorqueste", "reisty", "google", "instagram", "website"):
        for place in cluster:
            if place.get("source") == provider:
                photos.extend(p for p in (place.get("photos") or []) if p)

    rating = pick("rating", lambda p: p.get("rating"))
    record = dict(payload)
    # Not part of addRestaurantValidation — `rating` has no Restaurant field at
    # all (reviews are their own model) and photos upload separately. Kept here
    # because they are venue data; strip both before POSTing.
    record["rating"] = round(float(rating), 2) if rating is not None else None
    record["photos"] = photos[:6] or None
    record["menuUrl"] = next((p.get("menuUrl") for p in cluster if p.get("menuUrl")), None)
    # exploree keys its imported restaurants on googlePlaceId, so carrying it
    # turns an insert into an upsert against the 135 rows already there.
    record["googlePlaceId"] = next(
        (p.get("sourceId") for p in cluster if p.get("source") == "google"), None
    )

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
    city: str, per_source: int, photo_limit: int,
    rich: bool = True, enrich_deep: bool = True, with_reisty: bool = False,
) -> list[dict[str, Any]]:
    """Every configured provider — Google only when a key is present."""
    from config import settings
    from discover.flavorqueste import search_flavorqueste
    from discover.places import search_google_places, search_osm
    from discover.reisty import search_reisty

    # Under a lead provider the others are enrichment, not discovery: sampling
    # them to the same depth means matches happen only where two independent
    # samples happen to overlap. Reisty's whole catalogue is ~69 venues, so
    # pulling all of it is cheap and turns luck into coverage.
    reisty_cap = max(per_source, 100) if enrich_deep else per_source
    fq_cap = max(per_source, 250) if enrich_deep else per_source

    providers: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
        ("flavorqueste", lambda: search_flavorqueste(city, limit=fq_cap)),
    ]
    if with_reisty:
        providers.insert(
            0, ("reisty", lambda: search_reisty(city, limit=reisty_cap, detail=True))
        )
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
    ap.add_argument("--no-menu", action="store_true", help="skip Linktree/website menu discovery")
    ap.add_argument("--no-llm", action="store_true", help="skip DeepSeek ambience inference")
    ap.add_argument("--no-vision", action="store_true", help="skip Gemini photo ambience pass")
    ap.add_argument("--no-menu-llm", action="store_true", help="skip menu-based cuisine/meal inference")
    ap.add_argument("--no-ig-profile", action="store_true", help="skip Instagram profile read")
    ap.add_argument("--no-serp-facts", action="store_true", help="skip SERP founding-year/WhatsApp lookup")
    ap.add_argument("--with-reisty", action="store_true", help="re-enable the 69-venue Reisty directory")
    ap.add_argument("--ig-gap", type=float, default=20.0, help="seconds between IG profiles")
    ap.add_argument(
        "--max-reviews", type=int, default=None,
        help="only sample venues under this Google review count (tests thin data)",
    )
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
    places = collect(
        args.city, args.per_source, args.photo_limit,
        rich=not args.no_rich, enrich_deep=bool(args.primary),
        with_reisty=args.with_reisty,
    )
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

    if args.max_reviews is not None:
        def _reviews(cluster):
            return max(
                (p.get("ratingCount") or 0) for p in cluster
            ) if cluster else 0
        modest = [c for c in pool if 0 < _reviews(c) <= args.max_reviews]
        print(
            f"  {len(modest)}/{len(pool)} venues under {args.max_reviews} reviews",
            file=sys.stderr,
        )
        pool = modest or pool

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
                    find_menu=not args.no_menu,
                    client=client,
                )
            )
    print(
        f"  backfilled: {sum(1 for n in enrich_notes if n.get('geocoded'))} geocoded, "
        f"{sum(1 for n in enrich_notes if n.get('instagramFrom'))} ig-from-website, "
        f"{sum(1 for n in enrich_notes if n.get('menuFrom'))} menu-urls",
        file=sys.stderr,
    )

    # Photos answer lighting/coziness directly; text inference only fills what
    # the images could not show.
    visions = [{} if args.no_vision else infer_vibe_vision(c) for c in sample]
    print(
        f"  gemini vision: {sum(1 for v in visions if v)}/{len(sample)} venues "
        f"({sum(v.get('_photos', 0) for v in visions)} photos)",
        file=sys.stderr,
    )
    texts = [{} if args.no_llm else infer_vibe(c) for c in sample]
    print(
        f"  deepseek text: {sum(1 for v in texts if v)} venues returned ambience fields",
        file=sys.stderr,
    )
    vibes = [{**t, **{k: v for k, v in vis.items() if v is not None}}
             for t, vis in zip(texts, visions)]

    # Instagram fills what nothing else can: whatsApp, dateEstablished,
    # photographyAllowed, plus grid photos for venues with no directory images.
    if not args.no_ig_profile:
        from ig.logged_in_profile import fetch_profiles, parse_profile

        wanted = {}
        for cluster in sample:
            handle = next(
                (p.get("instagramHint") for p in cluster if p.get("instagramHint")), None
            )
            if handle:
                wanted[handle] = cluster
        if wanted:
            try:
                profiles = fetch_profiles(
                    list(wanted), min_gap_s=args.ig_gap, max_gap_s=args.ig_gap * 2
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ig profiles failed: {exc}", file=sys.stderr)
                profiles = {}
            for handle, raw in profiles.items():
                parsed = parse_profile(raw)
                row = {"source": "instagram", "name": wanted[handle][0].get("name")}
                for key in ("whatsApp", "dateEstablished", "photographyAllowed", "photos"):
                    if parsed.get(key) is not None:
                        row[key] = parsed[key]
                if parsed.get("linkInBio") and not any(
                    p.get("menuUrl") for p in wanted[handle]
                ):
                    row["menuUrl"] = parsed["linkInBio"]
                wanted[handle].append(row)
            print(
                f"  instagram: {len(profiles)}/{len(wanted)} profiles read",
                file=sys.stderr,
            )

    # Search snippets carry founding year / WhatsApp that no directory has.
    if not args.no_serp_facts:
        found = 0
        for cluster in sample:
            facts = infer_from_serp(cluster)
            row = {"source": "serp", "name": cluster[0].get("name")}
            for key in ("dateEstablished", "whatsApp", "dressCode", "bathroom"):
                if facts.get(key) is not None:
                    row[key] = facts[key]
                    found += 1
            if len(row) > 2:
                cluster.append(row)
        print(f"  serp facts: {found} values across {len(sample)} venues", file=sys.stderr)

    menus = [{} if args.no_menu_llm else infer_menu_taxonomy(c) for c in sample]
    print(
        f"  menu llm: {sum(1 for m in menus if m.get('cuisine') or m.get('meal'))}"
        f"/{len(sample)} venues classified from menu",
        file=sys.stderr,
    )
    mapped = [to_restaurant_payload(c, vibe=v, menu=m)
              for c, v, m in zip(sample, vibes, menus)]
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

"""
Draft Experience objects from Instagram post captions.

Aligned with the main product `ExperienceType` (and nested PricePoint /
schedule / DayOfWeek / ExperienceCategory / SourceType). This is heuristic —
fields we cannot fill stay null/empty and are listed under `missing`.
"""

from __future__ import annotations

import re
from typing import Any

from config import settings

# ── product enums (string values match the main app) ──────────────────────────

DAY_ORDER = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)
_DAY_INDEX = {d.lower(): i for i, d in enumerate(DAY_ORDER)}

EXPERIENCE_CATEGORIES = (
    "Food", "Drinks", "Dance", "Rave", "Art", "Games", "Music", "Movies",
    "Theater", "Festival", "Workshop", "Seminar", "Conference", "Networking",
    "Sports", "Fitness", "Wellness", "Exhibition", "Tour", "Outdoors",
    "Family", "Kids", "Charity", "Educational", "Business", "Technology",
    "Fashion", "Beauty", "Cultural", "Social",
)

_CATEGORY_KEYWORDS: list[tuple[str, re.Pattern[str]]] = [
    ("Food", re.compile(
        r"\b(sushi|dim\s*sum|brunch|buffet|tasting|menu|culinary|lunch|dinner|"
        r"unlimited|teppanyaki|hibachi|specials?)\b", re.I)),
    ("Drinks", re.compile(
        r"\b(cocktail|happy\s*hour|wine|champagne|heineken|bar|unlimited\s+"
        r"cocktails?|drinks?)\b", re.I)),
    ("Music", re.compile(
        r"\b(live\s+(dj|band|music|jazz|acoustics)|dj\b|concert|afrobeats)\b", re.I)),
    ("Dance", re.compile(r"\b(dance|club\s*night|rave|dark\s*room)\b", re.I)),
    ("Rave", re.compile(r"\b(rave|warehouse\s+party)\b", re.I)),
    ("Theater", re.compile(r"\b(theatre|theater|stage|curtain|play|drama)\b", re.I)),
    ("Art", re.compile(r"\b(art\s+show|exhibition|gallery)\b", re.I)),
    ("Festival", re.compile(r"\b(festival|fest\b)\b", re.I)),
    ("Cultural", re.compile(r"\b(cultural|culture\s+night)\b", re.I)),
    ("Social", re.compile(r"\b(networking|mixer|socials?\b)\b", re.I)),
    ("Wellness", re.compile(r"\b(wellness|yoga|spa)\b", re.I)),
    ("Fashion", re.compile(r"\b(fashion|runway)\b", re.I)),
]

# Caption must look like an experience announcement.
_SIGNAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("recurring", re.compile(
        r"\bevery\s+(mon|tues|wednes|thurs|fri|satur|sun)day\b|"
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*(?:[–—-]|to)\s*"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        re.I,
    )),
    ("weekday", re.compile(
        r"\b(this\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"weekend|tonight|this\s+week)\b",
        re.I,
    )),
    ("time", re.compile(
        r"\b([01]?\d|2[0-3])([:.]\d{2})?\s*(am|pm)\b",
        re.I,
    )),
    ("ticket", re.compile(r"\b(tickets?|rsvp|link\s+in\s+bio|book\s+now|reserve)\b", re.I)),
    ("price", re.compile(r"[₦$€£]\s?\d[\d,]*(?:\.\d+)?")),
    ("offering", re.compile(
        r"\b(unlimited|buffet|brunch|happy\s*hour|all\s*you\s*can\s*eat|"
        r"live\s+(dj|band|music|jazz|acoustics)|guest\s+dj|after\s*party|"
        r"open\s*mic|specials?|tasting\s+menu|teppanyaki|hibachi|seatings?)\b",
        re.I,
    )),
    ("show", re.compile(
        r"\b(show|concert|theatre|theater|premiere|doors\s+open|curtain|"
        r"performance|nightlife)\b",
        re.I,
    )),
    ("venue_cue", re.compile(r"\b(at\s+the|join\s+us|see\s+you|don'?t\s+miss)\b", re.I)),
]

_MIN_SCORE = 2

_BARE_DAY = re.compile(
    r"^(this\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$",
    re.I,
)
_RANGE_RE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*(?:[–—-]|to)\s*"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_EVERY_DAY_RE = re.compile(
    r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])([:.]\d{2})?\s*(am|pm)\b", re.I)
_PRICE_RE = re.compile(r"([₦$€£])\s?(\d[\d,]*(?:\.\d+)?)")
_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")
_MENTION_RE = re.compile(r"@([A-Za-z0-9._]+)")
_URL_RE = re.compile(r"https?://[^\s\]]+", re.I)
_DRESS_RE = re.compile(
    r"dress\s*code\s*[:\-]?\s*([^\n.|;]+)|"
    r"\b(smart\s+casual|black\s+tie|casual|formal|no\s+shorts|strictly\s+[^.\n]+)\b",
    re.I,
)
_AGE_RE = re.compile(r"\b(\d{2})\s*\+|age\s*[:\-]?\s*(\d{2})\+|(\d{2})\s*and\s*above\b", re.I)
_DATE_RE = re.compile(
    r"\b(?:aug|sep|oct|nov|dec|jan|feb|mar|apr|may|jun|jul)[a-z]*\s+\d{1,2}"
    r"(?:\s*[-–—]\s*\d{1,2})?(?:\s*,?\s*\d{4})?\b|"
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
    re.I,
)

# ExperienceType keys we attempt to populate from IG.
_FILLABLE = (
    "name", "slug", "description", "tags", "ageLimit", "categories", "dressCode",
    "sourceType", "ownerName", "host", "website", "coverImage", "imageUrl",
    "pricePoints", "appearances", "schedule", "active",
)
_NEEDS_PRODUCT = (
    "owner", "location", "emails", "phones", "rating",
)


_OFFERING_LINE = re.compile(
    r"\b(unlimited|buffet|brunch|happy\s*hour|all\s*you\s*can\s*eat|"
    r"live\s+(dj|band|music|jazz|acoustics)|specials?|tasting\s+menu|"
    r"sushi|dim\s*sum|guest\s+dj|open\s*mic|after\s*party|"
    r"teppanyaki|hibachi|seatings?)\b",
    re.I,
)
_PRICE_LINE = re.compile(
    r"[₦$€£]\s*\d|"
    r"^\s*\d{1,3}(?:,\d{3})+\s*[—–-]|"
    r"^\s*\d[\d,.]*\s*[—–-]",
)
_EVENTISH_HASHTAG = re.compile(
    r"(brunch|affairs?|night|party|festival|session|seating|unlimited|buffet|special|"
    r"kaffy|theatre|theater|concert)",
    re.I,
)
_NARRATIVE_NAME = re.compile(
    r"@|"
    r"\b(welcomes you|final episode|in the final|critics have|this is your|"
    r"queues like|don'?t hear|one last|the reviews are|have spoken|"
    r"you witnessed|came out in numbers|scan\s+here|last\s+show|final\s+show|"
    r"get\s+your\s+ticket)\b|"
    r"^the\s+.+\s+theat(?:re|er)\b",
    re.I,
)
_AS_TITLE = re.compile(
    r"\b(?:as|presents|presenting|announcing)\s+"
    r"([A-Z0-9][^.\n]{2,80}?)"
    r"(?=\s+(?:officially|opens?|premieres?|returns?|hits?\b|lands?\b|at\b|on\b|"
    r"this\b|from\b|by\b)|[.!]|$)",
)


def _hashtag_to_title(tag: str) -> str:
    """ShiroSundayBrunch → Shiro Sunday Brunch."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tag)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    spaced = re.sub(r"[_-]+", " ", spaced)
    return spaced.strip()


def _is_bad_experience_name(name: str) -> bool:
    cleaned = re.sub(r"\s+", " ", name).strip()
    if not cleaned or len(cleaned) < 4:
        return True
    if len(cleaned) > 72 or cleaned.count(" ") > 10:
        return True
    if _NARRATIVE_NAME.search(cleaned):
        return True
    if cleaned.endswith(":") or cleaned.endswith(","):
        return True
    # Full sentence marketing copy, not a title.
    if cleaned[:1].islower():
        return True
    if cleaned.endswith(".") and len(cleaned) > 40:
        return True
    return False


def _name_from_hashtags(caption: str) -> str | None:
    tags = _HASHTAG_RE.findall(caption)
    preferred: list[str] = []
    fallback: list[str] = []
    for tag in tags:
        title = _hashtag_to_title(tag)
        if len(title) < 8 or _is_bad_experience_name(title):
            continue
        words = title.split()
        if not (2 <= len(words) <= 5):
            continue
        if _EVENTISH_HASHTAG.search(tag):
            preferred.append(title[:160])
        else:
            fallback.append(title[:160])
    for title in preferred + fallback:
        return title
    return None


def _title_case_show(name: str) -> str:
    """DEAR KAFFY: DIARY OF A SINGLE WOMAN → Dear Kaffy: Diary of a Single Woman."""
    small = {"a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "at", "by"}
    out: list[str] = []
    word_i = 0
    for part in re.split(r"(\s+|:\s*)", name):
        if not part or part.isspace() or part.startswith(":"):
            out.append(part)
            continue
        lower = part.lower()
        if word_i > 0 and lower in small:
            out.append(lower)
        else:
            out.append(part[:1].upper() + part[1:].lower() if len(part) > 1 else part.upper())
        word_i += 1
    return "".join(out)


def _name_from_all_caps_line(caption: str) -> str | None:
    for line in caption.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip(" \t-–—•*|🔥📅🎭✨🎟📍🗓️")
        cleaned = re.sub(r"^[\W🔥📅🥂✨🥢🍻🎶📲🎭🎟️📍🗓️]+", "", cleaned).strip(" :")
        if not cleaned or _PRICE_LINE.search(cleaned):
            continue
        letters = [c for c in cleaned if c.isalpha()]
        if not letters:
            continue
        upper_ratio = sum(c.isupper() for c in letters) / len(letters)
        if upper_ratio < 0.85:
            continue
        if not (8 <= len(cleaned) <= 60):
            continue
        titled = _title_case_show(cleaned)
        if _is_bad_experience_name(titled):
            continue
        # Skip pure location shouts like "LONDON, THE CRITICS HAVE SPOKEN"
        if cleaned.rstrip(".!").count(",") >= 1 and len(cleaned.split()) > 5:
            continue
        return titled
    return None


def _name_from_as_clause(caption: str) -> str | None:
    m = _AS_TITLE.search(caption)
    if not m:
        return None
    title = m.group(1).strip(" :,-–—")
    title = re.sub(
        r"\s+(?:officially|opens?|premieres?|returns?|hits?|lands?).*$",
        "",
        title,
        flags=re.I,
    ).strip(" :,-–—")
    if _is_bad_experience_name(title):
        return None
    return title[:160]


def _experience_name(caption: str) -> str:
    """
    Caption fallback when card OCR finds nothing usable.

    Prefer a real show/offering label. Never use long SEO openers as the name.
    """
    from_caps = _name_from_all_caps_line(caption)
    if from_caps:
        return from_caps

    from_as = _name_from_as_clause(caption)
    if from_as:
        return from_as

    from_tag = _name_from_hashtags(caption)
    if from_tag:
        return from_tag

    candidates: list[str] = []
    for line in caption.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip(" \t-–—•*|")
        cleaned = re.sub(r"^[\W🔥📅🥂✨🥢🍻🎶📲🎭🎟️📍🗓️]+", "", cleaned).strip(" :")
        if not cleaned or len(cleaned) < 8:
            continue
        if _PRICE_LINE.search(cleaned) or _is_bad_experience_name(cleaned):
            continue
        if _OFFERING_LINE.search(cleaned):
            candidates.append(cleaned[:160])
    if candidates:
        candidates.sort(key=lambda c: (len(c) > 72, len(c)))
        name = candidates[0]
        # Teppanyaki Seatings → Teppanyaki
        name = re.sub(r"\s+seatings?\s*$", "", name, flags=re.I).strip(" :")
        core = re.search(
            r"\b(teppanyaki|hibachi|brunch|buffet|sushi|dim\s*sum)\b", name, re.I
        )
        if core and len(name.split()) <= 3:
            token = core.group(0)
            return token.title() if token.islower() or token.isupper() else token[0].upper() + token[1:]
        return name

    # Last resort: short quoted or Title: Subtitle fragment — never the narrative opener.
    for m in re.finditer(r"[“\"]([^”\"]{6,60})[”\"]", caption):
        frag = m.group(1).strip()
        if not _is_bad_experience_name(frag) and not frag.endswith((".", "!")):
            return frag
    return "Untitled"


def _slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80] or "experience"


def _to_hhmm(hour: int, minute: int, ampm: str) -> str:
    ampm = ampm.lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _parse_times(text: str) -> list[str]:
    times: list[str] = []
    for m in _TIME_RE.finditer(text):
        hour = int(m.group(1))
        minute = int((m.group(2) or ":00").lstrip(":") or "0")
        times.append(_to_hhmm(hour, minute, m.group(3)))
    return list(dict.fromkeys(times))


def _days_between(start: str, end: str) -> list[str]:
    i, j = _DAY_INDEX[start.lower()], _DAY_INDEX[end.lower()]
    if i <= j:
        return list(DAY_ORDER[i : j + 1])
    return list(DAY_ORDER[i:]) + list(DAY_ORDER[: j + 1])


def _parse_recurrence_days(text: str) -> list[str]:
    days: list[str] = []
    for m in _RANGE_RE.finditer(text):
        days.extend(_days_between(m.group(1), m.group(2)))
    for m in _EVERY_DAY_RE.finditer(text):
        day = m.group(1).capitalize()
        if day == "Tuesday" or m.group(1).lower().startswith("tues"):
            day = "Tuesday"
        # normalize
        key = m.group(1).lower()
        for full in DAY_ORDER:
            if full.lower().startswith(key[:3]):
                day = full
                break
        days.append(day)
    # "Monday – Friday" style already covered; also lone weekdays when recurring cue present
    if days:
        return list(dict.fromkeys(days))

    if re.search(r"\bevery\b|\bweekday", text, re.I):
        for m in re.finditer(
            r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text, re.I
        ):
            days.append(m.group(1).capitalize())
    return list(dict.fromkeys(days))


def _dedupe_when_hints(hints: list[str]) -> list[str]:
    unique = list(dict.fromkeys(h.strip() for h in hints if h and h.strip()))
    kept: list[str] = []
    for hint in unique:
        bare = _BARE_DAY.match(hint)
        if bare:
            day = bare.group(2).lower()
            if any(
                other.lower() != hint.lower() and day in other.lower()
                for other in unique
            ):
                continue
        kept.append(hint)
    return kept[:6]


def _parse_prices(text: str) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for m in _PRICE_RE.finditer(text):
        raw = m.group(2).replace(",", "")
        try:
            amount = float(raw) if "." in raw else int(raw)
        except ValueError:
            continue
        # Grab a short label from the same line if present
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end].strip()
        label = re.sub(r"[₦$€£]\s?\d[\d,]*(?:\.\d+)?", "", line).strip(" -:–—|")
        label = re.sub(r"^[\W🔥📅🥂✨🥢🍻🎶📲]+", "", label).strip(" -:–—|")
        label = re.sub(r"^(price|from|only)\s*[:\-]?\s*", "", label, flags=re.I).strip()
        points.append({
            "type": (label[:80] if label and 2 < len(label) < 60 else "Admission"),
            "description": label[:160] if label else None,
            "price": amount,
        })
    # Dedup by price+type
    seen: set[tuple[Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for p in points:
        key = (p["type"], p["price"])
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out[:6]


def _parse_categories(text: str) -> list[str]:
    found: list[str] = []
    for cat, pattern in _CATEGORY_KEYWORDS:
        if pattern.search(text):
            found.append(cat)
    return found


def _signals(text: str) -> tuple[list[str], list[str], int]:
    signals: list[str] = []
    when_hints: list[str] = []
    for kind, pattern in _SIGNAL_PATTERNS:
        if not pattern.search(text):
            continue
        signals.append(kind)
        if kind in ("recurring", "weekday", "time"):
            for m in pattern.finditer(text):
                when_hints.append(m.group(0).strip())
    signals = list(dict.fromkeys(signals))
    when_hints = _dedupe_when_hints(when_hints)
    score = len(signals)
    return signals, when_hints, score


def _has_concrete_when(schedule: dict[str, Any]) -> bool:
    if schedule.get("startTime"):
        return True
    if schedule.get("date"):
        return True
    days = (schedule.get("recurrence") or {}).get("days") or []
    return bool(days)


def _is_experience(signals: list[str], schedule: dict[str, Any]) -> bool:
    """
    Reject vibe / brand posts. Need a real offering or ticketed show
    plus a usable when (time, date, or recurrence days).
    """
    has_offer = "offering" in signals or "price" in signals
    has_ticketed_show = "ticket" in signals and "show" in signals
    has_when = _has_concrete_when(schedule)
    if not has_when:
        return False
    return has_offer or has_ticketed_show


def _build_schedule(text: str, signals: list[str]) -> dict[str, Any]:
    times = _parse_times(text)
    start_time = times[0] if times else ""
    end_time = times[1] if len(times) > 1 else ""
    recurrence_days = _parse_recurrence_days(text)
    date_hints = [m.group(0) for m in _DATE_RE.finditer(text)][:3]

    is_recurring = (
        "recurring" in signals
        or bool(recurrence_days)
        or bool(re.search(r"\bevery\b|\bweekday", text, re.I))
    )
    event_type = "recurring" if is_recurring else "one-time"

    schedule: dict[str, Any] = {
        "eventType": event_type,
        "date": date_hints[0] if date_hints and event_type == "one-time" else None,
        "startDate": "",
        "endDate": "",
        "startTime": start_time,
        "endTime": end_time,
    }
    if event_type == "recurring" and recurrence_days:
        schedule["recurrence"] = {
            "days": recurrence_days,
            "startDate": "",
            "endDate": "",
        }
    return schedule


def extract_from_text(text: str | None, *, min_len: int = 24) -> dict[str, Any] | None:
    """Gate + raw hints from any text blob (caption, flyer OCR, or both)."""
    body = (text or "").strip()
    if len(body) < min_len:
        return None
    signals, when_hints, score = _signals(body)
    schedule = _build_schedule(body, signals)
    if not _is_experience(signals, schedule):
        return None
    return {
        "title": _experience_name(body),
        "signals": signals,
        "score": score,
        "whenHints": when_hints,
        "priceHints": [m.group(0) for m in _PRICE_RE.finditer(body)][:4],
        "schedule": schedule,
    }


def extract_from_caption(caption: str | None) -> dict[str, Any] | None:
    """Gate + raw hints. Prefer `experience_from_post` for product-shaped output."""
    return extract_from_text(caption)


def _gate_prefers(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Pick the richer gate (higher score / concrete when / price hints)."""
    a_when = _has_concrete_when(a.get("schedule") or {})
    b_when = _has_concrete_when(b.get("schedule") or {})
    if b_when and not a_when:
        return b
    if a_when and not b_when:
        return a
    a_prices = len(a.get("priceHints") or [])
    b_prices = len(b.get("priceHints") or [])
    if b_prices > a_prices:
        return b
    if (b.get("score") or 0) > (a.get("score") or 0):
        return b
    return a


def experience_from_post(
    post: dict[str, Any],
    *,
    profile_name: str | None = None,
    profile_website: str | None = None,
    source_type: str = "Restaurant",
    use_card_ocr: bool = True,
    use_llm: bool | None = None,
    llm_allow_network: bool = True,
    ocr_text: str | None = None,
) -> dict[str, Any] | None:
    """
    Build a partial ExperienceType draft from one IG post.

    The experience gate uses caption + flyer OCR (when available), so a thin
    caption with a detailed flyer still qualifies. Name priority: DeepSeek
    (when enabled) → flyer OCR → hashtag → caption.
    """
    caption = (post.get("caption") or "").strip()
    if isinstance(caption, dict):
        caption = (caption.get("text") or "").strip()

    handle = (post.get("handle") or "").lower()
    post_id = str(post.get("_id") or post.get("id") or "")

    flyer_text = (ocr_text or "").strip()
    card_title = None
    if use_card_ocr and ocr_text is None:
        try:
            from pipeline.ocr import flyer_text_for_post, title_from_ocr

            flyer_text = (flyer_text_for_post(post) or "").strip()
            if flyer_text:
                card_title = title_from_ocr(flyer_text, caption=caption)
        except Exception:  # noqa: BLE001 — OCR is best-effort
            flyer_text = ""
            card_title = None
    elif flyer_text:
        try:
            from pipeline.ocr import title_from_ocr

            card_title = title_from_ocr(flyer_text, caption=caption)
        except Exception:  # noqa: BLE001
            card_title = None

    gate = extract_from_text(caption)
    gate_source = "caption"
    if flyer_text:
        combined = extract_from_text(
            f"{caption}\n{flyer_text}".strip() if caption else flyer_text,
            min_len=12,
        )
        if combined and (not gate or _gate_prefers(gate, combined) is combined):
            gate = combined
            gate_source = "caption+flyer" if caption else "flyer"

    if not gate:
        return None

    body = f"{caption}\n{flyer_text}".strip() if flyer_text else caption
    name = card_title or gate["title"]
    name_source = "card" if card_title else "caption"
    tags = _HASHTAG_RE.findall(caption)
    mentions = [m for m in _MENTION_RE.findall(caption) if m.lower() != handle]
    urls = _URL_RE.findall(caption) or _URL_RE.findall(flyer_text)
    dress = None
    dm = _DRESS_RE.search(body)
    if dm:
        dress = (dm.group(1) or dm.group(2) or "").strip()
    age = None
    am = _AGE_RE.search(body)
    if am:
        age = next(g for g in am.groups() if g) + "+"

    schedule = gate["schedule"]
    price_points = _parse_prices(body)
    categories = _parse_categories(body)
    website = urls[0] if urls else (profile_website or None)
    media = post.get("mediaUrl")
    owner_name = profile_name or handle

    experience: dict[str, Any] = {
        "_id": f"ig:{post_id}",
        "active": True,
        "name": name,
        "slug": _slugify(f"{handle}-{name}")[:80],
        "description": caption or flyer_text,
        "tags": tags[:20],
        "ageLimit": age or "",
        "categories": categories,
        "dressCode": dress or "",
        "sourceType": source_type,
        "owner": None,  # requires product Organizer/Restaurant link
        "ownerName": owner_name,
        "host": mentions[0] if mentions else None,
        "emails": [],
        "phones": [],
        "website": website,
        "coverImage": media,
        "imageUrl": media,
        "pricePoints": price_points,
        "location": None,  # needs venue address/coords from product DB
        "appearances": mentions[:10],
        "schedule": schedule,
        "rating": None,
    }

    missing = [k for k in _FILLABLE if _is_empty(experience.get(k))]
    missing.extend(k for k in _NEEDS_PRODUCT if _is_empty(experience.get(k)))

    draft: dict[str, Any] = {
        # Provenance for the ingest UI
        "id": experience["_id"],
        "postId": post_id,
        "handle": handle,
        "permalink": post.get("permalink"),
        "mediaType": post.get("mediaType"),
        "postedAt": post.get("postedAt"),
        "shortcode": post.get("shortcode"),
        "signals": gate["signals"],
        "score": gate["score"],
        "whenHints": gate["whenHints"],
        "priceHints": [
            f"₦{p['price']}" if isinstance(p["price"], (int, float)) else str(p["price"])
            for p in price_points
        ],
        "title": name,
        "nameSource": name_source,
        "gateSource": gate_source,
        "caption": caption,
        "mediaUrl": media,
        "filled": [k for k in _FILLABLE if k not in missing],
        "missing": missing,
        # Product-shaped draft
        "experience": experience,
    }

    should_llm = use_llm if use_llm is not None else None
    if should_llm is None:
        from pipeline import deepseek_extract

        should_llm = deepseek_extract.enabled()

    if should_llm:
        try:
            from pipeline import deepseek_extract

            llm = deepseek_extract.extract_experience_fields(
                post,
                heuristic_name=name,
                profile_name=profile_name,
                allow_network=llm_allow_network,
            )
            if llm and llm.get("isExperience") is False:
                return None
            if llm and llm.get("isExperience", True):
                draft = deepseek_extract.merge_into_draft(draft, llm)
                # refresh filled/missing after merge
                exp2 = draft["experience"]
                missing2 = [k for k in _FILLABLE if _is_empty(exp2.get(k))]
                missing2.extend(k for k in _NEEDS_PRODUCT if _is_empty(exp2.get(k)))
                draft["filled"] = [k for k in _FILLABLE if k not in missing2]
                draft["missing"] = missing2
        except Exception:  # noqa: BLE001 — LLM is best-effort
            pass

    return draft


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if value == "":
        return True
    if value == []:
        return True
    if isinstance(value, dict) and not any(
        v not in (None, "", [], {}) for v in value.values()
    ):
        return True
    if isinstance(value, dict) and value.get("eventType") and not value.get("startTime") and not value.get("recurrence"):
        # schedule present but useless
        return not value.get("date")
    return False


def event_from_post(post: dict[str, Any], **kwargs: Any) -> dict[str, Any] | None:
    """Alias kept for callers/tests — returns experience draft."""
    return experience_from_post(post, **kwargs)


def extract_events(
    posts: list[dict[str, Any]],
    *,
    min_score: int = _MIN_SCORE,
    profiles: dict[str, dict[str, Any]] | None = None,
    use_card_ocr: bool = True,
    use_llm: bool | None = None,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    from pipeline import deepseek_extract

    profiles = profiles or {}
    llm_on = deepseek_extract.enabled() if use_llm is None else use_llm
    budget = settings.DEEPSEEK_MAX_PER_REQUEST if llm_on else 0
    network_used = 0

    events: list[dict[str, Any]] = []
    for post in posts:
        handle = (post.get("handle") or "").lower()
        profile = profiles.get(handle) or {}
        allow_network = llm_on and network_used < budget
        event = experience_from_post(
            post,
            profile_name=profile.get("name"),
            profile_website=profile.get("website"),
            use_card_ocr=use_card_ocr,
            use_llm=llm_on,
            llm_allow_network=allow_network,
        )
        if event and event.get("llm") and not event["llm"].get("cached") and allow_network:
            network_used += 1
        if event and event["score"] >= min_score:
            events.append(event)
    if dedupe:
        events = dedupe_experiences(events)
    else:
        events.sort(key=lambda e: (e.get("postedAt") is None, e.get("postedAt")), reverse=True)
    return events


def _experience_name_key(name: str) -> str:
    """
    Collapse promo variants of the same show into one key.

    Dear Kaffy: Diary… / Dear Kaffy London → dear-kaffy
    Sunday Brunch Affairs stays distinct from Teppanyaki.
    """
    s = (name or "").lower().strip()
    s = re.sub(r"['’]", "", s)
    s = s.split(":")[0].strip()
    s = re.sub(
        r"\s+(london|lagos|abuja|nigeria|pretoria|edition|live|show)\s*$",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(r"\s+", " ", s).strip()
    return _slugify(s) or "untitled"


def _draft_richness(event: dict[str, Any]) -> tuple[int, Any]:
    """Prefer DeepSeek/card names, fuller schedules/prices, then newest post."""
    exp = event.get("experience") or {}
    score = 0
    if event.get("nameSource") == "deepseek":
        score += 8
    elif event.get("nameSource") == "card":
        score += 5
    sched = exp.get("schedule") or {}
    if sched.get("startTime"):
        score += 3
    if sched.get("date"):
        score += 2
    if (sched.get("recurrence") or {}).get("days"):
        score += 3
    score += min(len(exp.get("pricePoints") or []), 4)
    score += min(len(exp.get("categories") or []), 3)
    if exp.get("dressCode"):
        score += 1
    if event.get("mediaUrl"):
        score += 1
    # Prefer longer, more specific titles when equally rich
    score += min(len((exp.get("name") or "").split()), 4)
    return (score, event.get("postedAt") or "")


def _merge_experience_pair(primary: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Fold `other` into `primary` (mutates primary)."""
    p_exp = primary.setdefault("experience", {})
    o_exp = other.get("experience") or {}

    # Prefer longer concrete description unless primary already has a short LLM summary
    p_desc = (p_exp.get("description") or "").strip()
    o_desc = (o_exp.get("description") or "").strip()
    if o_desc and (not p_desc or (len(o_desc) > len(p_desc) and len(p_desc) < 80)):
        # keep primary's short summary if it came from deepseek and is intentional
        if primary.get("nameSource") != "deepseek" or len(p_desc) < 40:
            if len(o_desc) <= 1200:
                p_exp["description"] = o_desc

    # Union tags / categories / appearances / prices
    for key in ("tags", "categories", "appearances"):
        seen: list[Any] = list(p_exp.get(key) or [])
        for item in o_exp.get(key) or []:
            if item not in seen:
                seen.append(item)
        p_exp[key] = seen[:20] if key == "tags" else seen[:10]

    prices = list(p_exp.get("pricePoints") or [])
    price_keys = {(p.get("type"), p.get("price")) for p in prices}
    for p in o_exp.get("pricePoints") or []:
        key = (p.get("type"), p.get("price"))
        if key not in price_keys:
            prices.append(p)
            price_keys.add(key)
    p_exp["pricePoints"] = prices[:8]

    p_sched = dict(p_exp.get("schedule") or {})
    o_sched = o_exp.get("schedule") or {}
    if not p_sched.get("startTime") and o_sched.get("startTime"):
        p_sched["startTime"] = o_sched["startTime"]
        p_sched["endTime"] = o_sched.get("endTime") or p_sched.get("endTime") or ""
    if not p_sched.get("date") and o_sched.get("date"):
        p_sched["date"] = o_sched["date"]
    p_days = list((p_sched.get("recurrence") or {}).get("days") or [])
    o_days = list((o_sched.get("recurrence") or {}).get("days") or [])
    for d in o_days:
        if d not in p_days:
            p_days.append(d)
    if p_days:
        p_sched["recurrence"] = {
            "days": p_days,
            "startDate": "",
            "endDate": "",
        }
        p_sched["eventType"] = "recurring"
    elif o_sched.get("eventType") and not p_sched.get("eventType"):
        p_sched["eventType"] = o_sched["eventType"]
    p_exp["schedule"] = p_sched

    for field in ("dressCode", "ageLimit", "website", "host"):
        if not p_exp.get(field) and o_exp.get(field):
            p_exp[field] = o_exp[field]
    if not p_exp.get("coverImage") and other.get("mediaUrl"):
        p_exp["coverImage"] = other.get("mediaUrl")
        p_exp["imageUrl"] = other.get("mediaUrl")
        primary["mediaUrl"] = other.get("mediaUrl")

    sources = list(primary.get("sourcePosts") or [])
    if not sources:
        sources.append({
            "postId": primary.get("postId"),
            "shortcode": primary.get("shortcode"),
            "permalink": primary.get("permalink"),
            "postedAt": primary.get("postedAt"),
            "mediaUrl": primary.get("mediaUrl"),
        })
    sources.append({
        "postId": other.get("postId"),
        "shortcode": other.get("shortcode"),
        "permalink": other.get("permalink"),
        "postedAt": other.get("postedAt"),
        "mediaUrl": other.get("mediaUrl"),
    })
    # de-dupe by postId
    seen_ids: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for s in sources:
        pid = str(s.get("postId") or "")
        if pid and pid in seen_ids:
            continue
        if pid:
            seen_ids.add(pid)
        uniq.append(s)
    primary["sourcePosts"] = uniq
    primary["postCount"] = len(uniq)
    primary["score"] = max(int(primary.get("score") or 0), int(other.get("score") or 0))

    # Stable experience id for the cluster
    handle = primary.get("handle") or ""
    name = (p_exp.get("name") or primary.get("title") or "experience")
    primary["id"] = f"ig:{handle}:{_experience_name_key(name)}"
    p_exp["_id"] = primary["id"]
    primary["experience"] = p_exp
    return primary


def dedupe_experiences(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    One draft per venue experience.

    Multiple promo posts for the same show/brunch collapse into a single item
    with `sourcePosts` / `postCount`.
    """
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        handle = (event.get("handle") or "").lower()
        name = ((event.get("experience") or {}).get("name") or event.get("title") or "")
        key = (handle, _experience_name_key(name))
        buckets.setdefault(key, []).append(event)

    merged: list[dict[str, Any]] = []
    for _key, items in buckets.items():
        items.sort(key=_draft_richness, reverse=True)
        primary = dict(items[0])
        primary["experience"] = dict(primary.get("experience") or {})
        if len(items) == 1:
            primary.setdefault("postCount", 1)
            primary.setdefault(
                "sourcePosts",
                [{
                    "postId": primary.get("postId"),
                    "shortcode": primary.get("shortcode"),
                    "permalink": primary.get("permalink"),
                    "postedAt": primary.get("postedAt"),
                    "mediaUrl": primary.get("mediaUrl"),
                }],
            )
            merged.append(primary)
            continue
        for other in items[1:]:
            primary = _merge_experience_pair(primary, other)
        merged.append(primary)

    merged.sort(key=lambda e: (e.get("postedAt") is None, e.get("postedAt")), reverse=True)
    return merged


def group_by_handle(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        buckets.setdefault(event["handle"], []).append(event)

    groups: list[dict[str, Any]] = []
    for handle, items in buckets.items():
        items.sort(key=lambda e: (e.get("postedAt") is None, e.get("postedAt")), reverse=True)
        groups.append({
            "handle": handle,
            "eventCount": len(items),
            "experienceCount": len(items),
            "events": items,
        })
    groups.sort(key=lambda g: g["eventCount"], reverse=True)
    return groups

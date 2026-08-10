"""
DeepSeek-assisted experience extraction.

Heuristic gate still decides *whether* a post is an experience (cheap filter
over caption + flyer OCR). DeepSeek refines name / schedule / prices /
categories from caption + flyer OCR.

Uses the OpenAI-compatible Chat Completions API:
https://api-docs.deepseek.com/
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from config import settings

log = logging.getLogger("ig.deepseek")

_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "deepseek"

_SYSTEM = """You extract venue EXPERIENCES from Instagram posts for a Nigerian events product.

An experience is a concrete offering people can attend or book: brunch, buffet, teppanyaki seating,
theatre show, ticketed night, recurring party — with a usable when (time, date, or days).

Return ONLY compact JSON (no markdown) with this shape:
{
  "isExperience": boolean,
  "name": string|null,
  "description": string|null,
  "categories": string[],
  "schedule": {
    "eventType": "one-time"|"recurring"|null,
    "date": string|null,
    "startTime": "HH:MM"|null,
    "endTime": "HH:MM"|null,
    "recurrenceDays": string[]
  },
  "pricePoints": [{"type": string, "price": number}],
  "dressCode": string|null,
  "ageLimit": string|null,
  "venueHint": string|null,
  "host": string|null
}

Rules for name (critical):
- Prefer the on-image flyer/card title when OCR is present and readable.
- Never use SEO/marketing openers, @mentions, or long sentences as the name.
- Never use CTA/venue/status text: "Scan Here", "Last Show", "Get Your Ticket",
  "The Shaw Theatre" alone, production-company lines, phone numbers, prices.
- Strip suffixes like "Seatings" when the core offering is clear (Teppanyaki Seatings → Teppanyaki).
- Good examples: "Teppanyaki", "Sunday Brunch Affairs", "Dear Kaffy: Diary of a Single Woman".

Other rules:
- categories must be from: Food, Drinks, Dance, Rave, Art, Games, Music, Movies, Theater,
  Festival, Workshop, Seminar, Conference, Networking, Sports, Fitness, Wellness, Exhibition,
  Tour, Outdoors, Family, Kids, Charity, Educational, Business, Technology, Fashion, Beauty,
  Cultural, Social.
- recurrenceDays use full English day names (Monday…Sunday).
- startTime/endTime are 24h HH:MM when known.
- price is a number without currency symbols (Naira amounts as integers).
- If not a bookable/attendable experience, set isExperience=false and name=null.
"""


def enabled() -> bool:
    return bool(settings.DEEPSEEK_ENABLED and settings.DEEPSEEK_API_KEY)


def _cache_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode()).hexdigest()[:28]
    return _CACHE_DIR / f"{digest}.json"


def read_cached(key: str) -> dict[str, Any] | None:
    path = _cache_path(key)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_cached(key: str, data: dict[str, Any]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(key).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _cache_key(*, handle: str, post_id: str, caption: str, ocr_text: str) -> str:
    blob = f"{handle}\n{post_id}\n{caption.strip()}\n---\n{(ocr_text or '').strip()}"
    return f"v1:{hashlib.sha1(blob.encode()).hexdigest()}"


def _flyer_ocr_text(post: dict[str, Any]) -> str:
    """Best-effort raw OCR for the model (not the heuristic title picker)."""
    try:
        from pipeline.ocr import ocr_url
    except ImportError:
        return ""
    url = post.get("mediaUrl")
    if not url:
        raw = (post.get("source") or {}).get("raw") or {}
        url = raw.get("display_uri") or raw.get("display_url")
    if not url:
        return ""
    post_id = str(post.get("_id") or post.get("id") or "")
    try:
        return (ocr_url(url, cache_key=f"post:{post_id}") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("ocr for deepseek failed: %s", exc)
        return ""


def _user_payload(
    *,
    handle: str,
    caption: str,
    ocr_text: str,
    heuristic_name: str | None,
    profile_name: str | None,
) -> str:
    parts = [
        f"IG handle: @{handle}",
        f"Profile name: {profile_name or handle}",
        f"Heuristic name guess: {heuristic_name or '(none)'}",
        "",
        "CAPTION:",
        caption.strip() or "(empty)",
        "",
        "FLYER OCR (may be noisy):",
        (ocr_text.strip() or "(none)"),
    ]
    return "\n".join(parts)


def _parse_json_content(content: str) -> dict[str, Any] | None:
    text = (content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _call_api(user_content: str) -> dict[str, Any] | None:
    url = f"{settings.DEEPSEEK_BASE_URL}/chat/completions"
    body: dict[str, Any] = {
        "model": settings.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "enabled" if settings.DEEPSEEK_THINKING else "disabled"},
    }
    headers = {
        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=settings.DEEPSEEK_TIMEOUT_S) as client:
            resp = client.post(url, headers=headers, json=body)
            if resp.status_code >= 400:
                log.warning(
                    "deepseek HTTP %s: %s",
                    resp.status_code,
                    (resp.text or "")[:300],
                )
                return None
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("deepseek request failed: %s", exc)
        return None

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log.warning("deepseek unexpected response shape")
        return None
    return _parse_json_content(content)


def extract_experience_fields(
    post: dict[str, Any],
    *,
    heuristic_name: str | None = None,
    profile_name: str | None = None,
    ocr_text: str | None = None,
    use_cache: bool = True,
    allow_network: bool = True,
) -> dict[str, Any] | None:
    """
    Return DeepSeek JSON fields for a post, or None on failure/disabled.

    When allow_network is False, only cache hits are returned (for budgeted API passes).
    """
    if not enabled():
        return None

    handle = (post.get("handle") or "").lower()
    post_id = str(post.get("_id") or post.get("id") or "")
    caption = (post.get("caption") or "").strip()
    if isinstance(caption, dict):
        caption = (caption.get("text") or "").strip()

    if ocr_text is None:
        ocr_text = _flyer_ocr_text(post) if allow_network or use_cache else ""

    key = _cache_key(handle=handle, post_id=post_id, caption=caption, ocr_text=ocr_text or "")
    if use_cache:
        cached = read_cached(key)
        if cached is not None:
            cached = dict(cached)
            cached["_cached"] = True
            return cached

    if not allow_network:
        return None

    user = _user_payload(
        handle=handle,
        caption=caption,
        ocr_text=ocr_text or "",
        heuristic_name=heuristic_name,
        profile_name=profile_name,
    )
    data = _call_api(user)
    if not data:
        return None

    data["_cached"] = False
    data["_model"] = settings.DEEPSEEK_MODEL
    write_cached(key, {k: v for k, v in data.items() if not k.startswith("_")})
    return data


def merge_into_draft(
    draft: dict[str, Any],
    llm: dict[str, Any],
) -> dict[str, Any]:
    """Apply DeepSeek fields onto an existing experience draft (mutates and returns)."""
    if not llm or llm.get("isExperience") is False:
        return draft

    exp = draft.get("experience") or {}
    name = (llm.get("name") or "").strip()
    if name and len(name) >= 3 and len(name) <= 80:
        exp["name"] = name
        exp["slug"] = re.sub(r"[^a-z0-9]+", "-", f"{draft.get('handle','')}-{name}".lower()).strip("-")[:80]
        draft["title"] = name
        draft["nameSource"] = "deepseek"

    desc = (llm.get("description") or "").strip()
    if desc and len(desc) >= 12:
        exp["description"] = desc[:1200]

    cats = llm.get("categories")
    if isinstance(cats, list) and cats:
        cleaned = [c for c in cats if isinstance(c, str) and c]
        if cleaned:
            exp["categories"] = cleaned[:6]

    dress = llm.get("dressCode")
    if isinstance(dress, str) and dress.strip():
        exp["dressCode"] = dress.strip()[:80]

    age = llm.get("ageLimit")
    if isinstance(age, str) and age.strip():
        exp["ageLimit"] = age.strip()[:16]

    host = llm.get("host")
    if isinstance(host, str) and host.strip():
        exp["host"] = host.strip().lstrip("@")

    sched_in = llm.get("schedule") if isinstance(llm.get("schedule"), dict) else {}
    sched = dict(exp.get("schedule") or {})
    et = sched_in.get("eventType")
    if et in ("one-time", "recurring"):
        sched["eventType"] = et
    if sched_in.get("date"):
        sched["date"] = str(sched_in["date"])[:80]
    for key in ("startTime", "endTime"):
        val = sched_in.get(key)
        if isinstance(val, str) and re.match(r"^\d{1,2}:\d{2}$", val):
            # normalize H:MM → HH:MM
            h, m = val.split(":")
            sched[key] = f"{int(h):02d}:{m}"
    days = sched_in.get("recurrenceDays") or sched_in.get("days")
    if isinstance(days, list) and days:
        valid = [
            d for d in days
            if isinstance(d, str)
            and d.lower() in {
                "sunday", "monday", "tuesday", "wednesday",
                "thursday", "friday", "saturday",
            }
        ]
        if valid:
            titled = [d[:1].upper() + d[1:].lower() for d in valid]
            sched["recurrence"] = {
                "days": titled,
                "startDate": "",
                "endDate": "",
            }
            sched["eventType"] = "recurring"
    exp["schedule"] = sched

    prices = llm.get("pricePoints")
    if isinstance(prices, list) and prices:
        parsed: list[dict[str, Any]] = []
        for p in prices[:8]:
            if not isinstance(p, dict):
                continue
            try:
                price = float(p.get("price"))
            except (TypeError, ValueError):
                continue
            label = str(p.get("type") or "General").strip()[:60] or "General"
            parsed.append({"type": label, "price": int(price) if price == int(price) else price})
        if parsed:
            exp["pricePoints"] = parsed
            draft["priceHints"] = [f"₦{p['price']}" for p in parsed]

    venue = llm.get("venueHint")
    if isinstance(venue, str) and venue.strip():
        draft["venueHint"] = venue.strip()[:120]

    draft["experience"] = exp
    draft["llm"] = {
        "model": llm.get("_model") or settings.DEEPSEEK_MODEL,
        "cached": bool(llm.get("_cached")),
        "isExperience": bool(llm.get("isExperience", True)),
    }
    return draft

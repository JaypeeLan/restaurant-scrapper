"""
Extract product MenuType drafts from Instagram highlight slides.

Flow: logged-in slide fetch → OCR images → DeepSeek structure → MenuType rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config import settings

log = logging.getLogger("ig.menu")

MENU_CATEGORIES = {
    "appetizers",
    "main_course",
    "salads",
    "pizza_pasta",
    "seafood",
    "meat_poultry",
    "desserts",
    "sides",
    "sauces",
    "cocktails",
    "mocktails",
    "soft_drinks",
    "hot_beverages",
    "wine",
    "spirits",
    "champagne",
    "beer",
    "others",
}

_DRINK_CATS = {
    "cocktails",
    "mocktails",
    "soft_drinks",
    "hot_beverages",
    "wine",
    "spirits",
    "champagne",
    "beer",
}

_PRICE_RE = re.compile(
    r"(?:₦|ngn|naira|n)\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,7})\b"
    r"|(?:€|eur)\s*([0-9]+(?:[.,][0-9]{1,2})?)\b"
    r"|\$\s*([0-9]+(?:[.,][0-9]{1,2})?)\b"
    r"|([0-9]{1,3}(?:,[0-9]{3})+)\b",
    re.I,
)

_SYSTEM = """You extract restaurant MENU ITEMS from Instagram highlight flyer OCR.

Return ONLY compact JSON:
{
  "items": [
    {
      "itemName": string,
      "description": string,
      "price": number|null,
      "category": string,
      "type": "Food"|"Drink",
      "section": string
    }
  ]
}

Rules:
- category MUST be one of: appetizers, main_course, salads, pizza_pasta, seafood,
  meat_poultry, desserts, sides, sauces, cocktails, mocktails, soft_drinks,
  hot_beverages, wine, spirits, champagne, beer, others.
- type is Food or Drink (drinks categories → Drink).
- section is the on-page heading when present (e.g. "Sushi", "Dim Sum", "Cocktails").
- price is the numeric Naira amount as printed on Nigerian menus (always NGN).
  Examples: "N15,000" / "₦22,000" / "15k" → 15000; "32" beside a wine name on a
  Lagos menu usually means ₦32,000 (thousands abbreviated) → 32000.
  Never use euros or dollars. Never return bare 32 as 32 naira for a priced dish.
  Never invent prices. Promo slides with no price → price null.
- Skip headers, phone numbers, addresses, Instagram handles, "scan here", allergens-only lines.
- Prefer real dish/drink names. Deduplicate near-identical names.
- Ignore slides that are only marketing slogans / reservation CTAs with no dish list.
- OCR is noisy — fix obvious typos when confident.
"""


def _ocr_menu_score(text: str) -> float:
    """Higher = more likely a priced menu board (vs promo video cover)."""
    t = (text or "").strip()
    if len(t) < 40:
        return 0.0
    letters = sum(1 for c in t if c.isalpha())
    digits = sum(1 for c in t if c.isdigit())
    if letters < 25:
        return 0.0
    ratio = letters / max(len(t), 1)
    if ratio < 0.35:
        return 0.0
    price_hits = len(_PRICE_RE.findall(t))
    # Dense item lists usually have many digits even when OCR mangles ₦.
    score = min(letters, 800) / 10.0 + digits * 0.4 + price_hits * 25.0
    if price_hits == 0 and digits < 8:
        score *= 0.35
    return score


def select_menu_slides(
    slides: list[dict[str, Any]],
    *,
    max_slides: int | None = 16,
) -> list[dict[str, Any]]:
    """
    Prefer still images with dense OCR. Drop promo video covers when better
    slides exist.
    """
    limit = len(slides) if max_slides is None else max(0, int(max_slides))
    ranked: list[tuple[float, dict[str, Any]]] = []
    for slide in slides:
        text = str(slide.get("ocrText") or "")
        score = _ocr_menu_score(text)
        if slide.get("mediaType") == 1:
            score += 40.0
        elif slide.get("mediaType") == 2:
            score *= 0.75
        ranked.append((score, slide))
    ranked.sort(key=lambda x: (-x[0], int(x[1].get("order") or 0)))
    keep = [s for sc, s in ranked if sc >= 25.0][:limit]
    if not keep:
        # Fall back to highest-scoring non-empty OCR so promo trays still yield names.
        keep = [s for sc, s in ranked if (s.get("ocrText") or "").strip()][: min(6, limit)]
    keep.sort(key=lambda s: int(s.get("order") or 0))
    return keep


def _parse_price(raw: Any) -> float | int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        price = float(raw)
        if price < 0:
            return None
        return int(price) if price == int(price) else price
    s = str(raw).strip()
    if not s:
        return None
    # Common OCR: N15,000 / ₦22.000 / 15.000 (EU thousands) / 15000
    s_norm = s.replace("₦", "").replace("€", "").replace("$", "")
    s_norm = re.sub(r"(?i)\b(ngn|naira|eur|usd)\b", "", s_norm).strip()
    m = re.search(r"([0-9]{1,3}(?:[.,][0-9]{3})+|[0-9]+(?:[.,][0-9]{1,2})?)", s_norm)
    if not m:
        return None
    token = m.group(1)
    if re.fullmatch(r"[0-9]{1,3}([.,][0-9]{3})+", token):
        # thousand separators
        token = re.sub(r"[.,]", "", token)
    elif "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif token.count(",") == 1 and len(token.split(",")[-1]) <= 2:
        token = token.replace(",", ".")
    elif "," in token:
        token = token.replace(",", "")
    try:
        price = float(token)
    except ValueError:
        return None
    if price < 0:
        return None
    return int(price) if price == int(price) else price


def _normalize_naira_price(price: float | int | None) -> float | int | None:
    """
    Lagos menus quote Naira in thousands; OCR/LLM often drops trailing zeros (32 → 32000).
    """
    if price is None:
        return None
    p = float(price)
    if p <= 0:
        return None
    if p >= 1000:
        return int(p) if p == int(p) else p
    if p <= 300:
        scaled = p * 1000
        return int(scaled) if scaled == int(scaled) else scaled
    return int(p) if p == int(p) else p


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug_id(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _normalize_category(raw: str | None) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")
    aliases = {
        "main": "main_course",
        "mains": "main_course",
        "maincourse": "main_course",
        "pizza": "pizza_pasta",
        "pasta": "pizza_pasta",
        "meat": "meat_poultry",
        "poultry": "meat_poultry",
        "dessert": "desserts",
        "side": "sides",
        "sauce": "sauces",
        "cocktail": "cocktails",
        "mocktail": "mocktails",
        "soft_drink": "soft_drinks",
        "softdrinks": "soft_drinks",
        "hot_beverage": "hot_beverages",
        "coffee": "hot_beverages",
        "tea": "hot_beverages",
        "spirit": "spirits",
        "other": "others",
    }
    s = aliases.get(s, s)
    return s if s in MENU_CATEGORIES else "others"


def _normalize_type(raw: str | None, category: str) -> str:
    t = (raw or "").strip().title()
    if t in ("Food", "Drink"):
        return t
    return "Drink" if category in _DRINK_CATS else "Food"


def menu_items_from_llm(
    raw_items: list[dict[str, Any]],
    *,
    restaurant: str,
    tray_id: str,
) -> list[dict[str, Any]]:
    """Normalize LLM rows into MenuType-shaped dicts."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        name = re.sub(r"\s+", " ", str(row.get("itemName") or "")).strip()
        if len(name) < 2 or len(name) > 120:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        category = _normalize_category(str(row.get("category") or ""))
        item_type = _normalize_type(
            str(row.get("type") or "") if row.get("type") is not None else None,
            category,
        )
        desc = re.sub(r"\s+", " ", str(row.get("description") or "")).strip()[:400]
        price = _normalize_naira_price(_parse_price(row.get("price")))
        if price is None:
            blob = f"{name} {desc}".strip()
            if re.search(r"(?:₦|NGN|\bN)\s*[\d,]+|\b\d{1,3}(?:,\d{3})+\b", blob, re.I):
                price = _normalize_naira_price(_parse_price(blob))
        section = re.sub(r"\s+", " ", str(row.get("section") or "")).strip()[:80]
        out.append(
            {
                "_id": f"igmenu:{restaurant}:{tray_id}:{_slug_id(name, section)}",
                "restaurant": restaurant,
                "itemName": name[:120],
                "description": desc,
                "price": price if price is not None else 0,
                "category": category,
                "type": item_type,
                "section": section or category.replace("_", " ").title(),
                "sourceTrayId": tray_id,
            }
        )
    return out


def ocr_slide_urls(slides: list[dict[str, Any]], *, max_slides: int | None = None) -> list[dict[str, Any]]:
    """OCR each slide imageUrl; returns slides with ocrText attached."""
    from pipeline.ocr import ocr_url

    out: list[dict[str, Any]] = []
    limit = len(slides) if max_slides is None else max(0, int(max_slides))
    for slide in slides[:limit]:
        row = dict(slide)
        url = slide.get("imageUrl")
        sid = str(slide.get("id") or slide.get("order") or "")
        text = ""
        if url:
            try:
                text = ocr_url(str(url), cache_key=f"highlight-slide:{sid}", allow_fetch=True) or ""
            except Exception as exc:  # noqa: BLE001
                log.debug("slide OCR failed %s: %s", sid, exc)
                text = ""
        row["ocrText"] = text.strip()
        out.append(row)
    return out


def _call_deepseek(user_content: str) -> dict[str, Any] | None:
    if not (settings.DEEPSEEK_ENABLED and settings.DEEPSEEK_API_KEY):
        return None
    url = f"{settings.DEEPSEEK_BASE_URL}/chat/completions"
    body = {
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
        with httpx.Client(timeout=max(60, settings.DEEPSEEK_TIMEOUT_S)) as client:
            resp = client.post(url, headers=headers, json=body)
            if resp.status_code >= 400:
                log.warning("menu deepseek HTTP %s: %s", resp.status_code, resp.text[:240])
                return None
            content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        log.warning("menu deepseek failed: %s", exc)
        return None

    text = (content or "").strip()
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


def extract_menu_from_text(
    *,
    handle: str,
    source_id: str,
    source_title: str | None,
    text: str,
    source_kind: str = "highlight",
) -> list[dict[str, Any]]:
    """Run DeepSeek over plain menu text (PDF OCR, website HTML, highlight OCR)."""
    blob = (text or "").strip()
    if len(blob) < 40:
        return []

    if len(blob) > 28000:
        blob = blob[:28000] + "\n\n[truncated]"

    user = (
        f"IG handle: @{handle}\n"
        f"Menu source: {source_kind}\n"
        f"Menu title: {source_title or '(none)'}\n"
        f"Source id: {source_id}\n\n"
        f"MENU TEXT (Nigeria — prices in Naira):\n{blob}"
    )
    data = _call_deepseek(user)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return menu_items_from_llm(items, restaurant=handle, tray_id=source_id)


def extract_menu_from_ocr(
    *,
    handle: str,
    tray_id: str,
    tray_title: str | None,
    slides: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run DeepSeek over slide OCR (batched); merge + dedupe MenuType rows."""
    selected = select_menu_slides(slides)
    usable = [s for s in selected if (s.get("ocrText") or "").strip()]
    if not usable:
        return []

    batch_size = 4
    merged: list[dict[str, Any]] = []
    for start in range(0, len(usable), batch_size):
        batch = usable[start : start + batch_size]
        chunks: list[str] = []
        for slide in batch:
            text = (slide.get("ocrText") or "").strip()
            chunks.append(f"--- slide {slide.get('order', '?')} ---\n{text[:2800]}")
        blob = "\n\n".join(chunks)
        if len(blob) > 24000:
            blob = blob[:24000] + "\n\n[truncated]"

        naira_hint = bool(
            re.search(r"(?:₦|\bnaira\b|\bngn\b|\bN\s*\d{1,3}[,.]\d{3})", blob, re.I)
        )
        euro_hint = bool(re.search(r"(?:€|\beur\b)", blob, re.I))
        currency_note = (
            "Currency hints in OCR: "
            + (
                ", ".join(x for x, ok in (("Naira", naira_hint), ("Euro", euro_hint)) if ok)
                or "unclear"
            )
            + "."
        )

        user = (
            f"IG handle: @{handle}\n"
            f"Highlight title: {tray_title or '(none)'}\n"
            f"Tray id: {tray_id}\n"
            f"Batch slides: {[s.get('order') for s in batch]}\n"
            f"{currency_note}\n\n"
            f"FLYER OCR FROM HIGHLIGHT SLIDES:\n{blob}"
        )
        data = _call_deepseek(user)
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            merged.extend(items)

    return menu_items_from_llm(merged, restaurant=handle, tray_id=tray_id)


def extract_highlight_menu(
    db: Any,
    *,
    handle: str,
    tray_id: str,
    max_slides: int | None = 24,
    force: bool = False,
    from_stored: bool = False,
    max_age_days: int | None = None,
) -> dict[str, Any]:
    """
    End-to-end: fetch slides → OCR → MenuType items → persist on highlight doc.

    ``from_stored=True`` reuses slide OCR already on the highlight doc (no IG
    refetch / re-OCR) and only re-runs the LLM structurer.

    Without ``force``, skips trays that already have menuItems newer than
    ``max_age_days`` (defaults to ``MENU_EVERY_DAYS``).
    """
    from datetime import timedelta

    from ig import logged_in_search
    from pipeline import store

    handle = handle.strip().lstrip("@").lower()
    tray_id = str(tray_id).strip()
    doc_id = f"{handle}:{tray_id}"
    existing = db[settings.COL_HIGHLIGHTS].find_one({"_id": doc_id}) or {}
    age_days = (
        settings.MENU_EVERY_DAYS if max_age_days is None else max(1, int(max_age_days))
    )
    extracted_at = existing.get("menuExtractedAt")
    is_fresh = False
    if extracted_at and existing.get("menuItems") and int(existing.get("menuItemCount") or 0) > 0:
        if isinstance(extracted_at, datetime):
            at = extracted_at if extracted_at.tzinfo else extracted_at.replace(tzinfo=timezone.utc)
            is_fresh = at >= _now() - timedelta(days=age_days)
    if (
        not force
        and not from_stored
        and is_fresh
        and existing.get("slides")
    ):
        return {
            "handle": handle,
            "trayId": tray_id,
            "skipped": True,
            "itemCount": len(existing.get("menuItems") or []),
            "slideCount": len(existing.get("slides") or []),
        }

    if from_stored:
        slides = list(existing.get("slides") or [])
        if not any((s.get("ocrText") or "").strip() for s in slides):
            raise RuntimeError(
                f"no stored OCR on {doc_id} — run a full backfill first"
            )
        title = existing.get("title")
        cover = existing.get("coverUrl")
        media_count = existing.get("mediaCount")
    else:
        reel = logged_in_search.fetch_highlight_slides(tray_id, max_slides=max_slides)
        slides = ocr_slide_urls(reel.get("slides") or [], max_slides=max_slides)
        title = reel.get("title") or existing.get("title")
        cover = reel.get("coverUrl") or existing.get("coverUrl")
        media_count = reel.get("mediaCount")

    items = extract_menu_from_ocr(
        handle=handle,
        tray_id=tray_id,
        tray_title=title,
        slides=slides,
    )

    qr_urls: list[str] = []
    qr_menu_url: str | None = None
    try:
        from pipeline import qr_menu
        from pipeline.menu_merge import merge_menu_items

        qr_urls = qr_menu.qr_urls_from_slides(slides)
        if qr_urls:
            qr_text, qr_menu_url, _file_urls = qr_menu.price_text_from_qr_urls(qr_urls)
            if qr_text:
                qr_items = extract_menu_from_text(
                    handle=handle,
                    source_id=f"{tray_id}:qr",
                    source_title=f"{title or 'Menu'} prices",
                    text=qr_text,
                    source_kind="qr_folder",
                )
                if qr_items:
                    items = merge_menu_items(qr_items, items)
                    log.info(
                        "[menu] @%s tray %s QR prices → %d items (from %d highlight names)",
                        handle,
                        tray_id,
                        sum(1 for i in items if (i.get("price") or 0) > 0),
                        len(items),
                    )
    except Exception as exc:  # noqa: BLE001
        log.warning("[menu] QR price follow failed @%s %s: %s", handle, tray_id, exc)

    payload = {
        "handle": handle,
        "trayId": tray_id,
        "title": title,
        "coverUrl": cover,
        "mediaCount": media_count,
        "slides": [
            {
                "id": s.get("id"),
                "order": s.get("order"),
                "mediaType": s.get("mediaType"),
                "imageUrl": s.get("imageUrl"),
                "ocrText": (s.get("ocrText") or "")[:4000],
                "qrUrls": s.get("qrUrls") or [],
            }
            for s in slides
        ],
        "menuItems": items,
        "menuItemCount": len(items),
        "menuExtractedAt": _now(),
        "menuStatus": "ok" if items else "empty",
        "qrMenuUrl": qr_menu_url,
        "updatedAt": _now(),
    }
    store.upsert_highlight_menu(db, doc_id, payload)
    return {
        "handle": handle,
        "trayId": tray_id,
        "title": payload.get("title"),
        "skipped": False,
        "itemCount": len(items),
        "slideCount": len(slides),
        "items": items[:20],
    }

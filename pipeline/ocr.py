"""
OCR for Instagram post cards / flyers.

Captions are mostly SEO. The on-image title is the real experience name.
Results are cached under `.cache/ocr/` so the read API does not re-hit CDN
every refresh.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("ig.ocr")

_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "ocr"

_OFFERING_CORE = re.compile(
    r"\b(teppanyaki|hibachi|brunch|brunei|buffet|sushi|dim\s*sum|happy\s*hour|"
    r"unlimited\s+sushi|tasting\s+menu|dear\s+kaffy)\b",
    re.I,
)
_EVENTISH = re.compile(
    r"\b(brunch|brunei|affairs?|night|party|festival|session|seatings?|unlimited|"
    r"buffet|special|teppanyaki|hibachi|sushi|dim\s*sum|live\s+(dj|band|music|show)|"
    r"guest\s+dj|dear\s+kaffy|sunday|friday|saturday)\b",
    re.I,
)
_PRICEISH = re.compile(r"[₦$€£]\s*\d|\b\d{1,3}(?:,\d{3})+\b|\bper\s+person\b", re.I)
_TIMEISH = re.compile(r"\d{1,2}[:.]\d{2}\s*(am|pm)|\b\d{1,2}\s*(am|pm)\b", re.I)
_NOISE = re.compile(
    r"^(photo|image|may be|instagram|follow|link\s+in\s+bio|whats?app|"
    r"for\s+enquir|shiro|there'?s\s+only|for\s+reservations?)\b|"
    r"^@",
    re.I,
)
_PERSONISH = re.compile(r"\b(chef|hari|reservations?|call)\b", re.I)
_MENU_TIER = re.compile(
    r"\+|unlimited\s+food|food\s*\+|soft\s+beverage|alcoholic\s+drinks|"
    r"kids\s+brunch|per\s+person|for\s+enquir",
    re.I,
)
# Venue / CTA / status lines — never experience names.
_CTA_META = re.compile(
    r"\b(scan\s+here|get\s+your|tickets?|bit\.?ly|last\s+show|final\s+show|"
    r"final\s+call|be\s+there|or\s+ticket|ticket\s+link|production|"
    r"presented\s+by|austen-?peters)\b|"
    r"^the\s+.+\s+theat(?:re|er)\b|"
    r"^to\s*get\s+your\b",
    re.I,
)
_SUFFIX_STRIP = re.compile(
    r"\s+(seatings?|sessions?|experience|specials?)\s*$",
    re.I,
)
_HAS_DIGIT = re.compile(r"\d")
_BARE_DAY = re.compile(
    r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$",
    re.I,
)


def _cache_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode()).hexdigest()[:24]
    return _CACHE_DIR / f"{digest}.txt"


def read_cached(key: str) -> str | None:
    path = _cache_path(key)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def write_cached(key: str, text: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(key).write_text(text, encoding="utf-8")


def _merge_ocr_passes(texts: list[str]) -> str:
    """Union lines across OCR variants (order preserved, first-seen wins)."""
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        for raw in (text or "").splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            if not line:
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
    return "\n".join(out)


def _stitch_split_title_lines(text: str) -> str:
    """
    Flyer titles sometimes OCR as TEPPAN / YAKI or TEP / PANT AKI.
    Stitch short consecutive CAPS fragments back together.
    """
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        joined = "".join(re.sub(r"\s+", "", p) for p in buf)
        out.append(joined if len(buf) > 1 else buf[0])
        buf = []

    for line in lines:
        if not line:
            flush()
            out.append("")
            continue
        letters = [c for c in line if c.isalpha()]
        caps = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.7
        short = len(re.sub(r"\s+", "", line)) <= 12
        if caps and short and not re.search(r"\d", line):
            buf.append(line)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out)


def ocr_image_bytes(data: bytes) -> str:
    try:
        from io import BytesIO

        import pytesseract
        from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps
    except ImportError as exc:
        raise RuntimeError(
            "OCR deps missing — pip install pillow pytesseract and install tesseract"
        ) from exc

    image = Image.open(BytesIO(data))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image = image.convert("RGB")

    # CDN thumbs are often ~480px; orange titles on flame need heavy upscale.
    scale = 4 if max(image.size) < 900 else (2 if max(image.size) < 1400 else 1)
    if scale > 1:
        image = image.resize(
            (image.width * scale, image.height * scale),
            Image.Resampling.LANCZOS,
        )

    gray = ImageOps.autocontrast(ImageOps.grayscale(image)).filter(ImageFilter.SHARPEN)
    # Warm/orange titles (TEPPANYAKI) vanish in grayscale against fire — isolate R−B.
    r, _g, b = image.split()
    warm = ImageOps.autocontrast(ImageChops.subtract(r, b))
    warm = ImageEnhance.Contrast(warm).enhance(2.0)
    warm_bw = warm.point(lambda x: 255 if x > 80 else 0)

    passes = [
        pytesseract.image_to_string(gray, config="--psm 6") or "",
        pytesseract.image_to_string(warm, config="--psm 6") or "",
        pytesseract.image_to_string(warm_bw, config="--psm 6") or "",
        pytesseract.image_to_string(gray, config="--psm 11") or "",
    ]
    merged = _merge_ocr_passes(passes)
    return _stitch_split_title_lines(merged)


def ocr_url(url: str, *, cache_key: str | None = None) -> str:
    key = cache_key or url
    cached = read_cached(key)
    if cached is not None:
        return cached

    import httpx

    # Do not rewrite CDN size params — signatures are bound to the original URL.
    try:
        resp = httpx.get(
            url,
            timeout=25.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/*,*/*",
                "Referer": "https://www.instagram.com/",
            },
            follow_redirects=True,
        )
        resp.raise_for_status()
        text = ocr_image_bytes(resp.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("ocr failed for %s: %s", url[:80], exc)
        text = ""

    write_cached(key, text)
    return text


def _upper_ratio(line: str) -> float:
    letters = [c for c in line if c.isalpha()]
    if not letters:
        return 0.0
    return sum(c.isupper() for c in letters) / len(letters)


def _merge_flyer_lines(lines: list[str]) -> list[str]:
    """Join consecutive flyer-style lines: SUNDAY + BRUNCH AFFAIRS → one title."""
    merged: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            merged.append(" ".join(buf))
            buf = []

    for line in lines:
        if _HAS_DIGIT.search(line) or _TIMEISH.search(line) or _CTA_META.search(line):
            flush()
            # Keep non-meta lines for scoring; drop pure CTA/venue/status.
            if not _CTA_META.search(line) and not _TIMEISH.search(line) and not _HAS_DIGIT.search(line):
                merged.append(line)
            continue
        if re.match(r"^every\b", line, re.I):
            flush()
            continue
        caps = _upper_ratio(line) > 0.7 or line.isupper()
        short = len(line) <= 32
        if caps and short and len(buf) < 3:
            buf.append(line)
            continue
        flush()
        merged.append(line)
    flush()
    return merged


def _correct_title_with_caption(title: str, caption: str | None) -> str:
    """
    Fix common flyer OCR mistakes using caption/SEO text as a dictionary.

    Stylized fonts often read BRUNCH→BRUNEI, AFFAIRS→AFFAIR, TEPPANYAKI→LEPPANYAKI.
    """
    cap = caption or ""
    out = title
    if re.search(r"teppanyaki", cap, re.I) or re.search(r"teppanyaki|leppanyaki", out, re.I):
        out = re.sub(r"\bleppanyaki\b", "Teppanyaki", out, flags=re.I)
        out = re.sub(r"\bteppanyaki\b", "Teppanyaki", out, flags=re.I)
    if re.search(r"brunch", cap, re.I) or re.search(r"brunch|brunei", out, re.I):
        out = re.sub(r"\bbrunei\b", "Brunch", out, flags=re.I)
    if re.search(r"\bbrunch\b", out, re.I):
        out = re.sub(r"\baffair\b(?!s)", "Affairs", out, flags=re.I)
    out = re.sub(
        r"\s+every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*$",
        "",
        out,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", out).strip(" -–—\"'`")


def _normalize_experience_name(name: str) -> str:
    """Teppanyaki Seatings → Teppanyaki; keep Sunday Brunch Affairs."""
    original = name.strip(" -–—\"'`")
    stripped = _SUFFIX_STRIP.sub("", original).strip(" -–—\"'`")
    if stripped != original and len(stripped.split()) <= 2:
        core = _OFFERING_CORE.search(stripped)
        if core:
            token = core.group(0)
            return token.title() if token.islower() or token.isupper() else token[0].upper() + token[1:]
    return stripped or original


def _ocr_candidate_ok(line: str) -> bool:
    if _NOISE.search(line) or line.startswith("@"):
        return False
    if _MENU_TIER.search(line) or _CTA_META.search(line):
        return False
    if _PERSONISH.search(line) and not _OFFERING_CORE.search(line):
        return False
    # Must look like an event/offering label, not a slogan.
    if not (_EVENTISH.search(line) or _OFFERING_CORE.search(line)):
        return False
    return True


def title_from_ocr(text: str, *, caption: str | None = None) -> str | None:
    """
    Pick a flyer-style title from OCR text.

    Prefers ALL-CAPS / event-keyword lines (including multi-line flyer headers).
    Caption is used only to correct OCR mistakes, not as the primary name.
    """
    if not text or not text.strip():
        return None

    raw_lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" |.-•*\"'`")
        if len(line) < 3 or len(line) > 60:
            continue
        if _NOISE.search(line) or _PRICEISH.search(line) or _TIMEISH.search(line):
            continue
        if _MENU_TIER.search(line) or _CTA_META.search(line):
            continue
        if sum(c.isalpha() for c in line) < 3:
            continue
        raw_lines.append(line)

    # Merge CAPS flyer rows first (SUNDAY / BRUNEI / AFFAIR), then fix OCR typos.
    candidates = [
        _correct_title_with_caption(c, caption) for c in _merge_flyer_lines(raw_lines)
    ]

    scored: list[tuple[int, str]] = []
    for line in candidates:
        if _BARE_DAY.match(line) or not _ocr_candidate_ok(line):
            continue
        score = 0
        if _upper_ratio(line) > 0.7:
            score += 6
        if _EVENTISH.search(line):
            score += 8
        if _OFFERING_CORE.search(line):
            score += 10
        words = line.split()
        if 2 <= len(words) <= 4:
            score += 5  # flyer titles: Sunday Brunch Affairs
        elif len(words) == 1:
            score += 4  # single brand words like Teppanyaki
        elif len(words) > 6:
            score -= 8  # menu blurb, not a title
        if 8 <= len(line) <= 40:
            score += 3
        scored.append((score, line))

    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    best_score, best = scored[0]
    if best_score < 6:
        return None
    # Normalize casing: SUNDAY BRUNCH / mixed OCR → Sunday Brunch Affairs
    if _upper_ratio(best) > 0.55 or any(w.isupper() and len(w) > 2 for w in best.split()):
        best = best.title()
    best = _correct_title_with_caption(best, caption)
    return _normalize_experience_name(best)


def card_title_for_post(post: dict[str, Any]) -> str | None:
    """OCR the post media and return a likely experience name."""
    url = post.get("mediaUrl")
    if not url:
        raw = (post.get("source") or {}).get("raw") or {}
        url = raw.get("display_uri") or raw.get("display_url")
    if not url:
        return None

    post_id = str(post.get("_id") or post.get("id") or url)
    caption = post.get("caption") or ""
    if isinstance(caption, dict):
        caption = caption.get("text") or ""
    text = ocr_url(url, cache_key=f"post:{post_id}")
    return title_from_ocr(text, caption=caption)

"""
Discover and extract menus from Linktree / restaurant websites.

Instagram highlights are often stale; many Lagos venues publish current menus
on link-in-bio pages (Linktree PDFs) or their own site.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from config import settings

log = logging.getLogger("ig.web_menu")

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LINKTREE_RE = re.compile(r"(?:^|\.)linktr\.ee$|(?:^|\.)linktree\.com$", re.I)
MENU_HINT_RE = re.compile(
    r"menu|food|drink|beverage|wine|cocktail|brunch|kitchen|\bbar\b|dinner|lunch|"
    r"pastr(?:y|ies)|takeaway|cigar|spirits|beer",
    re.I,
)
SKIP_URL_RE = re.compile(
    r"instagram\.com|facebook\.com|tiktok\.com|twitter\.com|x\.com|linkedin\.com|"
    r"youtube\.com|threads\.net|whatsapp\.com|wa\.me|tel:|mailto:",
    re.I,
)
JUNK_URL_RE = re.compile(
    r"\[%|\{%|\{\{|<%=|item\.|downloadLink|"
    r"\.css(?:\?|$)|favicon|layout\.css|styles\.css|reset-min|flexslider|/main\.css",
    re.I,
)
JUNK_TITLE_RE = re.compile(r"\[%|\{%|\{\{|<%=|item\.|downloadLink", re.I)
STATIC_EXT_RE = re.compile(r"\.(css|js|ico|png|jpe?g|gif|svg|woff2?|ttf|map)(?:\?|$)", re.I)


@dataclass(frozen=True)
class WebMenuSource:
    title: str
    url: str
    kind: str  # pdf | page
    aggregator: str  # linktree | website
    profile_url: str | None = None


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


def _slug(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def _is_pdf(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or ".pdf" in path


def _url_path(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"


def _clean_title(raw: str) -> str:
    title = re.sub(r"\s+", " ", (raw or "")).strip()
    if not title or JUNK_TITLE_RE.search(title):
        return ""
    return title[:80]


def _is_usable_menu_url(url: str) -> bool:
    if not url.startswith("http"):
        return False
    if SKIP_URL_RE.search(url) or JUNK_URL_RE.search(url):
        return False
    if STATIC_EXT_RE.search(urlparse(url).path):
        return False
    if _is_pdf(url):
        return True
    return bool(MENU_HINT_RE.search(_url_path(url)))


def _kind_for_url(url: str) -> str:
    return "pdf" if _is_pdf(url) else "page"


def profile_link_urls(profile: dict[str, Any] | None) -> list[str]:
    """Collect unique bio / website URLs from an account profile."""
    profile = profile or {}
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str | None) -> None:
        u = (raw or "").strip()
        if not u or not u.startswith("http"):
            return
        key = u.rstrip("/").lower()
        if key in seen:
            return
        seen.add(key)
        out.append(u)

    add(profile.get("website"))
    for row in profile.get("bioLinks") or []:
        if isinstance(row, dict):
            add(row.get("url"))
        elif isinstance(row, str):
            add(row)
    return out


def _fetch_bytes(url: str, *, timeout: float = 45.0) -> bytes | None:
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                log.warning("[web-menu] HTTP %s for %s", resp.status_code, url[:120])
                return None
            return resp.content
    except Exception as exc:  # noqa: BLE001
        log.warning("[web-menu] fetch failed %s: %s", url[:120], exc)
        return None


def _fetch_text(url: str) -> str | None:
    data = _fetch_bytes(url)
    if not data:
        return None
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def _html_to_text(html: str) -> str:
    parser = _TextHTMLParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        pass
    return parser.text()


def _parse_linktree(html: str, profile_url: str) -> list[WebMenuSource]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    links = (data.get("props") or {}).get("pageProps", {}).get("account", {}).get("links") or []
    out: list[WebMenuSource] = []
    for row in links:
        if not isinstance(row, dict):
            continue
        title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
        if not title or row.get("type") == "HEADER":
            continue
        url = (row.get("url") or "").strip()
        ctx = row.get("context") or {}
        if not url and isinstance(ctx.get("data"), str):
            try:
                payload = json.loads(ctx["data"])
                url = str(payload.get("documentUrl") or payload.get("url") or "").strip()
            except json.JSONDecodeError:
                url = ""
        if not url and ctx.get("linkTypeId") == "document":
            continue
        if not url:
            continue
        if not _is_usable_menu_url(url):
            continue
        if not MENU_HINT_RE.search(title) and not _is_pdf(url):
            continue
        out.append(
            WebMenuSource(
                title=title,
                url=url,
                kind=_kind_for_url(url),
                aggregator="linktree",
                profile_url=profile_url,
            )
        )
    return out


def _discover_html_links(html: str, base_url: str) -> list[WebMenuSource]:
    out: list[WebMenuSource] = []
    seen: set[str] = set()
    for href in re.findall(r"""href=["']([^"']+)["']""", html, re.I):
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        url = urljoin(base_url, href)
        if not _is_usable_menu_url(url):
            continue
        key = url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        title = _clean_title(href.rsplit("/", 1)[-1].replace("-", " ").replace("_", " "))
        if not title:
            title = _clean_title(_url_path(url).rsplit("/", 1)[-1].replace("-", " "))
        if not title:
            continue
        out.append(
            WebMenuSource(
                title=title,
                url=url,
                kind=_kind_for_url(url),
                aggregator="website",
                profile_url=base_url,
            )
        )
    return out


def discover_menu_sources(profile_url: str) -> list[WebMenuSource]:
    """Resolve menu PDFs/pages from Linktree or a restaurant website."""
    host = urlparse(profile_url).netloc
    if LINKTREE_RE.search(host):
        html = _fetch_text(profile_url)
        if not html:
            return []
        return _parse_linktree(html, profile_url)

    html = _fetch_text(profile_url)
    if not html:
        return []
    sources = _discover_html_links(html, profile_url)

    # Homepage often links /menu — also try common paths when nothing found.
    if not sources:
        base = profile_url.rstrip("/")
        for suffix in ("/menu", "/menus", "/food-menu", "/drinks-menu", "/our-menu"):
            probe = f"{base}{suffix}"
            page = _fetch_text(probe)
            if not page:
                continue
            found = _discover_html_links(page, probe)
            if found:
                sources.extend(found)
            elif MENU_HINT_RE.search(probe):
                sources.append(
                    WebMenuSource(
                        title="Menu",
                        url=probe,
                        kind="page",
                        aggregator="website",
                        profile_url=profile_url,
                    )
                )
            if sources:
                break
    return sources


def discover_for_profile(profile: dict[str, Any] | None) -> list[WebMenuSource]:
    """Walk all profile URLs and merge discovered menu sources."""
    merged: list[WebMenuSource] = []
    seen: set[str] = set()
    for url in profile_link_urls(profile):
        for src in discover_menu_sources(url):
            key = src.url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(src)
    return merged


def pdf_to_text(data: bytes, *, max_pages: int = 12) -> str:
    """Extract text from PDF bytes; OCR raster pages when text layer is empty."""
    try:
        import fitz  # pymupdf
    except ImportError:
        log.warning("[web-menu] pymupdf not installed — PDF text extraction skipped")
        return ""

    from pipeline.ocr import ocr_image_bytes

    parts: list[str] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        log.warning("[web-menu] PDF open failed: %s", exc)
        return ""

    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        text = (page.get_text() or "").strip()
        if len(text) >= 40:
            parts.append(text)
            continue
        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            ocr = ocr_image_bytes(pix.tobytes("png"))
            if ocr.strip():
                parts.append(ocr.strip())
        except Exception as exc:  # noqa: BLE001
            log.debug("[web-menu] PDF page OCR failed p%s: %s", i, exc)
    return "\n\n".join(parts).strip()


def url_to_menu_text(source: WebMenuSource) -> str:
    data = _fetch_bytes(source.url)
    if not data:
        return ""
    if source.kind == "pdf" or _is_pdf(source.url):
        return pdf_to_text(data)
    try:
        html = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    return _html_to_text(html)


def is_junk_web_menu(doc: dict[str, Any]) -> bool:
    if doc.get("sourceType") != "web":
        return False
    title = str(doc.get("title") or "")
    menu_url = str(doc.get("menuUrl") or "")
    if JUNK_TITLE_RE.search(title) or JUNK_URL_RE.search(menu_url):
        return True
    if menu_url and not _is_usable_menu_url(menu_url):
        return True
    return False


def purge_junk_web_menus(db: Any) -> int:
    """Remove previously stored template/CSS junk from web menu docs."""
    cursor = db[settings.COL_HIGHLIGHTS].find(
        {"sourceType": "web"},
        {"title": 1, "menuUrl": 1, "sourceType": 1},
    )
    ids = [doc["_id"] for doc in cursor if is_junk_web_menu(doc)]
    if not ids:
        return 0
    result = db[settings.COL_HIGHLIGHTS].delete_many({"_id": {"$in": ids}})
    return int(result.deleted_count)


def backfill_web_menus(
    db: Any,
    *,
    limit: int = 20,
    handle: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    every_days: int | None = None,
) -> dict[str, int]:
    """
    Discover menus from profile.website / bio links; extract and store on highlight docs.
    """
    from pipeline import menu_extract, store

    store.ensure_indexes(db)
    purged = purge_junk_web_menus(db)
    every_days = max(1, int(every_days or settings.WEB_MENU_EVERY_DAYS))
    stale_before = datetime.now(timezone.utc) - timedelta(days=every_days)

    query: dict[str, Any] = {
        "$or": [
            {"profile.website": {"$exists": True, "$nin": [None, ""]}},
            {"profile.bioLinks.0": {"$exists": True}},
        ]
    }
    if handle:
        query["handle"] = handle.strip().lstrip("@").lower()

    accounts = list(
        db[settings.COL_ACCOUNTS]
        .find(query, {"handle": 1, "profile": 1})
        .sort([("handle", 1)])
        .limit(max(limit * 3, limit))
    )

    stats = {
        "accounts": len(accounts),
        "purged": purged,
        "sources": 0,
        "updated": 0,
        "ok": 0,
        "empty": 0,
        "error": 0,
        "skipped": 0,
        "dryRun": 0,
    }

    processed = 0
    for account in accounts:
        if processed >= limit:
            break
        handle_key = (account.get("handle") or "").lower()
        profile = account.get("profile") or {}
        sources = discover_for_profile(profile)
        if not sources:
            continue

        for src in sources:
            if processed >= limit:
                break
            stats["sources"] += 1
            doc_id = f"{handle_key}:web:{_slug(src.url)}"
            existing = db[settings.COL_HIGHLIGHTS].find_one({"_id": doc_id}) or {}
            if not force:
                fresh = existing.get("menuExtractedAt")
                has_items = int(existing.get("menuItemCount") or 0) > 0
                if (
                    has_items
                    and isinstance(fresh, datetime)
                    and fresh >= stale_before
                    and existing.get("menuStatus") == "ok"
                ):
                    stats["skipped"] += 1
                    continue

            if dry_run:
                stats["dryRun"] += 1
                stats["updated"] += 1
                processed += 1
                log.info("[web-menu] dry @%s → %s (%s)", handle_key, src.title, src.url[:80])
                continue

            text = url_to_menu_text(src)
            items: list[dict[str, Any]] = []
            status = "empty"
            if text.strip():
                tray_key = _slug(src.url)
                items = menu_extract.extract_menu_from_text(
                    handle=handle_key,
                    source_id=tray_key,
                    source_title=src.title,
                    text=text,
                    source_kind="website",
                )
                status = "ok" if items else "empty"
            else:
                status = "error"

            payload = {
                "handle": handle_key,
                "trayId": None,
                "title": src.title,
                "sourceType": "web",
                "webSource": src.aggregator,
                "sourceUrl": src.profile_url,
                "menuUrl": src.url,
                "menuItems": items,
                "menuItemCount": len(items),
                "menuStatus": status,
                "menuExtractedAt": datetime.now(timezone.utc),
            }
            store.upsert_highlight_menu(db, doc_id, payload)
            stats["updated"] += 1
            processed += 1
            if status == "ok":
                stats["ok"] += 1
            elif status == "error":
                stats["error"] += 1
            else:
                stats["empty"] += 1
            log.info(
                "[web-menu] @%s %s → %d items (%s)",
                handle_key,
                src.title,
                len(items),
                status,
            )

    return stats

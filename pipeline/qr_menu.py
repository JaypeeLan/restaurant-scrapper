"""
Follow “scan for prices” QR codes on Instagram menu highlights.

Sweet Sensation (and similar Lagos chains) print item names on Stories and put
the priced boards in a public Google Drive folder behind a QR.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("ig.qr_menu")

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

GDRIVE_FOLDER_RE = re.compile(
    r"(?:drive\.google\.com/drive/folders/|drive\.google\.com/embeddedfolderview\?id=)"
    r"([a-zA-Z0-9_-]+)",
    re.I,
)
GDRIVE_FILE_RE = re.compile(
    r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)",
    re.I,
)
FLIP_ENTRY_RE = re.compile(
    r'id="entry-([^"]+)"[\s\S]*?href="([^"]+)"[\s\S]*?class="flip-entry-title">([^<]*)</div>',
    re.I,
)
HTTP_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


@dataclass(frozen=True)
class DriveEntry:
    file_id: str
    title: str
    url: str
    kind: str  # folder | file


def decode_qr_from_bytes(data: bytes) -> list[str]:
    """Return unique http(s) payloads from QR codes in an image."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        log.debug("[qr] opencv not installed — skip QR decode")
        return []

    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return []

    det = cv2.QRCodeDetector()
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        for url in _http_urls(raw or ""):
            key = url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(url)

    try:
        payload, *_ = det.detectAndDecode(img)
        add(payload)
        ok, payloads, *_ = det.detectAndDecodeMulti(img)
        if ok and payloads is not None:
            for p in payloads:
                add(p)
    except Exception as exc:  # noqa: BLE001
        log.debug("[qr] decode failed: %s", exc)
    return found


def _http_urls(text: str) -> list[str]:
    return [m.group(0).rstrip(").,;") for m in HTTP_RE.finditer(text or "")]


def _fetch(url: str, *, timeout: float = 45.0, referer: str | None = None) -> httpx.Response | None:
    headers = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                log.warning("[qr] HTTP %s for %s", resp.status_code, url[:120])
                return None
            return resp
    except Exception as exc:  # noqa: BLE001
        log.warning("[qr] fetch failed %s: %s", url[:120], exc)
        return None


def fetch_image_bytes(url: str) -> bytes | None:
    resp = _fetch(url, referer="https://www.instagram.com/")
    if resp is None:
        return None
    return resp.content


def folder_id_from_url(url: str) -> str | None:
    m = GDRIVE_FOLDER_RE.search(url or "")
    return m.group(1) if m else None


def parse_embedded_folder(html: str) -> list[DriveEntry]:
    """Parse Google Drive embeddedfolderview HTML into files/folders."""
    out: list[DriveEntry] = []
    seen: set[str] = set()
    for file_id, href, title in FLIP_ENTRY_RE.findall(html or ""):
        if file_id in seen:
            continue
        seen.add(file_id)
        href = unescape(href)
        title = unescape(re.sub(r"\s+", " ", title)).strip() or file_id
        kind = "folder" if "/folders/" in href else "file"
        out.append(DriveEntry(file_id=file_id, title=title, url=href, kind=kind))
    return out


def list_gdrive_folder(folder_id: str) -> list[DriveEntry]:
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}"
    resp = _fetch(url)
    if resp is None:
        return []
    return parse_embedded_folder(resp.text)


def collect_gdrive_files(folder_url: str, *, max_files: int = 8, max_depth: int = 2) -> list[DriveEntry]:
    """Walk a public Drive folder (and one nested MENU folder) for downloadable files."""
    root = folder_id_from_url(folder_url)
    if not root:
        return []

    files: list[DriveEntry] = []
    seen_folders: set[str] = set()

    def walk(folder_id: str, depth: int) -> None:
        if folder_id in seen_folders or depth > max_depth or len(files) >= max_files:
            return
        seen_folders.add(folder_id)
        entries = list_gdrive_folder(folder_id)
        nested: list[DriveEntry] = []
        for entry in entries:
            if entry.kind == "file":
                files.append(entry)
                if len(files) >= max_files:
                    return
            elif entry.kind == "folder":
                nested.append(entry)
        for entry in nested:
            walk(entry.file_id, depth + 1)

    walk(root, 0)
    return files[:max_files]


def download_gdrive_file(file_id: str) -> bytes | None:
    url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    resp = _fetch(url)
    if resp is None:
        return None
    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/html" in ctype and len(resp.content) < 50_000:
        log.warning("[qr] Drive download looked like HTML for %s", file_id)
        return None
    return resp.content


def file_to_text(entry: DriveEntry, data: bytes) -> str:
    name = entry.title.lower()
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        from pipeline.web_menu import pdf_to_text

        return pdf_to_text(data)
    from pipeline.ocr import ocr_image_bytes

    return ocr_image_bytes(data) or ""


def qr_urls_from_slides(slides: list[dict[str, Any]]) -> list[str]:
    """Decode QR codes on highlight slides; reuse stored qrUrls when present."""
    found: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        u = (url or "").strip()
        if not u.startswith("http"):
            return
        key = u.rstrip("/").lower()
        if key in seen:
            return
        seen.add(key)
        found.append(u)

    for slide in slides:
        stored = slide.get("qrUrls") or []
        if isinstance(stored, list) and stored:
            for u in stored:
                add(str(u))
            continue
        url = slide.get("imageUrl")
        if not url:
            continue
        data = fetch_image_bytes(str(url))
        if not data:
            continue
        decoded = decode_qr_from_bytes(data)
        slide["qrUrls"] = decoded
        for u in decoded:
            add(u)
    return found


def price_text_from_qr_urls(urls: list[str]) -> tuple[str, str | None, list[str]]:
    """
    Follow QR URLs (Drive folders / PDFs / pages) and return
    (combined text, primary folder url, file urls).
    """
    parts: list[str] = []
    file_urls: list[str] = []
    primary: str | None = None

    for url in urls:
        folder_id = folder_id_from_url(url)
        if folder_id:
            primary = primary or url
            files = collect_gdrive_files(url)
            log.info("[qr] Drive folder %s → %d files", folder_id, len(files))
            for entry in files:
                data = download_gdrive_file(entry.file_id)
                if not data:
                    continue
                text = file_to_text(entry, data).strip()
                if not text:
                    continue
                file_urls.append(entry.url)
                parts.append(f"--- {entry.title} ---\n{text[:6000]}")
            continue

        if GDRIVE_FILE_RE.search(url):
            m = GDRIVE_FILE_RE.search(url)
            assert m
            data = download_gdrive_file(m.group(1))
            if data:
                text = file_to_text(
                    DriveEntry(file_id=m.group(1), title="Menu", url=url, kind="file"),
                    data,
                ).strip()
                if text:
                    primary = primary or url
                    file_urls.append(url)
                    parts.append(text[:6000])
            continue

        # Generic http page / PDF
        from pipeline.web_menu import WebMenuSource, url_to_menu_text

        kind = "pdf" if urlparse(url).path.lower().endswith(".pdf") else "page"
        text = url_to_menu_text(
            WebMenuSource(title="QR menu", url=url, kind=kind, aggregator="website", profile_url=url)
        )
        if text.strip():
            primary = primary or url
            file_urls.append(url)
            parts.append(text.strip()[:8000])

    return "\n\n".join(parts).strip(), primary, file_urls

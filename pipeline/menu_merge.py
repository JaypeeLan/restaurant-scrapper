"""
Merge menu trays/items from Instagram highlights and external sources.

When the same menu appears in both places, external (Linktree / website) wins
for item name and price.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


def _tray_title_key(title: str | None) -> str:
    s = re.sub(r"\s+", " ", (title or "").strip().lower())
    for suffix in (" menu", " pdf", " · pdf"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s or "menu"


def _item_name_key(name: str | None) -> str:
    s = (name or "").lower().strip()
    s = re.sub(r"['’]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def _host_label(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:  # noqa: BLE001
        return url


def tray_source_links(tray: dict[str, Any]) -> list[dict[str, str]]:
    """Build dashboard source chips for one tray."""
    out: list[dict[str, str]] = []
    is_web = tray.get("sourceType") == "web"
    menu_url = (tray.get("menuUrl") or "").strip()
    source_url = (tray.get("sourceUrl") or "").strip()
    permalink = (tray.get("permalink") or "").strip()
    title = (tray.get("title") or "").strip() or "Menu"

    if is_web:
        if menu_url:
            is_pdf = menu_url.lower().endswith(".pdf") or ".pdf" in menu_url.lower()
            out.append(
                {
                    "type": "Linktree" if tray.get("webSource") == "linktree" else "Website",
                    "label": f"{title} · PDF" if is_pdf else title,
                    "href": menu_url,
                    "origin": "web",
                }
            )
        if source_url and source_url != menu_url:
            out.append(
                {
                    "type": "Bio link",
                    "label": _host_label(source_url),
                    "href": source_url,
                    "origin": "web",
                }
            )
        return out

    href = permalink or menu_url
    if href:
        out.append(
            {
                "type": "Instagram",
                "label": title,
                "href": href,
                "origin": "highlight",
            }
        )
    return out


def _merge_item(external: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """External source fields win on overlap."""
    out = dict(other)
    for key in ("itemName", "price", "description", "category", "type", "section"):
        val = external.get(key)
        if val is None or val == "" or val == 0:
            continue
        out[key] = val
    return out


def merge_menu_items(
    external_items: list[dict[str, Any]],
    other_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Union items by normalized name. External rows override name/price on match.
    """
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for item in other_items:
        key = _item_name_key(item.get("itemName"))
        if not key:
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = dict(item)

    for item in external_items:
        key = _item_name_key(item.get("itemName"))
        if not key:
            continue
        if key in by_key:
            by_key[key] = _merge_item(item, by_key[key])
        else:
            by_key[key] = dict(item)
            order.append(key)

    return [by_key[k] for k in order if k in by_key]


def merge_menu_trays(trays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Collapse trays with the same title (e.g. IG highlight + Linktree PDF).

    External menu items take priority for name/price when items match.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for tray in trays:
        key = _tray_title_key(tray.get("title"))
        buckets.setdefault(key, []).append(tray)

    merged: list[dict[str, Any]] = []
    for group in buckets.values():
        if len(group) == 1:
            tray = dict(group[0])
            tray["sources"] = tray_source_links(tray)
            merged.append(tray)
            continue

        web_trays = [t for t in group if t.get("sourceType") == "web"]
        ig_trays = [t for t in group if t.get("sourceType") != "web"]
        web_trays.sort(key=lambda t: -int(t.get("menuItemCount") or 0))
        ig_trays.sort(key=lambda t: -int(t.get("menuItemCount") or 0))

        primary = dict(web_trays[0] if web_trays else ig_trays[0])
        web_items: list[dict[str, Any]] = []
        ig_items: list[dict[str, Any]] = []
        for t in web_trays:
            web_items.extend(t.get("menuItems") or [])
        for t in ig_trays:
            ig_items.extend(t.get("menuItems") or [])

        items = merge_menu_items(web_items, ig_items)
        primary["menuItems"] = items
        primary["menuItemCount"] = len(items)
        if web_trays:
            primary["sourceType"] = "web"

        # Combined source list (web links first).
        sources: list[dict[str, str]] = []
        seen_hrefs: set[str] = set()
        for t in web_trays + ig_trays:
            for src in tray_source_links(t):
                href = src["href"].rstrip("/").lower()
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                sources.append(src)
        primary["sources"] = sources
        primary["mergedFrom"] = [str(t.get("id") or "") for t in group if t.get("id")]

        merged.append(primary)

    merged.sort(key=lambda t: (-int(t.get("menuItemCount") or 0), (t.get("title") or "").lower()))
    return merged


def collapse_profile_menus(trays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    One menu card per restaurant. Hide trays with no extracted items.

    When both Instagram and web sources exist, merge all items and prefer
    external name/price on overlap.
    """
    with_items = [t for t in trays if int(t.get("menuItemCount") or 0) > 0]
    if not with_items:
        return []

    web_trays = [t for t in with_items if t.get("sourceType") == "web"]
    ig_trays = [t for t in with_items if t.get("sourceType") != "web"]
    web_trays.sort(key=lambda t: -int(t.get("menuItemCount") or 0))
    ig_trays.sort(key=lambda t: -int(t.get("menuItemCount") or 0))

    primary = dict(web_trays[0] if web_trays else ig_trays[0])
    web_items: list[dict[str, Any]] = []
    ig_items: list[dict[str, Any]] = []
    for t in web_trays:
        web_items.extend(t.get("menuItems") or [])
    for t in ig_trays:
        ig_items.extend(t.get("menuItems") or [])

    items = merge_menu_items(web_items, ig_items)
    primary["menuItems"] = items
    primary["menuItemCount"] = len(items)
    primary["title"] = "Menu"
    primary["kind"] = "menu"
    if web_trays:
        primary["sourceType"] = "web"

    sources: list[dict[str, str]] = []
    seen_hrefs: set[str] = set()
    for t in web_trays + ig_trays:
        for src in tray_source_links(t):
            href = src["href"].rstrip("/").lower()
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            sources.append(src)
    primary["sources"] = sources
    primary["mergedFrom"] = [str(t.get("id") or "") for t in with_items if t.get("id")]

    return [primary]

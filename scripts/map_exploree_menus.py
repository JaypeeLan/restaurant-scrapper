"""
Map venue menus into the exploree-api ``Menu`` create shape, one row per dish.

The restaurant mapper already finds a menu URL and reads it to decide cuisine
and meal service. This goes a step further and itemises it: every dish becomes
a Menu document with a name, price, category and type.

    python scripts/map_exploree_menus.py

Input is whatever the restaurant mapper last wrote, so run that first. Records
carry `restaurantRef` for resolving `Menu.restaurant` after the restaurants are
written; the import strips it before saving.

Dish extraction is DeepSeek over the menu text, reusing the extractor the
Instagram highlight pipeline already uses.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLES = Path("docs/sample_payloads")
MENU_TYPES = Path(
    "/Users/mac/Desktop/exploree-api/src/app/services/restaurant/menu/menu.types.ts"
)


def load_categories() -> set[str]:
    """Categories enum straight from the API source."""
    if not MENU_TYPES.exists():
        return set()
    src = MENU_TYPES.read_text()
    match = re.search(r"export enum Categories \{(.*?)\}", src, re.S)
    return set(re.findall(r"=\s*'([^']+)'", match.group(1))) if match else set()


def menu_text_for(record: dict[str, Any]) -> tuple[str, str]:
    """(text, source) for one venue, or ('', reason)."""
    from pipeline.web_menu import WebMenuSource, url_to_menu_text

    url = record.get("menuUrl")
    if not url:
        return "", "no menu url"
    source = WebMenuSource(
        title="menu",
        url=str(url),
        kind="pdf" if str(url).lower().endswith(".pdf") else "page",
        aggregator="website",
    )
    try:
        text = url_to_menu_text(source)
    except Exception as exc:  # noqa: BLE001
        return "", f"fetch failed: {exc}"
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) < 400:
        return "", f"too short ({len(text)} chars)"
    return text, str(url)


def build_menu_rows(
    record: dict[str, Any], categories: set[str]
) -> list[dict[str, Any]]:
    """One venue's menu text → validated Menu create bodies."""
    from pipeline.menu_extract import extract_menu_from_text

    name = record.get("name") or ""
    text, source = menu_text_for(record)
    if not text:
        print(f"  {name[:26]:<28} skipped: {source}", file=sys.stderr)
        return []

    handle = (record.get("socialMedia") or {}).get("ig") or name
    try:
        items = extract_menu_from_text(
            handle=handle,
            source_id=str(record.get("googlePlaceId") or name),
            source_title=f"{name} menu",
            text=text,
            source_kind="web",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  {name[:26]:<28} extraction failed: {exc}", file=sys.stderr)
        return []

    rows: list[dict[str, Any]] = []
    for item in items:
        category = item.get("category")
        if categories and category not in categories:
            continue
        # `price` is required and numeric. The extractor writes 0 when a menu
        # lists no price, which would publish every dish as free.
        price = item.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        rows.append(
            {
                "itemName": item["itemName"],
                "description": item.get("description") or None,
                "price": price,
                "category": category,
                "type": item.get("type"),
                "section": item.get("section") or None,
                # Resolved to the restaurant's _id during import.
                "restaurantRef": {
                    "googlePlaceId": record.get("googlePlaceId"),
                    "name": name,
                },
                "sourceUrl": source,
            }
        )
    print(f"  {name[:26]:<28} {len(rows)} dishes", file=sys.stderr)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--restaurants",
        default=str(SAMPLES / "exploree_restaurants_sample.json"),
        help="output of the restaurant mapper",
    )
    ap.add_argument("--out", default=str(SAMPLES / "exploree_menus_sample.json"))
    ap.add_argument("--limit", type=int, default=None, help="max venues to itemise")
    args = ap.parse_args()

    path = Path(args.restaurants)
    if not path.exists():
        print(f"no restaurants file at {path}", file=sys.stderr)
        return 1
    venues = json.loads(path.read_text())
    with_menu = [v for v in venues if v.get("menuUrl")]
    if args.limit:
        with_menu = with_menu[: args.limit]
    print(
        f"{len(with_menu)}/{len(venues)} venues have a menu url",
        file=sys.stderr,
    )

    categories = load_categories()
    print(f"loaded {len(categories)} menu categories from the API", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    for venue in with_menu:
        rows.extend(build_menu_rows(venue, categories))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False, default=str))

    by_type: dict[str, int] = {}
    for row in rows:
        by_type[row["type"]] = by_type.get(row["type"], 0) + 1
    print(f"\n  {len(rows)} dishes total  {by_type}", file=sys.stderr)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

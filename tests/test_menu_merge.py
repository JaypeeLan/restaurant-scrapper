"""Tests for menu tray/item merging."""

from pipeline.menu_merge import merge_menu_items, merge_menu_trays


def test_merge_items_external_price_wins() -> None:
    ig = [{"itemName": "Jollof Rice", "price": 5000, "category": "main_course", "type": "Food", "section": "Mains"}]
    web = [{"itemName": "Jollof Rice", "price": 8500, "category": "main_course", "type": "Food", "section": "Mains"}]
    merged = merge_menu_items(web, ig)
    assert len(merged) == 1
    assert merged[0]["price"] == 8500


def test_merge_items_keeps_ig_only() -> None:
    ig = [{"itemName": "Plantain", "price": 2000, "category": "sides", "type": "Food", "section": "Sides"}]
    web = [{"itemName": "Jollof Rice", "price": 8500, "category": "main_course", "type": "Food", "section": "Mains"}]
    merged = merge_menu_items(web, ig)
    assert len(merged) == 2
    names = {m["itemName"] for m in merged}
    assert names == {"Plantain", "Jollof Rice"}


def test_merge_trays_same_title() -> None:
    trays = [
        {
            "id": "foo:123",
            "title": "Food Menu",
            "sourceType": "highlight",
            "kind": "menu",
            "permalink": "https://instagram.com/h/123",
            "menuItems": [{"itemName": "Soup", "price": 3000}],
            "menuItemCount": 1,
        },
        {
            "id": "foo:web:abc",
            "title": "Food Menu",
            "sourceType": "web",
            "webSource": "linktree",
            "kind": "menu",
            "menuUrl": "https://example.com/food.pdf",
            "sourceUrl": "https://linktr.ee/foo",
            "menuItems": [{"itemName": "Soup", "price": 4500}],
            "menuItemCount": 1,
        },
    ]
    merged = merge_menu_trays(trays)
    assert len(merged) == 1
    assert merged[0]["menuItems"][0]["price"] == 4500
    assert len(merged[0]["sources"]) == 2

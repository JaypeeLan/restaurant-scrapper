"""Tests for menu tray/item merging."""

from pipeline.menu_merge import collapse_profile_menus, merge_menu_items


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


def test_collapse_profile_menus_one_card() -> None:
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
            "id": "foo:456",
            "title": "Drinks",
            "sourceType": "highlight",
            "kind": "menu",
            "permalink": "https://instagram.com/h/456",
            "menuItems": [{"itemName": "Beer", "price": 2000}],
            "menuItemCount": 1,
        },
        {
            "id": "foo:web:abc",
            "title": "Menu PDF",
            "sourceType": "web",
            "webSource": "website",
            "kind": "menu",
            "menuUrl": "https://example.com/menu.pdf",
            "sourceUrl": "https://example.com",
            "menuItems": [{"itemName": "Soup", "price": 4500}],
            "menuItemCount": 1,
        },
    ]
    from pipeline.menu_merge import collapse_profile_menus

    merged = collapse_profile_menus(trays)
    assert len(merged) == 1
    assert merged[0]["menuItemCount"] == 2
    assert merged[0]["menuItems"][0]["price"] == 4500


def test_collapse_hides_empty() -> None:
    from pipeline.menu_merge import collapse_profile_menus

    trays = [
        {
            "id": "foo:web:empty",
            "title": "Menu",
            "sourceType": "web",
            "menuUrl": "https://example.com/menu",
            "menuItems": [],
            "menuItemCount": 0,
        },
    ]
    assert collapse_profile_menus(trays) == []


"""Tests for web menu URL filtering."""

from pipeline.web_menu import _is_usable_menu_url, is_junk_web_menu


def test_rejects_template_urls() -> None:
    assert not _is_usable_menu_url(
        "https://www.bougainvilleabarbados.com/dining/[%= item.url %]"
    )
    assert not _is_usable_menu_url(
        "https://example.com/[%= item.downloadLink %]"
    )


def test_rejects_css_and_hostname_bar_false_positive() -> None:
    assert not _is_usable_menu_url("https://www.bougainvilleabarbados.com/css/main.css")
    assert not _is_usable_menu_url("https://www.bougainvilleabarbados.com/")


def test_accepts_menu_path_not_hostname() -> None:
    assert _is_usable_menu_url("https://www.bougainvilleabarbados.com/dining/menu.pdf")
    assert _is_usable_menu_url("https://slowlagos.com/menu")


def test_is_junk_web_menu_doc() -> None:
    assert is_junk_web_menu(
        {
            "sourceType": "web",
            "title": "[%= item.url %]",
            "menuUrl": "https://example.com/[%= item.url %]",
        }
    )

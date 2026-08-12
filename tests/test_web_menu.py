"""Tests for web menu URL filtering."""

from pipeline.web_menu import _accept_menu_candidate, _is_usable_menu_url, is_junk_web_menu, pick_best_source
from pipeline.web_menu import WebMenuSource


def test_anchor_text_accepts_menu_link() -> None:
    assert _accept_menu_candidate("https://example.com/files/spring-2024.pdf", "Download our menu")
    assert not _accept_menu_candidate("https://example.com/about", "About us")


def test_pick_best_source_prefers_pdf() -> None:
    sources = [
        WebMenuSource(title="Home", url="https://example.com/menu", kind="page", aggregator="website"),
        WebMenuSource(title="Menu PDF", url="https://example.com/menu.pdf", kind="pdf", aggregator="website"),
    ]
    best = pick_best_source(sources)
    assert best is not None
    assert best.url.endswith(".pdf")


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

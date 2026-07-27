"""Fixture-based tests for the HTML parsing paths (issue #8).

Fixtures are minimal, sanitized documents containing only the tags the
parsers read — no network access involved.
"""
from pathlib import Path

import product_analyzer as pa

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------- quick_parse_head ----------------

def test_quick_parse_head_prefers_jsonld_product():
    out = pa.quick_parse_head(load("jsonld_product.html"))
    assert out["title"] == "Espresso Machine Deluxe"
    assert out["price_amount"] == 49.99
    assert out["price_currency"] == "EUR"
    # First image of the JSON-LD list wins
    assert out["image"] == "https://cdn.example.org/espresso-front.jpg"


def test_quick_parse_head_opengraph_fallback():
    out = pa.quick_parse_head(load("opengraph_product.html"))
    # og:title is passed through _clean_title (marketplace suffix removed)
    assert out["title"] == "Wireless Earbuds"
    assert out["price_amount"] == 19.5
    # Meta price path has no currency information; USD default kept
    assert out["price_currency"] == "USD"
    assert out["image"] == "https://cdn.example.org/earbuds.jpg"


def test_quick_parse_head_embedded_price_regex_fallback():
    out = pa.quick_parse_head(load("embedded_price.html"))
    assert out["title"] == "Bluetooth Speaker Sale"
    # "currentPrice":"US $12.34" picked up by the script-JSON regex tier
    assert out["price_amount"] == 12.34


def test_quick_parse_head_empty_document():
    out = pa.quick_parse_head("<html><body><p>nothing</p></body></html>")
    assert out["title"] == ""
    assert out["price_amount"] is None
    assert out["image"] == ""


# ---------------- _parse_aliexpress ----------------

def test_parse_aliexpress_runparams():
    out = pa._parse_aliexpress(load("aliexpress_runparams.html"))
    assert out["title"] == "USB C Charging Cable 2m"
    assert out["images"] == [
        "https://ae01.alicdn.com/kf/cable-a.jpg",
        "https://ae01.alicdn.com/kf/cable-b.jpg",
    ]
    assert out["price_amount"] == 23.99
    # Currency is taken from the first symbol found in "US $23.99"
    assert out["price_currency"] == "$"
    assert out["specifications"] == {
        "Material": "Nylon Braided",
        "Length": "2m",
    }
    assert out["breadcrumbs"] == ["Home", "Accessories", "Cables"]


def test_parse_aliexpress_without_data_returns_defaults():
    out = pa._parse_aliexpress("<html><body><p>no runParams here</p></body></html>")
    assert out["title"] == ""
    assert out["images"] == []
    assert out["price_amount"] is None
    assert out["specifications"] == {}
    assert out["breadcrumbs"] == []

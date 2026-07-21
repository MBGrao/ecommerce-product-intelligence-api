"""Pure parsing/cleaning helpers."""
import product_analyzer as pa


# ---------------- _clean_title ----------------

def test_clean_title_strips_amazon_prefix():
    assert pa._clean_title("Amazon.com: Wireless Earbuds") == "Wireless Earbuds"


def test_clean_title_strips_buy_suffix():
    assert pa._clean_title("Cool Gadget | Buy online now") == "Cool Gadget"


def test_clean_title_strips_marketplace_suffix():
    assert pa._clean_title("Smart Watch - AliExpress 12345") == "Smart Watch"


def test_clean_title_collapses_whitespace():
    assert pa._clean_title("  Smart   Watch  ") == "Smart Watch"


def test_clean_title_empty_input():
    assert pa._clean_title("") == ""
    assert pa._clean_title(None) == ""


# ---------------- _first_non_empty ----------------

def test_first_non_empty_returns_first_real_value():
    assert pa._first_non_empty("", None, "  x ", "y") == "x"


def test_first_non_empty_all_empty():
    assert pa._first_non_empty(None, "", "   ") == ""


# ---------------- _filter_images_by_host ----------------

def test_filter_images_keeps_allowed_hosts_and_subdomains():
    urls = [
        "https://ae01.alicdn.com/kf/a.jpg",
        "https://evil.com/b.jpg",
        "https://alicdn.com/c.jpg",
        None,  # must be skipped, not crash
    ]
    assert pa._filter_images_by_host(urls, ["alicdn.com"]) == [
        "https://ae01.alicdn.com/kf/a.jpg",
        "https://alicdn.com/c.jpg",
    ]


def test_filter_images_empty_input():
    assert pa._filter_images_by_host([], ["alicdn.com"]) == []
    assert pa._filter_images_by_host(None, ["alicdn.com"]) == []


# ---------------- _clean_features ----------------

def test_clean_features_drops_junk_dedupes_and_caps_at_six():
    features = [
        "Font",          # junk
        "advertising",   # junk
        "LED Light",
        "led light",     # case-insensitive duplicate
        "",              # empty
        "Battery",
        "Waterproof",
        "Bluetooth 5.0",
        "Fast Charging",
        "Compact",
        "One Too Many",
    ]
    assert pa._clean_features(features) == [
        "LED Light",
        "Battery",
        "Waterproof",
        "Bluetooth 5.0",
        "Fast Charging",
        "Compact",
    ]


# ---------------- clean_specifications ----------------

def test_clean_specifications_drops_empty_and_zero_values():
    specs = {"zzz_custom": "  cotton ", "empty": "", "zero": "0"}
    assert pa.clean_specifications(specs) == {"zzz_custom": "cotton"}


# ---------------- extract_price_from_text ----------------

def test_extract_price_usd_with_thousands_separator():
    assert pa.extract_price_from_text("Total: 1,299.99 USD today") == {
        "amount": 1299.99,
        "currency": "USD",
    }


def test_extract_price_dollar_symbol():
    assert pa.extract_price_from_text("only 25.50$ each") == {
        "amount": 25.50,
        "currency": "USD",
    }


def test_extract_price_arabic_dirham():
    assert pa.extract_price_from_text("price 45 درهم") == {
        "amount": 45.0,
        "currency": "AED",
    }


def test_extract_price_absent():
    assert pa.extract_price_from_text("hello world") is None
    assert pa.extract_price_from_text("") is None

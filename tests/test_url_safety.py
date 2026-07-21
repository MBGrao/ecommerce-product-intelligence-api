"""SSRF protections: validate_public_url / is_private_host."""
import pytest
from fastapi import HTTPException

import product_analyzer as pa


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "gopher://example.com/1",
        "ws://example.com/socket",
    ],
)
def test_non_http_schemes_rejected(url):
    with pytest.raises(HTTPException) as exc:
        pa.validate_public_url(url)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8000/admin",
        "http://10.0.0.5/internal",
        "http://192.168.1.10/router",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://[::1]/",
    ],
)
def test_private_and_metadata_hosts_blocked(url):
    with pytest.raises(HTTPException) as exc:
        pa.validate_public_url(url)
    assert exc.value.status_code == 400


def test_url_without_hostname_rejected():
    with pytest.raises(HTTPException) as exc:
        pa.validate_public_url("http://")
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "url",
    [
        "https://www.aliexpress.com/item/100500.html",
        "https://ae01.alicdn.com/kf/image.jpg",
        "https://amazon.com/dp/B000000",
    ],
)
def test_trusted_ecommerce_hosts_allowed(url):
    # Trusted domains short-circuit before DNS resolution.
    pa.validate_public_url(url)  # must not raise


def test_unresolvable_host_fails_closed():
    # .invalid never resolves (RFC 2606); resolution failure must be
    # treated as unsafe rather than allowed through.
    assert pa.is_private_host("definitely-not-real.invalid") is True

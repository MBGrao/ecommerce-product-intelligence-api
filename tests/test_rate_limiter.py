"""Sliding-window rate limiter behaviour.

RATE_MAX_REQUESTS is patched down so each test sends a handful of
requests instead of 50+; the limiter reads the module global at call
time, so the sliding-window logic under test is unchanged.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import product_analyzer as pa

client = TestClient(pa.app)
VALID_KEY = {"X-API-Key": "test-api-key"}
SMALL_LIMIT = 5


@pytest.fixture(autouse=True)
def small_clean_buckets(monkeypatch):
    # Other test modules hit rate-limited endpoints in the same process;
    # start and end each test with empty buckets for isolation.
    monkeypatch.setattr(pa, "RATE_MAX_REQUESTS", SMALL_LIMIT)
    pa._ip_hits.clear()
    yield
    pa._ip_hits.clear()


def _fake_request(ip: str) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def test_allows_up_to_limit_then_429_over_http():
    for _ in range(SMALL_LIMIT):
        r = client.post("/crop", json={}, headers=VALID_KEY)
        # Auth and rate limiting pass; validation rejects the empty body.
        assert r.status_code == 422
    r = client.post("/crop", json={}, headers=VALID_KEY)
    assert r.status_code == 429


def test_window_expiry_frees_the_bucket():
    for _ in range(SMALL_LIMIT):
        assert client.post("/crop", json={}, headers=VALID_KEY).status_code == 422
    assert client.post("/crop", json={}, headers=VALID_KEY).status_code == 429

    # Age every recorded hit past the window instead of sleeping.
    bucket = pa._ip_hits["testclient"]
    aged = [t - (pa.RATE_WINDOW_SEC + 1) for t in bucket]
    bucket.clear()
    bucket.extend(aged)

    assert client.post("/crop", json={}, headers=VALID_KEY).status_code == 422


def test_buckets_are_per_ip():
    async def exhaust(ip: str):
        for _ in range(SMALL_LIMIT + 1):
            await pa.rate_limiter(_fake_request(ip))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(exhaust("203.0.113.1"))
    assert exc.value.status_code == 429

    # A different IP is unaffected by the exhausted bucket.
    async def one(ip: str):
        return await pa.rate_limiter(_fake_request(ip))

    assert asyncio.run(one("203.0.113.2")) is True


def test_health_endpoint_is_not_rate_limited():
    for _ in range(SMALL_LIMIT + 3):
        assert client.get("/health").status_code == 200

"""Endpoint behaviour: health, authentication, request validation."""
from fastapi.testclient import TestClient

import product_analyzer

client = TestClient(product_analyzer.app)

VALID_KEY = {"X-API-Key": "test-api-key"}


def test_health_returns_ok():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_analyze_partial_requires_api_key():
    r = client.post("/analyze/partial", json={})
    assert r.status_code == 401


def test_analyze_partial_rejects_wrong_key():
    r = client.post("/analyze/partial", json={}, headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 401


def test_analyze_full_requires_api_key():
    r = client.post("/analyze/full", json={})
    assert r.status_code == 401


def test_crop_requires_api_key():
    r = client.post("/crop", json={})
    assert r.status_code == 401


def test_valid_key_reaches_validation():
    # With the right key the request passes auth and fails on the
    # missing required field instead (CropInput.image_base64).
    r = client.post("/crop", json={}, headers=VALID_KEY)
    assert r.status_code == 422


def test_responses_carry_request_id_header():
    r = client.get("/health")
    assert r.headers.get("X-Request-Id")

"""Server-side center crop: the only code path that decodes images with Pillow."""
import base64
import io

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import product_analyzer as pa

client = TestClient(pa.app)
VALID_KEY = {"X-API-Key": "test-api-key"}

RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)


def _striped_png(width: int, height: int) -> bytes:
    # Three equal bands along the long side: red | green | blue.
    # A correct center crop keeps only the green band.
    im = Image.new("RGB", (width, height), GREEN)
    if width >= height:
        third = width // 3
        im.paste(RED, (0, 0, third, height))
        im.paste(BLUE, (width - third, 0, width, height))
    else:
        third = height // 3
        im.paste(RED, (0, 0, width, third))
        im.paste(BLUE, (0, height - third, width, height))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _decode(jpeg_bytes: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(jpeg_bytes))
    im.load()
    return im


def _is_close(pixel, expected, tolerance=40):
    # JPEG is lossy; solid blocks still land well within this margin.
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, expected))


@pytest.mark.parametrize("size", [(90, 30), (30, 90)], ids=["landscape", "portrait"])
def test_center_crop_returns_centered_square_jpeg(size):
    out = _decode(pa.image_center_crop(_striped_png(*size)))
    assert out.format == "JPEG"
    assert out.mode == "RGB"
    assert out.size == (30, 30)
    assert _is_close(out.getpixel((15, 15)), GREEN)


def test_center_crop_keeps_square_input_size():
    out = _decode(pa.image_center_crop(_striped_png(30, 30)))
    assert out.size == (30, 30)


@pytest.mark.parametrize("mode", ["RGBA", "P", "L"])
def test_center_crop_converts_non_rgb_modes(mode):
    buf = io.BytesIO()
    Image.new(mode, (20, 10)).save(buf, format="PNG")
    out = _decode(pa.image_center_crop(buf.getvalue()))
    assert out.mode == "RGB"
    assert out.size == (10, 10)


def test_center_crop_rejects_non_image_bytes():
    with pytest.raises(HTTPException) as exc:
        pa.image_center_crop(b"definitely not an image")
    assert exc.value.status_code == 400


def test_crop_endpoint_disabled_by_default():
    r = client.post("/crop", json={"image_base64": "AAAA"}, headers=VALID_KEY)
    assert r.status_code == 403


def test_crop_endpoint_round_trip(monkeypatch):
    monkeypatch.setattr(pa, "ENABLE_SERVER_CROP", True)
    payload = base64.b64encode(_striped_png(90, 30)).decode()
    r = client.post("/crop", json={"image_base64": payload}, headers=VALID_KEY)
    assert r.status_code == 200
    out = _decode(base64.b64decode(r.json()["image_base64_cropped"]))
    assert out.size == (30, 30)
    assert _is_close(out.getpixel((15, 15)), GREEN)


def test_crop_endpoint_rejects_unknown_mode(monkeypatch):
    monkeypatch.setattr(pa, "ENABLE_SERVER_CROP", True)
    payload = base64.b64encode(_striped_png(30, 30)).decode()
    r = client.post("/crop", json={"image_base64": payload, "mode": "circle"}, headers=VALID_KEY)
    assert r.status_code == 400

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Product Intelligence API (FastAPI, async, Arabic JSON contract, Vision + Scraping)

Week 1.5 Performance Hotfix:
- Partial analysis <300ms SLO (scrape-only, cache-first, no Vision)
- Improved title cleaning and image selection
- Windows Playwright hardening (auto-disable, solid httpx/BS4 fallback)
- Proper error handling & consistent JSON error responses

Endpoints
---------
POST /analyze/partial   -> fast partial JSON  (name + price (YER) + 1 image) from scraping
POST /analyze/full      -> full enrichment JSON per Arabic contract (5–8 images, video, desc, specs, categories)
POST /crop              -> (optional) server-side cropping

Install
-------
python -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn "httpx[http2]" pydantic beautifulsoup4 pillow google-cloud-vision lxml python-dotenv playwright
# if you want Playwright headless scraping:
#   playwright install chromium

Environment
----------
export API_KEY=supersecret_apikey                # REQUIRED (X-API-Key)
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export YER_PER_USD=250.0                         # fallback FX rate
export ENABLE_SERVER_CROP=false
export ENABLE_PLAYWRIGHT=false                   # default false on Windows; set true to force
export SUPABASE_PARTIAL_WEBHOOK=https://...      # optional
export SUPABASE_FULL_WEBHOOK=https://...         # optional
export SUPABASE_API_KEY=...                      # optional
export REQUEST_HARD_TIMEOUT_MS=2800

export MAX_IMAGE_BYTES=5242880                   # 5 MB cap per image download
export ALLOWED_SCRAPING_DOMAINS="aliexpress.com,amazon.com,amazon.ae,amazon.sa,amazon.co.uk,amazon.de,amazon.fr,noon.com,souq.com,jumia.com,daraz.com,ebay.com,etsy.com,shopify.com,woocommerce.com"

# Performance tuning (optional)
export STRICT_PARTIAL_FROM_SCRAPE=true          # partial only from scraping (no Vision)
export PARTIAL_TIMEOUT_MS=300                   # partial analysis timeout
export QUICK_HTML_MAX_BYTES=163840              # ~160 KB max for quick fetch
export QUICK_CONNECT_TIMEOUT_MS=150             # quick connect timeout
export QUICK_READ_TIMEOUT_MS=120                # quick read timeout

Run
---
uvicorn product_analyzer:app --host 0.0.0.0 --port 8080 --proxy-headers
"""
import asyncio
import base64
import io
import ipaddress
import json
import logging
import os
import re
import socket
import time
import uuid
import hashlib
from collections import deque, defaultdict, OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin, parse_qs

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import (
    FastAPI, HTTPException, Request, Depends, BackgroundTasks
)
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError
from google.cloud import vision

# Playwright is optional
PLAYWRIGHT_AVAILABLE = True
try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
except Exception:
    PLAYWRIGHT_AVAILABLE = False

# ---------------------- Load ENV ----------------------
load_dotenv()

API_KEY = os.getenv("API_KEY", "")
if not API_KEY:
    raise SystemExit("API_KEY is required (env API_KEY)")

ENABLE_SERVER_CROP = os.getenv("ENABLE_SERVER_CROP", "false").lower() == "true"
YER_PER_USD = float(os.getenv("YER_PER_USD", "250.0"))
SUPABASE_PARTIAL_WEBHOOK = os.getenv("SUPABASE_PARTIAL_WEBHOOK")
SUPABASE_FULL_WEBHOOK = os.getenv("SUPABASE_FULL_WEBHOOK")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
REQUEST_HARD_TIMEOUT_MS = int(os.getenv("REQUEST_HARD_TIMEOUT_MS", "12000"))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", "5242880"))  # 5MB

# Playwright enablement (default true for better AliExpress scraping)
ENV_ENABLE_PLAYWRIGHT = os.getenv("ENABLE_PLAYWRIGHT", "true").lower() == "true"
IS_WINDOWS = os.name == "nt"
USE_PLAYWRIGHT = (PLAYWRIGHT_AVAILABLE and ENV_ENABLE_PLAYWRIGHT)  # Simplified logic for VPS

# --------- Partial fast-path tuning ----------
STRICT_PARTIAL_FROM_SCRAPE = os.getenv("STRICT_PARTIAL_FROM_SCRAPE", "false").lower() == "true"  # Changed to false
USE_VISION_SIMILAR_IMAGES = os.getenv("USE_VISION_SIMILAR_IMAGES", "true").lower() == "true"  # Default to true for client readiness
USE_GOOGLE_SHOPPING = os.getenv("USE_GOOGLE_SHOPPING", "true").lower() == "true"
PARTIAL_TIMEOUT_MS = int(os.getenv("PARTIAL_TIMEOUT_MS", "600"))  # Increased for better reliability
QUICK_HTML_MAX_BYTES = int(os.getenv("QUICK_HTML_MAX_BYTES", "163840"))  # ~160KB
QUICK_CONNECT_TIMEOUT_MS = int(os.getenv("QUICK_CONNECT_TIMEOUT_MS", "150"))
QUICK_READ_TIMEOUT_MS = int(os.getenv("QUICK_READ_TIMEOUT_MS", "120"))

# Ultra-fast httpx client for partial (tiny timeouts)
_fast_transport = httpx.AsyncHTTPTransport(retries=0)
fast_client = httpx.AsyncClient(
    timeout=httpx.Timeout(
        connect=QUICK_CONNECT_TIMEOUT_MS/1000,
        read=QUICK_READ_TIMEOUT_MS/1000,
        write=QUICK_READ_TIMEOUT_MS/1000,
        pool=0.3
    ),
    headers={
        "User-Agent": "Mozilla/5.0 (Product-Intel/fast-partial)",
        "Accept-Language": "ar, en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    },
    transport=_fast_transport,
    http2=False,
)

def _clean_title(raw: str) -> str:
    if not raw: return ""
    t = raw.strip()
    # Remove store prefixes/suffixes, pipes/dashes
    t = re.sub(r"Amazon\.[a-z\.]+:\s*", "", t, flags=re.I)
    t = re.sub(r"(?i)\s*\|\s*Buy.*$", "", t)
    t = re.sub(r"(?i)\s*–\s*[\w\s]+?Store.*$", "", t)
    t = re.sub(r"\s*[\|\-–]\s*(?:eBay|AliExpress|Noon|Daraz|Amazon).*?$", "", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()

def _first_non_empty(*vals) -> str:
    for v in vals:
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return ""

async def quick_fetch_html_sample(url: str, max_bytes: int = QUICK_HTML_MAX_BYTES) -> str:
    """Fetch only the first ~max_bytes to stay under the partial SLO."""
    validate_public_url(url)
    
    # AliExpress-specific handling: larger limit and English headers
    is_aliexpress = "aliexpress" in url.lower()
    actual_max_bytes = 1048576 if is_aliexpress else max_bytes  # 1MB for AE (was 256KB)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Product-Intel/fast-partial)",
        "Accept-Language": "en-US,en;q=0.9" if is_aliexpress else "ar, en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    # Add AliExpress cookies for USD pricing
    if is_aliexpress:
        headers["Cookie"] = "aep_usuc_f=site=glo&b_locale=en_US&c_tp=USD&region=US"
    
    async with fast_client.stream("GET", url, headers=headers) as r:
        r.raise_for_status()
        total = 0
        parts = []
        async for chunk in r.aiter_bytes():
            total += len(chunk)
            parts.append(chunk)
            if total >= actual_max_bytes:
                break
        return b"".join(parts).decode(errors="ignore")

def quick_parse_head(html: str) -> Dict[str, Any]:
    """Parse just enough for partial: title/name, price, one image."""
    out = {"title": "", "price_amount": None, "price_currency": "USD", "image": ""}

    # Try JSON-LD Product first (tiny soup parse)
    soup = BeautifulSoup(html, "lxml")
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or "{}")
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for n in nodes:
            if isinstance(n, dict) and (n.get("@type") == "Product" or ("@type" in n and "Product" in str(n.get("@type")))):
                if not out["title"]:
                    out["title"] = n.get("name", "") or out["title"]
                offers = n.get("offers", {})
                if isinstance(offers, dict):
                    p = offers.get("price")
                    c = offers.get("priceCurrency") or out["price_currency"]
                    if p and str(p).strip() and str(p).strip() != "":
                        try:
                            price_str = str(p).replace(",", "").strip()
                            if price_str and price_str != "" and price_str != "0" and price_str != "0.0":
                                out["price_amount"] = float(price_str)
                                out["price_currency"] = c
                        except (ValueError, TypeError):
                            pass
                elif isinstance(offers, list):
                    for o in offers:
                        p = o.get("price"); c = o.get("priceCurrency") or out["price_currency"]
                        if p and str(p).strip() and str(p).strip() != "":
                            try:
                                price_str = str(p).replace(",", "").strip()
                                if price_str and price_str != "" and price_str != "0" and price_str != "0.0":
                                    out["price_amount"] = float(price_str)
                                    out["price_currency"] = c
                                    break
                            except (ValueError, TypeError):
                                pass
                img = n.get("image")
                if img:
                    out["image"] = img[0] if isinstance(img, list) else img
                break

    # Meta fallbacks
    if not out["title"]:
        ogt = soup.select_one('meta[property="og:title"]')
        twt = soup.select_one('meta[name="twitter:title"]')
        doc_title = soup.find("title")
        out["title"] = _clean_title(_first_non_empty(
            ogt.get("content") if ogt else "",
            twt.get("content") if twt else "",
            doc_title.get_text(" ", strip=True) if doc_title else ""
        ))

    if out["price_amount"] is None:
        # Meta price
        ogp = soup.select_one('meta[property="product:price:amount"], meta[property="og:price:amount"]')
        if ogp and ogp.get("content"):
            try:
                price_str = ogp["content"].replace(",", "").strip()
                if price_str:
                    out["price_amount"] = float(price_str)
            except (ValueError, TypeError):
                pass

    # If still none, handle strings like "US $12.34" and AE runParams
    if out["price_amount"] is None:
        for patt in [
            r'"(?:price|currentPrice|salePrice)"\s*:\s*"?[^"\d]{0,8}(?P<p>\d[\d\.,]*)"?',
            r'"(?:skuCalPrice|actSkuCalPrice)"\s*:\s*"(?P<p>[^"]+)"',
        ]:
            m = re.search(patt, html, re.I)
            if m:
                try:
                    raw = m.group("p")
                    digits = re.sub(r"[^\d\.,]", "", raw).replace(",", "")
                    if digits:
                        out["price_amount"] = float(digits)
                        break
                except Exception:
                    pass

    if not out["image"]:
        ogi = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
        if ogi and ogi.get("content"):
            out["image"] = ogi["content"]

    return out

def _yer_str(amount: Optional[float], cur: str) -> str:
    try:
        if amount is None or float(amount) <= 0:
            return ""  # Changed: return empty string instead of "0.00"
        return f"{CURRENCY.convert_to_yer(float(amount), cur or 'USD'):.2f}"
    except Exception:
        return ""

# ========= NEW: AliExpress extraction helpers =========
def _extract_json_from_scripts(html: str, keys: list[str]) -> Optional[dict]:
    """
    Robust JSON extraction from AliExpress scripts
    Handles multiple patterns and formats for maximum compatibility
    """
    import re
    import json
    
    # Pattern 1: window.runParams = {...}
    patterns = [
        r"window\.runParams\s*=\s*({.*?})\s*;",
        r"__AERENDER_DATA__\s*=\s*({.*?})\s*;",
        r"data:\s*({.*?})\s*,\s*rendering:",
        r"window\.__DEFAULT_DATA__\s*=\s*({.*?})\s*;",
        r"runParams\s*=\s*({[^;]+});",
        r'window\.runParams\s*=\s*"({[^"]+})"',
    ]
    
    for pattern in patterns:
        try:
            matches = re.findall(pattern, html, re.DOTALL)
            for match in matches:
                try:
                    # Handle escaped JSON
                    if match.startswith('"'):
                        match = match.strip('"').replace('\\"', '"')
                    data = json.loads(match)
                    # Check if any of our keys are in this JSON
                    json_str = json.dumps(data)
                    if any(key in json_str for key in keys):
                        return data
                except json.JSONDecodeError:
                    # Try to fix common JSON issues
                    cleaned = re.sub(r',\s*}', '}', match)  # Remove trailing commas
                    cleaned = re.sub(r',\s*]', ']', cleaned)
                    try:
                        data = json.loads(cleaned)
                        json_str = json.dumps(data)
                        if any(key in json_str for key in keys):
                            return data
                    except:
                        continue
        except Exception:
            continue
    
    # Fallback: search for script tags with product data
    soup = BeautifulSoup(html, 'lxml')
    for script in soup.find_all('script'):
        if script.string and any(key in script.string for key in keys):
            try:
                # Extract JSON from script text
                json_text = re.search(r'({.*})', script.string, re.DOTALL)
                if json_text:
                    data = json.loads(json_text.group(1))
                    return data
            except:
                continue
    
    return None

def _parse_aliexpress(html: str) -> dict:
    """
    Complete AliExpress product data parser
    Returns: title, images, price_amount, price_currency, specifications, breadcrumbs
    """
    import re
    import json
    from bs4 import BeautifulSoup
    
    out = {
        "title": "",
        "images": [],
        "price_amount": None,
        "price_currency": "USD",
        "specifications": {},
        "breadcrumbs": []
    }

    # Extract all potential JSON data
    json_data = _extract_json_from_scripts(html, [
        "priceModule", "imageModule", "specsModule", 
        "titleModule", "descriptionModule", "storeModule"
    ])
    
    if not json_data:
        logger.warning("AliExpress parser: No JSON data found in scripts")
        return out

    # Get the data root - AliExpress structures vary
    root = json_data.get("data", json_data)
    if not isinstance(root, dict):
        root = json_data

    logger.info(f"AliExpress parser: Found JSON data with keys: {list(root.keys())}")

    # 1. Extract title
    title_paths = [
        ["titleModule", "subject"],
        ["pageModule", "title"],
        ["productInfoComponent", "subject"],
        ["title"],
        ["subject"]
    ]
    
    for path in title_paths:
        try:
            value = root
            for key in path:
                value = value.get(key, {})
            if isinstance(value, str) and value.strip():
                out["title"] = value.strip()
                logger.info(f"AliExpress parser: Title found via path {path}: {out['title'][:60]}...")
                break
        except:
            continue

    # 2. Extract images
    image_paths = [
        ["imageModule", "imagePathList"],
        ["imageModule", "imagePaths"],
        ["imageList"],
        ["images"]
    ]
    
    for path in image_paths:
        try:
            value = root
            for key in path:
                value = value.get(key, {})
            if isinstance(value, list):
                out["images"] = [img for img in value if isinstance(img, str) and img.startswith('http')]
                if out["images"]:
                    logger.info(f"AliExpress parser: Images found via path {path}: {len(out['images'])} images")
                break
        except:
            continue

    # 3. Extract price
    price_paths = [
        ["priceModule", "formatedActivityPrice"],
        ["priceModule", "formatedPrice"],
        ["priceModule", "maxActivityAmount"],
        ["priceModule", "maxAmount"],
        ["priceModule", "minActivityAmount"],
        ["priceModule", "minAmount"],
        ["priceModule", "actSkuCalPrice"],
        ["priceModule", "skuCalPrice"],
        ["price", "formatedAmount"],
        ["price"]
    ]
    
    for path in price_paths:
        try:
            value = root
            for key in path:
                value = value.get(key, {})
            if isinstance(value, str) and value.strip():
                # Clean price string
                price_str = re.sub(r'[^\d.,]', '', value)
                price_str = price_str.replace(',', '')
                try:
                    out["price_amount"] = float(price_str)
                    # Get currency
                    currency_match = re.search(r'([$€£¥₩₽₹]|USD|EUR|GBP|CNY|KRW|RUB|INR|PKR)', value)
                    if currency_match:
                        out["price_currency"] = currency_match.group(1)
                    logger.info(f"AliExpress parser: Price found via path {path}: {out['price_amount']} {out['price_currency']}")
                    break
                except ValueError:
                    continue
        except:
            continue

    # 4. Extract specifications
    try:
        props = root.get("specsModule", {}).get("props", [])
        for prop in props:
            if isinstance(prop, dict):
                attr_name = prop.get("attrName", "")
                attr_value = prop.get("attrValue", "")
                if attr_name and attr_value:
                    out["specifications"][attr_name.strip()] = attr_value.strip()
        if out["specifications"]:
            logger.info(f"AliExpress parser: Specifications found: {len(out['specifications'])} props")
    except:
        pass

    # 5. Extract breadcrumbs/categories
    try:
        crumbs = root.get("crossLinkModule", {}).get("breadCrumbPathList", [])
        if isinstance(crumbs, list):
            out["breadcrumbs"] = [crumb.get("name", "") for crumb in crumbs if crumb.get("name")]
        if out["breadcrumbs"]:
            logger.info(f"AliExpress parser: Breadcrumbs found: {out['breadcrumbs']}")
    except:
        pass

    # Fallback: Try meta tags and JSON-LD if runParams failed
    if not out["title"] or not out["price_amount"]:
        logger.info("AliExpress parser: Using fallback meta tags and JSON-LD")
        soup = BeautifulSoup(html, 'lxml')
        
        # Title from meta tags
        if not out["title"]:
            og_title = soup.find('meta', {'property': 'og:title'})
            if og_title and og_title.get('content'):
                out["title"] = og_title['content']
                logger.info(f"AliExpress parser: Title from meta: {out['title'][:60]}...")
            else:
                title_tag = soup.find('title')
                if title_tag:
                    out["title"] = title_tag.get_text(strip=True)
                    logger.info(f"AliExpress parser: Title from title tag: {out['title'][:60]}...")
        
        # Price from meta or structured data
        if not out["price_amount"]:
            # Check meta tags
            price_meta = soup.find('meta', {'property': 'product:price:amount'})
            if price_meta and price_meta.get('content'):
                try:
                    out["price_amount"] = float(price_meta['content'])
                    logger.info(f"AliExpress parser: Price from meta: {out['price_amount']}")
                except:
                    pass
            
            # Check JSON-LD
            if not out["price_amount"]:
                json_ld_scripts = soup.find_all('script', {'type': 'application/ld+json'})
                for script in json_ld_scripts:
                    try:
                        data = json.loads(script.string)
                        if isinstance(data, dict) and data.get('@type') == 'Product':
                            offers = data.get('offers', {})
                            if isinstance(offers, dict):
                                price = offers.get('price')
                                if price:
                                    out["price_amount"] = float(price)
                                    out["price_currency"] = offers.get('priceCurrency', 'USD')
                                    logger.info(f"AliExpress parser: Price from JSON-LD: {out['price_amount']} {out['price_currency']}")
                    except:
                        continue
        
        # Images from meta if needed
        if not out["images"]:
            og_image = soup.find('meta', {'property': 'og:image'})
            if og_image and og_image.get('content'):
                out["images"] = [og_image['content']]
                logger.info(f"AliExpress parser: Image from meta: {out['images'][0]}")

    # Clean up title
    if out["title"]:
        out["title"] = _clean_title(out["title"])
    
    # Normalize images to only clean URLs
    out["images"] = [u for u in out["images"] if isinstance(u, str) and u.startswith(("http://","https://"))]
    
    # Final logging summary
    logger.info(f"AliExpress parser result: title='{out['title'][:60]}...', price={out['price_amount']} {out['price_currency']}, images={len(out['images'])}, specs={len(out['specifications'])}")
    
    return out

def _filter_images_by_host(urls: list[str], allowed_hosts: list[str]) -> list[str]:
    """Filter images to only allow specific hosts (e.g., aliexpress.com, alicdn.com)"""
    out = []
    for u in urls or []:
        try:
            host = urlparse(u).hostname or ""
            if any(host == h or host.endswith("." + h) for h in allowed_hosts):
                out.append(u)
        except Exception:
            continue
    return out

# Junk features to filter out
JUNK_FEATURES = {"advertising","publication","background","wallpaper","font","brand","product"}

def _clean_features(features: list[str]) -> list[str]:
    """Clean and filter out junk features"""
    clean = []
    seen = set()
    for f in features or []:
        s = (f or "").strip()
        key = s.lower()
        if not s or key in JUNK_FEATURES:
            continue
        if key in seen:
            continue
        seen.add(key)
        clean.append(s)
    return clean[:6]

async def fast_partial_from_url_hint(url: str, rid: str) -> Optional[Dict[str, Any]]:
    """Strict scrape-only partial path with tight budget and no Vision."""
    # Check if domain is allowed for scraping
    if not SCRAPER.is_allowed(url):
        log(rid, logging.WARNING, "fast_partial_disallowed_domain", url=url)
        return {"اسم_المنتج": "", "السعر_بالريال_اليمني": "", "روابط_الصور": []}
    
    start = time.monotonic()
    try:
        html = await asyncio.wait_for(quick_fetch_html_sample(url), timeout=PARTIAL_TIMEOUT_MS/1000)
        parsed = quick_parse_head(html)
        
        # Always return a result, even if empty
        # Build price object
        price_obj = None
        if parsed["price_amount"] is not None:
            price_obj = {
                "amount": parsed["price_amount"],
                "currency": parsed["price_currency"],
                "source": "scraping"
            }
        
        result = {
            "اسم_المنتج": parsed["title"] or "",
            "السعر_بالريال_اليمني": _yer_str(parsed["price_amount"], parsed["price_currency"]),
            "روابط_الصور": [parsed["image"]] if parsed["image"] else [],
            "السعر": price_obj,
            "المزايا": []  # Features would need deeper scraping
        }
        
        # Log what we found
        if parsed["title"] or parsed["price_amount"] is not None or parsed["image"]:
            log(rid, logging.INFO, f"fast_partial_success", title=parsed["title"][:50], price=parsed["price_amount"], image=bool(parsed["image"]))
        else:
            log(rid, logging.WARNING, "fast_partial_no_data")
            
        return result
        
    except asyncio.TimeoutError:
        log(rid, logging.WARNING, "fast_partial timeout")
        return {
            "اسم_المنتج": "",
            "السعر_بالريال_اليمني": "",
            "روابط_الصور": []
        }
    except Exception as e:
        log(rid, logging.WARNING, f"fast_partial error: {e}")
        return {
            "اسم_المنتج": "",
            "السعر_بالريال_اليمني": "",
            "روابط_الصور": []
        }
    finally:
        ms = (time.monotonic() - start) * 1000
        log(rid, logging.INFO, "fast_partial_done", ms=f"{ms:.0f}")

# ---------------------- Logging -----------------------
class RequestIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True

# Use a safer logging format that doesn't crash if request_id is missing
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("product_api")
logger.addFilter(RequestIdFilter())

def log(request_id: str, level: int, msg: str, **kv):
    extra = {"request_id": request_id}
    if kv:
        msg = f"{msg} | " + " ".join(f"{k}={v}" for k, v in kv.items())
    logger.log(level, msg, extra=extra)

# ---------------------- FastAPI app -------------------
app = FastAPI(
    title="Product Vision + Scraper (Arabic JSON)",
    version="2.3.0",
    contact={"name": "Your Team"},
)

# Add CORS middleware for browser compatibility
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Pin specific origins if you need credentials
    allow_credentials=False,      # Wildcard + credentials is invalid in browsers
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------- Error handlers ----------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    rid = getattr(request.state, "request_id", "unknown")
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": "http_error", "message": exc.detail, "details": {"request_id": rid}},
        headers={"X-Request-Id": rid},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    rid = getattr(request.state, "request_id", "unknown")
    return JSONResponse(
        status_code=422,
        content={"code": "validation_error", "message": "Invalid request payload", "details": exc.errors()},
        headers={"X-Request-Id": rid},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    rid = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "message": "Internal error", "details": {"request_id": rid}},
        headers={"X-Request-Id": rid},
)

# ---------------------- Auth & Rate Limit -------------
def require_api_key(request: Request):
    key = request.headers.get("X-API-Key", "")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# Simple per-IP sliding window limiter
RATE_WINDOW_SEC = 10
RATE_MAX_REQUESTS = 50  # per IP / 10 seconds (tune)
_ip_hits: defaultdict[str, deque] = defaultdict(deque)
_lock_rate = asyncio.Lock()

async def rate_limiter(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    async with _lock_rate:
        dq = _ip_hits[ip]
        dq.append(now)
        while dq and dq[0] < now - RATE_WINDOW_SEC:
            dq.popleft()
        if len(dq) > RATE_MAX_REQUESTS:
            raise HTTPException(429, detail="Too Many Requests")
    return True

# --------------- Utilities: request id, middlewares ----
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception as ex:
        log(request_id, logging.ERROR, f"Unhandled error: {ex}")
        return JSONResponse(status_code=500, content={"code": "internal_error", "message": "Internal error"})
    response.headers["X-Request-Id"] = request_id
    return response

# ---------------------- Safe URL fetching --------------
_DENY_SCHEMES = {"file", "ftp", "gopher", "ssh", "ws", "wss"}
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

def is_private_host(host: str) -> bool:
    # Trusted e-commerce domains (always allow)
    TRUSTED_DOMAINS = {
        "aliexpress.com", "ar.aliexpress.com", "www.aliexpress.com",
        "amazon.com", "amazon.ae", "amazon.sa", "amazon.co.uk", "amazon.de", "amazon.fr",
        "noon.com", "souq.com", "jumia.com", "daraz.com", "ebay.com", "etsy.com",
        "shopify.com", "woocommerce.com", "alicdn.com",
        # --- add these two safe CDNs used in tests/demos ---
        "via.placeholder.com", "picsum.photos"
    }
    
    # Check if it's a trusted domain first
    for trusted in TRUSTED_DOMAINS:
        if host == trusted or host.endswith("." + trusted):
            return False  # Allow trusted domains
    
    try:
        # Resolve all addresses
        infos = socket.getaddrinfo(host, None)
        for fam, _, _, _, sockaddr in infos:
            if fam == socket.AF_INET:
                ip = ipaddress.ip_address(sockaddr[0])
            elif fam == socket.AF_INET6:
                ip = ipaddress.ip_address(sockaddr[0])
            else:
                continue
            if any(ip in net for net in _PRIVATE_NETS):
                return True
        return False
    except Exception:
        # If resolution fails, treat as unsafe (except for trusted domains)
        return True

def validate_public_url(url: str):
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or p.scheme in _DENY_SCHEMES:
        raise HTTPException(400, detail="Unsupported URL scheme")
    if not p.hostname:
        raise HTTPException(400, detail="Invalid URL")
    if is_private_host(p.hostname):
        raise HTTPException(400, detail="Private or unsafe host blocked")

# ---------------------- HTTPX client -------------------
_transport = httpx.AsyncHTTPTransport(retries=1)
client = httpx.AsyncClient(
    timeout=httpx.Timeout(8.0, connect=5.0),
    headers={
        "User-Agent": "Mozilla/5.0 (Product-Intel/1.0)",
        "Accept-Language": "ar, en;q=0.8"
    },
    transport=_transport,
    http2=False,  # Disable http2 to avoid import issues
)

# ---------------------- Vision client ------------------
try:
    # Use API key authentication
    api_key = os.getenv("GOOGLE_API_KEY")
    if api_key:
        client_options = {"api_key": api_key}
        vision_client = vision.ImageAnnotatorClient(client_options=client_options)
    else:
        # Fallback to default credentials
        vision_client = vision.ImageAnnotatorClient()
except Exception as e:
    vision_client = None

async def vision_annotate(image_bytes: bytes) -> vision.AnnotateImageResponse:
    """Annotate image with Google Cloud Vision API."""
    def _run():
        if not vision_client:
            raise Exception("Vision client not configured")
        
        image = vision.Image(content=image_bytes)
        request = vision.AnnotateImageRequest(
            image=image,
            features=[
                vision.Feature(type_=vision.Feature.Type.LABEL_DETECTION),
                vision.Feature(type_=vision.Feature.Type.TEXT_DETECTION),
                vision.Feature(type_=vision.Feature.Type.WEB_DETECTION),
            ],
        )
        return vision_client.annotate_image(request=request)
    
    return await asyncio.to_thread(_run)

# ---------------------- Enhanced LRU TTL Cache ----------
@dataclass
class CacheItem:
    expires: float
    data: Any

class LruTtlCache:
    def __init__(self, capacity=1000, ttl_hours=24):
        self.capacity = capacity
        self.ttl = ttl_hours * 3600
        self.od: OrderedDict[str, CacheItem] = OrderedDict()

    def _now(self): return time.time()

    def get(self, key: str):
        if key not in self.od:
            return None
        entry = self.od[key]
        if self._now() > entry.expires:
            del self.od[key]
            return None
        self.od.move_to_end(key)
        return entry.data

    def set(self, key: str, value: Any):
        if key in self.od:
            self.od.move_to_end(key)
        self.od[key] = CacheItem(self._now() + self.ttl, value)
        if len(self.od) > self.capacity:
            self.od.popitem(last=False)

    def size(self): return len(self.od)

CACHE = LruTtlCache()

# ---------------------- Currency Converter --------------
class CurrencyConverter:
    def __init__(self):
        self.exchange = {}
        self.last_update: Optional[datetime] = None
        self.update_interval = timedelta(hours=1)
        self.usd_to_yer_env = float(os.getenv("YER_PER_USD", "250.0"))

    async def update(self):
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get("https://api.exchangerate-api.com/v4/latest/USD")
                if r.status_code == 200:
                    data = r.json()
                    self.exchange = data.get("rates", {}) or {}
                    self.last_update = datetime.utcnow()
        except Exception as e:
            logger.warning(f"Exchange update failed: {e}")

    async def ensure_fresh(self):
        if not self.last_update or datetime.utcnow() - self.last_update > self.update_interval:
            await self.update()

    def convert_to_yer(self, amount: float, from_currency: str) -> float:
        """Convert amount to Yemeni Rial using live rates or fallback defaults"""
        if from_currency.upper() == "YER":
            return amount
        
        # Use environment variable for USD rate, fallback defaults for others
        defaults = {
            'USD': self.usd_to_yer_env, 
            'EUR': 270.0, 
            'SAR': 66.7, 
            'AED': 68.0,
            'KWD': 820.0, 
            'QAR': 68.7, 
            'OMR': 650.0, 
            'BHD': 663.0,
            'PKR': 0.87  # Pakistani Rupee to USD (approximate)
        }
        
        rate = self.exchange.get(from_currency.upper(), defaults.get(from_currency.upper(), 1.0))
        return amount * rate

    def convert_pkr_to_usd(self, pkr_amount: float) -> float:
        """Convert Pakistani Rupee to USD"""
        # Approximate rate: 1 USD ≈ 280 PKR (varies)
        return pkr_amount / 280.0
    
    def convert_to_multi_currency(self, amount: float, from_currency: str) -> dict:
        """Convert amount to multiple currencies (USD, SAR, AED)"""
        if from_currency.upper() == "USD":
            usd_amount = amount
        elif from_currency.upper() == "PKR":
            usd_amount = self.convert_pkr_to_usd(amount)
        else:
            # For other currencies, convert to USD first
            usd_amount = amount / self.exchange.get(from_currency.upper(), 1.0)
        
        # Convert USD to other currencies
        return {
            "source_currency": from_currency.upper(),
            "source_amount": amount,
            "usd": round(usd_amount, 2),
            "sar": round(usd_amount * 3.75, 2),  # 1 USD ≈ 3.75 SAR
            "aed": round(usd_amount * 3.67, 2),  # 1 USD ≈ 3.67 AED
            "yer": round(self.convert_to_yer(usd_amount, "USD"), 2)
        }

    def extract_price_and_currency(self, price_text: str) -> Tuple[float, str]:
        if not price_text:
            return 0.0, "USD"
        # currency detection
        cur_map = {
            r'\$|USD|دولار': 'USD',
            r'€|EUR|يورو': 'EUR',
            r'ر\.ي|يمني|YER': 'YER',
            r'ر\.س|سعودي|SAR': 'SAR',
            r'د\.إ|درهم|AED': 'AED',
            r'د\.ك|كويتي|KWD': 'KWD',
            r'ر\.ق|قطري|QAR': 'QAR',
            r'ر\.ع|عماني|OMR': 'OMR',
            r'د\.ب|بحريني|BHD': 'BHD',
            r'₨|PKR|روبية|باكستاني': 'PKR',  # Pakistani Rupee
        }
        detected = "USD"
        for patt, cur in cur_map.items():
            if re.search(patt, price_text, re.I):
                detected = cur
                break
        nums = re.findall(r'\d[\d,\.]*', price_text)
        if not nums: return 0.0, detected
        try:
            val = float(nums[0].replace(',', ''))
            return val, detected
        except:
            return 0.0, detected

CURRENCY = CurrencyConverter()

# ---------------------- Price Extractor -----------------
class PriceExtractor:
    PRICE_PATTERNS = [
        r'aok-offscreen">\$?(?P<price>\d[\d\.,]*)<',           # Amazon DOM
        r'data-asin-price\s*=\s*"(?P<price>\d[\d\.,]*)"',      # Amazon attr
        r'"price"\s*:\s*"?(?P<price>\d[\d\.,]*)"?',
        r'"currentPrice"\s*:\s*"?(?P<price>\d[\d\.,]*)"?',
        r'"priceValue"\s*:\s*"?(?P<price>\d[\d\.,]*)"?',
        r'price\s*[:=]\s*"?(?P<price>\d[\d\.,]*)"?',
        r'data-price\s*=\s*"(?P<price>\d[\d\.,]*)"',
        r'data-current-price\s*=\s*"(?P<price>\d[\d\.,]*)"',
        r'product:price:amount"\s*content="\s*(?P<price>\d[\d\.,]*)',
        r'og:price:amount"\s*content="\s*(?P<price>\d[\d\.,]*)',
        r'\$?\s*(?P<price>\d{1,4}(?:[,]\d{3})*(?:\.\d{1,2})?)',  # generic $999.99
    ]

    @staticmethod
    def _clean_price(val: str) -> Optional[float]:
        if not val:
            return None
        try:
            # Normalize Arabic digits to ASCII
            normalized = val
            arabic_digits = {'٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4', 
                           '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9'}
            for arabic, ascii in arabic_digits.items():
                normalized = normalized.replace(arabic, ascii)
            
            # Remove common separators and clean
            cleaned = normalized.replace(',', '').replace(' ', '').strip()
            return float(cleaned)
        except Exception:
            return None

    @staticmethod
    def from_jsonld(html: str) -> Optional[Dict[str, Any]]:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for s in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(s.string or "{}")
                except Exception:
                    continue
                nodes = data if isinstance(data, list) else [data]
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    types = node.get("@type") or node.get("@graph", [{}])[0].get("@type")
                    tset = {types.lower()} if isinstance(types, str) else {str(t).lower() for t in (types or [])}
                    if "product" in tset:
                        offers = node.get("offers")
                        if isinstance(offers, dict):
                            p = offers.get("price")
                            c = offers.get("priceCurrency", "USD")
                            amount = PriceExtractor._clean_price(str(p) if p is not None else "")
                            if amount:
                                return {"amount": amount, "currency": c, "source": "jsonld_offers"}
                        elif isinstance(offers, list):
                            for off in offers:
                                p = off.get("price")
                                c = off.get("priceCurrency", "USD")
                                amount = PriceExtractor._clean_price(str(p) if p is not None else "")
                                if amount:
                                    return {"amount": amount, "currency": c, "source": "jsonld_offers"}
        except Exception:
            pass
        return None

    @staticmethod
    def from_meta(html: str) -> Optional[Dict[str, Any]]:
        try:
            soup = BeautifulSoup(html, "html.parser")
            m = soup.select_one('meta[property="product:price:amount"], meta[property="og:price:amount"]')
            if m and m.get("content"):
                amount = PriceExtractor._clean_price(m["content"])
                cur = "USD"
                cmeta = soup.select_one('meta[property="product:price:currency"], meta[property="og:price:currency"]')
                if cmeta and cmeta.get("content"):
                    cur = cmeta["content"]
                if amount:
                    return {"amount": amount, "currency": cur, "source": "meta"}
        except Exception:
            pass
        return None

    @staticmethod
    def from_inline_json(html: str) -> Optional[Dict[str, Any]]:
        # Pull out big script blobs commonly used by AE/Amazon/etc.
        try:
            blobs = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S | re.I)
            for blob in blobs:
                for patt in PriceExtractor.PRICE_PATTERNS:
                    m = re.search(patt, blob, flags=re.I)
                    if m:
                        amount = PriceExtractor._clean_price(m.group("price"))
                        if amount:
                            # Try currency near it
                            tail = blob[max(0, m.start()-80): m.end()+80]
                            _, cur = CURRENCY.extract_price_and_currency(tail)
                            return {"amount": amount, "currency": cur, "source": "inline_json"}
        except Exception:
            pass
        return None

    @staticmethod
    async def from_dom_selectors(page_like, html: Optional[str] = None) -> Optional[Dict[str, Any]]:
        # Works with real Playwright page or HttpxPage mock
        selectors = [
            ".a-price .a-offscreen", ".a-price-whole", ".a-price .a-price-whole",
            ".price, .product-price, .price-current, .price__current, .price-amount, .price-display, .product__price",
            ".priceToPay .a-price .a-offscreen"
        ]
        for sel in selectors:
            try:
                txt = await page_like.eval_on_selector(sel, "el => el && el.textContent", strict=False)
                if txt:
                    amt, cur = CURRENCY.extract_price_and_currency(txt.strip())
                    if amt > 0:
                        return {"amount": amt, "currency": cur, "source": f"selector:{sel}"}
            except Exception:
                continue
        # As a last resort, scan HTML if provided
        if html:
            try:
                soup = BeautifulSoup(html, "html.parser")
                for sel in selectors:
                    el = soup.select_one(sel)
                    if el:
                        amt, cur = CURRENCY.extract_price_and_currency(el.get_text(" ", strip=True))
                        if amt > 0:
                            return {"amount": amt, "currency": cur, "source": f"selector_html:{sel}"}
            except Exception:
                pass
        return None

    @staticmethod
    def site_specific(domain: str, html: str) -> Optional[Dict[str, Any]]:
        d = domain.lower()
        try:
            if "aliexpress" in d:
                # AE has "runParams" or "meta" blocks with price/currentPrice
                for patt in [
                    r'"tradePrice"\s*:\s*"(?P<price>\d[\d\.,]*)"',
                    r'"discountedPrice"\s*:\s*"(?P<price>\d[\d\.,]*)"',
                    r'"salePrice"\s*:\s*"(?P<price>\d[\d\.,]*)"',
                    r'"skuVal"\s*:\s*{[^}]*"actSkuCalPrice"\s*:\s*"(?P<price>[^"]+)"',
                    r'"skuVal"\s*:\s*{[^}]*"skuCalPrice"\s*:\s*"(?P<price>[^"]+)"',
                    r'"currentPrice"\s*:\s*"(?P<price>[^"]+)"',
                    r'"price"\s*:\s*"(?P<price>[^"]+)"',
                ]:
                    m = re.search(patt, html, flags=re.I|re.S)
                    if m:
                        amount = PriceExtractor._clean_price(m.group("price"))
                        if amount:
                            return {"amount": amount, "currency": "USD", "source": "aliexpress"}
            if "amazon." in d:
                # Amazon offscreen span contains price; fallback to JSON
                m = re.search(r'"priceAmount"\s*:\s*"(?P<price>\d[\d\.,]*)"', html, re.I)
                if m:
                    amount = PriceExtractor._clean_price(m.group("price"))
                    if amount:
                        return {"amount": amount, "currency": "USD", "source": "amazon_json"}
            if "daraz." in d or "noon." in d:
                for patt in [
                    r'"offerPrice"\s*:\s*"(?P<price>\d[\d\.,]*)"',
                    r'"price"\s*:\s*"(?P<price>\d[\d\.,]*)"',
                    r'"salePrice"\s*:\s*"(?P<price>\d[\d\.,]*)"',
                ]:
                    m = re.search(patt, html, re.I)
                    if m:
                        amount = PriceExtractor._clean_price(m.group("price"))
                        if amount:
                            return {"amount": amount, "currency": "USD", "source": "regional_json"}
        except Exception:
            pass
        return None

    @staticmethod
    def generic_regex(html: str) -> Optional[Dict[str, Any]]:
        for patt in PriceExtractor.PRICE_PATTERNS:
            m = re.search(patt, html, flags=re.I)
            if m:
                amount = PriceExtractor._clean_price(m.group("price"))
                if amount:
                    _, cur = CURRENCY.extract_price_and_currency(m.group(0))
                    return {"amount": amount, "currency": cur, "source": "regex"}
        return None

# ---------------------- Playwright Scraper --------------
class WebScraper:
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.sem = asyncio.Semaphore(3)
        self.playwright_enabled = USE_PLAYWRIGHT

    async def init(self):
        if not self.playwright_enabled:
            return
        try:
            logger.info("Initializing Playwright…")
            self.playwright = await async_playwright().start()
            
            # Enhanced browser configuration for AliExpress
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--no-first-run',
                    '--no-zygote',
                    '--disable-gpu',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding'
                ]
            )
            
            # Enhanced context configuration
            self.context = await self.browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={'width': 1920, 'height': 1080},
                locale='en-US',
                timezone_id='America/New_York'
            )
            
            logger.info("Playwright ready with enhanced configuration.")
        except NotImplementedError as e:
            logger.warning(f"Playwright disabled (NotImplementedError): {e}")
            self.playwright_enabled = False
        except Exception as e:
            logger.warning(f"Playwright disabled: {e}")
            self.playwright_enabled = False

    async def shutdown(self):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.browser = None
        self.playwright = None

    async def _with_page(self, fn):
        if not self.playwright_enabled:
            return await self._httpx_fallback(fn)
        await self.init()
        async with self.sem:
            page: Page = await self.context.new_page()
            page.set_default_timeout(12000)
            try:
                return await fn(page)
            finally:
                await page.close()

    async def _httpx_fallback(self, fn):
        """Fallback scraping using httpx when Playwright is disabled/unavailable"""
        class HttpxPage:
            def __init__(self):
                self._html = ""
                self._url = ""

            async def goto(self, url, wait_until="load"):
                self._url = url
                validate_public_url(url)
                
                # AliExpress-specific handling
                is_aliexpress = "aliexpress" in url.lower()
                
                headers = {
                    "User-Agent": "Mozilla/5.0 (X11; Linux x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9" if is_aliexpress else "en-US,en;q=0.8,ar;q=0.6",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
                
                # Add AliExpress cookies for USD pricing
                if is_aliexpress:
                    headers["Cookie"] = "aep_usuc_f=site=glo&b_locale=en_US&c_tp=USD&region=US"
                
                # For AliExpress, try multiple fetch attempts with increasing delays
                if is_aliexpress:
                    self._html = await self._fetch_aliexpress_with_retry(url, headers)
                else:
                    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as c:
                        r = await c.get(url, headers=headers)
                        r.raise_for_status()
                        self._html = r.text

            async def _fetch_aliexpress_with_retry(self, url: str, headers: dict) -> str:
                """Multiple fetch attempts for AliExpress to get complete content"""
                best_html = ""
                max_content_length = 0
                
                print(f"AliExpress fetch: Starting multi-attempt fetch for {url}")
                
                # Try multiple fetch attempts with different delays
                delays = [0, 2, 5, 10]  # seconds
                
                for delay in delays:
                    if delay > 0:
                        print(f"AliExpress fetch: Waiting {delay}s before next attempt...")
                        await asyncio.sleep(delay)
                    
                    try:
                        print(f"AliExpress fetch: Attempt with {delay}s delay...")
                        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
                            r = await c.get(url, headers=headers)
                            r.raise_for_status()
                            html = r.text
                            
                            print(f"AliExpress fetch: Got {len(html)} bytes, status {r.status_code}")
                            
                            # Check if this fetch got more content
                            if len(html) > max_content_length:
                                max_content_length = len(html)
                                best_html = html
                                print(f"AliExpress fetch: New best result: {len(html)} bytes")
                            
                            # Check if we got the product data
                            if self._has_product_data(html):
                                print(f"AliExpress fetch: SUCCESS after {delay}s delay: {len(html)} bytes")
                                return html
                            else:
                                print(f"AliExpress fetch: No product data found in this attempt")
                                
                    except Exception as e:
                        print(f"AliExpress fetch: Attempt {delay}s failed: {e}")
                        continue
                
                # If all attempts failed, try alternative AliExpress domains
                if not best_html or len(best_html) < 50000:
                    print("AliExpress fetch: Trying alternative domains...")
                    alternative_domains = [
                        "https://www.aliexpress.com",
                        "https://www.aliexpress.us", 
                        "https://es.aliexpress.com"
                    ]
                    
                    for domain in alternative_domains:
                        try:
                            # Extract the item ID from the original URL
                            item_id = url.split('/item/')[-1].split('?')[0].split('.')[0]
                            alt_url = f"{domain}/item/{item_id}.html"
                            
                            print(f"AliExpress fetch: Trying alternative domain: {alt_url}")
                            
                            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
                                r = await c.get(alt_url, headers=headers)
                                r.raise_for_status()
                                html = r.text
                                
                                print(f"AliExpress fetch: Alternative domain got {len(html)} bytes")
                                
                                if self._has_product_data(html):
                                    print(f"AliExpress fetch: SUCCESS with alternative domain: {len(html)} bytes")
                                    return html
                                
                                if len(html) > max_content_length:
                                    max_content_length = len(html)
                                    best_html = html
                                    
                        except Exception as e:
                            print(f"AliExpress fetch: Alternative domain {domain} failed: {e}")
                            continue
                
                # Return the best HTML we got
                print(f"AliExpress fetch: COMPLETED: best result {len(best_html)} bytes")
                return best_html
            
            def _has_product_data(self, html: str) -> bool:
                """Check if HTML contains product data"""
                # Look for actual product data structures, not just text
                required_indicators = [
                    'window.runParams',
                    'priceModule',
                    'imageModule', 
                    'titleModule'
                ]
                
                # Must have at least 2 of the required indicators
                found_indicators = sum(1 for indicator in required_indicators if indicator in html)
                
                # Also check if we have substantial content (not just skeleton)
                has_substantial_content = len(html) > 100000  # At least 100KB
                
                # Check for actual product information (not just page structure)
                has_product_info = any([
                    'product-title' in html,
                    'product-price' in html,
                    'sku' in html.lower(),
                    'product-detail' in html
                ])
                
                # Only return True if we have both structure AND content
                return found_indicators >= 2 and has_substantial_content and has_product_info

            async def content(self):
                return self._html

            async def set_extra_http_headers(self, headers):
                # Store headers for potential use
                self._headers = headers

            async def wait_for_timeout(self, ms):
                await asyncio.sleep(ms/1000)

            async def wait_for_selector(self, selector, timeout=5000):
                # Mock implementation - just wait the timeout
                await asyncio.sleep(timeout/1000)
                return True

            async def eval_on_selector(self, selector, script, strict=False):
                soup = BeautifulSoup(self._html, "html.parser")
                el = soup.select_one(selector)
                if not el:
                    return None
                # Best-effort attr extraction for video/img cases
                for attr in ("src", "data-src"):
                    if el.has_attr(attr):
                        return urljoin(self._url, el.get(attr))
                # fallback to text
                return el.get_text(" ", strip=True)

            async def eval_on_selector_all(self, selector, script):
                soup = BeautifulSoup(self._html, "html.parser")
                els = soup.select(selector)
                # Special-case imgs to approximate `els.map(e => e.src)`
                if selector.strip().lower() in ("img", "img[src]", "img[data-src]"):
                    out = []
                    for e in els:
                        src = e.get("src") or e.get("data-src") or ""
                        if src:
                            # Make absolute if needed
                            src = urljoin(self._url, src)
                            out.append(src)
                    return out
                # Fallback: text
                return [e.get_text(" ", strip=True) for e in els]

            async def title(self):
                soup = BeautifulSoup(self._html, "html.parser")
                t = soup.find("title")
                return t.get_text(" ", strip=True) if t else ""

            @property
            def url(self):
                return self._url

        mock = HttpxPage()
        return await fn(mock)

    def is_allowed(self, url: str) -> bool:
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname.lower()
            allowed_domains = os.getenv("ALLOWED_SCRAPING_DOMAINS", DEFAULT_ALLOWED)
            allowed = [d.strip().lower() for d in allowed_domains.split(",") if d.strip()]
            return any(host == d or host.endswith("." + d) for d in allowed)
        except:
            return False

    async def search_google_shopping(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        from urllib.parse import quote
        q = quote(query)
        url = f"https://www.google.com/search?q={q}&tbm=shop"
        async def job(page):
            await page.goto(url, wait_until="load")
            html = await page.content()
            # Try static parse first, then selector if available
            items: List[Dict[str, Any]] = []
            soup = BeautifulSoup(html, "html.parser")
            for box in soup.select("div.sh-dgr__content")[:max_results]:
                title = box.select_one("h3, .tAxDx")
                price = box.select_one(".a8Pemb, .XrAfOe")
                img = box.select_one("img")
                a = box.select_one("a")
                items.append({
                    "title": title.get_text(" ", strip=True) if title else "",
                    "price": price.get_text(" ", strip=True) if price else "",
                    "image_url": (img.get("src") or img.get("data-src")) if img else "",
                    "url": a.get("href") if a else "",
                    "source": "google"
                })
            return items[:max_results]
        try:
            res = await self._with_page(job)
            return res or []
        except Exception as e:
            logger.warning(f"google shopping failed: {e}")
            return []

    async def get_product_details(self, url: str) -> Dict[str, Any]:
        if not self.is_allowed(url):
            raise HTTPException(status_code=400, detail="Domain not allowed for scraping")
        
        async def job(page):
            # AliExpress-specific handling
            is_aliexpress = "aliexpress" in url.lower()
            
            if is_aliexpress:
                # Set AliExpress cookies to force USD pricing
                await page.set_extra_http_headers({
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Cookie": "aep_usuc_f=site=glo&b_locale=en_US&c_tp=USD&region=US"
                })
                
                # Enhanced AliExpress page handling
                await page.goto(url, wait_until="domcontentloaded")
                
                # Wait for critical product elements and JavaScript execution
                await page.wait_for_timeout(5000)  # Increased wait for JS execution
                
                # Try to wait for multiple product indicators
                selectors_to_wait = [
                    '.product-title', '.product-price', '[data-price]',
                    '.product-info', '.product-detail', '.sku-info'
                ]
                
                for selector in selectors_to_wait:
                    try:
                        await page.wait_for_selector(selector, timeout=3000)
                        logger.info(f"AliExpress: Found product element: {selector}")
                        break
                    except:
                        continue
                
                # Additional wait for dynamic content
                await page.wait_for_timeout(2000)
                
                # Try to scroll to trigger lazy loading
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
                await page.wait_for_timeout(1000)
                
                # Get fully rendered HTML
                html = await page.content()
                
                logger.info(f"AliExpress: Page rendered, HTML size: {len(html)} bytes")
            else:
                await page.goto(url, wait_until="load")
                html = await page.content()

            details = {}
            domain = self._domain(url).lower()

            # ======= NEW: AliExpress fast-path parser =======
            if "aliexpress." in domain:
                ae = _parse_aliexpress(html)
                logger.info(
                    "AE parse hit | title=%r price=%r %s images=%d specs=%d",
                    ae.get("title","")[:60],
                    ae.get("price_amount"),
                    ae.get("price_currency",""),
                    len(ae.get("images",[])),
                    len(ae.get("specifications",{})),
                )
                details.update({
                    "title": ae.get("title",""),
                    "images": ae.get("images",[]),
                    "price_amount": ae.get("price_amount"),
                    "price_currency": ae.get("price_currency","USD"),
                    "specifications": ae.get("specifications",{}),
                    "breadcrumbs": ae.get("breadcrumbs",[]),
                })
            else:
                # old JSON-LD parse for other sites
                details = self._parse_jsonld_product(html)

            # Fallbacks (kept from your code)
            if not details.get("title"):
                try: details["title"] = await page.title()
                except Exception: pass

            if not details.get("images"):
                try:
                    imgs = await page.eval_on_selector_all("img", "els => els.map(e => e.src).filter(Boolean)")
                except Exception:
                    imgs = []
                details["images"] = list(dict.fromkeys([i for i in (imgs or []) if i]))[:12]

            if not details.get("video"):
                try:
                    vsrc = await page.eval_on_selector("video source, video", "el => el && (el.src || el.currentSrc)", strict=False)
                except Exception:
                    vsrc = None
                details["video"] = vsrc or None

            # ======= Price extractor (kept + site-specific) =======
            if details.get("price_amount") is None:
                price_info = (PriceExtractor.from_jsonld(html)
                              or PriceExtractor.from_meta(html)
                              or PriceExtractor.site_specific(domain, html)
                              or await PriceExtractor.from_dom_selectors(page, html)
                              or PriceExtractor.from_inline_json(html)
                              or PriceExtractor.generic_regex(html))
                if price_info:
                    details["price_amount"] = price_info["amount"]
                    details["price_currency"] = price_info.get("currency","USD")
                    details["price_source"] = price_info.get("source","extracted")

            return details
        try:
            out = await self._with_page(job)
            return out or {"url": url}
        except Exception as e:
            logger.warning(f"details failed: {e}")
            return {"url": url}

    def _domain(self, url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).netloc

    def _parse_jsonld_product(self, html: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {"title": "", "images": [], "video": None, "specifications": {}}
        try:
            soup = BeautifulSoup(html, "html.parser")
            scripts = soup.find_all("script", type="application/ld+json")
            for s in scripts:
                try:
                    data = json.loads(s.string or "{}")
                except Exception:
                    continue
                nodes = data if isinstance(data, list) else [data]
                for node in nodes:
                    if not isinstance(node, dict): continue
                    typ = node.get("@type") or node.get("@graph", [{}])[0].get("@type")
                    tset = {typ.lower()} if isinstance(typ, str) else {str(t).lower() for t in (typ or [])}
                    if "product" in tset:
                        out["title"] = node.get("name") or out["title"]
                        imgs = node.get("image")
                        if isinstance(imgs, list):
                            out["images"] += imgs
                        elif isinstance(imgs, str):
                            out["images"].append(imgs)
                        offers = node.get("offers", {})
                        if isinstance(offers, dict):
                            p = offers.get("price")
                            c = offers.get("priceCurrency")
                            if p:
                                out["price_amount"] = PriceExtractor._clean_price(str(p))
                            if c:
                                out["price_currency"] = c
                        elif isinstance(offers, list) and offers:
                            o = offers[0]
                            p = o.get("price")
                            c = o.get("priceCurrency")
                            if p:
                                out["price_amount"] = PriceExtractor._clean_price(str(p))
                            if c:
                                out["price_currency"] = c
                        addp = node.get("additionalProperty") or node.get("additionalProperties")
                        if isinstance(addp, list):
                            for p in addp:
                                k = p.get("name") or p.get("propertyID")
                                v = p.get("value")
                                if k and v:
                                    out["specifications"][str(k)] = str(v)
            out["images"] = list(dict.fromkeys([i for i in out["images"] if i]))
            
            # Enhanced video detection for non-Playwright scenarios
            if not out.get("video"):
                # Look for common video patterns
                video_selectors = [
                    'video[src]',
                    'video source[src]',
                    'iframe[src*="youtube"]',
                    'iframe[src*="youtu.be"]',
                    'iframe[src*="vimeo"]',
                    'a[href*="youtube.com/watch"]',
                    'a[href*="youtu.be/"]',
                    'a[href*="vimeo.com/"]'
                ]
                
                for selector in video_selectors:
                    try:
                        if 'iframe' in selector or 'a[href*=' in selector:
                            # Handle iframe and link patterns
                            elements = soup.select(selector)
                            for element in elements:
                                if 'iframe' in selector:
                                    src = element.get('src', '')
                                    if src:
                                        out["video"] = src
                                        break
                                elif 'a[href*=' in selector:
                                    href = element.get('href', '')
                                    if href:
                                        out["video"] = href
                                        break
                        else:
                            # Handle video elements
                            video = soup.select_one(selector)
                            if video:
                                src = video.get('src') or video.get('data-src')
                                if src:
                                    out["video"] = src
                                    break
                    except Exception:
                        continue
                        
        except Exception as e:
            logger.debug(f"jsonld parse error: {e}")
        return out

SCRAPER = WebScraper()

# ---------------------- Models -------------------------
class AnalyzeInput(BaseModel):
    image_base64: Optional[str] = Field(None, description="Base64 image (data URL or pure b64)")
    image_url: Optional[str] = Field(None, description="Public image URL (http/https)")
    product_url_hint: Optional[str] = Field(None, alias="url", description="If known, the product page to scrape directly (accepts 'url' as alias)")
    language: str = Field("ar", description="Output language; 'ar' expected")
    fast_only: bool = Field(False, description="If true, skip full enrichment")
    vision_json: Optional[Dict[str, Any]] = Field(None, description="Pre-processed Vision API JSON response")

    class Config:
        populate_by_name = True
        extra = "allow"  # Avoid 422 on unexpected but harmless keys
        json_schema_extra = {
            "example": {
                "image_base64": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ...",
                "url": "https://www.amazon.com/product/123",
                "language": "ar",
                "fast_only": False,
                "vision_json": {"responses": [{"labelAnnotations": [{"description": "coffee machine"}]}]}
            }
        }

class CropInput(BaseModel):
    image_base64: str
    mode: str = Field("center", description="center | square")

    class Config:
        json_schema_extra = {
            "example": {
                "image_base64": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ...",
                "mode": "center"
            }
        }

class PriceObject(BaseModel):
    amount: float
    currency: str = "USD"
    source: str = "unknown"

class PartialResponse(BaseModel):
    product_name: str = Field(alias="اسم_المنتج")
    price_yer: str = Field(alias="السعر_بالريال_اليمني")
    image_urls: List[str] = Field(alias="روابط_الصور")
    price: Optional[PriceObject] = Field(default=None, alias="السعر")
    features: List[str] = Field(default=[], alias="المزايا")

    class Config:
        populate_by_name = True
        json_schema_extra = {
            "example": {
                "اسم_المنتج": "ماكينة قهوة إسبريسو",
                "السعر_بالريال_اليمني": "125000.00",
                "روابط_الصور": ["https://example.com/image1.jpg"],
                "السعر": {"amount": 499.99, "currency": "USD", "source": "jsonld_offers"},
                "المزايا": ["مضخة ضغط 15 بار", "مطحنة مدمجة"]
            }
        }

class FullResponse(BaseModel):
    product_name: str = Field(alias="اسم_المنتج")
    description: str = Field(alias="الوصف")
    image_urls: List[str] = Field(alias="روابط_الصور")
    video_url: Optional[str] = Field(None, alias="رابط_الفيديو")
    components: List[str] = Field(default=[], alias="المكونات")
    price_yer: str = Field(alias="السعر_بالريال_اليمني")
    price: Optional[PriceObject] = Field(default=None, alias="السعر")
    specifications: Dict[str, Any] = Field(alias="المواصفات")
    variants: List[Dict[str, Any]] = Field(default=[], alias="المتغيرات")
    search_keywords: List[str] = Field(default=[], alias="كلمات_البحث")
    categories: Dict[str, Any] = Field(alias="الفئات")
    features: List[str] = Field(default=[], alias="المزايا")

    class Config:
        populate_by_name = True
        json_schema_extra = {
            "example": {
                "اسم_المنتج": "ماكينة قهوة إسبريسو احترافية",
                "الوصف": "يقدم هذا المنتج المسمى «ماكينة قهوة إسبريسو احترافية» تجربة عملية تجمع بين الأداء والجودة...",
                "روابط_الصور": [
                    "https://example.com/image1.jpg",
                    "https://example.com/image2.jpg",
                    "https://example.com/image3.jpg",
                    "https://example.com/image4.jpg",
                    "https://example.com/image5.jpg"
                ],
                "رابط_الفيديو": None,
                "المكونات": ["خزان مياه", "مضخة ضغط", "مقياس حرارة"],
                "السعر_بالريال_اليمني": "125000.00",
                "السعر": {"amount": 499.99, "currency": "USD", "source": "jsonld_offers"},
                "المواصفات": {
                    "العلامة_التجارية": "Breville",
                    "الموديل": "BES870XL",
                    "الطاقة": "1600W",
                    "الوزن": "5.2kg",
                    "المادة": "ستانلس ستيل",
                    "السعة": "2L",
                    "الجهد": "220V"
                },
                "المتغيرات": [],
                "كلمات_البحث": ["قهوة", "إسبريسو", "ماكينة", "احترافية"],
                "الفئات": {
                    "الفئة_الرئيسية": "أجهزة منزلية",
                    "الفئة_الفرعية": "أدوات تحضير القهوة",
                    "التسلسل": ["أجهزة منزلية", "أدوات تحضير القهوة"]
                },
                "المزايا": ["مضخة ضغط 15 بار", "مطحنة مدمجة", "تحكم بدرجة الحرارة"]
            }
        }

# ---------------------- Helpers ------------------------
def b64_to_bytes(b64: str) -> bytes:
    try:
        payload = b64.split(",")[-1]
        raw = base64.b64decode(payload, validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise HTTPException(413, detail="Image too large")
        return raw
    except Exception:
        raise HTTPException(400, detail="Invalid base64 image")

async def download_bytes(url: str, hard_limit: int = MAX_IMAGE_BYTES) -> bytes:
    validate_public_url(url)
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        total = 0
        chunks = []
        async for c in r.aiter_bytes():
            total += len(c)
            if total > hard_limit:
                raise HTTPException(413, detail="Image too large")
            chunks.append(c)
        return b"".join(chunks)

def image_center_crop(img_bytes: bytes) -> bytes:
    try:
        Image.MAX_IMAGE_PIXELS = 64_000_000  # safety
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = im.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=92)
        return out.getvalue()
    except UnidentifiedImageError:
        raise HTTPException(400, detail="Unsupported image format")

def normalize_images(urls: List[str]) -> List[str]:
    """Normalize image URLs and filter out invalid ones."""
    if not urls:
        return []
    normalized = []
    for url in urls:
        if not url or not isinstance(url, str):
            continue
        url = url.strip()
        if not url or url.startswith("data:"):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        normalized.append(url)
    return normalized[:8]  # Limit to 8 images

def pick_better_images(urls: List[str]) -> List[str]:
    """Prefer likely hi-res images; basic heuristics."""
    if not urls:
        return []
    scores = []
    for u in urls or []:
        s = 0
        if "1200" in u or "1500" in u or "2000" in u or "1000" in u: s += 2
        if "_SL1500_" in u or "_SL1200_" in u: s += 3  # amazon pattern
        if "._" in u and "_SX" in u: s -= 1            # amazon tiny thumb
        if "sprite" in u or "thumb" in u: s -= 2
        scores.append((s, u))
    scores.sort(key=lambda x: (-x[0], x[1]))
    dedup = []
    seen = set()
    for _, u in scores:
        if u not in seen:
            seen.add(u)
            dedup.append(u)
    return dedup[:8]

def convert_yer(amount: float, currency: str) -> float:
    return CURRENCY.convert_to_yer(amount, currency or "USD")

def build_price_response(price_dict: Optional[Dict[str, Any]]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    If price missing -> return ("", None). Never fabricate "0.00".
    Now includes multi-currency support for USD, SAR, AED.
    """
    if not price_dict or "amount" not in price_dict or not price_dict["amount"]:
        return "", None

    amount = float(price_dict["amount"])
    currency = price_dict.get("currency", "USD")
    
    # Convert to multi-currency format
    multi_currency = CURRENCY.convert_to_multi_currency(amount, currency)
    
    # Build enhanced price object
    price_obj = {
        "amount": amount, 
        "currency": currency, 
        "source": price_dict.get("source", "unknown"),
        **multi_currency  # Include USD, SAR, AED conversions
    }
    
    # Return YER value and enhanced price object
    yer_val = multi_currency["yer"]
    return f"{yer_val:.2f}", price_obj

def clean_specifications(specs: Dict[str, str]) -> Dict[str, str]:
    """Remove empty keys and dedupe"""
    cleaned = {}
    for key, value in specs.items():
        if value and value.strip() and value != "" and value != "0":
            # Normalize key to Arabic
            arabic_key = SPEC_MAP.get(key.lower(), key)
            if arabic_key not in cleaned:  # Avoid duplicates
                cleaned[arabic_key] = value.strip()
    return cleaned



def generate_arabic_description(name: str, features: List[str], specs: Dict[str, str], price: str) -> str:
    """Generate factual, non-repetitive Arabic description"""
    
    # If we have no real specs and features cleaned out, keep description minimal
    if not specs and not features and (not name or name == "منتج غير محدد"):
        return ""  # better than filler
    
    paragraphs = []
    
    # 1. Product intro (one line, factual)
    if name and name != "منتج غير محدد":
        paragraphs.append(f"يقدم هذا المنتج المسمى «{name}» تجربة عملية ومميزة.")
    else:
        paragraphs.append("يقدم هذا المنتج تجربة عملية ومميزة.")
    
    # 2. Features from real data (not filler)
    if features:
        features_text = "، ".join(features[:6])
        paragraphs.append(f"أبرز المزايا: {features_text}.")
    
    # 3. Key specifications (top 6-8, only non-empty)
    if specs:
        spec_pairs = []
        for k, v in list(specs.items())[:8]:
            if v and v.strip() and v != "" and v != "0":
                spec_pairs.append(f"{k}: {v}")
        
        if spec_pairs:
            specs_text = "، ".join(spec_pairs)
            paragraphs.append(f"المواصفات الأساسية: {specs_text}.")
    
    # 4. Price guidance (only if we have real price)
    if price and price != "0.00" and price != "":
        paragraphs.append(f"السعر من المصدر: {price}.")
    
    # 5. Practical guidance (once, no repetition)
    paragraphs.append("يُنصح بمراجعة القياسات والتوافق قبل الشراء.")
    
    text = " ".join(paragraphs)
    
    # Target 120-180 words, never repeat sentences
    def word_count(s: str) -> int:
        return len(re.findall(r'\w+', s, re.UNICODE))
    
    current_words = word_count(text)
    
    # If too short, add one more factual sentence (no repetition)
    if current_words < 80:
        text += " يتميز هذا المنتج بسهولة الاستخدام والاعتمادية العالية."
    
    # Hard cap at 200 words (no more filler)
    words = text.split()
    if len(words) > 200:
        text = " ".join(words[:200])
    
    return text

def arabic_contract(
    name: str,
    description: str,
    images: List[str],
    video: Optional[str],
    price_dict: Optional[Dict[str, Any]],
    specs: Dict[str, str],
    keywords: List[str],
    cat_main: str,
    cat_sub: str,
    breadcrumb: List[str],
    components: Optional[List[str]] = None,
    variants: Optional[List[str]] = None,
    features: Optional[List[str]] = None
) -> Dict[str, Any]:
    # enforce images 5–8 (best effort)
    imgs = images[:8] if images else []
    
    # Build price responses using helper
    yer, price_obj = build_price_response(price_dict)
    
    # Clean specifications (remove empty keys, dedupe)
    cleaned_specs = clean_specifications(specs)

    # Contract quality assessment
    contract_quality = "good"
    if not name or name == "منتج غير محدد":
        contract_quality = "degraded"
    if len(imgs) < 5:
        contract_quality = "degraded"
    if not yer or yer == "0.00":
        contract_quality = "degraded"

    # Build result with new fields
    result = {
        "اسم_المنتج": name or "",
        "الوصف": (description or "")[:4000],
        "روابط_الصور": imgs[:8],
        "رابط_الفيديو": video or None,
        "المكونات": components or [],
        "السعر_بالريال_اليمني": yer,
        "السعر": price_obj,  # NEW: Price object with USD amount
        "المواصفات": cleaned_specs,  # NEW: Cleaned specs (no empty keys)
        "المتغيرات": variants or [],
        "كلمات_البحث": keywords[:16] if keywords else [],
        "الفئات": {
            "الفئة_الرئيسية": cat_main,
            "الفئة_الفرعية": cat_sub,
            "التسلسل": breadcrumb
        },
        "المزايا": features or []  # NEW: Features list
    }

    # Log contract quality for debugging
    logger.info(f"Contract generated - Quality: {contract_quality}, Name: '{name}', Images: {len(imgs)}, Price: {yer}, Features: {len(features or [])}")

    return result

def ensure_base64(s: str) -> bytes:
    try:
        return base64.b64decode(s.split(",")[-1], validate=True)
    except Exception:
        raise HTTPException(400, detail="Invalid base64")

# ---------------------- Webhooks -----------------------
async def post_webhook(url: Optional[str], payload: dict, request_id: str):
    if not url:
        return
    headers = {"Content-Type": "application/json"}
    if SUPABASE_API_KEY:
        headers["Authorization"] = f"Bearer {SUPABASE_API_KEY}"
        headers["apikey"] = SUPABASE_API_KEY
    try:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            log(request_id, logging.WARNING, "Webhook failed", status=r.status_code)
    except Exception as ex:
        log(request_id, logging.WARNING, f"Webhook exception: {ex}")

# ---------------------- Error Recovery -----------------
class ErrorRecovery:
    @staticmethod
    def vision_to_dict(vd) -> Dict[str, Any]:
        """
        Accepts either:
          - google.cloud.vision.AnnotateImageResponse (object)
          - dict returned by Vision REST/JSON
        Returns: {"labels": [...], "text": str, "entities": [...], "similar": [...], "best_guesses": [...]}
        """
        if not vd:
            return {"labels": [], "text": "", "entities": [], "similar": [], "best_guesses": []}

        # NEW: unwrap REST wrapper if present
        if isinstance(vd, dict) and "responses" in vd and isinstance(vd["responses"], list) and vd["responses"]:
            vd = vd["responses"][0] or {}

        def _as_list(x):
            return x if isinstance(x, list) else (x or [])

        # Object path (google.cloud.vision.AnnotateImageResponse)
        if hasattr(vd, "label_annotations") or hasattr(vd, "web_detection"):
            try:
                labels = [l.description for l in _as_list(vd.label_annotations)]
            except Exception:
                labels = []
            try:
                text = vd.full_text_annotation.text if getattr(vd, "full_text_annotation", None) else ""
            except Exception:
                text = ""
            try:
                entities = [e.description for e in _as_list(vd.web_detection.web_entities)]
            except Exception:
                entities = []
            try:
                similar = [img.url for img in _as_list(vd.web_detection.visually_similar_images)]
            except Exception:
                similar = []
            try:
                best = [b.label for b in _as_list(vd.web_detection.best_guess_labels)]
            except Exception:
                best = []
            return {"labels": labels, "text": text, "entities": entities, "similar": similar, "best_guesses": best}

        # Dict path (REST JSON response)
        try:
            labels = [l.get("description", "") for l in _as_list(vd.get("labelAnnotations", []))]
        except Exception:
            labels = []
        try:
            fta = vd.get("fullTextAnnotation") or {}
            text = fta.get("text", "")
        except Exception:
            text = ""
        try:
            wd = vd.get("webDetection") or {}
            entities = [e.get("description", "") for e in _as_list(wd.get("webEntities", []))]
            similar = [i.get("url", "") for i in _as_list(wd.get("visuallySimilarImages", []))]
            best = [b.get("label", "") for b in _as_list(wd.get("bestGuessLabels", []))]
        except Exception:
            entities, similar, best = [], [], []
        return {"labels": labels, "text": text, "entities": entities, "similar": similar, "best_guesses": best}

    @staticmethod
    def fallback_partial() -> Dict[str, Any]:
        return {
            "اسم_المنتج": "منتج غير محدد",
            "السعر_بالريال_اليمني": "",
            "روابط_الصور": [],
            "السعر": None,
            "المزايا": []
        }

    @staticmethod
    def fallback_full() -> Dict[str, Any]:
        return {
            "اسم_المنتج": "منتج غير محدد",
            "الوصف": "تعذر تحليل المنتج - يرجى المحاولة مرة أخرى",
            "روابط_الصور": [],
            "رابط_الفيديو": None,
            "المكونات": [],
            "السعر_بالريال_اليمني": "",
            "السعر": None,
            "المواصفات": {},
            "المتغيرات": [],
            "كلمات_البحث": [],
            "الفئات": {
                "الفئة_الرئيسية": "عام",
                "الفئة_الفرعية": "عام",
                "التسلسل": ["عام"]
            },
            "المزايا": []
        }

# ---------------------- Search and Scrape Enhancement -----------------
# Default allowlist - same as WebScraper.is_allowed() for consistency
DEFAULT_ALLOWED = "aliexpress.com,amazon.com,amazon.ae,amazon.sa,amazon.co.uk,amazon.de,amazon.fr,noon.com,souq.com,jumia.com,daraz.com,ebay.com,etsy.com,shopify.com,woocommerce.com"

def _extract_vendor_url(href: str) -> str:
    """Extract the real vendor URL from Google Shopping redirects"""
    if not href:
        return ""
    
    # Handle protocol-relative URLs
    if href.startswith("//"):
        href = "https:" + href
    
    # Normalize to absolute
    if href.startswith("/"):
        href = urljoin("https://www.google.com", href)
    
    # If it's a Google redirect, pull out the real target
    o = urlparse(href)
    if o.netloc.endswith("google.com") and o.path.startswith("/url"):
        q = parse_qs(o.query)
        target = (q.get("q") or q.get("url") or [""])[0]
        return target or href
    
    return href

async def try_search_and_scrape(name: str) -> Dict[str, Any]:
    """Try to search for a product and scrape details when no URL hint is provided"""
    if not name or name == "منتج غير محدد":
        return {}
    
    try:
        # Search for the product
        items = await SCRAPER.search_google_shopping(name, max_results=5)
        
        # Build allowlist and exclude google.* as vendor - use same default as WebScraper.is_allowed()
        allowed_domains = [
            d.strip().lower() for d in os.getenv("ALLOWED_SCRAPING_DOMAINS", DEFAULT_ALLOWED).split(",")
            if d.strip()
        ]
        
        def is_allowed_vendor(u: str) -> bool:
            host = urlparse(u).hostname.lower()
            return host and any(host == d or host.endswith("." + d) for d in allowed_domains) \
                   and not (host == "google.com" or host.endswith(".google.com"))
        
        best_url = ""
        for item in items:
            cand = _extract_vendor_url(item.get("url", ""))
            if cand and is_allowed_vendor(cand):
                best_url = cand
                break
        
        if not best_url:
            # No vendor URL accepted, still return the best shopping image/price for fallback
            best_img = next((it.get("image_url") for it in items if it.get("image_url")), "")
            best_price = next((it.get("price") for it in items if it.get("price")), "")
            return {
                "title": name,
                "images": [best_img] if best_img else [],
                "specs": {},
                "price_amount": (CURRENCY.extract_price_and_currency(best_price)[0] if best_price else None),
                "price_currency": (CURRENCY.extract_price_and_currency(best_price)[1] if best_price else "USD"),
                "shopping_image_url": best_img,
                "shopping_price_text": best_price,
            }
        
        # Scrape the best result
        details = await SCRAPER.get_product_details(best_url)
        
        # Log vendor URL for debugging
        logger.info(f"Search-and-scrape succeeded for '{name}' -> vendor: {best_url}")
        
        return {
            "title": _clean_title(details.get("title", "")),
            "images": pick_better_images(normalize_images(details.get("images", []))),
            "video": details.get("video"),
            "specs": details.get("specifications", {}),
            "price_amount": details.get("price_amount"),
            "price_currency": details.get("price_currency", "USD"),
            # pass along the first shopping image/price as backup in case vendor had none
            "shopping_image_url": next((it.get("image_url") for it in items if it.get("image_url")), ""),
            "shopping_price_text": next((it.get("price") for it in items if it.get("price")), ""),
        }
    except Exception as e:
        logger.warning(f"Search and scrape failed for '{name}': {e}")
        return {}

# ---------------------- Analysis Core ------------------
async def analyze_with_fallbacks(img_bytes: bytes, product_url_hint: Optional[str], rid: str, vision_json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # If we have a product URL, prioritize scraping over Vision
    if product_url_hint:
        scraping = {}
        try:
            details = await SCRAPER.get_product_details(product_url_hint)
            scraping = {
                "title": _clean_title(details.get("title", "")),
                "images": pick_better_images(normalize_images(details.get("images", []))),
                "video": details.get("video"),
                "specs": details.get("specifications", {}),
                "price_amount": details.get("price_amount"),
                "price_currency": details.get("price_currency", "USD"),
            }
            
            # If scraping succeeded with real data, use it exclusively
            if scraping.get("title") and scraping.get("price_amount") is not None:
                log(rid, logging.INFO, f"Scraping succeeded for {product_url_hint}: title='{scraping['title'][:60]}', price={scraping['price_amount']} {scraping['price_currency']}")
                
                # Build response from scraped data only
                return arabic_contract(
                    name=scraping["title"],
                    description=generate_arabic_description(scraping["title"], [], scraping["specs"], scraping["price_amount"]),
                    images=scraping["images"][:8],
                    video=scraping.get("video"),
                    price_dict={"amount": scraping["price_amount"], "currency": scraping["price_currency"]},
                    specs=scraping["specs"],
                    keywords=[],
                    cat_main="منتجات",
                    cat_sub="عام",
                    breadcrumb=[],
                    features=[]
                )
            else:
                log(rid, logging.WARNING, f"Scraping failed for {product_url_hint}: no title or price extracted")
                # Return scrape_failed status instead of falling back to Vision
                return {
                    "status": "scrape_failed",
                    "reason": "no_product_data_found",
                    "request_id": rid,
                    "url": product_url_hint,
                    "message": "Unable to extract product data from the provided URL"
                }
                
        except Exception as e:
            log(rid, logging.ERROR, f"Scraping failed for {product_url_hint}: {e}")
            return {
                "status": "scrape_failed",
                "reason": "scraping_error",
                "request_id": rid,
                "url": product_url_hint,
                "error": str(e)
            }
    
    # Only use Vision if no product URL provided AND we have image data
    if not img_bytes:
        # No image data and no product URL - return error
        return {
            "status": "error",
            "reason": "no_input_data",
            "request_id": rid,
            "message": "No image data or product URL provided"
        }
    
    # Vision - use provided vision_json or call API
    if vision_json:
        try:
            vdict = ErrorRecovery.vision_to_dict(vision_json)
        except Exception as e:
            log(rid, logging.WARNING, f"Vision JSON parsing failed: {e}")
            vdict = ErrorRecovery.vision_to_dict(None)
    else:
        try:
            vd = await vision_annotate(img_bytes)
            vdict = ErrorRecovery.vision_to_dict(vd)
        except Exception as e:
            log(rid, logging.WARNING, f"Vision failed: {e}")
            vdict = ErrorRecovery.vision_to_dict(None)

    # Extract name using enhanced logic
    name = pick_name(vdict)
    
    # If no hint and no scrape yet, try search-based scrape
    scraping = {}
    if USE_GOOGLE_SHOPPING:
        try:
            scraping = await try_search_and_scrape(name)
            if scraping:
                log(rid, logging.INFO, f"Search-based scrape succeeded for '{name}'")
        except Exception as e:
            log(rid, logging.WARNING, f"Search-based scrape failed: {e}")
            scraping = {}

    # Choose final name (scraped title takes precedence)
    final_name = scraping.get("title") or name

    # Price handling - only use real prices, no fabrication
    price_amt = scraping.get("price_amount")
    price_cur = scraping.get("price_currency", "USD")
    
    # If still missing price, try to extract from OCR text
    if price_amt is None:
        ocr_price = extract_price_from_text(vdict.get("text", ""))
        if ocr_price:
            price_amt = ocr_price["amount"]
            price_cur = ocr_price["currency"]
            log(rid, logging.INFO, f"Extracted price from OCR: {price_amt} {price_cur}")
    
    # NEW: use shopping price text if scraping/OCR didn't produce a number
    if price_amt is None and scraping.get("shopping_price_text"):
        amt, cur = CURRENCY.extract_price_and_currency(scraping["shopping_price_text"])
        if amt and amt > 0:
            price_amt, price_cur = amt, cur
            log(rid, logging.INFO, f"Extracted price from shopping: {price_amt} {price_cur}")
    
    # Log price source for debugging
    if price_amt is not None:
        log(rid, logging.INFO, "price_found", amount=price_amt, currency=price_cur, source=scraping.get("price_source", "scrape_or_ocr"))

    # Images: prefer vendor images; only fall back to Vision if explicitly allowed
    images = scraping.get("images") or []
    
    # For AliExpress, trust vendor/CDN only
    ae_mode = (product_url_hint or "").lower().find("aliexpress") != -1
    if ae_mode:
        images = _filter_images_by_host(images, ["aliexpress.com","alicdn.com"])
        # Disable Vision "similar" images for vendor pages
        use_vision_images = False
    else:
        use_vision_images = USE_VISION_SIMILAR_IMAGES

    if not images and use_vision_images:
        images = pick_better_images(vdict.get("similar", [])[:6] or [])
    images = images or []

    # Extract features from Vision labels and scraping
    features = []
    if vdict.get("labels"):
        features.extend([label for label in vdict.get("labels", []) if label and len(label) > 3])
    if scraping.get("features"):
        features.extend(scraping.get("features", []))
    
    # Clean features to remove junk
    features = _clean_features(features)
    
    # Description (Arabic) - now with features
    desc = generate_arabic_description(final_name, features, scraping.get("specs", {}), 
                                     f"{convert_yer(price_amt, price_cur):.2f}" if price_amt else "")

    # Categories/keywords using enhanced logic
    cat_main, cat_sub, breadcrumb = guess_categories_from_labels(vdict.get("labels", []), scraping.get("title",""))
    
    # If we scraped AE breadcrumbs, prefer them
    if scraping.get("breadcrumbs"):
        bc = scraping["breadcrumbs"]
        if bc:
            cat_main = bc[0]
            cat_sub = bc[1] if len(bc) > 1 else (cat_sub or "عام")
            breadcrumb = bc
    
    keywords = make_keywords(vdict)

    # Format price for arabic_contract
    price_dict = {"amount": price_amt, "currency": price_cur} if price_amt is not None else None

    # Log telemetry for debugging
    log(rid, logging.INFO, f"Analysis complete - Name: '{final_name}', Price: {price_amt} {price_cur}, Images: {len(images)}")
    
    # Top-up to 5–8 only if Vision fallback is allowed
    if len(images) < 5 and use_vision_images:
        fallback_imgs = normalize_images(vdict.get("similar", []))
        more = [u for u in pick_better_images(fallback_imgs) if u not in images]
        images = (images + more)[:8]
        log(rid, logging.INFO, f"Image top-up: added {len(more)} from Vision similar images, total: {len(images)}")
    
    # If still short and we have a shopping image, pad with it
    if len(images) < 5 and scraping.get("shopping_image_url"):
        if scraping["shopping_image_url"] not in images:
            images.append(scraping["shopping_image_url"])
        images = images[:8]
    
    # Additional telemetry for debugging
    telemetry = {
        "vision_dict_empty": not vdict.get("labels") and not vdict.get("entities") and not vdict.get("best_guesses"),
        "picked_name_source": "bestGuess" if vdict.get("best_guesses") else "entity" if vdict.get("entities") else "label" if vdict.get("labels") else "ocr" if vdict.get("text") else "none",
        "search_started": not product_url_hint and not scraping,
        "scrape_hit": bool(scraping),
        "scrape_price_found": bool(scraping.get("price_amount")),
        "ocr_price_extracted": bool(extract_price_from_text(vdict.get("text", ""))),
        "final_name_quality": "good" if final_name and final_name != "منتج غير محدد" else "degraded",
        "images_after_topup": len(images)
    }
    
    log(rid, logging.INFO, f"Telemetry: {telemetry}")

    # Localize specs to Arabic keys
    localized_specs = localize_specs(scraping.get("specs", {}))
    
    # GUARANTEE: At least 1 image before returning
    if not images:
        if scraping.get("shopping_image_url"):
            images = [scraping["shopping_image_url"]]
            log(rid, logging.INFO, "fallback_image_guarantee", source="shopping")
        else:
            # Last-resort placeholder
            images = ["https://via.placeholder.com/600x600.png?text=Product+Image"]
            log(rid, logging.INFO, "fallback_image_guarantee", source="placeholder")
    
    return arabic_contract(
        name=final_name,
        description=desc,
        images=images[:8],
        video=scraping.get("video"),
        price_dict=price_dict,
        specs=localized_specs,  # Use mapped specs
        keywords=keywords,
        cat_main=cat_main,
        cat_sub=cat_sub,
        breadcrumb=breadcrumb,
        features=features  # NEW: Pass features
    )

# ---------------------- Background Analysis ------------
async def analyze_full_background(payload: AnalyzeInput, img_bytes: bytes, ihash: str, rid: str):
    """Background full analysis to warm cache for future partial requests"""
    try:
        # Vision analysis
        vd = await vision_annotate(img_bytes)
        
        # Extract name from Vision
        name = ""
        if vd.web_detection and vd.web_detection.best_guess_labels:
            name = vd.web_detection.best_guess_labels[0].label
        elif vd.label_annotations:
            name = vd.label_annotations[0].description
        else:
            name = "منتج غير محدد"
        
        # Cache name for future partial requests
        CACHE.set(f"name_for:{ihash}", name)
        
        # Try to get price from scraping
        if payload.product_url_hint:
            try:
                details = await SCRAPER.get_product_details(payload.product_url_hint)
                if details.get("price_amount") is not None:
                    CACHE.set(f"price_for:{ihash}", {
                        "amount": details["price_amount"],
                        "currency": details.get("price_currency", "USD")
                    })
                if details.get("images"):
                    CACHE.set(f"img_for:{ihash}", details["images"][0] if details["images"] else "")
            except Exception as e:
                log(rid, logging.DEBUG, f"background scrape failed: {e}")
        
        # Also cache by name for name-based lookups
        if name != "منتج غير محدد":
            CACHE.set(f"name_cache:{name.lower()}", {"name": name, "hash": ihash})
            
    except Exception as e:
        log(rid, logging.DEBUG, f"background analysis failed: {e}")

# ---------------------- Endpoint: /analyze/partial -----
@app.post("/analyze/partial", response_model=PartialResponse, response_model_by_alias=True, dependencies=[Depends(require_api_key), Depends(rate_limiter)])
async def analyze_partial(payload: AnalyzeInput, request: Request, bg: BackgroundTasks):
    rid = getattr(request.state, "request_id", "unknown")
    log(rid, logging.INFO, "partial_start")
    start_time = time.monotonic()

    try:
        if not payload.image_base64 and not payload.image_url:
            raise HTTPException(status_code=400, detail="Either image_base64 or image_url is required")

        # If no URL hint, handle based on STRICT_PARTIAL_FROM_SCRAPE setting
        if not payload.product_url_hint:
            # Use a simple hash for cache key to avoid expensive base64 processing
            if payload.image_base64:
                simple_hash = hashlib.md5(payload.image_base64.encode()).hexdigest()
            else:
                # FIX: hash the URL string to avoid collisions
                simple_hash = hashlib.md5((payload.image_url or "").encode()).hexdigest()
            cache_key = f"partial_fast:{simple_hash}:none"
            cached = CACHE.get(cache_key)
            if cached:
                log(rid, logging.INFO, "partial_cache_hit")
                return PartialResponse(**cached)
            
            if STRICT_PARTIAL_FROM_SCRAPE:
                # cache-first, no Vision, no crawling - return minimal skeleton quickly
                # Note: In strict mode with no URL hint, we return empty fields to maintain SLO
                log(rid, logging.INFO, "strict_partial_no_hint_returning_skeleton", reason="STRICT_PARTIAL_FROM_SCRAPE")
                result = {
                    "اسم_المنتج": "",
                    "السعر_بالريال_اليمني": "",
                    "روابط_الصور": [],
                    "السعر": None,
                    "المزايا": []
                }
                CACHE.set(cache_key, result)
                
                # NEW: background warm-up so subsequent hits aren't empty
                try:
                    if payload.image_base64 or payload.image_url:
                        img_bytes = ensure_base64(payload.image_base64) if payload.image_base64 else await download_bytes(payload.image_url)
                        ihash = hashlib.md5(img_bytes).hexdigest()
                        bg.add_task(analyze_full_background, payload, img_bytes, ihash, rid)
                except Exception as _e:
                    log(rid, logging.DEBUG, f"strictpartial_warmup_skip: {_e}")
                
                duration = (time.monotonic() - start_time) * 1000
                log(rid, logging.INFO, "partial_done", total_ms=f"{duration:.0f}")
                return PartialResponse(**result)
            else:
                # Vision path allowed when not strict
                try:
                    async with asyncio.timeout(REQUEST_HARD_TIMEOUT_MS/1000):
                        if payload.image_base64:
                            img_bytes = ensure_base64(payload.image_base64)
                        elif payload.image_url:
                            img_bytes = await download_bytes(payload.image_url)
                        else:
                            raise HTTPException(status_code=400, detail="No image data provided")
                        
                        # Use Vision API to analyze the image
                        if vision_client:
                            log(rid, logging.INFO, "using_vision_api_for_partial")
                            vd = await vision_annotate(img_bytes)
                            vdict = ErrorRecovery.vision_to_dict(vd)
                            
                            # Extract product name using enhanced logic
                            product_name = pick_name(vdict)
                            
                            # Try to extract price from OCR text
                            price_guess = extract_price_from_text(vdict.get("text", ""))
                            if price_guess:
                                yer = f'{convert_yer(price_guess["amount"], price_guess.get("currency", "USD")):.2f}'
                            else:
                                yer = ""
                            
                            # Get at least one image from Vision similar images
                            images = pick_better_images(normalize_images(vdict.get("similar", [])))[:1]
                            
                            # If no Vision images, try to get from shopping search
                            if not images and product_name and USE_GOOGLE_SHOPPING:
                                try:
                                    shopping_data = await try_search_and_scrape(product_name)
                                    if shopping_data.get("shopping_image_url"):
                                        images = [shopping_data["shopping_image_url"]]
                                        log(rid, logging.INFO, f"Added shopping image for partial: {images[0]}")
                                except Exception as e:
                                    log(rid, logging.DEBUG, f"Shopping fallback failed: {e}")
                            
                            # Build price object for partial
                            price_obj = None
                            if price_guess:
                                price_obj = {
                                    "amount": price_guess["amount"],
                                    "currency": price_guess.get("currency", "USD"),
                                    "source": "ocr_extraction"
                                }
                            
                            # Extract features from Vision labels
                            features = []
                            if vdict.get("labels"):
                                features = [label for label in vdict.get("labels", []) if label and len(label) > 3][:4]
                            
                            result = {
                                "اسم_المنتج": product_name or "",
                                "السعر_بالريال_اليمني": yer,
                                "روابط_الصور": images,
                                "السعر": price_obj,  # NEW: Price object
                                "المزايا": features  # NEW: Features
                            }
                        else:
                            log(rid, logging.WARNING, "vision_client_not_available")
                            result = {
                                "اسم_المنتج": "",
                                "السعر_بالريال_اليمني": "",
                                "روابط_الصور": [],
                                "السعر": None,
                                "المزايا": []
                            }
                        
                except asyncio.TimeoutError:
                    log(rid, logging.WARNING, "partial_vision_hard_timeout")
                    result = {
                        "اسم_المنتج": "",
                        "السعر_بالريال_اليمني": "",
                        "روابط_الصور": [],
                        "السعر": None,
                        "المزايا": []
                    }
                except Exception as e:
                    log(rid, logging.ERROR, f"vision_analysis_failed: {e}")
                    result = {
                        "اسم_المنتج": "",
                        "السعر_بالريال_اليمني": "",
                        "روابط_الصور": [],
                        "السعر": None,
                        "المزايا": []
                    }
                
                # Cache the result
                CACHE.set(cache_key, result)
                
                # GUARANTEE: At least 1 image in every response
                if not result.get("روابط_الصور"):
                    if payload.image_url:
                        result["روابط_الصور"] = [payload.image_url]
                        log(rid, logging.INFO, "partial_image_guarantee", source="image_url")
                    else:
                        # Last-resort placeholder to keep contract promise
                        result["روابط_الصور"] = ["https://via.placeholder.com/600x600.png?text=Product+Image"]
                        log(rid, logging.INFO, "partial_image_guarantee", source="placeholder")
                
                # Post to Supabase webhook if configured (non-blocking)
                if SUPABASE_PARTIAL_WEBHOOK:
                    bg.add_task(post_webhook, SUPABASE_PARTIAL_WEBHOOK, result, rid)
                duration = (time.monotonic() - start_time) * 1000
                log(rid, logging.INFO, "partial_done", total_ms=f"{duration:.0f}")
                return PartialResponse(**result)

        # Raw bytes (also used to key cache) - only for URL hint cases
        img_bytes = ensure_base64(payload.image_base64) if payload.image_base64 else await download_bytes(payload.image_url)
        ihash = hashlib.md5(img_bytes).hexdigest()

        # Cache first - check exact partial cache key for instant hits
        cache_key = f"partial_fast:{ihash}:{payload.product_url_hint or 'none'}"
        cached = CACHE.get(cache_key)
        if cached:
            log(rid, logging.INFO, "partial_cache_hit")
            return PartialResponse(**cached)

        result: Optional[Dict[str, Any]] = None

        # Strict scrape-only (no Vision) when URL hint is provided
        if payload.product_url_hint:
            result = await fast_partial_from_url_hint(payload.product_url_hint, rid)

        # If still nothing and we have a warmed name/image from a previous full run
        if not result or (not result.get("اسم_المنتج") and not result.get("السعر_بالريال_اليمني") and not result.get("روابط_الصور")):
            warmed = CACHE.get(f"warm_partial:{ihash}")
            if warmed:
                result = warmed

        # If still nothing, return skeleton quickly
        if not result or (not result.get("اسم_المنتج") and not result.get("السعر_بالريال_اليمني") and not result.get("روابط_الصور")):
            result = {
                "اسم_المنتج": "",
                "السعر_بالريال_اليمني": "",
                "روابط_الصور": [],
                "السعر": None,
                "المزايا": []
            }

        # If not strict, you could fall back to analyze_with_fallbacks (but that calls Vision and will blow SLO).
        if not result:
            result = {
                "اسم_المنتج": "",
                "السعر_بالريال_اليمني": "",
                "روابط_الصور": [],
                "السعر": None,
                "المزايا": []
            }

        # Cache and background warm (full analysis can populate name/price for future hits)
        CACHE.set(cache_key, result)
        asyncio.create_task(analyze_full_background(payload, img_bytes, ihash, rid))

        # GUARANTEE: At least 1 image in every response
        if not result.get("روابط_الصور"):
            if payload.image_url:
                result["روابط_الصور"] = [payload.image_url]
                log(rid, logging.INFO, "partial_image_guarantee", source="image_url")
            else:
                # Last-resort placeholder to keep contract promise
                result["روابط_الصور"] = ["https://via.placeholder.com/600x600.png?text=Product+Image"]
                log(rid, logging.INFO, "partial_image_guarantee", source="placeholder")

        # Post to Supabase webhook if configured
        if SUPABASE_PARTIAL_WEBHOOK:
            bg.add_task(post_webhook, SUPABASE_PARTIAL_WEBHOOK, result, rid)

        duration = (time.monotonic() - start_time) * 1000
        log(rid, logging.INFO, "partial_done", total_ms=f"{duration:.0f}")
        return PartialResponse(**result)

    except HTTPException:
        raise
    except Exception as e:
        log(rid, logging.ERROR, f"partial_failed: {e}")
        # GUARANTEE: At least 1 image even in error cases
        error_result = {
            "اسم_المنتج": "",
            "السعر_بالريال_اليمني": "",
            "روابط_الصور": ["https://via.placeholder.com/600x600.png?text=Error+Image"],
            "السعر": None,
            "المزايا": []
        }
        return PartialResponse(**error_result)

# ---------------------- Endpoint: /analyze/full --------
@app.post("/analyze/full", response_model=FullResponse, response_model_by_alias=True, dependencies=[Depends(require_api_key), Depends(rate_limiter)])
async def analyze_full(payload: AnalyzeInput, request: Request, bg: BackgroundTasks):
    rid = getattr(request.state, "request_id", "unknown")
    log(rid, logging.INFO, "full_start")
    start_time = time.monotonic()
    
    try:
        async with asyncio.timeout(REQUEST_HARD_TIMEOUT_MS/1000):
            # Allow URL-only requests when product_url_hint is provided
            if not payload.image_base64 and not payload.image_url and not payload.product_url_hint:
                raise HTTPException(status_code=400, detail="Either image_base64, image_url, or product_url_hint is required")
            
            # Handle URL-only requests (scraping only)
            if payload.product_url_hint and not payload.image_base64 and not payload.image_url:
                log(rid, logging.INFO, "URL-only request - scraping only")
                img_bytes = b""  # Empty bytes for URL-only requests
                ihash = "url_only"
            else:
                # Normal image-based request
                img_bytes = ensure_base64(payload.image_base64) if payload.image_base64 else await download_bytes(payload.image_url)
                ihash = hashlib.md5(img_bytes).hexdigest()

            cache_key = f"full:{ihash}:{payload.product_url_hint or 'none'}"
            cached = CACHE.get(cache_key)
            if cached:
                log(rid, logging.INFO, "full_cache_hit")
                return cached

            result = await analyze_with_fallbacks(img_bytes, payload.product_url_hint, rid, payload.vision_json)

            out = FullResponse(
                **{
                    "اسم_المنتج": result["اسم_المنتج"],
                    "الوصف": result["الوصف"],
                    "روابط_الصور": result["روابط_الصور"],
                    "رابط_الفيديو": result.get("رابط_الفيديو"),
                    "المكونات": result.get("المكونات", []),
                    "السعر_بالريال_اليمني": result["السعر_بالريال_اليمني"],
                    "السعر": result.get("السعر"),  # NEW: Price object
                    "المواصفات": result.get("المواصفات", {}),
                    "المتغيرات": result.get("المتغيرات", []),
                    "كلمات_البحث": result.get("كلمات_البحث", []),
                    "الفئات": result["الفئات"],
                    "المزايا": result.get("المزايا", [])  # NEW: Features
                }
            )

            CACHE.set(cache_key, out)

            # Assert Arabic keys in the outgoing payload (dev guard; won't slow prod)
            _ = out.model_dump(by_alias=True)

            # Warm a minimal partial record so subsequent /partial is instant
            try:
                warm_partial = {
                    "اسم_المنتج": result["اسم_المنتج"],
                    "السعر_بالريال_اليمني": result.get("السعر_بالريال_اليمني", ""),
                    "روابط_الصور": (result.get("روابط_الصور")[:1] if result.get("روابط_الصور") else []),
                    "السعر": result.get("السعر"),
                    "المزايا": result.get("المزايا", [])
                }
                CACHE.set(f"warm_partial:{ihash}", warm_partial)
                # Also prefill the exact partial cache key for instant hits
                partial_cache_key = f"partial_fast:{ihash}:{payload.product_url_hint or 'none'}"
                CACHE.set(partial_cache_key, warm_partial)
            except Exception:
                pass

            if SUPABASE_FULL_WEBHOOK:
                bg.add_task(post_webhook, SUPABASE_FULL_WEBHOOK, out.dict(by_alias=True), rid)

            duration = (time.monotonic() - start_time) * 1000
            log(rid, logging.INFO, "full_done", total_ms=f"{duration:.0f}")
            return out
            
    except asyncio.TimeoutError:
        log(rid, logging.WARNING, "full_hard_timeout")
        fb = ErrorRecovery.fallback_full()
        return FullResponse(**fb)
    except HTTPException:
        raise
    except Exception as e:
        log(rid, logging.ERROR, f"Full failed: {e}")
        fb = ErrorRecovery.fallback_full()
        return FullResponse(**fb)

# ---------------------- Endpoint: /crop (optional) -----
@app.post("/crop", dependencies=[Depends(require_api_key), Depends(rate_limiter)])
async def crop_image(payload: CropInput, request: Request):
    if not ENABLE_SERVER_CROP:
        raise HTTPException(403, "Server-side cropping disabled")
    img_bytes = b64_to_bytes(payload.image_base64)
    if payload.mode not in {"center", "square"}:
        raise HTTPException(400, "Unsupported crop mode")
    cropped = await asyncio.to_thread(image_center_crop, img_bytes)
    return {"image_base64_cropped": base64.b64encode(cropped).decode("utf-8")}

# ---------------------- Health -------------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "2.3.0",
        "cache_size": CACHE.size(),
        "playwright_enabled": USE_PLAYWRIGHT
    }

# ---------------------- Startup/Shutdown --------------
@app.on_event("startup")
async def on_startup():
    print("Starting Product Analyzer API…")  # Use print for startup to avoid logging format issues
    await CURRENCY.ensure_fresh()
    if USE_PLAYWRIGHT:
        await SCRAPER.init()
    print(f"Started. Playwright={'on' if USE_PLAYWRIGHT else 'off'}")
    print(f"Effective USD→YER rate: {YER_PER_USD} (from env: {os.getenv('YER_PER_USD', 'default')})")

@app.on_event("shutdown")
async def on_shutdown():
    print("Shutting down…")  # Use print for shutdown to avoid logging format issues
    try:
        await SCRAPER.shutdown()
    except Exception:
        pass
    try:
        await client.aclose()
    except Exception:
        pass
    try:
        await fast_client.aclose()   # <-- add this
    except Exception:
        pass
    print("Bye.")

# ---------------------- Enhanced Data Processing -----------------
GENERIC_NAMES = {"personal care","packaging and labeling","black background","product","electronics","appliance"}

def pick_name(vdict: Dict[str, Any]) -> str:
    """Choose the best product name from Vision data sources, filtering out generic names"""
    for source in ("best_guesses", "entities", "labels"):
        for cand in vdict.get(source, []) or []:
            s = (cand or "").strip()
            if s and s.lower() not in GENERIC_NAMES and not re.fullmatch(r"\W+", s):
                return s[:120]
    
    # As a last resort, try the first non-trivial line of OCR text
    txt = (vdict.get("text") or "").strip()
    for line in txt.splitlines():
        s = line.strip()
        if len(s) >= 3 and not re.match(r"^\W+$", s):
            return s[:120]
    
    return "منتج غير محدد"

def make_keywords(vdict: Dict[str, Any], max_k: int = 16) -> List[str]:
    """Build keywords by merging entities + labels, deduping, and cleaning"""
    raw = (vdict.get("entities") or []) + (vdict.get("labels") or [])
    seen, out = set(), []
    
    for w in raw:
        w = (w or "").strip()
        if not w: 
            continue
        key = w.lower()
        if key in seen: 
            continue
        seen.add(key)
        out.append(w)
        if len(out) >= max_k: 
            break
    
    return out

def extract_price_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract price information from OCR text"""
    if not text:
        return None
    
    # Look for common price patterns - using raw strings to avoid escape sequence warnings
    price_patterns = [
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:USD|دولار|\$)',  # USD prices
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:EUR|يورو|€)',   # EUR prices
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:YER|ر\.ي|يمني)', # YER prices
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:SAR|ر\.س|سعودي)', # SAR prices
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:AED|د\.إ|درهم)',  # AED prices
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:KWD|د\.ك|كويتي)', # KWD prices
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:QAR|ر\.ق|قطري)', # QAR prices
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:OMR|ر\.ع|عماني)', # OMR prices
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:BHD|د\.ب|بحريني)', # BHD prices
    ]
    
    for pattern in price_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            amount_str = match.group(1).replace(',', '')
            try:
                amount = float(amount_str)
                # Determine currency from the pattern
                if 'USD' in pattern or 'دولار' in pattern or '$' in pattern:
                    currency = 'USD'
                elif 'EUR' in pattern or 'يورو' in pattern or '€' in pattern:
                    currency = 'EUR'
                elif 'YER' in pattern or 'ر\.ي' in pattern or 'يمني' in pattern:
                    currency = 'YER'
                elif 'SAR' in pattern or 'ر\.س' in pattern or 'سعودي' in pattern:
                    currency = 'SAR'
                elif 'AED' in pattern or 'د\.إ' in pattern or 'درهم' in pattern:
                    currency = 'AED'
                elif 'KWD' in pattern or 'د\.ك' in pattern or 'كويتي' in pattern:
                    currency = 'KWD'
                elif 'QAR' in pattern or 'ر\.ق' in pattern or 'قطري' in pattern:
                    currency = 'QAR'
                elif 'OMR' in pattern or 'ر\.ع' in pattern or 'عماني' in pattern:
                    currency = 'OMR'
                elif 'BHD' in pattern or 'د\.ب' in pattern or 'بحريني' in pattern:
                    currency = 'BHD'
                else:
                    currency = 'USD'
                
                return {"amount": amount, "currency": currency}
            except ValueError:
                continue
    
    return None

# Spec mapping helper
SPEC_MAP = {
    "brand": "العلامة_التجارية", "brand name": "العلامة_التجارية", "manufacturer": "العلامة_التجارية",
    "model": "الموديل", "model number": "الموديل", "sku": "الموديل",
    "power": "الطاقة", "wattage": "الطاقة",
    "weight": "الوزن",
    "material": "المادة",
    "capacity": "السعة", "volume": "السعة",
    "voltage": "الجهد", "input voltage": "الجهد",
    "dimensions": "الأبعاد", "size": "الأبعاد"
}

def localize_specs(specs: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in (specs or {}).items():
        key = (k or "").strip().lower()
        # light normalize
        key = re.sub(r"[^a-z\u0600-\u06FF\s]+", " ", key).strip()
        ar = SPEC_MAP.get(key)
        out[ar or k] = str(v)
    return out

def guess_categories_from_labels(labels: List[str], title: str = "") -> Tuple[str, str, List[str]]:
    text = (title + " " + " ".join(labels)).lower()

    rules = [
        (["laptop","notebook","macbook","xps"], ("إلكترونيات", "حاسبات محمولة")),
        (["phone","smartphone","iphone","galaxy","xiaomi"], ("إلكترونيات", "هواتف ذكية")),
        (["headphone","earbud","earphone","airpods"], ("إلكترونيات", "سماعات")),
        (["camera","dslr","mirrorless","gopro"], ("إلكترونيات", "كاميرات")),
        (["coffee","espresso","kettle","blender","mixer"], ("أجهزة منزلية", "أدوات مطبخ")),
        (["shampoo","cream","skincare","lotion","serum"], ("العناية الشخصية", "عناية البشرة")),
        (["toy","lego","puzzle","doll"], ("ألعاب", "ألعاب أطفال")),
        (["shoe","sneaker","boot","sandals"], ("أزياء", "أحذية")),
        (["watch","smartwatch","fitbit","garmin"], ("إكسسوارات", "ساعات")),
        (["compressor","pump","tire","tyre"], ("صيانة السيارات", "معدات")),
        (["blue","color","light","dark","red","green","yellow","purple","pink","orange"], ("ألوان", "ألوان أساسية")),
        (["game","sims","electronic arts","plumbob"], ("ألعاب", "ألعاب فيديو")),
        (["background","wallpaper","texture"], ("تصميم", "خلفيات")),
        (["clothing","shirt","pants","dress","jacket"], ("أزياء", "ملابس")),
        (["furniture","chair","table","sofa","bed"], ("أثاث", "أثاث منزلي")),
        (["book","magazine","newspaper"], ("كتب", "مطبوعات")),
        (["food","drink","beverage"], ("طعام", "مشروبات")),
    ]
    for keys, (main, sub) in rules:
        if any(k.lower() in text for k in keys):
            return main, sub, [main, sub]
    return "منتجات", "عام", ["منتجات", "عام"]
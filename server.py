#!/usr/bin/env python3
"""AI Designer - FastAPI server with secure session tokens and SQLite storage."""

import json
import base64
import hashlib
import hmac
import ipaddress
import os
import io
import re
import secrets
import sys
import subprocess
import importlib.util
import threading
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse, quote
import requests as req_lib

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import anyio
import uvicorn

import db
import userdb

PORT = int(os.getenv("PORT", "8080"))

# ---- API Config ----
API_BASE = os.getenv("API_BASE", "").strip().rstrip("/")
if not API_BASE:
    raise RuntimeError("API_BASE environment variable is required (configure your image provider endpoint)")
API_KEY = os.getenv("API_KEY", "")
if not API_KEY:
    raise RuntimeError("API_KEY environment variable is required (set it in your shell or deployment environment)")
IMAGE_API_PROVIDER = os.getenv("IMAGE_API_PROVIDER", "openai").strip().lower()
if IMAGE_API_PROVIDER not in {"openai", "toapis"}:
    raise RuntimeError("IMAGE_API_PROVIDER must be openai or toapis")
MODEL = os.getenv("MODEL", "gpt-image-2.5-sunburst")
IMAGE_RESOLUTION = os.getenv("IMAGE_RESOLUTION", "").strip().upper()
if IMAGE_RESOLUTION and IMAGE_RESOLUTION not in {"1K", "2K", "4K"}:
    raise RuntimeError("IMAGE_RESOLUTION must be empty, 1K, 2K, or 4K")
IMAGE_SIZE_ENABLED = os.getenv("IMAGE_SIZE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
REWRITE_MODEL = os.getenv("REWRITE_MODEL", "gpt-5.6-luna-official")
REWRITE_API_KEY = os.getenv("REWRITE_API_KEY", "").strip() or API_KEY
REWRITE_API_BASE = (os.getenv("REWRITE_API_BASE", "").strip() or API_BASE).rstrip("/")
REWRITE_ENABLED = os.getenv("REWRITE_ENABLED", "1").strip().lower() not in {"0", "false", "no"}

# ---- Stock photo sources ----
# Both optional; each enables a "source" in the in-app reference search.
#
# Pexels (official, free): clean library, but Western-skewed — weaker for the
# 日韩清新 house style. Key at https://www.pexels.com/api/.
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
PEXELS_API_BASE = "https://api.pexels.com/v1"
#
# Pinterest is an optional gallery-dl integration, disabled by default.
# Enable only when authorized by the platform and content owners. Installing
# gallery-dl alone does not enable it. Browser credentials remain local.
PINTEREST_SCRAPE = (
    os.getenv("PINTEREST_SCRAPE", "0").strip().lower() in {"1", "true", "yes"}
    and importlib.util.find_spec("gallery_dl") is not None
)
PINTEREST_COOKIES_BROWSER = os.getenv("PINTEREST_COOKIES_BROWSER", "").strip()

# ---- Auth Config ----
# Two tiers: the regular APP account is subject to the per-IP daily rate limit;
# the admin account bypasses it (for owner/testing).
APP_USERNAME = os.getenv("APP_USERNAME", "").strip()
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes"}
RUNNING_IN_PRODUCTION = (
    os.getenv("APP_ENV", "").strip().lower() in {"prod", "production"}
    or bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID") or os.getenv("RENDER_EXTERNAL_URL"))
)

# ---- Accounts mode (email signup + credits) ----
# Activated purely by DATABASE_URL: set it (Neon) and the app switches from the
# shared-account + per-IP rate limit model to per-user email accounts with a
# credit balance. Unset → legacy behavior, nothing changes.
ACCOUNTS_ENABLED = userdb.enabled()
# Credits charged per generation, and the require-verification toggle.
GENERATION_COST = int(os.getenv("GENERATION_COST", "1"))
REGISTRATION_ENABLED = (
    os.getenv("REGISTRATION_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
)
REQUIRE_EMAIL_VERIFICATION = (
    os.getenv("REQUIRE_EMAIL_VERIFICATION", "").strip().lower() in {"1", "true", "yes"}
)
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "").strip()
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
if bool(TURNSTILE_SITE_KEY) != bool(TURNSTILE_SECRET_KEY):
    raise RuntimeError("TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY must be set together")
TURNSTILE_ENABLED = bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY)

if bool(APP_USERNAME) != bool(APP_PASSWORD):
    raise RuntimeError("APP_USERNAME and APP_PASSWORD must be set together")
if AUTH_DISABLED:
    AUTH_REQUIRED = False
elif APP_USERNAME and APP_PASSWORD:
    AUTH_REQUIRED = True
elif ACCOUNTS_ENABLED:
    # Email accounts require login (to attribute credits), but the gate is the
    # account token, not the shared APP_USERNAME/PASSWORD.
    AUTH_REQUIRED = True
elif RUNNING_IN_PRODUCTION:
    raise RuntimeError("APP_USERNAME and APP_PASSWORD are required in production (or set AUTH_DISABLED=1 explicitly)")
else:
    AUTH_REQUIRED = False

# Optional second account that bypasses the per-IP rate limit (for owner/testing).
# Defaults to the same password as APP_PASSWORD so set just ADMIN_USERNAME to enable.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip() or APP_PASSWORD
ADMIN_ENABLED = bool(ADMIN_USERNAME and ADMIN_PASSWORD)

# Session token TTL (default 7 days)
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "604800"))

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
LOCAL_UPLOAD_DIR = "reference_uploads"
# Ingested style references live in a dedicated public image directory.
STYLE_UPLOAD_CATEGORY = os.getenv("STYLE_UPLOAD_CATEGORY", "apparel").strip() or "apparel"
STYLE_UPLOAD_DIR = os.path.join(LOCAL_UPLOAD_DIR, "styles", STYLE_UPLOAD_CATEGORY)
PUBLIC_BASE_URL_CACHE = None

# ---- Rate Limit Config ----
# Per-IP daily limit. A real seller workflow is "try 3-5 styles on one product,
# regenerate the misses" — and office mates often share one egress IP — so the
# old limit of 3 locked users out after a single retry. Failed generations are
# refunded (see _refund_rate_limit call sites).
DEFAULT_LIMIT = int(os.getenv("DAILY_GENERATION_LIMIT", "20"))
TIME_WINDOW = 24 * 3600  # 24 hours in seconds

# ---- Trusted proxy header for client IP extraction ----
# Caddy: "X-Forwarded-For"   Cloudflare: "CF-Connecting-IP"
# Direct (no proxy): ""
TRUSTED_IP_HEADER = os.getenv("TRUSTED_IP_HEADER", "X-Forwarded-For")
TRUSTED_PROXY_IPS = {
    value.strip()
    for value in os.getenv("TRUSTED_PROXY_IPS", "").split(",")
    if value.strip()
}

# ---- Request size limits ----
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(12 * 1024 * 1024)))
MAX_INPUT_IMAGES = int(os.getenv("MAX_INPUT_IMAGES", "2"))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(6 * 1024 * 1024)))
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "12000"))

# Account endpoint throttles. These are persisted in Neon when accounts mode
# is enabled, so restarting the app instance does not reset an attack window.
REGISTER_RATE_LIMIT = int(os.getenv("REGISTER_RATE_LIMIT", "3"))
REGISTER_RATE_WINDOW_SECONDS = int(os.getenv("REGISTER_RATE_WINDOW_SECONDS", "86400"))
LOGIN_IP_RATE_LIMIT = int(os.getenv("LOGIN_IP_RATE_LIMIT", "30"))
LOGIN_ACCOUNT_RATE_LIMIT = int(os.getenv("LOGIN_ACCOUNT_RATE_LIMIT", "10"))
LOGIN_RATE_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_WINDOW_SECONDS", "3600"))


def elapsed_ms(start):
    return int((time.perf_counter() - start) * 1000)

def kb_size(value):
    if not value:
        return 0
    return round(len(value) / 1024, 1)

def extract_image_item(result):
    items = result.get("data") or []
    if not items and isinstance(result.get("result"), dict):
        items = result["result"].get("data") or []
    return items[0] if items else None


def image_upstream_error_details(response):
    """Log enough to distinguish gateway failures without dumping response bodies."""
    private_values = (API_KEY, REWRITE_API_KEY, API_BASE, REWRITE_API_BASE,
                      urlparse(API_BASE).netloc, urlparse(REWRITE_API_BASE).netloc)

    def redact(value, limit):
        text = str(value)
        for secret in private_values:
            if secret:
                text = text.replace(secret, "[redacted]")
        text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
        # Redact before truncation so a boundary cannot expose part of a key.
        return text[:limit]

    headers = response.headers
    details = {name: redact(headers.get(name, ""), 120) for name in (
        "Content-Type", "Server", "CF-Ray", "X-Request-Id",
    )}
    message = ""
    if "json" in headers.get("Content-Type", "").lower():
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            message = error.get("message", "") if isinstance(error, dict) else error or ""
        except (ValueError, TypeError):
            pass
    else:
        title = re.search(r"<title[^>]*>(.*?)</title>", getattr(response, "text", ""), re.I | re.S)
        if title:
            message = title.group(1)
    return json.dumps(dict(details, message=redact(message, 400)), ensure_ascii=False)

def decode_reference_image(image_item):
    data = image_item.get("data", "")
    mime = image_item.get("mime") or "image/jpeg"
    if data.startswith("data:"):
        header, data = data.split(",", 1)
        if ";base64" in header and header.startswith("data:"):
            mime = header[5:].split(";", 1)[0] or mime
    return base64.b64decode(data), mime

def json_error(status_code, message):
    return JSONResponse(status_code=status_code, content={"error": message})

def file_ext_for_mime(mime):
    return {
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime, "jpg")

def get_ngrok_public_url():
    global PUBLIC_BASE_URL_CACHE
    if PUBLIC_BASE_URL_CACHE:
        return PUBLIC_BASE_URL_CACHE
    try:
        resp = req_lib.get("http://127.0.0.1:4040/api/tunnels", timeout=2)
        resp.raise_for_status()
        tunnels = resp.json().get("tunnels") or []
        for tunnel in tunnels:
            public_url = tunnel.get("public_url", "")
            if public_url.startswith("https://"):
                PUBLIC_BASE_URL_CACHE = public_url.rstrip("/")
                return PUBLIC_BASE_URL_CACHE
    except Exception:
        pass
    return ""

def get_public_base_url(request: Request):
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/")

    forwarded_host = (request.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
    host = forwarded_host or (request.headers.get("Host") or "").strip()
    if host and not host.startswith(("localhost", "127.0.0.1", "0.0.0.0")):
        scheme = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip()
        if not scheme:
            scheme = "https" if "ngrok" in host else "http"
        return f"{scheme}://{host}".rstrip("/")

    return get_ngrok_public_url()

def save_reference_image(image_bytes, ext):
    os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(LOCAL_UPLOAD_DIR, filename)
    with open(path, "wb") as f:
        f.write(image_bytes)
    return f"/{LOCAL_UPLOAD_DIR}/{filename}"

def save_stock_reference(image_bytes, ext):
    """Persist a stock pick under the served styles dir so it gets a stable,
    cacheable same-origin URL — the same path curated style images use, which
    the frontend already knows how to fetch as a style reference. These build
    up the user's local reference library (see db.stock_library)."""
    base = os.path.dirname(os.path.abspath(__file__))
    stock_dir = os.path.join(base, LOCAL_UPLOAD_DIR, "styles", "stock")
    os.makedirs(stock_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(stock_dir, filename), "wb") as f:
        f.write(image_bytes)
    return f"/{LOCAL_UPLOAD_DIR}/styles/stock/{filename}"

def parse_json_object(raw):
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]
    return json.loads(raw)

STYLE_SPEC_FIELDS = [
    "chineseLabel",
    "shotIntent",
    "shotType",
    "lighting",
    "palette",
    "camera",
    "composition",
    "styling",
    "mood",
    "materials",
    "sceneDirection",
    "commercialIntent",
    "variationRules",
    "avoid",
]

def normalize_style_spec(spec):
    if not isinstance(spec, dict):
        spec = {}
    normalized = {}
    for key in STYLE_SPEC_FIELDS:
        value = spec.get(key, "")
        if isinstance(value, list):
            value = ", ".join(str(v).strip() for v in value if str(v).strip())
        normalized[key] = str(value).strip()
    return normalized

def style_spec_to_text(spec):
    spec = normalize_style_spec(spec)
    return " ".join(spec.get(k, "") for k in STYLE_SPEC_FIELDS if spec.get(k, "")).strip()

STYLE_ANALYZE_SYSTEM = (
    "Extract broad photography directions from this style reference. Do not describe "
    "clothing, products, people, logos or prints; do not prescribe poses, props or scene details. "
    "Return STRICT JSON with these fields:\n"
    '- "chineseLabel": a distinctive 3-5 Chinese character photography style label.\n'
    '- "shotIntent": one of worn_full_body, worn_half_body, hanger, print_detail, '
    'editorial_lifestyle, studio_editorial.\n'
    '- "shotType": broad type of photograph.\n'
    '- "lighting": overall light quality.\n'
    '- "palette": environment color mood only.\n'
    '- "camera": broad framing and perspective.\n'
    '- "composition": general composition feel.\n'
    '- "mood": overall atmosphere.\n'
    "Use a short English phrase of at most 8 words per photography field. "
    "Leave creative decisions to the image model."
)


def analyze_style_image(image_item, req_id):
    if not REWRITE_ENABLED:
        raise ValueError("风格分析暂未配置，请使用已有风格模板")
    if not isinstance(image_item, dict):
        raise ValueError("image_item must be a dict")
    data = image_item.get("data", "")
    mime = image_item.get("mime") or "image/jpeg"
    url = data if data.startswith("data:") else f"data:{mime};base64,{data}"
    user_content = [
        {
            "type": "text",
            "text": "Extract reusable styleSpec from this reference image.",
        },
        {"type": "image_url", "image_url": {"url": url}},
    ]
    resp = req_lib.post(
        f"{REWRITE_API_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {REWRITE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": REWRITE_MODEL,
            "messages": [
                {"role": "system", "content": STYLE_ANALYZE_SYSTEM},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=90,
    )
    resp.raise_for_status()
    raw = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    spec = normalize_style_spec(parse_json_object(raw))
    print(f"[INFO {req_id}] style_analyze mood={spec.get('mood', '')[:60]!r}")
    return spec


# ---- Brief photography guidance from the style reference only ----
# The image model sees both original images. The auxiliary model sees only
# the style reference, so product details are never translated into prose.
ART_DIRECT_FIELDS = ("lighting", "camera", "composition", "mood")

ART_DIRECT_SYSTEM = (
    "Look only at the style reference and summarize its photographic feel. "
    "Give broad directions for light and shadow, composition and overall aesthetic in 1-2 short English "
    "sentences, at most 40 words. Do not describe clothing, patterns, products or people. "
    "Do not prescribe exact poses, gestures, props, camera settings or scene details. "
    "Leave those choices to the image model. Output only the photography guidance."
)

ART_DIRECT_IMAGE_PROMPT = (
    "Image 1 is the product: preserve its design, colors, fabric, patterns and prints faithfully. "
    "Image 2 is a photography reference only; do not transfer its clothing or product details. "
    "Create a new {size_desc}commercial photograph of Image 1's product, inspired by "
    "Image 2's light and shadow, composition and overall aesthetic. {shot_hint}\n"
    "Photography direction: {brief}\n"
    "Choose the pose and scene details freely within that direction. "
    "Photorealistic; no added text or watermarks."
)

ART_DIRECT_IMAGE_PROMPT_SINGLE = (
    "The image shows the product: preserve its design, colors, fabric, patterns and prints faithfully. "
    "Create a new {size_desc}commercial photograph of this product. {shot_hint}\n"
    "Photography direction: {brief}\n"
    "Choose the pose and scene details freely within that direction. "
    "Photorealistic; no added text or watermarks."
)

SIZE_DESCRIPTIONS = {
    "1024x1536": "vertical 2:3 portrait ",
    "1024x1024": "square 1:1 ",
    "1536x1024": "horizontal 3:2 landscape ",
}

SHOT_INTENT_HINTS = {
    "worn_full_body": "Show the product worn by a model, with full-body framing.",
    "worn_half_body": "Show the product worn by a model, with half-body framing.",
    "hanger": "Show the garment on a hanger.",
    "print_detail": "Make a close-up of the product details.",
    "editorial_lifestyle": "Show the product naturally worn in a lifestyle photograph.",
    "studio_editorial": "Show the product worn in a studio editorial photograph.",
}
DEFAULT_SHOT_HINT = "Show wearable products naturally worn by a model."
REFERENCE_BRIEF = "Take inspiration from the reference's light and shadow, composition and overall aesthetic."


def _style_spec_brief_text(style_spec):
    """Bound legacy style fields; omit garment, staging and pose descriptions."""
    spec = style_spec if isinstance(style_spec, dict) else {}
    pieces = []
    for key in ART_DIRECT_FIELDS:
        value = " ".join(str(spec.get(key) or "").split()[:10])[:80].strip()
        if value:
            pieces.append(f"{key}: {value}")
    return "; ".join(pieces)


def fallback_brief(style_spec, style_image_item=None):
    if isinstance(style_image_item, dict) and style_image_item.get("data"):
        return REFERENCE_BRIEF
    return _style_spec_brief_text(style_spec) or "Natural lighting and a relaxed commercial photography feel."


def art_direct_brief(style_spec, req_id, style_image_item=None):
    """Summarize the reference only; never accept product images or previous briefs."""
    if not REWRITE_ENABLED:
        return fallback_brief(style_spec, style_image_item)
    has_reference = isinstance(style_image_item, dict) and bool(style_image_item.get("data"))
    instruction = "Summarize the reference's light and shadow, composition and aesthetic in 1-2 short sentences."
    if not has_reference:
        instruction += "\nPhotography notes: " + _style_spec_brief_text(style_spec)
    user_content = [{"type": "text", "text": instruction}]
    if has_reference:
        data = style_image_item["data"]
        mime = style_image_item.get("mime") or "image/jpeg"
        url = data if data.startswith("data:") else f"data:{mime};base64,{data}"
        user_content.append({"type": "image_url", "image_url": {"url": url}})
    payload = {
        "model": REWRITE_MODEL,
        "messages": [
            {"role": "system", "content": ART_DIRECT_SYSTEM},
            {"role": "user", "content": user_content},
        ],
    }
    # The upstream occasionally returns an empty body (200 but no JSON) under
    # load; one quick retry recovers it before we drop to the fallback brief.
    last_err = None
    for attempt in range(2):
        try:
            resp = req_lib.post(
                f"{REWRITE_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {REWRITE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            brief = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            if not brief:
                raise ValueError("empty brief")
            if len(brief.split()) > 60 or len(brief) > 420:
                print(f"[WARN {req_id}] art_direct guidance too long; using short fallback")
                return fallback_brief(style_spec, style_image_item)
            print(f"[INFO {req_id}] art_direct brief={brief[:120]!r}")
            return brief
        except Exception as e:
            last_err = e
            print(f"[WARN {req_id}] art_direct attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    raise last_err


# ---- Upstream API session ----
SESSION = req_lib.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
})


# ---- Client IP extraction (trusted proxy aware) ----

def get_client_ip(request: Request) -> str:
    """Extract the real client IP using the configured trusted proxy header."""
    peer = request.client.host if request.client else "unknown"
    if not TRUSTED_IP_HEADER:
        # No proxy — use the direct connection IP
        return peer

    if not is_trusted_proxy_peer(peer):
        return peer

    header_value = request.headers.get(TRUSTED_IP_HEADER, "")
    if not header_value:
        return peer

    if TRUSTED_IP_HEADER.lower() == "x-forwarded-for":
        # X-Forwarded-For: client, proxy1, proxy2
        # Only trusted proxy peers are allowed to supply this header. Once the
        # peer is trusted, the leftmost value is the original client.
        parts = [p.strip() for p in header_value.split(",") if p.strip()]
        return parts[0] if parts else peer
    else:
        # Single-value headers like CF-Connecting-IP
        return header_value.strip()


def is_trusted_proxy_peer(peer: str) -> bool:
    if peer in TRUSTED_PROXY_IPS:
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if str(ip) in TRUSTED_PROXY_IPS:
        return True
    if ip.is_loopback:
        return True
    # Caddy/nginx-style reverse proxies often reach the app from private
    # ranges, but local LAN clients should not get this trust by default.
    return RUNNING_IN_PRODUCTION and (ip.is_private or ip.is_link_local)


# ---- Auth helpers ----

# Sessions are HMAC-signed stateless tokens: username|expiry|signature. The
# previous DB-backed sessions lived in ephemeral SQLite, so every
# deploy/restart force-logged-out all users. The signing key is derived PER
# ACCOUNT from that account's own password — the shared APP_PASSWORD must
# never be enough to mint an admin token (admin bypasses the rate limit).
# Changing an account's password (or SESSION_SECRET) invalidates its tokens.
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
if not SESSION_SECRET:
    if ACCOUNTS_ENABLED and RUNNING_IN_PRODUCTION:
        raise RuntimeError("SESSION_SECRET is required for production accounts mode")
    SESSION_SECRET = "aid-session-v1"


def _auth_identifier(scope: str, value: str) -> str:
    """HMAC an auth throttle key so raw emails/IPs are never stored."""
    normalized = (value or "unknown").strip().lower()
    return hmac.new(
        SESSION_SECRET.encode("utf-8"),
        f"{scope}:{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _consume_account_auth_attempt(action: str, identifier: str,
                                  limit: int, window_seconds: int) -> bool:
    if not ACCOUNTS_ENABLED:
        return True
    return userdb.consume_auth_attempt(
        action,
        _auth_identifier(action, identifier),
        limit,
        window_seconds,
    )


def _account_auth_attempt_allowed(action: str, identifier: str,
                                  limit: int, window_seconds: int) -> bool:
    if not ACCOUNTS_ENABLED:
        return True
    return userdb.auth_attempt_allowed(
        action,
        _auth_identifier(action, identifier),
        limit,
        window_seconds,
    )


def _record_login_failure(client_ip: str, account_key: str):
    for action, identifier, limit in (
        ("login_ip", client_ip, LOGIN_IP_RATE_LIMIT),
        ("login_account", account_key, LOGIN_ACCOUNT_RATE_LIMIT),
    ):
        try:
            _consume_account_auth_attempt(
                action, identifier, limit, LOGIN_RATE_WINDOW_SECONDS
            )
        except Exception as e:
            print(f"[ERROR] login failure throttle write: {type(e).__name__}: {e}")


def _clear_login_account_failures(account_key: str):
    if not ACCOUNTS_ENABLED:
        return
    try:
        userdb.clear_auth_attempts(
            "login_account",
            _auth_identifier("login_account", account_key),
        )
    except Exception as e:
        print(f"[WARN] login throttle clear failed: {type(e).__name__}: {e}")


def _verify_turnstile(token: str, remote_ip: str) -> bool:
    """Validate a single-use Cloudflare Turnstile registration token."""
    if not TURNSTILE_ENABLED:
        return True
    if not token:
        return False
    try:
        resp = req_lib.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": TURNSTILE_SECRET_KEY,
                "response": token,
                "remoteip": remote_ip,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return bool(resp.json().get("success"))
    except Exception as e:
        print(f"[ERROR] turnstile verification failed: {type(e).__name__}: {e}")
        return False


def _session_signing_key(username: str) -> bytes:
    if ADMIN_ENABLED and username == ADMIN_USERNAME:
        secret = ADMIN_PASSWORD
    else:
        secret = APP_PASSWORD
    return hmac.new(
        SESSION_SECRET.encode("utf-8"),
        f"{username}:{secret}".encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _session_signature(username: str, payload: str) -> str:
    return hmac.new(_session_signing_key(username), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(username: str) -> str:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{username}|{expires_at}"
    raw = f"{payload}|{_session_signature(username, payload)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def validate_session_token(token: str) -> str | None:
    """Return the username for a valid, unexpired signed token, else None."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        username, expires_s, sig = decoded.rsplit("|", 2)
        # Only tokens for known accounts are meaningful — and a username
        # containing '|' could otherwise confuse the payload split.
        if username not in {APP_USERNAME, ADMIN_USERNAME} or not username:
            return None
        if not hmac.compare_digest(sig, _session_signature(username, f"{username}|{expires_s}")):
            return None
        if time.time() > int(expires_s):
            return None
        return username
    except Exception:
        return None


def _verify_credentials(username: str, password: str) -> str | None:
    """Return the matched username if credentials are valid, else None."""
    if APP_USERNAME and hmac.compare_digest(username, APP_USERNAME) and hmac.compare_digest(password, APP_PASSWORD):
        return APP_USERNAME
    if ADMIN_ENABLED and hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(password, ADMIN_PASSWORD):
        return ADMIN_USERNAME
    return None


# ---- Account tokens (email-account users in accounts mode) ----
# Distinct from the admin/legacy signed tokens above: payload is
# "acct|<user_id>|<expiry>" signed with the global SESSION_SECRET. user_id is
# not secret, so an attacker still can't forge a token without SESSION_SECRET.
# The "acct" prefix keeps these from ever colliding with username tokens.
_ACCOUNT_TOKEN_PREFIX = "acct"


def create_account_token(user_id: int) -> str:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{_ACCOUNT_TOKEN_PREFIX}|{user_id}|{expires_at}"
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def validate_account_token(token: str) -> int | None:
    """Return the user_id for a valid, unexpired account token, else None."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        prefix, uid_s, expires_s, sig = decoded.split("|", 3)
        if prefix != _ACCOUNT_TOKEN_PREFIX:
            return None
        payload = f"{prefix}|{uid_s}|{expires_s}"
        expected = hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if time.time() > int(expires_s):
            return None
        return int(uid_s)
    except Exception:
        return None


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def _get_auth_user(request: Request) -> str | None:
    """Return the username from a valid admin/legacy Bearer token, or None.

    This is the username-based identity (admin account, legacy shared APP
    account). Email-account users are identified by _get_account_user_id."""
    if not AUTH_REQUIRED:
        return None  # auth not required, treat as anonymous
    token = _bearer_token(request)
    if not token:
        return None
    user = validate_session_token(token)
    if user:
        return user
    # Legacy DB-backed tokens issued before the signed-token deploy.
    return db.validate_session(token)


def _get_account_user_id(request: Request) -> int | None:
    """Return the email-account user_id from a valid account token, or None."""
    if not ACCOUNTS_ENABLED:
        return None
    token = _bearer_token(request)
    if not token:
        return None
    return validate_account_token(token)


def _check_auth(request: Request) -> bool:
    """Return True if the request is authenticated (or auth is disabled)."""
    if not AUTH_REQUIRED:
        return True
    return _get_auth_user(request) is not None or _get_account_user_id(request) is not None


def _is_admin(request: Request) -> bool:
    """Return True if the request is from the admin account."""
    return ADMIN_ENABLED and _get_auth_user(request) == ADMIN_USERNAME


# ---- Status hints ----
STATUS_HINTS = {
    429: "请求过于频繁，请稍后再试",
    500: "服务器内部错误，请重新生成",
    502: "服务器网关错误，请重新生成",
    503: "服务暂时不可用，请稍后重试",
    504: "生成超时（图片可能仍在处理），请稍后重新生成",
}

# User-facing friendly messages for generation failures. Never leak raw
# exception text (ConnectionPool, ProxyError, RemoteDisconnected etc.) —
# the end user is a shop owner, not an engineer.
FRIENDLY_GEN_ERROR = "生成失败，请重新生成"
FRIENDLY_CONNECTION_ERROR = "服务暂时不可用，请稍后重试"
FRIENDLY_TIMEOUT_ERROR = "生成超时，请稍后重试"
FRIENDLY_HTTP_MESSAGES = {
    400: "请求格式错误，请刷新页面重试",
    401: "登录已失效，请重新登录",
    402: "服务额度不足，请联系管理员",
    403: "无权访问该服务",
    404: "服务接口不存在",
    429: "请求过于频繁，请稍后再试",
    500: "服务繁忙，请稍后重试",
    502: "服务网关错误，请稍后重试",
    503: "服务暂时不可用，请稍后重试",
    504: "服务响应超时，请稍后重试",
}
GENERATE_MAX_ATTEMPTS = 2  # one retry on transient failures


# ============================================================================
# FastAPI application
# ============================================================================

def on_startup():
    db.init_db()
    db.migrate_from_json()
    db.cleanup_sessions()
    if ACCOUNTS_ENABLED:
        userdb.init_db()
        recovered = userdb.recover_stale_generation_jobs()
        if recovered:
            print(f"[userdb] recovered and refunded {recovered} stale generation job(s)")
        print("🔐 Accounts mode ON (email signup + credits, Postgres)")
    else:
        print("🔓 Accounts mode OFF (shared account + per-IP rate limit)")
    print(f"🎨 AI Designer running at http://localhost:{PORT}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    on_startup()
    yield


app = FastAPI(title="AI Designer", docs_url=None, redoc_url=None, lifespan=lifespan)


# ---- No-cache middleware ----

@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/"):
        return response
    # Curated style images are stable content (filenames don't change once
    # ingested), so let the browser and any CDN in front cache them hard.
    # These are the bulk of first-load weight (~25MB across 213 files) and
    # were previously re-validated on every page view.
    if path.startswith("/reference_uploads/"):
        response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    else:
        # HTML/JS/CSS: force revalidation so code deploys reach users without
        # a manual hard-refresh.
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ---- API Routes ----

@app.get("/api/auth-status")
async def api_auth_status():
    # `accounts` tells the client to show the email register/login UI instead
    # of the single shared-password box. `stock` gates the reference-search
    # button; `stockSources` lists which sources the source-switcher shows.
    sources = []
    if PINTEREST_SCRAPE:
        sources.append("pinterest")
    if PEXELS_API_KEY:
        sources.append("pexels")
    return {
        "required": AUTH_REQUIRED,
        "accounts": ACCOUNTS_ENABLED,
        "registrationEnabled": ACCOUNTS_ENABLED and REGISTRATION_ENABLED,
        "stock": bool(sources),
        "stockSources": sources,
        # Public site key only. The secret is used exclusively server-side.
        "turnstileSiteKey": TURNSTILE_SITE_KEY,
    }


@app.get("/api/verify")
async def api_verify(request: Request):
    """Tiny auth-required ping so the client can validate its token without
    re-downloading the full /api/style-assets payload on startup."""
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})
    return {"ok": True}


@app.post("/api/login")
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="请求格式错误")

    username = body.get("username", "")
    password = body.get("password", "")
    email_raw = (body.get("email") or "").strip()
    # Account lookups are case-insensitive on email; admin/legacy username
    # matching uses the raw value (the username may be case-sensitive).
    email = (email_raw or username).lower()

    if not AUTH_REQUIRED:
        return {"token": ""}

    if ACCOUNTS_ENABLED:
        client_ip = get_client_ip(request)
        account_key = email or username or "missing"
        try:
            ip_allowed = _account_auth_attempt_allowed(
                "login_ip", client_ip, LOGIN_IP_RATE_LIMIT, LOGIN_RATE_WINDOW_SECONDS
            )
            account_allowed = _account_auth_attempt_allowed(
                "login_account", account_key,
                LOGIN_ACCOUNT_RATE_LIMIT, LOGIN_RATE_WINDOW_SECONDS,
            )
        except Exception as e:
            print(f"[ERROR] login throttle db error: {type(e).__name__}: {e}")
            return JSONResponse(status_code=503, content={"error": "服务暂时不可用，请稍后重试"})
        if not ip_allowed or not account_allowed:
            return JSONResponse(status_code=429, content={"error": "登录尝试过多，请稍后再试"})

    # Admin / legacy shared account (env credentials), checked first so the
    # owner can always sign in even with accounts mode on. The legacy UI sends
    # the identifier in `username`; the accounts-mode UI only has an email box,
    # so accept the admin username typed there too.
    for ident in (username, email_raw):
        matched_user = _verify_credentials(ident, password) if ident else None
        if matched_user:
            if ACCOUNTS_ENABLED:
                _clear_login_account_failures(account_key)
            return {"token": create_session_token(matched_user), "kind": "admin"}

    # Email account login.
    if ACCOUNTS_ENABLED and email:
        try:
            uid = userdb.verify_login(email, password)
        except Exception as e:
            print(f"[ERROR] login db error: {type(e).__name__}: {e}")
            return JSONResponse(status_code=503, content={"error": "服务暂时不可用，请稍后重试"})
        if uid:
            user = userdb.get_user_by_id(uid)
            _clear_login_account_failures(account_key)
            return {
                "token": create_account_token(uid),
                "kind": "account",
                "email": user["email"],
                "credits": user["credits"],
                "email_verified": user["email_verified"],
            }
        _record_login_failure(client_ip, account_key)
        return JSONResponse(status_code=401, content={"error": "邮箱或密码错误"})

    if ACCOUNTS_ENABLED:
        _record_login_failure(client_ip, account_key)
    return JSONResponse(status_code=401, content={"error": "用户名或密码错误"})


@app.post("/api/register")
async def api_register(request: Request):
    """Create an email account when public registration is explicitly open."""
    if not ACCOUNTS_ENABLED:
        return JSONResponse(status_code=400, content={"error": "注册未开放"})
    if not REGISTRATION_ENABLED:
        return JSONResponse(status_code=403, content={"error": "暂未开放注册"})
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="请求格式错误")

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not userdb.valid_email(email):
        return JSONResponse(status_code=400, content={"error": "邮箱格式不正确"})
    if not isinstance(password, str) or len(password) < 6:
        return JSONResponse(status_code=400, content={"error": "密码至少 6 位"})
    if len(password) > 128:
        return JSONResponse(status_code=400, content={"error": "密码过长"})

    if not _verify_turnstile(body.get("turnstile_token") or "", get_client_ip(request)):
        return JSONResponse(status_code=400, content={"error": "人机验证失败，请重试"})
    try:
        allowed = _consume_account_auth_attempt(
            "register_ip", get_client_ip(request),
            REGISTER_RATE_LIMIT, REGISTER_RATE_WINDOW_SECONDS,
        )
    except Exception as e:
        print(f"[ERROR] register throttle db error: {type(e).__name__}: {e}")
        return JSONResponse(status_code=503, content={"error": "服务暂时不可用，请稍后重试"})
    if not allowed:
        return JSONResponse(status_code=429, content={"error": "注册次数过多，请明天再试"})

    try:
        uid = userdb.create_user(email, password)
    except userdb.EmailExists:
        return JSONResponse(status_code=409, content={"error": "该邮箱已注册，请直接登录"})
    except Exception as e:
        print(f"[ERROR] register db error: {type(e).__name__}: {e}")
        return JSONResponse(status_code=503, content={"error": "服务暂时不可用，请稍后重试"})

    user = userdb.get_user_by_id(uid)
    return {
        "token": create_account_token(uid),
        "kind": "account",
        "email": user["email"],
        "credits": user["credits"],
        "email_verified": user["email_verified"],
    }


@app.get("/api/account")
def api_account(request: Request):
    """Current account info (email + credit balance) for the logged-in user."""
    if not ACCOUNTS_ENABLED:
        return {"enabled": False}
    if _is_admin(request):
        return {"enabled": True, "admin": True, "credits": -1}
    uid = _get_account_user_id(request)
    if uid is None:
        return JSONResponse(status_code=401, content={"error": "未登录"})
    user = userdb.get_user_by_id(uid)
    if not user:
        return JSONResponse(status_code=401, content={"error": "账号不存在"})
    return {
        "enabled": True,
        "email": user["email"],
        "credits": user["credits"],
        "email_verified": user["email_verified"],
    }


_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _thumb_url(image_url):
    """Return the 400px WebP thumbnail URL if it exists on disk, else the original.

    Thumbnails are pre-generated by scripts/gen_thumbnails.py and committed to
    the repo (foo.jpg -> foo.t400.webp). Gallery cards use this small variant;
    the original stays the full-res style reference sent to the image model.
    """
    if not image_url:
        return image_url
    base, ext = os.path.splitext(image_url)
    if ext.lower() not in (".jpg", ".jpeg", ".png"):
        return image_url
    thumb = base + ".t400.webp"
    if os.path.exists(os.path.join(_PROJECT_DIR, thumb.lstrip("/"))):
        return thumb
    return image_url


@app.get("/api/style-assets")
async def api_get_style_assets(request: Request):
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})

    assets = db.load_style_assets()
    public_assets = []
    for asset in assets:
        image = asset.get("image")
        if not image:
            continue
        if asset.get("hidden"):
            continue
        public_assets.append({
            "id": asset.get("id"),
            "sampleImage": image,
            "thumbImage": _thumb_url(image),
            "styleSpec": normalize_style_spec(asset.get("styleSpec") or {}),
            "sourceName": asset.get("sourceName") or "",
            "createdAt": asset.get("createdAt") or 0,
            "qualityScore": asset.get("qualityScore") or 0,
        })
    # Merge in the user's permanently-saved stock picks. Their recent (ms)
    # createdAt sorts them to the front, so freshly-saved references lead.
    for a in db.load_stock_library():
        img = a.get("sampleImage")
        if not img:
            continue
        public_assets.append({
            "id": a.get("id"),
            "sampleImage": img,
            "thumbImage": a.get("thumbImage") or img,
            "styleSpec": normalize_style_spec(a.get("styleSpec") or {}),
            "sourceName": a.get("sourceName") or "",
            "createdAt": a.get("createdAt") or 0,
            "qualityScore": 0,
        })
    public_assets.sort(key=lambda a: (a.get("createdAt", 0), a.get("qualityScore", 0)), reverse=True)
    return {"assets": public_assets}


# ---- Stock photo search ----
# /search dispatches to a source (pexels | pinterest), normalizes each result
# to {id, thumb, full, alt, photographer, photographerUrl}; /use downloads the
# picked photo, runs the same styleSpec analysis curated assets get, and hands
# back a style-asset-shaped object the frontend drops into its existing
# style-reference flow. Keeping keys server-side also dodges the browser CORS wall.

STOCK_PER_PAGE = 24
STOCK_ORIENTATIONS = {"portrait", "landscape", "square"}
# Bias toward the app's house style: 日韩清新 (Japanese/Korean fresh) — young
# Asian models, soft natural daylight, casual effortless outfits. Empirically
# these tokens pull results toward that look and away from cold Western
# editorial. Skips tokens already in the query; toggleable from the client via
# enhance=0.
STOCK_QUALITY_TERMS = ("asian", "natural", "light", "casual")
# Empty query → on-brand default browse instead of a blank/irrelevant grid.
STOCK_DEFAULT_QUERY = "asian woman casual outfit natural light"
# Hosts /use is allowed to fetch from (SSRF guard): the two sources' image CDNs.
STOCK_IMAGE_HOSTS = ("pexels.com", "pinimg.com")
STOCK_FETCH_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def enhance_stock_query(query: str) -> str:
    low = query.lower()
    extra = [t for t in STOCK_QUALITY_TERMS if t not in low]
    return (query + " " + " ".join(extra)).strip() if extra else query


def _effective_query(query: str, enhance: bool) -> str:
    if not query:
        return STOCK_DEFAULT_QUERY
    return enhance_stock_query(query) if enhance else query


def _pexels_search(query, page, orientation, enhance):
    if not PEXELS_API_KEY:
        return JSONResponse(status_code=503, content={"error": "Pexels 未配置（缺少 PEXELS_API_KEY）"})
    params = {
        "query": _effective_query(query, enhance),
        "page": page,
        "per_page": STOCK_PER_PAGE,
        "size": "large",  # only sources with a high-res original — pro shots
    }
    if orientation in STOCK_ORIENTATIONS:
        params["orientation"] = orientation
    try:
        resp = req_lib.get(f"{PEXELS_API_BASE}/search",
                           headers={"Authorization": PEXELS_API_KEY}, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[ERROR] pexels search {query!r}: {type(e).__name__}: {e}")
        return JSONResponse(status_code=502, content={"error": "图库搜索失败，请重试"})
    photos = []
    for p in (data.get("photos") or []):
        src = p.get("src") or {}
        thumb = src.get("medium") or src.get("small") or src.get("tiny")
        # Full = the reference downloaded, analyzed, and sent to the image model
        # — highest res available for a sharper styleSpec.
        full = src.get("large2x") or src.get("large") or src.get("original")
        if not thumb or not full:
            continue
        photos.append({
            "id": p.get("id"),
            "thumb": thumb,
            "full": full,
            "alt": p.get("alt") or "",
            "photographer": p.get("photographer") or "",
            "photographerUrl": p.get("photographer_url") or "",
        })
    total = data.get("total_results") or 0
    return {"photos": photos, "page": page, "hasMore": page * STOCK_PER_PAGE < total}


# Pinterest images arrive as .../originals/<ext>; swap the size segment for a
# lighter grid thumbnail. Pinterest serves sized variants as JPG regardless of
# the original extension, so force .jpg (a sized .png/.webp URL 403s).
_PINIMG_SIZE_RE = re.compile(r"/(originals|\d+x)/")


def _pinimg_variant(url, size):
    if not url:
        return url
    u = _PINIMG_SIZE_RE.sub(f"/{size}/", url, count=1)
    return re.sub(r"\.(png|webp|gif)$", ".jpg", u, flags=re.IGNORECASE)


def _gallerydl_pin_urls(keyword, start, end):
    """Return (urls, ok): primary i.pinimg URLs for search positions start-end,
    or (None, False) on timeout/failure. gallery-dl `-g` prints one URL per pin
    plus `| <url>` alternate-format fallback lines, which we drop."""
    search_url = "https://www.pinterest.com/search/pins/?q=" + quote(keyword)
    cmd = [sys.executable, "-m", "gallery_dl", "--ignore-config", "-g", "--range", f"{start}-{end}"]
    if PINTEREST_COOKIES_BROWSER:
        cmd += ["--cookies-from-browser", PINTEREST_COOKIES_BROWSER]
    cmd.append(search_url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
    except subprocess.TimeoutExpired:
        print(f"[ERROR] pinterest scrape timeout {keyword!r}")
        return None, False
    urls, seen = [], set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("https://i.pinimg.com/") or line in seen:
            continue
        seen.add(line)
        urls.append(line)
    if not urls and proc.returncode != 0:
        # Third-party diagnostics may include private paths or credentials.
        print(f"[ERROR] pinterest scrape failed rc={proc.returncode}")
        return None, False
    return urls, True


def _pin_photo(url):
    return {
        "id": url.rsplit("/", 1)[-1].rsplit(".", 1)[0],
        "thumb": _pinimg_variant(url, "474x"),
        "full": url,
        "alt": "",
        "photographer": "",
        "photographerUrl": "",
    }


STOCK_SCAN_BATCH = 48    # gallery-dl positions fetched per scan step
STOCK_SCAN_MAX = 144     # positions to scan before giving up (dedup headroom)


def _pinterest_search(query, page, enhance):
    """Scrape Pinterest search via gallery-dl, skipping images already seen or
    saved so repeat searches surface fresh results (scanning forward in batches
    until a full page of unseen images is found). Runs in a worker thread — see
    the dispatcher — so its few-second subprocess doesn't block the event loop."""
    if not PINTEREST_SCRAPE:
        return JSONResponse(status_code=503, content={"error": "Pinterest 未启用（需安装可选依赖并显式开启）"})
    keyword = _effective_query(query, enhance)
    seen = db.stock_seen_urls()
    fresh, chosen = [], set()
    pos = (page - 1) * STOCK_PER_PAGE + 1
    scanned, failed = 0, False
    while len(fresh) < STOCK_PER_PAGE and scanned < STOCK_SCAN_MAX:
        urls, ok = _gallerydl_pin_urls(keyword, pos, pos + STOCK_SCAN_BATCH - 1)
        if not ok:
            failed = True
            break
        if not urls:
            break  # search exhausted
        for u in urls:
            if u in seen or u in chosen:
                continue
            chosen.add(u)
            fresh.append(_pin_photo(u))
            if len(fresh) >= STOCK_PER_PAGE:
                break
        pos += STOCK_SCAN_BATCH
        scanned += STOCK_SCAN_BATCH
    if not fresh and failed:
        return JSONResponse(status_code=502, content={"error": "Pinterest 搜索失败（可能被限流），稍后再试"})
    # Remember what we're showing so it won't repeat next time.
    db.mark_stock_seen([p["full"] for p in fresh])
    return {"photos": fresh, "page": page, "hasMore": len(fresh) >= STOCK_PER_PAGE}


# ---- Pinterest home feed ----
# The homepage appends pins directly below the curated style cards, so this
# path runs on every gallery load. Unlike _pinterest_search it must be cheap
# and repeatable: deterministic position-based pagination, NO seen-marking
# (merely displaying a feed must not burn pins out of the discovery pool),
# and a short in-memory cache so reloads don't re-run the gallery-dl
# subprocess for the same (keyword, page).
STOCK_FEED_TTL = 600        # seconds a cached feed page stays fresh
STOCK_FEED_CACHE_MAX = 64   # (keyword, page) entries kept before evicting oldest
_stock_feed_cache = {}      # (keyword, page) -> (fetched_at, photos, has_more)


def _pinterest_feed(query, page, enhance):
    if not PINTEREST_SCRAPE:
        return JSONResponse(status_code=503, content={"error": "Pinterest 未启用（需安装可选依赖并显式开启）"})
    keyword = _effective_query(query, enhance)
    key = (keyword, page)
    now = time.time()
    hit = _stock_feed_cache.get(key)
    if hit and now - hit[0] < STOCK_FEED_TTL:
        photos, has_more = hit[1], hit[2]
    else:
        start = (page - 1) * STOCK_PER_PAGE + 1
        urls, ok = _gallerydl_pin_urls(keyword, start, start + STOCK_PER_PAGE - 1)
        if not ok:
            return JSONResponse(status_code=502, content={"error": "Pinterest 加载失败（可能被限流），稍后再试"})
        photos = [_pin_photo(u) for u in urls]
        # In-batch dedup can shave a couple of URLs off a full page — allow
        # slack so pagination doesn't stop prematurely.
        has_more = len(photos) >= STOCK_PER_PAGE - 4
        _stock_feed_cache[key] = (now, photos, has_more)
        if len(_stock_feed_cache) > STOCK_FEED_CACHE_MAX:
            oldest = min(_stock_feed_cache, key=lambda k: _stock_feed_cache[k][0])
            _stock_feed_cache.pop(oldest, None)
    # Hide pins that are already style cards, so the feed doesn't duplicate
    # the gallery above it. Deliberately filtered against the stock LIBRARY
    # (actually-imported pins), not stock_seen — the legacy overlay marked
    # everything it ever displayed as seen, which would blank out whole feed
    # pages. Filtered at serve time — the cache stays raw, so a fresh import
    # takes effect without cache invalidation.
    library_urls = {a.get("sourceUrl") for a in db.load_stock_library()}
    photos = [p for p in photos if p["full"] not in library_urls]
    return {"photos": photos, "page": page, "hasMore": has_more}


@app.get("/api/stock/search")
async def api_stock_search(request: Request):
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})
    source = (request.query_params.get("source") or "pexels").strip().lower()
    query = (request.query_params.get("q") or "").strip()
    try:
        page = max(1, min(int(request.query_params.get("page", "1")), 100))
    except ValueError:
        page = 1
    orientation = (request.query_params.get("orientation") or "").strip().lower()
    enhance = (request.query_params.get("enhance") or "1").strip().lower() not in {"0", "false", "no"}
    # feed=1 → homepage feed semantics (stable pages, no seen-marking, cached).
    # Pexels pagination is already deterministic and API-backed, so only the
    # Pinterest path needs a dedicated feed helper.
    feed = (request.query_params.get("feed") or "0").strip().lower() in {"1", "true", "yes"}
    # Both helpers block (HTTP / subprocess); run off the event loop so a slow
    # source doesn't stall other requests (e.g. generation polling).
    if source == "pinterest":
        if feed:
            return await anyio.to_thread.run_sync(_pinterest_feed, query, page, enhance)
        return await anyio.to_thread.run_sync(_pinterest_search, query, page, enhance)
    return await anyio.to_thread.run_sync(_pexels_search, query, page, orientation, enhance)


@app.post("/api/stock/use")
async def api_stock_use(request: Request):
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})
    if not (PEXELS_API_KEY or PINTEREST_SCRAPE):
        return JSONResponse(status_code=503, content={"error": "图库未配置"})
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        return json_error(400, "请求格式错误")
    if not isinstance(body, dict):
        return json_error(400, "请求格式错误")
    url = (body.get("url") or "").strip()
    # SSRF guard: only fetch from the sources' own image CDNs — this endpoint
    # takes a URL and fetches it server-side, so it must never be a general
    # proxy. host must equal or be a subdomain of an allowed host.
    host = (urlparse(url).hostname or "").lower()
    allowed = url.startswith("https://") and any(
        host == h or host.endswith("." + h) for h in STOCK_IMAGE_HOSTS
    )
    if not allowed:
        return json_error(400, "仅支持 Pexels / Pinterest 图片地址")
    req_id = uuid.uuid4().hex[:8]
    try:
        # Some CDNs (i.pinimg.com) 403 the default requests UA — send a browser one.
        img_resp = req_lib.get(url, headers={"User-Agent": STOCK_FETCH_UA}, timeout=30)
        img_resp.raise_for_status()
        image_bytes = img_resp.content
    except Exception as e:
        print(f"[ERROR {req_id}] stock fetch {url!r}: {type(e).__name__}: {e}")
        return JSONResponse(status_code=502, content={"error": "图片下载失败，请重试"})
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return json_error(413, "图片过大，请换一张")
    mime = (img_resp.headers.get("Content-Type") or "image/jpeg").split(";", 1)[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    rel_path = save_stock_reference(image_bytes, file_ext_for_mime(mime))
    # Same styleSpec analysis curated assets get — best effort so a failed
    # vision call still yields a usable (plain) style reference.
    try:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        style_spec = analyze_style_image({"data": b64, "mime": mime}, req_id)
    except Exception as e:
        print(f"[WARN {req_id}] stock style_analyze failed: {type(e).__name__}: {e}")
        style_spec = normalize_style_spec({})
    photographer = str(body.get("photographer") or "").strip()
    source_name = photographer or ("Pinterest" if "pinimg.com" in host else "Pexels")
    asset = {
        "id": f"stock-{req_id}",
        "sampleImage": rel_path,
        "thumbImage": rel_path,
        "styleSpec": style_spec,
        "sourceName": source_name,
        "createdAt": int(time.time() * 1000),
        "sourceUrl": url,
    }
    # Persist permanently (survives reload — separate table from the curated
    # assets that migrate_from_json rehydrates) and remember the image so it
    # won't resurface in future searches.
    try:
        db.add_stock_library(asset)
        db.mark_stock_seen([url])
    except Exception as e:
        print(f"[WARN {req_id}] stock library persist failed: {type(e).__name__}: {e}")
    return asset


# ---- Favorite counts ----
# Each template's displayed favorite count = base + delta.
# `base` is a stable pseudo-random number derived from the template_id (so it
# survives service restarts and doesn't reset to zero on cold start). `delta`
# is the cumulative +/-1 from real user clicks, stored in SQLite.

FAVORITE_BASE_MIN = 30
FAVORITE_BASE_MAX = 280

def favorite_base(template_id: str) -> int:
    h = hashlib.sha256(template_id.encode("utf-8")).digest()
    span = FAVORITE_BASE_MAX - FAVORITE_BASE_MIN + 1
    return FAVORITE_BASE_MIN + (int.from_bytes(h[:4], "big") % span)


def favorite_count(template_id: str, deltas: dict) -> int:
    return max(0, favorite_base(template_id) + deltas.get(template_id, 0))


@app.get("/api/favorite-counts")
async def api_favorite_counts(request: Request):
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})
    # Frontend sends the list of template ids it cares about as ?ids=a,b,c.
    ids_param = request.query_params.get("ids", "")
    ids = [s for s in (ids_param.split(",") if ids_param else []) if s]
    deltas = db.load_favorite_deltas()
    return {"counts": {tid: favorite_count(tid, deltas) for tid in ids}}


@app.post("/api/favorite-toggle")
async def api_favorite_toggle(request: Request):
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "请求格式错误"})
    template_id = (body.get("template_id") or "").strip()
    action = body.get("action")
    if not template_id or action not in {"add", "remove"}:
        return JSONResponse(status_code=400, content={"error": "参数错误"})
    change = 1 if action == "add" else -1
    new_delta = db.bump_favorite_delta(template_id, change)
    return {"count": max(0, favorite_base(template_id) + new_delta)}


class GenerationError(Exception):
    """Generation failed with a user-facing message."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class InsufficientCredits(Exception):
    """Raised when an async job cannot atomically reserve its credit."""


def _parse_generate_request(request: Request, req_id: str,
                            defer_account_charge: bool = False):
    """Validate a generate request (auth is checked by the caller).

    Returns a params dict, or a JSONResponse error to send back immediately.
    Cheap work only — the LLM brief and the upstream call happen later, so
    the async endpoint can hand back a job id without waiting on either.
    """
    try:
        read_start = time.perf_counter()
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    return json_error(413, "请求图片过大，请压缩后重试")
            except ValueError:
                return json_error(400, "请求格式错误")
        body_bytes = anyio.from_thread.run(request.body)
        if len(body_bytes) > MAX_REQUEST_BYTES:
            return json_error(413, "请求图片过大，请压缩后重试")
        body = json.loads(body_bytes) if body_bytes else {}
        if not isinstance(body, dict):
            return json_error(400, "请求格式错误")
        print(f"[TIMING {req_id}] receive_parse={elapsed_ms(read_start)}ms request={round(len(body_bytes) / 1024, 1)}KB")
    except Exception as e:
        print(f"[ERROR {req_id}] request setup: {e}")
        return JSONResponse(status_code=400, content={"error": "请求处理失败，请重试"})

    client_ip = get_client_ip(request)
    # is_local must trust only the real socket peer: get_client_ip honors
    # X-Forwarded-For behind a trusted proxy, and that header's leftmost value
    # is client-controlled — a spoofed "127.0.0.1" must not grant unlimited quota.
    peer = request.client.host if request.client else ""
    is_local = peer in {"127.0.0.1", "::1"} and not RUNNING_IN_PRODUCTION
    rate_key = f"ip_{client_ip}"
    rate_limited = not is_local and not _is_admin(request)

    # Validate BEFORE charging quota — a malformed request shouldn't cost a
    # generation. Failed generations are refunded in the error paths.
    prompt = body.get("prompt", "")
    if not prompt or not isinstance(prompt, str):
        return json_error(400, "prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        return json_error(413, "描述过长，请缩短后重试")

    input_images = body.get("image") or []
    if not isinstance(input_images, list):
        return json_error(400, "图片参数错误")
    # Reject malformed items up front (before quota is charged) — downstream
    # the dual/single prompt template is chosen by image COUNT, so a junk item
    # slipping through would claim "TWO images" while uploading one.
    if any(not isinstance(it, dict) or not it.get("data") for it in input_images):
        return json_error(400, "图片参数错误")
    if len(input_images) > MAX_INPUT_IMAGES:
        return json_error(413, "图片数量过多，请重新上传")
    output_size = body.get("size", "auto")
    if output_size not in {"auto", "1024x1024", "1024x1536", "1536x1024"}:
        output_size = "auto"

    # ---- Charge the generation ----
    # Accounts mode: deduct credits from the logged-in user (admin is free).
    # Legacy mode: per-IP daily rate limit. The chosen charge is recorded so
    # _refund_on_failure / _quota_remaining can act on the right ledger.
    if ACCOUNTS_ENABLED:
        if _is_admin(request):
            charge = {"kind": "unlimited"}
        else:
            uid = _get_account_user_id(request)
            if uid is None:
                return json_error(401, "请先登录")
            if REQUIRE_EMAIL_VERIFICATION and not userdb.is_verified(uid):
                return JSONResponse(status_code=403, content={"error": "请先验证邮箱", "code": "EMAIL_UNVERIFIED"})
            if not defer_account_charge:
                balance = userdb.try_consume_credit(uid, GENERATION_COST, reason="generate")
                if balance is None:
                    return JSONResponse(status_code=402, content={"error": "额度不足，请充值", "code": "NO_CREDITS"})
            charge = {"kind": "credits", "user_id": uid, "amount": GENERATION_COST}
    else:
        if rate_limited and not db.check_rate_limit(rate_key, DEFAULT_LIMIT, TIME_WINDOW):
            return JSONResponse(status_code=429, content={"error": "生成次数已用完，额度会在 24 小时内陆续恢复"})
        charge = {"kind": "ip", "rate_key": rate_key, "rate_limited": rate_limited}

    return {
        "prompt": prompt,
        "input_images": input_images,
        "output_size": output_size,
        # Both original images go to image generation; only the style reference
        # goes to the auxiliary model for short photography guidance.
        "art_direct": bool(body.get("art_direct")) and bool(body.get("style_spec")),
        "style_spec": body.get("style_spec") or {},
        "shot_intent": body.get("shot_intent") or "",
        # Optional small style thumbnail for the brief LLM (halves its vision
        # cost); the image model itself always gets the full-size reference.
        "style_thumb": (
            body["style_thumb_image"]
            if isinstance(body.get("style_thumb_image"), dict) and body["style_thumb_image"].get("data")
            else None
        ),
        # How this generation was charged — drives refunds on failure and
        # remaining-quota reporting (see _refund_on_failure / _quota_remaining).
        "charge": charge,
    }


def _refund_on_failure(params: dict):
    """Give the quota back when a generation fails — users shouldn't pay for
    our (or the upstream's) errors."""
    charge = params.get("charge") or {}
    if charge.get("kind") == "credits":
        userdb.refund_credit(charge["user_id"], charge["amount"])
    elif charge.get("kind") == "ip" and charge.get("rate_limited"):
        db.refund_rate_limit(charge["rate_key"])


def _quota_remaining(params: dict) -> int:
    """Remaining generations for this client; -1 means unlimited (admin/local).
    In accounts mode this is the user's credit balance."""
    charge = params.get("charge") or {}
    kind = charge.get("kind")
    if kind == "credits":
        if "remaining" in charge:
            return int(charge["remaining"])
        return userdb.get_credits(charge["user_id"])
    if kind == "ip" and charge.get("rate_limited"):
        return db.rate_limit_remaining(charge["rate_key"], DEFAULT_LIMIT, TIME_WINDOW)
    return -1  # unlimited (admin / local / no rate limit)


def _uses_durable_account_job(params: dict) -> bool:
    return (params.get("charge") or {}).get("kind") == "credits"


def _create_async_job(job_id: str, params: dict):
    """Persist a job in the storage appropriate for its auth mode."""
    charge = params.get("charge") or {}
    if _uses_durable_account_job(params):
        balance = userdb.consume_credit_and_create_generation_job(
            job_id,
            int(charge["user_id"]),
            int(charge.get("amount") or 0),
        )
        if balance is None:
            raise InsufficientCredits()
        charge["job_id"] = job_id
        charge["remaining"] = balance
        return
    db.create_gen_job(job_id)


def _finish_async_job(job_id: str, params: dict, image: str, brief: str):
    if _uses_durable_account_job(params):
        if not userdb.finish_generation_job(job_id, image=image, brief=brief):
            raise RuntimeError("generation job was already terminal")
        return
    db.finish_gen_job(job_id, image=image, brief=brief)


def _fail_async_job(job_id: str, params: dict, error: str):
    """Record failure and refund once. Postgres performs both atomically."""
    if _uses_durable_account_job(params):
        userdb.fail_generation_job(job_id, error)
        return
    _refund_on_failure(params)
    db.finish_gen_job(job_id, error=error)


def _get_async_job(job_id: str, request: Request) -> dict | None:
    account_user_id = _get_account_user_id(request)
    if account_user_id is not None:
        return userdb.get_generation_job(job_id, account_user_id)
    return db.get_gen_job(job_id)


def _submit_toapis_generation(prompt, output_size, reference_blobs, reference_urls):
    """ToAPIs uses uploaded reference URLs and an asynchronous JSON request.

    Retain successful uploads across retries so a transient error does not
    re-upload every product/reference image.
    """
    for idx, buf, mime, ext in reference_blobs[len(reference_urls):]:
        upload = SESSION.post(
            f"{API_BASE}/uploads/images",
            files={"file": (f"reference-{idx + 1}.{ext}", io.BytesIO(buf), mime)},
            timeout=60,
        )
        upload.raise_for_status()
        body = upload.json()
        url = (body.get("data") or {}).get("url")
        if not body.get("success") or not url:
            raise RuntimeError("Reference upload did not return an image URL")
        reference_urls.append(url)
    size = {"1024x1024": "1:1", "1024x1536": "2:3", "1536x1024": "3:2"}[output_size]
    payload = {
        "model": MODEL, "prompt": prompt, "n": 1,
        # Match the provider's lowercase request tokens; uppercase values
        # have returned smaller images despite the requested resolution.
        "size": size, "quality": "high",
        "response_format": "url",
    }
    if IMAGE_RESOLUTION:
        payload["resolution"] = IMAGE_RESOLUTION.lower()
    if reference_urls:
        payload["reference_images"] = reference_urls
    return SESSION.post(f"{API_BASE}/images/generations", json=payload, timeout=60)


def _run_generation(req_id: str, params: dict) -> tuple[str, str]:
    """Blocking generation: shoot-brief building, reference decode, upstream
    call with retry. Returns (base64 image, brief used), or raises
    GenerationError with a user-facing message. Runs in a worker thread for
    async jobs."""
    total_start = time.perf_counter()
    prompt = params["prompt"]
    input_images = params["input_images"]
    output_size = params["output_size"]
    brief = ""

    # === payload prep (LLM brief + reference decode) ===
    try:
        input_image_kb = sum(kb_size(item.get("data", "")) for item in input_images if isinstance(item, dict))

        final_prompt = prompt
        if params["art_direct"] and input_images:
            style_spec = params["style_spec"]
            shot_intent = params["shot_intent"]
            style_image = params.get("style_thumb") or (input_images[1] if len(input_images) > 1 else None)
            try:
                brief = art_direct_brief(
                    style_spec, req_id, style_image_item=style_image,
                )
            except Exception as e:
                brief = fallback_brief(style_spec, style_image)
                print(f"[WARN {req_id}] art_direct LLM failed ({type(e).__name__}: {e}); using fallback brief")
            template = ART_DIRECT_IMAGE_PROMPT if len(input_images) > 1 else ART_DIRECT_IMAGE_PROMPT_SINGLE
            final_prompt = template.format(
                brief=brief,
                size_desc=SIZE_DESCRIPTIONS.get(output_size, ""),
                shot_hint=SHOT_INTENT_HINTS.get(shot_intent, DEFAULT_SHOT_HINT),
            )
        print(
            f"[TIMING {req_id}] mode={'image-to-image' if input_images else 'text-to-image'} "
            f"model={MODEL} size={output_size} "
            f"provider={IMAGE_API_PROVIDER} "
            f"resolution={IMAGE_RESOLUTION.lower() if IMAGE_API_PROVIDER == 'toapis' and IMAGE_RESOLUTION else 'provider-default'} "
            f"input_images={len(input_images)} "
            f"input_image_b64={input_image_kb}KB"
        )

        # Decode + pin reference image bytes once. We re-wrap into BytesIO on
        # every attempt so retry sends the full multipart body (a consumed
        # BytesIO would post empty).
        reference_blobs = []
        for index, image_item in enumerate(input_images):
            if not isinstance(image_item, dict):
                continue
            prepare_start = time.perf_counter()
            image_bytes, mime = decode_reference_image(image_item)
            if len(image_bytes) > MAX_IMAGE_BYTES:
                raise GenerationError(413, "单张图片过大，请压缩后重试")
            ext = file_ext_for_mime(mime)
            reference_blobs.append((index, image_bytes, mime, ext))
            print(
                f"[TIMING {req_id}] reference_prepare={elapsed_ms(prepare_start)}ms "
                f"bytes={round(len(image_bytes) / 1024, 1)}KB mime={mime}"
            )
    except GenerationError:
        raise
    except Exception as e:
        print(f"[ERROR {req_id}] generation setup: {e}")
        raise GenerationError(400, "请求处理失败，请重试")

    # === upstream call with retry on transient failures ===
    reference_urls = []
    upstream_task_id = None
    poll_start = None
    result = None
    for attempt in range(1, GENERATE_MAX_ATTEMPTS + 1):
        try:
            # Once an async task exists, retry its polling/download rather than
            # submitting (and potentially billing) a second generation.
            api_start = time.perf_counter()
            if upstream_task_id is not None or (result is not None and extract_image_item(result)):
                resp = None
            elif IMAGE_API_PROVIDER == "toapis":
                resp = _submit_toapis_generation(final_prompt, output_size, reference_blobs, reference_urls)
            elif reference_blobs:
                reference_files = [
                    ("image[]" if len(reference_blobs) > 1 else "image",
                     (f"reference-{idx + 1}.{ext}", io.BytesIO(buf), mime))
                    for idx, buf, mime, ext in reference_blobs
                ]
                edit_data = {
                    "model": MODEL,
                    "prompt": final_prompt,
                    "n": "1",
                    "quality": "high",
                    "response_format": "b64_json",
                }
                if IMAGE_SIZE_ENABLED:
                    edit_data["size"] = output_size
                if not MODEL.startswith("gpt-image-2"):
                    edit_data["input_fidelity"] = "high"
                api_start = time.perf_counter()
                resp = SESSION.post(
                    f"{API_BASE}/images/edits",
                    data=edit_data,
                    files=reference_files,
                    timeout=600,
                )
            else:
                generation_data = {
                    "model": MODEL,
                    "prompt": final_prompt,
                    "n": 1,
                    "quality": "high",
                    "response_format": "b64_json",
                }
                if IMAGE_SIZE_ENABLED:
                    generation_data["size"] = output_size
                api_start = time.perf_counter()
                resp = SESSION.post(
                    f"{API_BASE}/images/generations",
                    json=generation_data,
                    timeout=600,
                )
            if resp is not None:
                api_ms = elapsed_ms(api_start)
                print(f"[TIMING {req_id}] attempt={attempt} upstream_post={api_ms}ms status={resp.status_code} response={round(len(resp.content) / 1024, 1)}KB")
                if resp.status_code >= 400:
                    print(f"[WARN {req_id}] upstream_error={image_upstream_error_details(resp)}")
                resp.raise_for_status()
                result = resp.json()
                if result.get("id") and result.get("status") in {"pending", "queued", "in_progress", "completed"}:
                    upstream_task_id = result["id"]
                    poll_start = time.perf_counter()

            if result.get("status") == "failed":
                raise GenerationError(502, FRIENDLY_GEN_ERROR)

            if upstream_task_id and result.get("status") != "completed":
                while elapsed_ms(poll_start) < 300000:
                    time.sleep(5 + secrets.randbelow(1000) / 1000 if IMAGE_API_PROVIDER == "toapis" else 2)
                    task_resp = SESSION.get(f"{API_BASE}/images/generations/{upstream_task_id}", timeout=30)
                    if task_resp.status_code in {429, 503}:
                        try:
                            delay = float(task_resp.headers.get("Retry-After", "10"))
                        except (TypeError, ValueError):
                            delay = 10
                        time.sleep(max(1, min(delay, 60)))
                        continue
                    task_resp.raise_for_status()
                    result = task_resp.json()
                    status = result.get("status")
                    print(f"[TIMING {req_id}] task_poll={elapsed_ms(poll_start)}ms status={status}")
                    if status == "completed":
                        break
                    if status == "failed":
                        raise GenerationError(502, FRIENDLY_GEN_ERROR)
                else:
                    raise GenerationError(504, FRIENDLY_TIMEOUT_ERROR)

            img_data = None
            item = extract_image_item(result)
            if item:
                if isinstance(item, str):
                    item = {"url": item}
                if item.get("b64_json"):
                    img_data = item["b64_json"]
                elif item.get("url"):
                    image_fetch_start = time.perf_counter()
                    # CDN URLs are public/signed; never forward the API key.
                    img_resp = req_lib.get(item["url"], timeout=60)
                    img_resp.raise_for_status()
                    img_data = base64.b64encode(img_resp.content).decode("ascii")
                    print(f"[TIMING {req_id}] image_fetch={elapsed_ms(image_fetch_start)}ms fetched={round(len(img_resp.content) / 1024, 1)}KB")

            if not img_data:
                # No data in response — treat as transient and retry once.
                raise RuntimeError("Empty response from upstream")

            print(f"[TIMING {req_id}] success attempt={attempt} total={elapsed_ms(total_start)}ms output_b64={kb_size(img_data)}KB")
            return img_data, brief

        except GenerationError:
            raise

        except (req_lib.exceptions.ConnectionError,
                req_lib.exceptions.ChunkedEncodingError) as e:
            # Transient network/proxy issues — always worth retrying once.
            if attempt < GENERATE_MAX_ATTEMPTS:
                print(f"[RETRY {req_id}] attempt {attempt} connection issue, sleeping 2s: {type(e).__name__}")
                time.sleep(2)
                continue
            print(f"[FAIL {req_id}] all attempts failed (connection): {type(e).__name__} total={elapsed_ms(total_start)}ms")
            raise GenerationError(502, FRIENDLY_CONNECTION_ERROR)

        except req_lib.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", 500)
            # 5xx upstream errors are usually transient; 4xx is the client's fault.
            if 500 <= status < 600 and attempt < GENERATE_MAX_ATTEMPTS:
                print(f"[RETRY {req_id}] attempt {attempt} HTTP {status}, sleeping 2s")
                time.sleep(2)
                continue
            print(f"[FAIL {req_id}] HTTP {status} total={elapsed_ms(total_start)}ms")
            raise GenerationError(status, FRIENDLY_HTTP_MESSAGES.get(status, FRIENDLY_GEN_ERROR))

        except req_lib.exceptions.Timeout:
            # Already waited up to 600s — retrying just doubles the pain.
            print(f"[FAIL {req_id}] timeout total={elapsed_ms(total_start)}ms")
            raise GenerationError(504, FRIENDLY_TIMEOUT_ERROR)

        except Exception as e:
            # Empty response, parse error, RuntimeError — try once more.
            if attempt < GENERATE_MAX_ATTEMPTS:
                print(f"[RETRY {req_id}] attempt {attempt} unexpected error, sleeping 2s: {type(e).__name__}: {e}")
                time.sleep(2)
                continue
            print(f"[FAIL {req_id}] {type(e).__name__}: {e} total={elapsed_ms(total_start)}ms")
            raise GenerationError(500, FRIENDLY_GEN_ERROR)


@app.post("/api/generate")
def api_generate(request: Request):
    """Synchronous generation — kept as a fallback; the web client now uses
    the async job endpoints below."""
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})

    req_id = uuid.uuid4().hex[:8]
    params = _parse_generate_request(request, req_id)
    if isinstance(params, JSONResponse):
        return params
    try:
        img_data, brief = _run_generation(req_id, params)
    except GenerationError as e:
        _refund_on_failure(params)
        return JSONResponse(status_code=e.status_code, content={"error": e.message})
    return JSONResponse(content={"image": img_data, "brief": brief})


@app.post("/api/generate-async")
def api_generate_async(request: Request):
    """Submit a generation job and return a job id immediately.

    The generation runs in a daemon thread. Account jobs land in Postgres with
    an owner and atomic charge ledger; legacy/admin jobs use local SQLite. The
    client can background, lock, or reload and later poll /api/generate-result.
    """
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})

    req_id = uuid.uuid4().hex[:8]
    params = _parse_generate_request(request, req_id, defer_account_charge=True)
    if isinstance(params, JSONResponse):
        return params

    job_id = uuid.uuid4().hex
    try:
        _create_async_job(job_id, params)
    except InsufficientCredits:
        return JSONResponse(
            status_code=402,
            content={"error": "额度不足，请充值", "code": "NO_CREDITS"},
        )
    except Exception as e:
        # Account credits and the durable job are one Postgres transaction, so
        # a failed insert rolls the charge back. Legacy IP quota was consumed
        # during parsing and still needs an explicit refund here.
        print(f"[ERROR {req_id}] create_gen_job failed: {type(e).__name__}: {e}")
        if not _uses_durable_account_job(params):
            _refund_on_failure(params)
        return JSONResponse(status_code=500, content={"error": FRIENDLY_GEN_ERROR})

    def _worker():
        try:
            img_data, brief = _run_generation(req_id, params)
            _finish_async_job(job_id, params, image=img_data, brief=brief)
        except GenerationError as e:
            try:
                _fail_async_job(job_id, params, e.message)
            except Exception as finish_error:
                print(
                    f"[ERROR {req_id}] could not persist failed job {job_id}: "
                    f"{type(finish_error).__name__}: {finish_error}"
                )
        except Exception as e:
            # Belt and braces — never leave a job pending forever.
            print(f"[ERROR {req_id}] job {job_id} crashed: {type(e).__name__}: {e}")
            try:
                _fail_async_job(job_id, params, FRIENDLY_GEN_ERROR)
            except Exception as finish_error:
                print(
                    f"[ERROR {req_id}] could not persist crashed job {job_id}: "
                    f"{type(finish_error).__name__}: {finish_error}"
                )

    try:
        threading.Thread(target=_worker, daemon=True, name=f"gen-{req_id}").start()
    except Exception as e:
        print(f"[ERROR {req_id}] thread start failed: {type(e).__name__}: {e}")
        try:
            _fail_async_job(job_id, params, FRIENDLY_GEN_ERROR)
        except Exception as finish_error:
            print(
                f"[ERROR {req_id}] could not persist unstarted job {job_id}: "
                f"{type(finish_error).__name__}: {finish_error}"
            )
        return JSONResponse(status_code=500, content={"error": FRIENDLY_GEN_ERROR})
    print(f"[JOB {req_id}] submitted job={job_id}")
    return JSONResponse(content={"job_id": job_id, "remaining": _quota_remaining(params)})


@app.get("/api/generate-result/{job_id}")
def api_generate_result(job_id: str, request: Request):
    """Poll a generation job. Job states come back as 200 so the client can
    tell them apart from transport errors. Account jobs are owner-scoped and
    durable; unknown/expired legacy jobs report not_found."""
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})

    try:
        job = _get_async_job(job_id, request)
    except Exception as e:
        print(f"[ERROR] generation result lookup failed: {type(e).__name__}: {e}")
        return JSONResponse(status_code=503, content={"error": "服务暂时不可用，请稍后重试"})
    if not job:
        return JSONResponse(content={"status": "not_found"})
    if job["status"] == "done":
        return JSONResponse(content={"status": "done", "image": job["image"], "brief": job.get("brief") or ""})
    if job["status"] == "error":
        return JSONResponse(content={"status": "error", "error": job["error"] or FRIENDLY_GEN_ERROR})
    return JSONResponse(content={"status": "pending"})


@app.get("/api/quota")
def api_quota(request: Request):
    """Remaining generations for this client. remaining=-1 → unlimited.
    In accounts mode this reports the logged-in user's credit balance."""
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "未登录"})
    if ACCOUNTS_ENABLED:
        if _is_admin(request):
            return {"limit": -1, "remaining": -1}
        uid = _get_account_user_id(request)
        if uid is None:
            return JSONResponse(status_code=401, content={"error": "未登录"})
        return {"limit": -1, "remaining": userdb.get_credits(uid)}
    client_ip = get_client_ip(request)
    peer = request.client.host if request.client else ""
    is_local = peer in {"127.0.0.1", "::1"} and not RUNNING_IN_PRODUCTION
    if is_local or _is_admin(request):
        return {"limit": DEFAULT_LIMIT, "remaining": -1}
    remaining = db.rate_limit_remaining(f"ip_{client_ip}", DEFAULT_LIMIT, TIME_WINDOW)
    return {"limit": DEFAULT_LIMIT, "remaining": remaining}


# ---- Static file serving ----
# Serve only the files the frontend needs. Do not mount the project root:
# that would expose .env, aid.db, source files, and other runtime artifacts.
_static_dir = os.path.dirname(os.path.abspath(__file__))
_styles_dir = os.path.join(_static_dir, LOCAL_UPLOAD_DIR, "styles")


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def serve_index():
    return FileResponse(os.path.join(_static_dir, "index.html"))


@app.get("/app.js", include_in_schema=False)
async def serve_app_js():
    return FileResponse(os.path.join(_static_dir, "app.js"), media_type="application/javascript")


@app.get("/style.css", include_in_schema=False)
async def serve_style_css():
    return FileResponse(os.path.join(_static_dir, "style.css"), media_type="text/css")


app.mount(
    "/reference_uploads/styles",
    StaticFiles(directory=_styles_dir),
    name="style_images",
)


# ---- Entry point ----
if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, log_level="info")

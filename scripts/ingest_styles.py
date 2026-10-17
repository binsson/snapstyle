#!/usr/bin/env python3
"""Ingest local style images into the style asset system.

Default workflow:
  1. Put new inspiration references in reference_uploads/styles_inbox/
  2. Run: python3 scripts/ingest_styles.py
  3. The script extracts styleSpec and writes style assets to SQLite.

Watch workflow:
  python3 scripts/ingest_styles.py --watch

The script is intentionally local/admin-only. End users never upload style
assets through the app UI.
"""

import argparse
import base64
import io
import mimetypes
import os
import sys
import threading
import time
import uuid
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

MISSING_API_KEY = not os.getenv("API_KEY")
if MISSING_API_KEY:
    os.environ["API_KEY"] = "__missing_api_key_for_dry_run__"
MISSING_API_BASE = not os.getenv("API_BASE", "").strip()
if MISSING_API_BASE:
    os.environ["API_BASE"] = "https://unconfigured.example.invalid/v1"

import server  # noqa: E402


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
DEFAULT_SOURCE_DIR = ROOT / "reference_uploads" / "styles_inbox"

# Compression settings — applied once at ingest time. The compressed JPEG
# becomes the canonical asset: used both for the LLM styleSpec extraction
# call and as the frontend styleRefUrl.
COMPRESS_MAX_SIDE = 1024
COMPRESS_QUALITY = 85


def compress_image_bytes(src_path):
    """Return (jpeg_bytes, mime) for the compressed image."""
    with Image.open(src_path) as im:
        im = im.convert("RGB")
        im.thumbnail((COMPRESS_MAX_SIDE, COMPRESS_MAX_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=COMPRESS_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"


def image_payload_from_bytes(jpeg_bytes, mime):
    data = base64.b64encode(jpeg_bytes).decode("ascii")
    return {"type": "base64", "mime": mime, "data": data}


def public_style_path(path):
    rel = path.relative_to(ROOT)
    return "/" + rel.as_posix()


def existing_source_keys(assets):
    keys = set()
    for asset in assets:
        source = asset.get("sourcePath") or asset.get("sourceName") or ""
        if source:
            keys.add(source)
    return keys


def safe_filename(stem_path, style_dir, ext=".jpg"):
    stem = re.sub(r"[^a-zA-Z0-9]+", "-", stem_path.stem).strip("-").lower() or "style-ref"
    candidate = f"{stem}{ext}"
    index = 2
    while (style_dir / candidate).exists():
        candidate = f"{stem}-{index:02d}{ext}"
        index += 1
    return candidate


def write_compressed_to_style_store(jpeg_bytes, src_path):
    style_dir = ROOT / server.STYLE_UPLOAD_DIR
    filename = safe_filename(src_path, style_dir, ext=".jpg")
    dest = style_dir / filename
    style_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(jpeg_bytes)
    return public_style_path(dest)


def iter_images(source_dir):
    if not source_dir.exists():
        return []
    return sorted(
        p for p in source_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def ingest_image(path, existing_keys, args):
    """Analyze one image. Returns the new asset dict (or None to skip)."""
    source_key = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    if source_key in existing_keys and not args.force:
        print(f"[SKIP] already ingested: {source_key}")
        return None

    req_id = f"ingest-{uuid.uuid4().hex[:6]}"
    print(f"[INGEST] {source_key}")

    if args.dry_run:
        print("  dry-run: would compress and analyze")
        return None

    jpeg_bytes, mime = compress_image_bytes(path)
    payload = image_payload_from_bytes(jpeg_bytes, mime)
    print(f"  compressed: {len(jpeg_bytes) / 1024:.0f} KB")

    last_error = None
    for attempt in range(1, max(1, args.retries) + 1):
        try:
            style_spec = server.analyze_style_image(payload, req_id)
            break
        except Exception as e:
            last_error = e
            if attempt >= max(1, args.retries):
                raise
            print(f"  retry {attempt}/{args.retries} after error: {e}")
            time.sleep(max(1, args.retry_delay))
    else:
        raise last_error
    image_path = write_compressed_to_style_store(jpeg_bytes, path)
    asset = {
        "id": f"asset_{uuid.uuid4().hex[:10]}",
        "image": image_path,
        "mime": mime,
        "styleSpec": style_spec,
        "qualityScore": 0,
        "hidden": False,
        "sourcePath": source_key,
        "sourceName": path.name,
        "createdAt": int(time.time()),
    }
    print(f"  -> {asset['id']}")
    return asset


def process_source(source_dir, args):
    images = iter_images(source_dir)
    if not images:
        print(f"No images found in {source_dir}")
        return False, 0
    if MISSING_API_KEY and not args.dry_run:
        print("API_KEY is required to analyze style images. Set API_KEY or run with --dry-run.")
        raise SystemExit(2)
    if MISSING_API_BASE and not args.dry_run:
        print("API_BASE is required to analyze style images. Set API_BASE or run with --dry-run.")
        raise SystemExit(2)

    if args.limit and args.limit > 0:
        images = images[:args.limit]
        print(f"--limit {args.limit}: processing first {len(images)} image(s) only")

    assets = server.db.load_style_assets()
    before_assets = len(assets)
    existing_keys = existing_source_keys(assets)
    save_lock = threading.Lock()

    def worker(path):
        try:
            asset = ingest_image(path, existing_keys, args)
        except Exception as e:
            print(f"[FAIL] {path}: {e}")
            return False, True
        if asset is None:
            return False, False
        with save_lock:
            assets.append(asset)
            existing_keys.add(asset.get("sourcePath") or asset.get("sourceName") or "")
            if not args.dry_run:
                server.db.save_style_assets(assets)
        return True, False

    changed = False
    failed = 0
    if args.concurrency <= 1:
        for path in images:
            item_changed, did_fail = worker(path)
            changed = item_changed or changed
            if did_fail:
                failed += 1
    else:
        print(f"Concurrency: {args.concurrency} workers")
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(worker, p): p for p in images}
            for fut in as_completed(futures):
                item_changed, did_fail = fut.result()
                changed = item_changed or changed
                if did_fail:
                    failed += 1

    print(f"Done. assets: {before_assets} -> {len(assets)}, failed: {failed}")
    return changed, len(assets)


def main():
    parser = argparse.ArgumentParser(description="Ingest local style images into style assets.")
    parser.add_argument(
        "source",
        nargs="?",
        default=str(DEFAULT_SOURCE_DIR),
        help="Directory of style images to ingest. Default: reference_uploads/styles_inbox",
    )
    parser.add_argument("--force", action="store_true", help="Re-ingest files even if sourcePath already exists.")
    parser.add_argument("--dry-run", action="store_true", help="List work without calling the model or writing files.")
    parser.add_argument("--watch", action="store_true", help="Continuously scan the source directory for new images.")
    parser.add_argument("--interval", type=int, default=20, help="Watch polling interval in seconds. Default: 20.")
    parser.add_argument("--retries", type=int, default=3, help="Retries per image when the analysis API fails. Default: 3.")
    parser.add_argument("--retry-delay", type=int, default=3, help="Seconds between retries. Default: 3.")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N images (0 = all). Useful for smoke-testing.")
    parser.add_argument("--concurrency", type=int, default=1, help="Number of parallel workers. Default: 1 (serial).")
    args = parser.parse_args()

    source_dir = Path(args.source).expanduser()
    if not source_dir.is_absolute():
        source_dir = ROOT / source_dir

    if args.watch:
        print(f"Watching {source_dir} every {args.interval}s. Ctrl+C to stop.")
        while True:
            process_source(source_dir, args)
            time.sleep(max(2, args.interval))
    process_source(source_dir, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

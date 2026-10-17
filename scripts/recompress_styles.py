#!/usr/bin/env python3
"""Compress already-ingested style assets in place.

Use this once to migrate PNG-era assets (pre-compression) to the new JPEG
pipeline. Skips LLM calls — styleSpec is reused as-is.

For each asset:
  1. Load the current image from disk
  2. Resize to COMPRESS_MAX_SIDE longest edge, save as JPEG quality 85
  3. Write to <stem>.jpg in the same directory
  4. Delete the old file if its extension changed
  5. Update asset.image and asset.mime in the DB

Usage:
    python3 scripts/recompress_styles.py [--dry-run]
"""

import argparse
import io
import os
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

MAX_SIDE = 1024
QUALITY = 85


def main():
    parser = argparse.ArgumentParser(description="Recompress already-ingested style images in place.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    assets = db.load_style_assets()
    changed = 0
    skipped = 0
    failed = 0

    for asset in assets:
        image_path = asset.get("image", "")
        if not image_path.startswith("/"):
            continue
        rel = image_path.lstrip("/")
        src = ROOT / rel
        if not src.exists():
            print(f"[MISSING] {rel}")
            failed += 1
            continue

        # Read original size and skip if already small JPEG
        try:
            with Image.open(src) as im:
                w, h = im.size
                already_small = max(w, h) <= MAX_SIDE
                fmt_ok = im.format == "JPEG"
        except Exception as e:
            print(f"[FAIL] {rel}: {e}")
            failed += 1
            continue

        if already_small and fmt_ok and src.suffix.lower() in {".jpg", ".jpeg"}:
            skipped += 1
            continue

        # Compress
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=QUALITY, optimize=True)
                jpeg_bytes = buf.getvalue()
        except Exception as e:
            print(f"[FAIL compress] {rel}: {e}")
            failed += 1
            continue

        new_path = src.with_suffix(".jpg")
        old_size = src.stat().st_size
        if args.dry_run:
            print(f"[DRY] {rel}  {old_size/1024:.0f}KB -> {len(jpeg_bytes)/1024:.0f}KB")
            continue

        new_path.write_bytes(jpeg_bytes)
        if new_path != src:
            src.unlink()
        new_rel = "/" + str(new_path.relative_to(ROOT)).replace(os.sep, "/")
        asset["image"] = new_rel
        asset["mime"] = "image/jpeg"
        changed += 1
        print(f"[OK]  {rel}  {old_size/1024:.0f}KB -> {len(jpeg_bytes)/1024:.0f}KB  ({new_rel})")

    if not args.dry_run and changed:
        db.save_style_assets(assets)
        print(f"\nSaved DB. changed={changed} skipped={skipped} failed={failed}")
    else:
        print(f"\nDone. changed={changed} skipped={skipped} failed={failed}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

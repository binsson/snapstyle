#!/usr/bin/env python3
"""Backfill chineseLabel onto existing style assets.

For every asset whose styleSpec.chineseLabel is missing/empty, call the LLM
with the (already compressed) image and ask for a 3-5 Chinese character
label only — much cheaper than re-running the full styleSpec extraction.

Usage:
    python3 scripts/relabel_styles.py [--concurrency 2] [--limit N] [--force]
"""

import argparse
import base64
import io
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests as req_lib
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
import db  # noqa: E402

LABEL_SYSTEM = (
    "You are labeling a commercial photography reference image for a Chinese "
    "e-commerce style library.\n\n"
    "Look at the image and output a single 3-5 Chinese character tag that "
    "captures this image's most distinctive style flavor. Use commercial "
    "photography / fashion vocabulary.\n\n"
    "Good examples (varied, distinctive):\n"
    "  美式学院 / 奶油慵懒 / 雾霾高级 / 国潮新中式 / 暗黑酷感 / 街头活力 / "
    "田园治愈 / 法式优雅 / 日系治愈 / 港风复古 / 极简白底 / Y2K复古 / "
    "运动学院 / 暖调编辑 / 户外自然 / 都市街拍\n\n"
    "Avoid generic labels like 棚拍上身, 模特上身, 半身上身 — these describe "
    "the shot type, not the style flavor.\n\n"
    "Output ONLY the 3-5 character label. No quotes, no punctuation, no "
    "explanation, no prefix."
)

# Strip anything that's not Chinese, latin alphanumeric, or simple connectors
_CLEAN_RE = re.compile(r"[^一-鿿\w\-\s]")


def clean_label(raw):
    text = _CLEAN_RE.sub("", (raw or "")).strip()
    return text[:8]


def label_image(image_path, req_id):
    """Call LLM on a local image file, return cleaned label."""
    src = ROOT / image_path.lstrip("/")
    with Image.open(src) as im:
        im = im.convert("RGB")
        # Image is already compressed to 1024 by ingest, but downsize again
        # defensively in case of legacy assets.
        im.thumbnail((1024, 1024), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        jpeg = buf.getvalue()
    b64 = base64.b64encode(jpeg).decode("ascii")
    url = f"data:image/jpeg;base64,{b64}"

    resp = req_lib.post(
        f"{server.REWRITE_API_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {server.REWRITE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": server.REWRITE_MODEL,
            "messages": [
                {"role": "system", "content": LABEL_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": "Label this image."},
                    {"type": "image_url", "image_url": {"url": url}},
                ]},
            ],
            "reasoning_effort": "none",
            "max_completion_tokens": 256,
        },
        timeout=60,
    )
    resp.raise_for_status()
    raw = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    label = clean_label(raw)
    print(f"[OK {req_id}] {label!r}  ({src.name})")
    return label


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true",
                        help="Re-label assets that already have a chineseLabel.")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=int, default=3)
    args = parser.parse_args()

    if not os.getenv("API_KEY"):
        print("API_KEY is required.")
        return 2

    assets = db.load_style_assets()
    todo = []
    for a in assets:
        spec = a.get("styleSpec") or {}
        if not args.force and (spec.get("chineseLabel") or "").strip():
            continue
        todo.append(a)

    if args.limit > 0:
        todo = todo[:args.limit]
        print(f"--limit {args.limit}: relabeling first {len(todo)} of {len(assets)} assets")
    else:
        print(f"Relabeling {len(todo)} of {len(assets)} assets (skipped: {len(assets) - len(todo)})")

    save_lock = threading.Lock()
    failed = 0

    def worker(asset):
        req_id = f"label-{uuid.uuid4().hex[:6]}"
        last_err = None
        for attempt in range(1, max(1, args.retries) + 1):
            try:
                label = label_image(asset["image"], req_id)
                if not label:
                    raise ValueError("empty label")
                with save_lock:
                    spec = asset.get("styleSpec") or {}
                    spec["chineseLabel"] = label
                    asset["styleSpec"] = spec
                    db.save_style_assets(assets)
                return True
            except Exception as e:
                last_err = e
                if attempt >= max(1, args.retries):
                    print(f"[FAIL] {asset.get('image')}: {e}")
                    return False
                print(f"  retry {attempt}/{args.retries}: {e}")
                time.sleep(args.retry_delay)
        return False

    if args.concurrency <= 1:
        for a in todo:
            if not worker(a):
                failed += 1
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(worker, a) for a in todo]
            for fut in as_completed(futures):
                if not fut.result():
                    failed += 1

    labeled = sum(1 for a in assets if (a.get("styleSpec") or {}).get("chineseLabel"))
    print(f"\nDone. labeled: {labeled}/{len(assets)}, failed this run: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate 400px-wide WebP thumbnails for every style reference image.

Gallery cards render at ~170-229px, but the source JPEGs are ~700px wide
(~118KB each). A 400px WebP (~28KB) fully covers a 2x-retina phone card and
cuts the first-screen download ~5x. We commit the thumbnails into the repo so
each deployment includes them without generating files at runtime.

Thumbnail path = original path with the extension replaced by ".t400.webp",
e.g. foo.jpg -> foo.t400.webp, kept next to the original. The server exposes it
as `thumbImage` in /api/style-assets; the original stays the model reference.

Run:  python3 scripts/gen_thumbnails.py        # only missing/stale
      python3 scripts/gen_thumbnails.py --force # regenerate all
"""
import os
import sys
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLES_DIR = os.path.join(ROOT, "reference_uploads", "styles")
THUMB_WIDTH = 400
QUALITY = 80
SRC_EXTS = (".jpg", ".jpeg", ".png")


def thumb_path(src_path):
    base, _ = os.path.splitext(src_path)
    return base + ".t400.webp"


def is_stale(src_path, dst_path):
    if not os.path.exists(dst_path):
        return True
    return os.path.getmtime(src_path) > os.path.getmtime(dst_path)


def main():
    force = "--force" in sys.argv
    if not os.path.isdir(STYLES_DIR):
        print(f"no styles dir: {STYLES_DIR}")
        return
    made = skipped = failed = 0
    src_total = dst_total = 0
    for dirpath, _dirs, files in os.walk(STYLES_DIR):
        for fn in files:
            low = fn.lower()
            if not low.endswith(SRC_EXTS) or low.endswith(".t400.webp"):
                continue
            src = os.path.join(dirpath, fn)
            dst = thumb_path(src)
            if not force and not is_stale(src, dst):
                skipped += 1
                src_total += os.path.getsize(src)
                dst_total += os.path.getsize(dst)
                continue
            try:
                with Image.open(src) as im:
                    im = im.convert("RGB")
                    w, h = im.size
                    if w > THUMB_WIDTH:
                        im = im.resize((THUMB_WIDTH, round(h * THUMB_WIDTH / w)), Image.LANCZOS)
                    im.save(dst, "WEBP", quality=QUALITY, method=6)
                made += 1
                src_total += os.path.getsize(src)
                dst_total += os.path.getsize(dst)
            except Exception as e:
                failed += 1
                print(f"FAIL {src}: {type(e).__name__}: {e}")
    print(
        f"thumbnails: made={made} skipped={skipped} failed={failed} | "
        f"src={src_total/1e6:.1f}MB -> thumbs={dst_total/1e6:.1f}MB "
        f"({(src_total/dst_total):.1f}x smaller)" if dst_total else "no output"
    )


if __name__ == "__main__":
    main()

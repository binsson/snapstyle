#!/usr/bin/env python3
"""Export the SQLite style_assets table to style_assets.json.

The JSON file is the source of truth shipped with the repo. Each deployment (including a
fresh checkout) hydrates SQLite from it on startup via db.migrate_from_json.

Usage:
    python3 scripts/export_styles.py

Run this after `ingest_styles.py` and commit the resulting JSON + new images
under reference_uploads/styles/.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

OUT = ROOT / "style_assets.json"


def main():
    assets = db.load_style_assets()
    OUT.write_text(json.dumps(assets, ensure_ascii=False, indent=2), encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"Exported {len(assets)} assets → {OUT.name} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

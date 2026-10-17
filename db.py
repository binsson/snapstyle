"""SQLite-backed storage for AI Designer.

Replaces the old JSON-file approach (usage_db.json, style_assets.json) with
a single SQLite database that is:
  - Thread-safe (WAL mode)
  - Atomic (transactions)
  - Persistent across container restarts (if volume is mounted)

Also provides secure session management: login creates a random UUID token
stored server-side; the client never receives the password back.
"""

import json
import os
import sqlite3
import threading
import time
import uuid

DB_PATH = os.getenv("AID_DB_PATH", "aid.db")

# Module-level lock for serialising writes (SQLite WAL handles reads fine
# concurrently, but Python's sqlite3 module is safest with a write lock).
_lock = threading.Lock()


def _connect():
    """Return a new connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist.  Idempotent — safe to call on
    every server startup."""
    with _lock:
        conn = _connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    username   TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rate_limits (
                    identifier TEXT NOT NULL,
                    timestamp  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rate_limits
                    ON rate_limits(identifier, timestamp);

                CREATE TABLE IF NOT EXISTS style_assets (
                    id   TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS favorite_deltas (
                    template_id TEXT PRIMARY KEY,
                    delta       INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS gen_jobs (
                    id         TEXT PRIMARY KEY,
                    status     TEXT NOT NULL,
                    image      TEXT,
                    error      TEXT,
                    brief      TEXT,
                    created_at REAL NOT NULL
                );

                -- Permanently-saved stock reference picks (Pinterest/Pexels).
                -- Kept separate from style_assets, which migrate_from_json wipes
                -- and rehydrates from the shipped style_assets.json on startup.
                CREATE TABLE IF NOT EXISTS stock_library (
                    id         TEXT PRIMARY KEY,
                    data       TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                -- Image URLs already shown/saved, to dedup future searches.
                CREATE TABLE IF NOT EXISTS stock_seen (
                    url        TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                );
            """)
            # Pre-existing local databases created before the brief column.
            try:
                conn.execute("ALTER TABLE gen_jobs ADD COLUMN brief TEXT")
            except sqlite3.OperationalError:
                pass
            conn.commit()
        finally:
            conn.close()


# ---------- Session management ----------

def create_session(username: str, ttl_seconds: int = 604800) -> str:
    """Create a new session and return the random token.

    Args:
        username: the authenticated username.
        ttl_seconds: token lifetime in seconds (default 7 days).

    Returns:
        A random UUID token string.
    """
    token = uuid.uuid4().hex
    now = time.time()
    expires_at = now + ttl_seconds
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO sessions (token, username, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token, username, now, expires_at),
            )
            conn.commit()
        finally:
            conn.close()
    return token


def validate_session(token: str) -> str | None:
    """Return the username for a valid, non-expired token, or None."""
    if not token:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT username, expires_at FROM sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        if time.time() > row["expires_at"]:
            # Expired — delete it
            with _lock:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                conn.commit()
            return None
        return row["username"]
    finally:
        conn.close()


def cleanup_sessions():
    """Delete all expired sessions.  Call periodically (e.g. on startup)."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            conn.commit()
        finally:
            conn.close()


# ---------- Rate limiting ----------

def check_rate_limit(identifier: str, max_requests: int,
                     window_seconds: int = 86400) -> bool:
    """Check whether *identifier* is within its rate limit.

    Returns True if the request is allowed (and records it), False if the
    limit has been reached.
    """
    now = time.time()
    cutoff = now - window_seconds
    with _lock:
        conn = _connect()
        try:
            # Prune old entries for this identifier
            conn.execute(
                "DELETE FROM rate_limits WHERE identifier = ? AND timestamp < ?",
                (identifier, cutoff),
            )
            # Count recent entries
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM rate_limits "
                "WHERE identifier = ? AND timestamp >= ?",
                (identifier, cutoff),
            ).fetchone()
            count = row["cnt"] if row else 0
            if count >= max_requests:
                conn.commit()
                return False
            # Record this request
            conn.execute(
                "INSERT INTO rate_limits (identifier, timestamp) VALUES (?, ?)",
                (identifier, now),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def refund_rate_limit(identifier: str):
    """Remove the most recent recorded request for *identifier*.

    Called when a generation fails — the user shouldn't pay quota for our
    (or the upstream's) failures."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM rate_limits WHERE rowid = ("
                "  SELECT rowid FROM rate_limits WHERE identifier = ? "
                "  ORDER BY timestamp DESC LIMIT 1)",
                (identifier,),
            )
            conn.commit()
        finally:
            conn.close()


def rate_limit_remaining(identifier: str, max_requests: int,
                         window_seconds: int = 86400) -> int:
    """How many requests *identifier* has left in the window (no recording)."""
    cutoff = time.time() - window_seconds
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM rate_limits "
            "WHERE identifier = ? AND timestamp >= ?",
            (identifier, cutoff),
        ).fetchone()
        count = row["cnt"] if row else 0
        return max(0, max_requests - count)
    finally:
        conn.close()


# ---------- Style assets ----------

def load_style_assets() -> list[dict]:
    """Return all style assets as a list of dicts."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT data FROM style_assets ORDER BY rowid").fetchall()
        return [json.loads(r["data"]) for r in rows]
    finally:
        conn.close()


def save_style_assets(assets: list[dict]):
    """Replace all style assets with the given list."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM style_assets")
            for asset in assets:
                asset_id = asset.get("id", uuid.uuid4().hex)
                conn.execute(
                    "INSERT OR REPLACE INTO style_assets (id, data) VALUES (?, ?)",
                    (asset_id, json.dumps(asset, ensure_ascii=False)),
                )
            conn.commit()
        finally:
            conn.close()


def upsert_style_asset(asset: dict):
    """Insert or update a single style asset."""
    asset_id = asset.get("id")
    if not asset_id:
        raise ValueError("asset must have an 'id' field")
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO style_assets (id, data) VALUES (?, ?)",
                (asset_id, json.dumps(asset, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()


# ---------- Stock reference library + seen-dedup ----------

def load_stock_library() -> list[dict]:
    """Permanently-saved stock reference picks, newest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT data FROM stock_library ORDER BY created_at DESC"
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]
    finally:
        conn.close()


def add_stock_library(asset: dict):
    """Insert/replace one saved stock reference (keyed by its id)."""
    asset_id = asset.get("id")
    if not asset_id:
        raise ValueError("asset must have an 'id' field")
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO stock_library (id, data, created_at) VALUES (?, ?, ?)",
                (asset_id, json.dumps(asset, ensure_ascii=False), asset.get("createdAt") or time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def stock_seen_urls() -> set:
    """Image URLs already shown or saved — used to dedup future searches."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT url FROM stock_seen").fetchall()
        return {r["url"] for r in rows}
    finally:
        conn.close()


def mark_stock_seen(urls):
    """Record image URLs as seen (idempotent)."""
    urls = [u for u in urls if u]
    if not urls:
        return
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO stock_seen (url, created_at) VALUES (?, ?)",
                [(u, now) for u in urls],
            )
            conn.commit()
        finally:
            conn.close()


# ---------- Favorite deltas (server-side favorite counter) ----------

def load_favorite_deltas() -> dict:
    """Return {template_id: delta} for all known deltas."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT template_id, delta FROM favorite_deltas").fetchall()
        return {r["template_id"]: r["delta"] for r in rows}
    finally:
        conn.close()


def bump_favorite_delta(template_id: str, change: int) -> int:
    """Increment (or decrement) the delta for *template_id*. Returns new delta."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO favorite_deltas (template_id, delta) VALUES (?, ?) "
                "ON CONFLICT(template_id) DO UPDATE SET delta = delta + excluded.delta",
                (template_id, change),
            )
            row = conn.execute(
                "SELECT delta FROM favorite_deltas WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            conn.commit()
            return row["delta"] if row else change
        finally:
            conn.close()


# ---------- Generation jobs (async background generation) ----------
# Results are polled by the client within minutes; rows older than the
# cleanup window are unreachable (the client gives up polling long before)
# and only waste disk, so they're pruned opportunistically on every create.

GEN_JOB_TTL_SECONDS = 7200  # 2 hours


def create_gen_job(job_id: str):
    """Insert a new pending job, pruning expired ones in the same write."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM gen_jobs WHERE created_at < ?",
                (now - GEN_JOB_TTL_SECONDS,),
            )
            conn.execute(
                "INSERT INTO gen_jobs (id, status, created_at) VALUES (?, 'pending', ?)",
                (job_id, now),
            )
            conn.commit()
        finally:
            conn.close()


def finish_gen_job(job_id: str, image: str | None = None, error: str | None = None,
                   brief: str | None = None):
    """Mark a job done (with base64 image) or errored (with a user-facing message).
    `brief` is the shoot brief used — the client stores it and sends it back on
    换一张 so the next brief can deliberately differ."""
    status = "done" if image else "error"
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE gen_jobs SET status = ?, image = ?, error = ?, brief = ? WHERE id = ?",
                (status, image, error, brief, job_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_gen_job(job_id: str) -> dict | None:
    """Return {id, status, image, error, brief, created_at} or None if unknown/expired."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, status, image, error, brief, created_at FROM gen_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------- One-time migration from JSON files ----------

def migrate_from_json(style_assets_file: str = "style_assets.json"):
    """Hydrate SQLite style assets from the tracked JSON source of truth.

    A fresh deployment may have no SQLite database, while existing installations
    can keep an older aid.db around.  `style_assets.json` is the shipped source of
    truth, so startup syncs the table whenever the JSON content differs.
    """
    if not os.path.exists(style_assets_file):
        return

    try:
        with open(style_assets_file, "r", encoding="utf-8") as f:
            assets = json.load(f)
        if not isinstance(assets, list) or not assets:
            return
        current_assets = load_style_assets()
        current_json = json.dumps(
            current_assets,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        source_json = json.dumps(
            assets,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if current_json != source_json:
            save_style_assets(assets)
            print(f"[db] synced {len(assets)} style assets from {style_assets_file}")
    except Exception as e:
        print(f"[db] style_assets sync failed: {e}")

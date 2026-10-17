"""Postgres-backed accounts + credits for AI Designer.

The original app stored everything in an ephemeral SQLite file (fine for the
shared-account + per-IP rate limit model, where losing the data on a service
restart is harmless). Real user accounts and a paid credit balance MUST NOT be
ephemeral, so they live in a managed Postgres database (Neon).

Activation is controlled purely by the DATABASE_URL env var:
  - DATABASE_URL set   -> accounts mode (email signup + credits)
  - DATABASE_URL unset -> module is dormant; server.py keeps the legacy
    shared-account + per-IP rate-limit behavior.

psycopg is imported lazily so the app still boots without the driver installed
when accounts mode is off.
"""

import hashlib
import hmac
import os
import re
import secrets
import time

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Public registration is closed by default. If it is reopened after payments
# launch, new accounts start at zero unless an operator explicitly restores a
# promotional bonus.
FREE_SIGNUP_CREDITS = int(os.getenv("FREE_SIGNUP_CREDITS", "0"))

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def enabled() -> bool:
    """True when accounts mode should be active (DATABASE_URL configured)."""
    return bool(DATABASE_URL)


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


class EmailExists(Exception):
    """Raised when registering an email that already has an account."""


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------
# Low traffic: a fresh short-lived connection per operation is simpler than a
# pool and never blocks Neon's free-tier autosuspend. Neon can take ~0.5s to
# wake from suspend, so connecting gets a couple of retries.

def _psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg, dict_row
    except ImportError as e:  # pragma: no cover - surfaced clearly at startup
        raise RuntimeError(
            "DATABASE_URL is set but psycopg is not installed. "
            "Run: pip install -r requirements.txt"
        ) from e


def _connect(retries: int = 3):
    psycopg, dict_row = _psycopg()
    last = None
    for attempt in range(retries):
        try:
            return psycopg.connect(
                DATABASE_URL, row_factory=dict_row, connect_timeout=15
            )
        except psycopg.OperationalError as e:
            last = e
            time.sleep(0.6 * (attempt + 1))  # Neon may be waking from suspend
    raise last


def init_db():
    """Create tables if absent. Idempotent — safe on every startup."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id             BIGSERIAL PRIMARY KEY,
                email          TEXT UNIQUE NOT NULL,
                password_hash  TEXT NOT NULL,
                credits        INTEGER NOT NULL DEFAULT 0,
                email_verified BOOLEAN NOT NULL DEFAULT FALSE,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS credit_logs (
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                delta      INTEGER NOT NULL,
                reason     TEXT NOT NULL,
                order_id   BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_credit_logs_user "
            "ON credit_logs(user_id, created_at);"
        )
        # Orders table is created now so the schema is complete; the recharge
        # endpoints that write to it land in a later step (payment provider).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id                BIGSERIAL PRIMARY KEY,
                user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                out_trade_no      TEXT UNIQUE NOT NULL,
                package           TEXT,
                amount_fen        INTEGER NOT NULL,
                credits           INTEGER NOT NULL,
                status            TEXT NOT NULL DEFAULT 'pending',
                provider          TEXT,
                provider_trade_no TEXT,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                paid_at           TIMESTAMPTZ
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_user "
            "ON orders(user_id, created_at);"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_codes (
                email      TEXT NOT NULL,
                code       TEXT NOT NULL,
                purpose    TEXT NOT NULL DEFAULT 'verify',
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_codes "
            "ON email_codes(email, purpose);"
        )
        # Persistent auth throttling. Keeping this in Postgres (rather than an
        # in-process dict) means a service restart cannot reset a brute-force or
        # mass-registration window.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_rate_events (
                id              BIGSERIAL PRIMARY KEY,
                action          TEXT NOT NULL,
                identifier_hash TEXT NOT NULL,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_rate_events_lookup "
            "ON auth_rate_events(action, identifier_hash, created_at);"
        )
        # Account-owned generation jobs live in Postgres so a service deploy or
        # instance restart cannot lose the charge ledger and strand a user's
        # deducted credit. The image is kept here temporarily; moving large
        # outputs to object storage is the next infrastructure step.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generation_jobs (
                id            TEXT PRIMARY KEY,
                user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status        TEXT NOT NULL DEFAULT 'pending',
                image         TEXT,
                error         TEXT,
                brief         TEXT,
                charge_amount INTEGER NOT NULL DEFAULT 0,
                refunded      BOOLEAN NOT NULL DEFAULT FALSE,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_jobs_user_created "
            "ON generation_jobs(user_id, created_at DESC);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_jobs_status_created "
            "ON generation_jobs(status, created_at);"
        )
    print("[userdb] Postgres schema ready")


# ---------------------------------------------------------------------------
# Password hashing (stdlib scrypt — no extra dependency)
# ---------------------------------------------------------------------------

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(email: str, password: str) -> int:
    """Create an account with the configured initial credits. Raises EmailExists."""
    psycopg, _ = _psycopg()
    pw = hash_password(password)
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO users (email, password_hash, credits) "
                "VALUES (%s, %s, %s) RETURNING id",
                (email, pw, FREE_SIGNUP_CREDITS),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise EmailExists(email) from e
        user_id = row["id"]
        if FREE_SIGNUP_CREDITS:
            conn.execute(
                "INSERT INTO credit_logs (user_id, delta, reason) "
                "VALUES (%s, %s, %s)",
                (user_id, FREE_SIGNUP_CREDITS, "signup_bonus"),
            )
        return user_id


def get_user_by_id(user_id: int) -> dict | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT id, email, credits, email_verified, created_at "
            "FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()


def get_user_by_email(email: str) -> dict | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT id, email, password_hash, credits, email_verified "
            "FROM users WHERE email = %s",
            (email,),
        ).fetchone()


def verify_login(email: str, password: str) -> int | None:
    """Return the user id if email+password match, else None."""
    user = get_user_by_email(email)
    if not user:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user["id"]


# ---------------------------------------------------------------------------
# Persistent auth throttling
# ---------------------------------------------------------------------------

def consume_auth_attempt(action: str, identifier_hash: str, limit: int,
                         window_seconds: int) -> bool:
    """Atomically consume one auth attempt inside a sliding window.

    Returns False without inserting when the limit is already exhausted. A
    Postgres advisory lock serialises concurrent attempts for the same key so
    a burst cannot race past the limit.
    """
    if limit <= 0 or window_seconds <= 0:
        return True
    lock_key = f"{action}:{identifier_hash}"
    with _connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0));",
            (lock_key,),
        )
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM auth_rate_events "
            "WHERE action = %s AND identifier_hash = %s "
            "AND created_at > now() - make_interval(secs => %s)",
            (action, identifier_hash, window_seconds),
        ).fetchone()
        if row and row["count"] >= limit:
            return False
        conn.execute(
            "INSERT INTO auth_rate_events (action, identifier_hash) VALUES (%s, %s)",
            (action, identifier_hash),
        )
        # Opportunistic pruning keeps this tiny without a separate cron job.
        conn.execute(
            "DELETE FROM auth_rate_events WHERE created_at < now() - interval '7 days'"
        )
        return True


def auth_attempt_allowed(action: str, identifier_hash: str, limit: int,
                         window_seconds: int) -> bool:
    """Read the current window without consuming it (used before login)."""
    if limit <= 0 or window_seconds <= 0:
        return True
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM auth_rate_events "
            "WHERE action = %s AND identifier_hash = %s "
            "AND created_at > now() - make_interval(secs => %s)",
            (action, identifier_hash, window_seconds),
        ).fetchone()
        return not row or row["count"] < limit


def clear_auth_attempts(action: str, identifier_hash: str):
    """Clear an account-specific failure window after a successful login."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM auth_rate_events WHERE action = %s AND identifier_hash = %s",
            (action, identifier_hash),
        )


def get_credits(user_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT credits FROM users WHERE id = %s", (user_id,)
        ).fetchone()
        return row["credits"] if row else 0


def is_verified(user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT email_verified FROM users WHERE id = %s", (user_id,)
        ).fetchone()
        return bool(row and row["email_verified"])


# ---------------------------------------------------------------------------
# Credits
# ---------------------------------------------------------------------------

def try_consume_credit(user_id: int, amount: int = 1,
                       reason: str = "generate") -> int | None:
    """Atomically deduct *amount* credits. Returns the new balance, or None if
    the balance was insufficient (the row is only updated when credits >=
    amount, so this is race-safe under concurrent generations)."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE users SET credits = credits - %s "
            "WHERE id = %s AND credits >= %s RETURNING credits",
            (amount, user_id, amount),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "INSERT INTO credit_logs (user_id, delta, reason) VALUES (%s, %s, %s)",
            (user_id, -amount, reason),
        )
        return row["credits"]


def add_credits(user_id: int, amount: int, reason: str,
                order_id: int | None = None) -> int | None:
    """Add credits (recharge / refund). Returns the new balance."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE users SET credits = credits + %s WHERE id = %s RETURNING credits",
            (amount, user_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "INSERT INTO credit_logs (user_id, delta, reason, order_id) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, amount, reason, order_id),
        )
        return row["credits"]


def refund_credit(user_id: int, amount: int = 1) -> int | None:
    """Give credits back when a generation fails."""
    return add_credits(user_id, amount, "refund")


# ---------------------------------------------------------------------------
# Durable generation jobs (accounts mode)
# ---------------------------------------------------------------------------

GEN_JOB_TTL_SECONDS = 7200
GEN_JOB_STALE_SECONDS = 1800


def consume_credit_and_create_generation_job(job_id: str, user_id: int,
                                             charge_amount: int) -> int | None:
    """Atomically charge an account and create its pending generation job.

    Returns the new credit balance, or None when the balance is insufficient.
    If inserting the job fails, the surrounding transaction rolls the credit
    deduction back as well.
    """
    amount = max(0, int(charge_amount))
    # A user starting another generation should immediately recover any old
    # job abandoned by a previous instance, rather than waiting for a restart.
    recover_stale_generation_jobs(user_id=user_id)
    with _connect() as conn:
        conn.execute(
            "DELETE FROM generation_jobs "
            "WHERE status <> 'pending' "
            "AND created_at < now() - make_interval(secs => %s)",
            (GEN_JOB_TTL_SECONDS,),
        )
        balance_row = conn.execute(
            "UPDATE users SET credits = credits - %s "
            "WHERE id = %s AND credits >= %s RETURNING credits",
            (amount, user_id, amount),
        ).fetchone()
        if balance_row is None:
            return None
        conn.execute(
            "INSERT INTO generation_jobs (id, user_id, charge_amount) "
            "VALUES (%s, %s, %s)",
            (job_id, user_id, amount),
        )
        if amount:
            conn.execute(
                "INSERT INTO credit_logs (user_id, delta, reason) VALUES (%s, %s, %s)",
                (user_id, -amount, f"generate:{job_id}"),
            )
        return balance_row["credits"]


def finish_generation_job(job_id: str, image: str, brief: str = "") -> bool:
    """Complete a pending job. Returns False if it was already terminal."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE generation_jobs SET status = 'done', image = %s, brief = %s, "
            "error = NULL, updated_at = now() "
            "WHERE id = %s AND status = 'pending' RETURNING id",
            (image, brief, job_id),
        ).fetchone()
        return bool(row)


def fail_generation_job(job_id: str, error: str) -> bool:
    """Fail a pending job and refund its charge exactly once, atomically."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id, charge_amount, refunded, status "
            "FROM generation_jobs WHERE id = %s FOR UPDATE",
            (job_id,),
        ).fetchone()
        if not row or row["status"] == "done":
            return False
        should_refund = not row["refunded"] and row["charge_amount"] > 0
        if should_refund:
            conn.execute(
                "UPDATE users SET credits = credits + %s WHERE id = %s",
                (row["charge_amount"], row["user_id"]),
            )
            conn.execute(
                "INSERT INTO credit_logs (user_id, delta, reason) VALUES (%s, %s, %s)",
                (row["user_id"], row["charge_amount"], f"generation_refund:{job_id}"),
            )
        conn.execute(
            "UPDATE generation_jobs SET status = 'error', error = %s, image = NULL, "
            "refunded = refunded OR %s, updated_at = now() WHERE id = %s",
            (error, should_refund, job_id),
        )
        return True


def get_generation_job(job_id: str, user_id: int) -> dict | None:
    """Return a job only to its owning account."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, status, image, error, brief, created_at, "
            "(status = 'pending' AND created_at < "
            "now() - make_interval(secs => %s)) AS stale "
            "FROM generation_jobs WHERE id = %s AND user_id = %s",
            (GEN_JOB_STALE_SECONDS, job_id, user_id),
        ).fetchone()
    if row and row["stale"]:
        fail_generation_job(job_id, "任务执行超时，额度已退回，请重新生成")
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, status, image, error, brief, created_at, FALSE AS stale "
                "FROM generation_jobs WHERE id = %s AND user_id = %s",
                (job_id, user_id),
            ).fetchone()
    return row


def recover_stale_generation_jobs(stale_seconds: int = GEN_JOB_STALE_SECONDS,
                                  user_id: int | None = None) -> int:
    """Refund jobs abandoned by a deploy/restart and mark them retryable.

    This runs at startup before new workers exist. The row locks plus refunded
    flag make repeated startup recovery safe.
    """
    recovered = 0
    with _connect() as conn:
        if user_id is None:
            rows = conn.execute(
                "SELECT id, user_id, charge_amount, refunded FROM generation_jobs "
                "WHERE status = 'pending' "
                "AND created_at < now() - make_interval(secs => %s) FOR UPDATE",
                (stale_seconds,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, user_id, charge_amount, refunded FROM generation_jobs "
                "WHERE status = 'pending' AND user_id = %s "
                "AND created_at < now() - make_interval(secs => %s) FOR UPDATE",
                (user_id, stale_seconds),
            ).fetchall()
        for row in rows:
            should_refund = not row["refunded"] and row["charge_amount"] > 0
            if should_refund:
                conn.execute(
                    "UPDATE users SET credits = credits + %s WHERE id = %s",
                    (row["charge_amount"], row["user_id"]),
                )
                conn.execute(
                    "INSERT INTO credit_logs (user_id, delta, reason) VALUES (%s, %s, %s)",
                    (row["user_id"], row["charge_amount"], f"restart_refund:{row['id']}"),
                )
            conn.execute(
                "UPDATE generation_jobs SET status = 'error', "
                "error = %s, refunded = refunded OR %s, updated_at = now() "
                "WHERE id = %s",
                ("任务因服务重启中断，额度已退回，请重新生成", should_refund, row["id"]),
            )
            recovered += 1
        conn.execute(
            "DELETE FROM generation_jobs WHERE status <> 'pending' "
            "AND created_at < now() - make_interval(secs => %s)",
            (GEN_JOB_TTL_SECONDS,),
        )
    return recovered


# ---------------------------------------------------------------------------
# Email verification codes (optional; enforced only when the server sets
# REQUIRE_EMAIL_VERIFICATION). Kept here so the plumbing is ready to switch on.
# ---------------------------------------------------------------------------

CODE_TTL_SECONDS = 600  # 10 minutes


def create_email_code(email: str, purpose: str = "verify") -> str:
    code = f"{secrets.randbelow(1000000):06d}"
    with _connect() as conn:
        # One active code per (email, purpose): clear old ones first.
        conn.execute(
            "DELETE FROM email_codes WHERE email = %s AND purpose = %s",
            (email, purpose),
        )
        conn.execute(
            "INSERT INTO email_codes (email, code, purpose, expires_at) "
            "VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
            (email, code, purpose, CODE_TTL_SECONDS),
        )
    return code


def check_email_code(email: str, code: str, purpose: str = "verify") -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT code FROM email_codes "
            "WHERE email = %s AND purpose = %s AND expires_at > now() "
            "ORDER BY created_at DESC LIMIT 1",
            (email, purpose),
        ).fetchone()
        if not row or not hmac.compare_digest(row["code"], str(code)):
            return False
        conn.execute(
            "DELETE FROM email_codes WHERE email = %s AND purpose = %s",
            (email, purpose),
        )
        return True


def mark_verified(user_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET email_verified = TRUE WHERE id = %s", (user_id,)
        )

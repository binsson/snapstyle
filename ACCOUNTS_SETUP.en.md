# Email accounts and credits

[简体中文](ACCOUNTS_SETUP.md) | **English**

Set `DATABASE_URL` to enable email signup, login, and per-user credit balances.

| Configuration | Behavior |
|---|---|
| `DATABASE_URL` is set | Email accounts with individual credit balances; generation deducts credits. |
| `DATABASE_URL` is empty | Shared-account authentication and daily IP-based limits. |

Public registration is disabled by default, and new accounts receive no free credits.
The purchase-credits button is a placeholder; payment processing is not connected.

## 1. Create a Postgres database

Create a database with a provider such as [Neon](https://neon.tech), choosing a region
appropriate for your users. Copy the connection string from your provider's dashboard.
For deployments that need connection pooling, select the provider's pooled connection.

An example connection string looks like this:

```text
postgresql://user:password@database.example/dbname?sslmode=require
```

The hostname and credentials above are placeholders. Keep the real connection string private.

## 2. Run locally

Follow the [quick start](README.en.md#quick-start) to create a virtual environment and install
dependencies. Copy `.env.example` to `.env` if needed, then edit your private `.env`:

```bash
API_BASE=https://image-provider.example/v1
API_KEY=
DATABASE_URL=postgresql://user:password@database.example/dbname?sslmode=require
REGISTRATION_ENABLED=0
FREE_SIGNUP_CREDITS=0
```

Replace the example endpoints and credentials with your own values. Load the file before starting:

```bash
set -a && source .env && set +a
python3 server.py
```

The startup log should include `Accounts mode ON`. Tables are created automatically on
startup. The interface shows email login; public signup remains disabled until explicitly enabled.

## 3. Configure production accounts

Set these values in your hosting platform's environment settings. For the
[Linux deployment](deploy/DEPLOY.en.md), edit `/etc/ai-designer.env`:

| Variable | Recommended configuration |
|---|---|
| `APP_ENV` | `production` |
| `DATABASE_URL` | Your private Postgres connection string. |
| `REGISTRATION_ENABLED` | `0` until you are ready to accept signups. |
| `FREE_SIGNUP_CREDITS` | `0` unless you intentionally offer an initial allowance. |
| `SESSION_SECRET` | A long random secret; required for production account mode. |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | Configure together if using registration verification. |

Changing `SESSION_SECRET` invalidates existing login tokens. Configuring only one of the
Turnstile keys prevents startup so verification cannot silently remain misconfigured.

Shared `APP_USERNAME` / `APP_PASSWORD` credentials are no longer needed in account mode.
You may configure `ADMIN_USERNAME` / `ADMIN_PASSWORD` for an administrator who bypasses
credit charges and IP quotas. Keep administrator credentials private.

Restart or redeploy after changing environment settings. For the Linux example:

```bash
sudo systemctl restart ai-designer
```

Pushing to GitHub alone does not update a manually managed server. See the
[deployment guide](deploy/DEPLOY.en.md#5-updates-and-verification) for update steps.

## Data model

The following tables are created in Postgres:

| Table | Purpose |
|---|---|
| `users` | Email, scrypt password hash, credit balance, verification status, and creation time. |
| `credit_logs` | Auditable credit changes, including generation charges and refunds. |
| `orders` | Order records reserved for payment integration. |
| `email_codes` | Email verification codes; delivery requires additional integration. |
| `auth_rate_events` | Persistent login and signup throttling across restarts. |
| `generation_jobs` | Account-owned generation jobs, charges, and refund state. |

## Credit behavior

- Public signup is off and the initial credit balance is zero by default.
- Set `REGISTRATION_ENABLED=1` when ready to accept users. Adjust `FREE_SIGNUP_CREDITS` separately for any promotional allowance.
- Each generation deducts `GENERATION_COST` credits, defaulting to `1`. The charge and job creation share a database transaction.
- Failed jobs are refunded atomically. Stale interrupted jobs can also be recovered and refunded during startup, polling, or a subsequent request.
- Administrator accounts bypass credit charges. Local development without account mode can run without a quota.
- An account without sufficient credits receives HTTP `402`, and the interface displays a request to top up.
- Before enabling `REQUIRE_EMAIL_VERIFICATION=1`, implement and configure the email delivery workflow.

## Account endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/register` | Email signup when `REGISTRATION_ENABLED=1`. |
| POST | `/api/login` | Email, shared-account, or administrator login. |
| GET | `/api/account` | Current account details and remaining credits. |
| GET | `/api/quota` | Remaining allowance; account mode uses the credit balance. |

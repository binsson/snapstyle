# Deploy to Render

[简体中文](RENDER.md) | **English**

This guide uses generic examples, without private service identifiers or deployment records.

## 1. Create a Web Service

Connect your repository, choose a deployment branch, and configure:

- Build Command: `pip install -r requirements.txt`
- Start Command: `python3 server.py`

Set `PORT` as required by the hosting platform and provide your own environment settings:

| Variable | Configuration |
|---|---|
| `APP_ENV` | `production` |
| `API_BASE` | Your image API base URL, usually including `/v1`. |
| `API_KEY` | Your image service credential. |
| `IMAGE_API_PROVIDER` | `openai` or `toapis`, matching your provider's interface. |
| `MODEL` | A model supported by your image service. |
| `IMAGE_SIZE_ENABLED` | Defaults to `0`; adjust to your service's capabilities. |
| `REWRITE_API_BASE` | Optional separate guidance endpoint; falls back to `API_BASE`. |
| `REWRITE_API_KEY` | Corresponding guidance credential; falls back to `API_KEY`. |
| `REWRITE_MODEL` | A vision model supported by your guidance service. |
| `DATABASE_URL` | Optional Postgres connection string for account mode. |
| `SESSION_SECRET` | Long random secret; required for production account mode. |
| `APP_USERNAME` / `APP_PASSWORD` | Shared authentication when account mode is disabled. |
| `PINTEREST_SCRAPE` | Defaults to `0`; see the [README](../README.en.md#optional-image-sources) before enabling. |

If guidance is unnecessary, set `REWRITE_ENABLED=0` to use local briefs for existing styles.
Store secrets in the hosting platform's environment settings. Keep screenshots of those
settings, logs, and configuration exports out of the repository.

## 2. Domain and persistence

For a custom domain, add it under Custom Domains, configure the DNS records shown in the
service dashboard, and verify HTTPS. Set `PUBLIC_BASE_URL` to your public HTTPS URL if needed.

Provide persistent storage for runtime databases and user-added images, or adapt the
application to use external databases and object storage. Do not assume local instance
files survive deployment; back up runtime data before updates.

## 3. Verification

Check the HTTPS homepage and `/api/auth-status`, then verify login and the style library.
Run a generation with your own service configuration and inspect the result. Avoid printing
credentials or database connection strings in verification logs.

# SnapStyle

**An AI image product focused on intuitive visual creation and thoughtful user experience.**

SnapStyle explores how image generation can become a useful everyday creative product.
Start with a product photo, browse style references, and create new product photography
through a simple visual workflow.

The focus is on the experience: helping people discover a look, create an image, and
explore variations with clear interactions and useful defaults. Existing image models
power the generation; product design shapes how people use them.

## The experience

- **Start with an image.** Upload a product photo and choose a visual reference without writing a detailed prompt.
- **Discover a style.** Browse the built-in style library or connect optional image sources.
- **Keep creating.** Explore variations and revisit saved results in your personal gallery.

## Where it is heading

SnapStyle is an evolving product exploration. The aim is to build a richer creative
ecosystem around image generation, with more image sources, reusable style collections,
editing workflows, and flexible model integrations.

Contributions in interaction design, visual design, creative workflows, and integrations
are welcome, alongside code improvements. Product feedback and ideas from everyday use
are especially valuable.

## Contact

For collaboration, product feedback, or security reports: [binsson@outlook.com](mailto:binsson@outlook.com).
For general questions and feature requests, please open a [GitHub issue](https://github.com/binsson/snapstyle/issues).

## Quick start

Use Python 3.12 and configure your own image generation service:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set your own API_BASE and API_KEY.
# The .example addresses are placeholders, not working endpoints.
set -a && source .env && set +a
python3 server.py
# Open http://localhost:8080
```

The current interface and built-in style labels are in Chinese. This documentation is in English.

## How it works

```text
Upload a product photo
  → Browse the style cards and choose a reference
  → Generate a new image in the background
  → View saved results in your personal gallery
```

The product photo stays in memory until submitted; refreshing the page clears the upload.
Completed results are stored in the browser's IndexedDB. The main workflow uses visual
references without a text prompt editor. Only the apparel category is currently shown.

When a reference image is available, the optional vision model looks at that reference
and produces one or two sentences about lighting, composition, and mood. The image model
receives both the original product photo and the reference, with brief instructions to
preserve the product. Older, detailed style descriptions are omitted from that path.
Generating another variation does not reuse the previous photography brief.

If the guidance service fails or returns an overly long response, generation falls back
to a short local brief. With the default image settings, no size or resolution parameter
is sent; the requested orientation is described in the prompt, and final dimensions
depend on the provider. The optional `toapis` adapter uses its own upload and task flow.

## Optional image sources

- **Built-in library:** included in the repository and available independently of online search.
- **Pexels:** enabled when you provide your own `PEXELS_API_KEY`. Requests use the official API; the key stays on the server.
- **Pinterest:** an optional gallery-dl integration, disabled by default. Once you have the required authorization, install `pip install -r requirements-pinterest.txt` and set `PINTEREST_SCRAPE=1` in your private environment. Installing the dependency alone does not enable it.

Automated access to Pinterest must comply with its [Terms of Service](https://policy.pinterest.com/en/terms-of-service).
Keep cookies, browser sessions, and proxy credentials out of the repository. The integration
ignores gallery-dl's global configuration. Optional browser session access is configured
locally through `PINTEREST_COOKIES_BROWSER`.

## Configuration

Store real configuration in a private `.env` file or your hosting platform's environment settings.

| Variable | Required | Purpose |
|---|---|---|
| `API_BASE` | Yes | Your image API base URL, usually including `/v1`; no private endpoint is built in. |
| `API_KEY` | Yes | Your image service credential. |
| `IMAGE_API_PROVIDER` | No | `openai` by default; `toapis` selects the alternative provider adapter. |
| `MODEL` | No | Image model identifier; defaults to `gpt-image-2.5-sunburst`. Set a model supported by your provider. |
| `IMAGE_SIZE_ENABLED` | No | Defaults to `0`; set to `1` to send pixel dimensions to the compatible image API. |
| `IMAGE_RESOLUTION` | No | Empty by default; the `toapis` adapter accepts `1k`, `2k`, or `4k`. |
| `REWRITE_API_BASE` | No | Vision and photography guidance endpoint; falls back to `API_BASE`. |
| `REWRITE_API_KEY` | No | Guidance service credential; falls back to `API_KEY`. Set it separately when using another service. |
| `REWRITE_MODEL` | No | Vision model for guidance, style analysis, and labeling; defaults to `gpt-5.6-luna-official`. |
| `REWRITE_ENABLED` | No | Set to `0` to use local briefs for existing styles and disable new reference analysis. |
| `PEXELS_API_KEY` | No | Enables Pexels search. |
| `PINTEREST_SCRAPE` | No | Defaults to `0`; requires explicit opt-in and the optional dependency. |
| `DATABASE_URL` | No | Enables email accounts and per-user credits; see [account setup](ACCOUNTS_SETUP.md). |
| `REGISTRATION_ENABLED` | No | Defaults to `0`; controls public signup. |
| `FREE_SIGNUP_CREDITS` | No | Initial credits for a new account; defaults to `0`. |
| `GENERATION_COST` | No | Credits charged per generation; defaults to `1`. |
| `SESSION_SECRET` | In production account mode | Random signing secret. Changing it invalidates existing sessions. |
| `APP_USERNAME` / `APP_PASSWORD` | For shared-account authentication | Shared login when account mode is disabled. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | No | Administrator account that bypasses credit charges and IP limits. The password falls back to `APP_PASSWORD`. |
| `APP_ENV` | In production | Set to `production` to enable production checks. |
| `PORT` | No | Defaults to `8080`. |
| `PUBLIC_BASE_URL` | No | Your public site URL; inferred from the request when empty. |
| `STYLE_UPLOAD_CATEGORY` | No | Library ingestion subdirectory; defaults to `apparel`. |

All example endpoints are placeholders. Supply compatible services and model identifiers
for your own deployment. Changing a local `.env` does not update a hosted environment.

## Deployment

See the guides for [Linux servers](deploy/DEPLOY.md) and [Render](deploy/RENDER.md).

- Configure API endpoints, credentials, database connections, and account settings through private environment variables.
- Set `APP_ENV=production` and configure authentication. Production account mode also requires a random `SESSION_SECRET`.
- Serve only the frontend files and style image directory. Do not expose the project root as a static directory.
- Keep runtime databases and uploaded images on persistent storage and back them up before deployment updates.

## Project structure

| File | Role |
|---|---|
| `server.py` | FastAPI backend, image service adapters, authentication, and explicit static routes. |
| `db.py` | SQLite storage for legacy sessions, rate limits, styles, favorites, and generation jobs. |
| `userdb.py` | Postgres accounts, credits, authentication throttles, and account-owned generation jobs. |
| `app.js` | Framework-free frontend and browser-side result storage. |
| `index.html` / `style.css` | Application markup and styles. |
| `style_assets.json` | Versioned source of truth for the built-in style library. |
| `reference_uploads/styles/` | Built-in style images and thumbnails. |
| `scripts/ingest_styles.py` | Compress local references, analyze their style, and add them to SQLite. |
| `scripts/relabel_styles.py` | Generate missing Chinese style labels. |
| `scripts/recompress_styles.py` | Recompress existing PNG references as JPEG. |
| `scripts/gen_thumbnails.py` | Generate style card thumbnails. |
| `scripts/export_styles.py` | Export SQLite style assets to `style_assets.json`. |

## API overview

| Route | Purpose |
|---|---|
| `GET /api/auth-status` | Authentication requirements and available image sources. |
| `POST /api/login` | Shared, administrator, or email account login; returns a session token. |
| `POST /api/register` | Email signup when registration is enabled. |
| `GET /api/account` | Current account information and credit balance. |
| `GET /api/style-assets` | Visible style assets. |
| `GET /api/favorite-counts?ids=a,b,c` | Favorite counts, combining the display baseline and user deltas. |
| `POST /api/favorite-toggle` | Apply an add/remove action and return the updated count. |
| `GET /api/stock/search` | Search a configured online image source. |
| `POST /api/stock/use` | Import an online reference into the local library. |
| `POST /api/generate` | Synchronous generation endpoint. |
| `POST /api/generate-async` | Submit a background job and return its ID and remaining allowance. |
| `GET /api/generate-result/{id}` | Poll for `pending`, `done`, `error`, or `not_found`. |
| `GET /api/quota` | Remaining allowance; account mode uses credits. |

## Data and persistence

The application uses local SQLite (`aid.db`) and, optionally, Postgres for email accounts.
SQLite style assets are synchronized from the versioned `style_assets.json` on startup
through `db.migrate_from_json`.

| Data | Storage and behavior |
|---|---|
| Legacy sessions | SQLite; current login flows use signed tokens whose validity depends on signing secrets and expiry. |
| IP rate limits | SQLite; persistence depends on the underlying disk. |
| Built-in styles | JSON and images in Git, loaded into SQLite at startup. |
| Favorite deltas | SQLite; persistence depends on the underlying disk. |
| Imported online references | Local image files and SQLite metadata; excluded from Git. |
| Email accounts and credits | Postgres when `DATABASE_URL` is configured. |
| Account generation jobs | Postgres, including ownership, charges, and refund state. |
| Personal gallery | Browser IndexedDB; stored on the user's device. |

Displayed favorite counts include a generated baseline of 30–280 plus the stored user
delta. They are not a count of distinct users.

## Maintaining the style library

```text
Add reference images to reference_uploads/styles_inbox/
  → python3 scripts/ingest_styles.py --concurrency 2
  → Compress to JPEG, up to 1024 px on the longest side
  → Analyze the style and store metadata in SQLite
  → Copy images to reference_uploads/styles/<category>/
  → python3 scripts/relabel_styles.py  (optional)
  → python3 scripts/export_styles.py
  → Review and commit style_assets.json and the corresponding images
  → Deploy; startup synchronizes SQLite from the JSON export
```

Ingestion retries each image up to three times after transient errors. Adjust concurrency
to your provider's limits. Style cards prefer matching `.t400.webp` thumbnails while the
original reference is sent for image generation. Names come from `styleSpec.chineseLabel`,
with frontend fallbacks for missing labels. The `shotIntent` field guides card proportions
and generation prompts.

## Development checks

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
python3 -m py_compile server.py db.py userdb.py
node --check app.js
```

Tests use placeholder credentials and mocked providers. They do not require live API calls.

## Known limitations

- Deployment updates must preserve runtime databases and user-imported images. Back up these files and any external database.
- Some older references have no generated label and use a generic frontend fallback.
- Favorite counts include a synthetic baseline; they do not measure unique users.
- Legacy quotas are IP-based, so users behind the same NAT can share an allowance. Configure trusted proxy handling for your environment.
- Ingested references are compressed to a maximum long edge of 1024 px and are not intended as print originals.
- Only the apparel category is currently visible; other categories remain placeholders.
- Credit purchase UI is a placeholder. Payment integration and email delivery still need deployment-specific implementation.

## Roadmap

- [ ] Refine the visual creation flow and improve reference selection.
- [ ] Expand style curation and discovery across more product categories.
- [ ] Add targeted editing, including masks and background changes.
- [ ] Recommend styles based on the uploaded product.
- [ ] Explore multiple candidate images for easier comparison, with clear generation costs.
- [ ] Improve account experiences and replace synthetic favorite baselines with meaningful activity data.
- [ ] Strengthen backup and recovery workflows.
- [ ] Extend image source and model integrations to support a broader creative ecosystem.

## Release notes

The repository contains the application and built-in style library. Runtime uploads,
cookies, credentials, logs, backups, and databases are excluded. Keep private configuration
in environment variables and review staged files before publishing.

This repository starts from a sanitized snapshot. Do not merge private development history
that could reintroduce deployment details. Review hosted workflow logs and release
attachments separately before changing repository visibility.

An open-source license has not yet been selected. A `LICENSE` file must be added before
presenting the project as an open-source release.

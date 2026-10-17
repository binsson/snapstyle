# Deploy to a Linux server

[简体中文](DEPLOY.md) | **English**

All domains, paths, and account names below are generic examples. Adapt them to your server.
Architecture: `browser → HTTPS reverse proxy → uvicorn(127.0.0.1:8080)`.

## 1. Prepare the server

Use a Linux server with Python 3.12, configure DNS for your domain, and allow the required
SSH, HTTP, and HTTPS ports. Keep application port 8080 accessible only to the local proxy.

```bash
sudo useradd -r -m -d /opt/ai-designer -s /usr/sbin/nologin ai-designer
sudo mkdir -p /opt/ai-designer/app
```

Upload the project to `/opt/ai-designer/app/`. Exclude `.git`, real `.env*` files, databases
and their WAL/SHM files, cookies, logs, backups, user uploads, and
`reference_uploads/styles/stock/`. Configuration templates such as `.env.example` may be included.
Back up runtime data before updates and avoid overwriting existing databases or user images.

## 2. Install dependencies and configure the environment

```bash
sudo python3 -m venv /opt/ai-designer/venv
sudo /opt/ai-designer/venv/bin/pip install -r /opt/ai-designer/app/requirements.txt
sudo chown -R ai-designer:ai-designer /opt/ai-designer
sudo install -m 600 /opt/ai-designer/app/deploy/secpic.env.example /etc/ai-designer.env
sudoedit /etc/ai-designer.env
```

Set your own `API_BASE`, `API_KEY`, model, and authentication configuration. The `.example`
addresses in the templates are placeholders. If the guidance service differs from the image
service, set `REWRITE_API_BASE` and `REWRITE_API_KEY` separately.

Set `APP_ENV=production` and configure shared credentials or `DATABASE_URL` account mode.
Account mode requires a random `SESSION_SECRET`. Generate a value locally and store it in
the private environment file, for example:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

See [account setup](../ACCOUNTS_SETUP.en.md) for details. Pinterest is disabled by default;
optional dependencies and authorization requirements are described under
[optional image sources](../README.en.md#optional-image-sources).

## 3. Start the application

```bash
sudo cp /opt/ai-designer/app/deploy/secpic.service /etc/systemd/system/ai-designer.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-designer
curl -s http://127.0.0.1:8080/api/auth-status
```

The systemd template reads `/etc/ai-designer.env` and binds the application to loopback.
Restart the service after changing environment settings:

```bash
sudo systemctl restart ai-designer
```

## 4. Configure an HTTPS reverse proxy

Choose a proxy and replace `example.com` in its template with your domain:

- **Caddy:** install `deploy/Caddyfile` as `/etc/caddy/Caddyfile`, then reload Caddy.
- **Nginx:** install and enable `deploy/nginx.conf`, validate it with `sudo nginx -t`, and configure HTTPS with a certificate tool appropriate to your environment.

Enable HTTPS before accepting production logins. Proxy requests to the application;
do not serve the project root as a static directory.

## 5. Updates and verification

After updating code, install any changed dependencies and restart the application.
Check the homepage, authentication, style cards, and image generation workflow.

Back up runtime databases and images regularly. If account mode uses an external Postgres
database, include it in your backup and recovery plan. Keep database exports, logs,
credentials, private keys, and real infrastructure configuration outside the code repository.

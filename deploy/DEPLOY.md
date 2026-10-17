# 部署到 Linux 服务器

以下域名、路径和账号均为通用示例，需要根据自己的服务器调整。
架构：`用户 → HTTPS 反向代理 → uvicorn(127.0.0.1:8080)`。

## 1. 准备服务器

使用支持 Python 3.12 的 Linux 服务器，配置域名 DNS，并开放必要的 SSH / HTTP / HTTPS
端口。应用端口 8080 仅供本机反向代理访问。

```bash
sudo useradd -r -m -d /opt/ai-designer -s /usr/sbin/nologin ai-designer
sudo mkdir -p /opt/ai-designer/app
```

将项目代码上传到 `/opt/ai-designer/app/`。上传时排除 `.git`、`.env*`（示例文件除外）、
数据库及 WAL/SHM、Cookie、日志、备份、用户上传图片和 `reference_uploads/styles/stock/`。
更新部署时先备份运行数据，避免覆盖已有数据库与用户图片。

## 2. 安装依赖与配置

```bash
sudo python3 -m venv /opt/ai-designer/venv
sudo /opt/ai-designer/venv/bin/pip install -r /opt/ai-designer/app/requirements.txt
sudo chown -R ai-designer:ai-designer /opt/ai-designer
sudo install -m 600 /opt/ai-designer/app/deploy/secpic.env.example /etc/ai-designer.env
sudoedit /etc/ai-designer.env
```

填写自己的 `API_BASE`、`API_KEY`、模型和认证配置。模板中的 `.example` 地址不能用于真实调用。
提示词服务与图片服务不同时，分别设置 `REWRITE_API_BASE` 和 `REWRITE_API_KEY`。
生产环境设置 `APP_ENV=production`；配置共享账号或 `DATABASE_URL` 账户模式。
账户模式必须设置随机的 `SESSION_SECRET`，例如用 `python3 -c 'import secrets; print(secrets.token_hex(32))'`
生成后填入私密环境文件。账号说明见 [ACCOUNTS_SETUP.md](../ACCOUNTS_SETUP.md)。

Pinterest 默认关闭；在线图库的可选安装和授权要求见 [README](../README.md#可选在线图库)。

## 3. 启动应用

```bash
sudo cp /opt/ai-designer/app/deploy/secpic.service /etc/systemd/system/ai-designer.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-designer
curl -s http://127.0.0.1:8080/api/auth-status
```

systemd 模板使用 `/etc/ai-designer.env`，仅监听本机。修改环境变量后重启服务：

```bash
sudo systemctl restart ai-designer
```

## 4. 配置 HTTPS 反向代理

选择一种方案，先把模板中的 `example.com` 改成自己的域名：

- Caddy：使用 `deploy/Caddyfile`，安装到 `/etc/caddy/Caddyfile` 后重载 Caddy。
- Nginx：使用 `deploy/nginx.conf`，安装并启用站点配置后运行 `sudo nginx -t`；
  再使用适合自己环境的证书工具启用 HTTPS。

正式登录前确认 HTTPS 已启用。代理仅转发请求，不要将项目根目录作为静态站点根目录。

## 5. 更新与核验

更新代码后安装所需依赖并重启应用。确认首页、认证、风格卡片和生成流程正常。
日常备份数据库及运行时图片；账户模式还需对外部 Postgres 数据库制定备份策略。
不要将数据库备份、服务日志、账号、私钥或真实基础设施配置放进代码仓库。

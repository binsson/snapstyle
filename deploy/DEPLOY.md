# 部署到 Linux 服务器

**简体中文** | [English](DEPLOY.en.md)

以下域名、路径和账号均为通用示例，请根据自己的服务器调整。
架构：`浏览器 → HTTPS 反向代理 → uvicorn(127.0.0.1:8080)`。

## 1. 准备服务器

使用支持 Python 3.12 的 Linux 服务器，配置域名 DNS，并开放必要的 SSH、HTTP 和 HTTPS 端口。
应用端口 8080 仅供本机反向代理访问。

```bash
sudo useradd -r -m -d /opt/ai-designer -s /usr/sbin/nologin ai-designer
sudo mkdir -p /opt/ai-designer/app
```

将项目上传到 `/opt/ai-designer/app/`。排除 `.git`、真实 `.env*`、数据库及其 WAL/SHM 文件、
Cookie、日志、备份、用户上传图片和 `reference_uploads/styles/stock/`。
可以包含 `.env.example` 等配置模板。更新前先备份运行数据，避免覆盖数据库或用户图片。

## 2. 安装依赖与配置环境

```bash
sudo python3 -m venv /opt/ai-designer/venv
sudo /opt/ai-designer/venv/bin/pip install -r /opt/ai-designer/app/requirements.txt
sudo chown -R ai-designer:ai-designer /opt/ai-designer
sudo install -m 600 /opt/ai-designer/app/deploy/secpic.env.example /etc/ai-designer.env
sudoedit /etc/ai-designer.env
```

填写自己的 `API_BASE`、`API_KEY`、模型和认证配置；模板中的 `.example` 地址不能用于真实调用。
摄影指导服务与图片服务不同时，分别填写 `REWRITE_API_BASE` 和 `REWRITE_API_KEY`。

生产环境设置 `APP_ENV=production`，并配置共享账号或 `DATABASE_URL` 账户模式。
账户模式需要随机的 `SESSION_SECRET`。例如在本地生成后，填入私密环境文件：

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

详见[账号配置](../ACCOUNTS_SETUP.md)。Pinterest 默认关闭，
可选依赖与授权要求见[可选图源](../README.md#可选图源)。

## 3. 启动应用

```bash
sudo cp /opt/ai-designer/app/deploy/secpic.service /etc/systemd/system/ai-designer.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-designer
curl -s http://127.0.0.1:8080/api/auth-status
```

systemd 模板读取 `/etc/ai-designer.env`，仅监听本机地址。
修改环境变量后重启服务：

```bash
sudo systemctl restart ai-designer
```

## 4. 配置 HTTPS 反向代理

选择一种代理方案，将模板中的 `example.com` 替换为自己的域名：

- **Caddy：** 将 `deploy/Caddyfile` 安装到 `/etc/caddy/Caddyfile`，然后重载 Caddy。
- **Nginx：** 安装并启用 `deploy/nginx.conf`，运行 `sudo nginx -t` 验证，再用适合环境的证书工具配置 HTTPS。

正式接受用户登录前先启用 HTTPS。代理只负责转发请求，不要将项目根目录作为静态站点目录。

## 5. 更新与核验

更新代码后安装变化的依赖并重启应用，检查首页、认证、风格卡片和图片生成流程。

定期备份运行数据库和图片；账户模式使用外部 Postgres 时，也应纳入备份与恢复计划。
数据库导出、日志、凭据、私钥和真实基础设施配置应保存在代码仓库之外。

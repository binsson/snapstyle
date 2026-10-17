# 部署到 Render

本文使用通用配置，不包含现有服务、域名或部署记录。

## 1. 新建 Web Service

连接自己的代码仓库，选择部署分支，并配置：

- Build Command：`pip install -r requirements.txt`
- Start Command：`python3 server.py`

按托管平台要求配置 `PORT`。环境变量中填写自己的配置：

| 变量 | 说明 |
|---|---|
| `APP_ENV` | `production` |
| `API_BASE` | 自己的图片服务基础地址，通常含 `/v1` |
| `API_KEY` | 自己的图片服务密钥 |
| `IMAGE_API_PROVIDER` | `openai` 或 `toapis`，按实际接口选择 |
| `MODEL` | 服务支持的图片模型 |
| `IMAGE_SIZE_ENABLED` | 默认 `0`，按服务能力调整 |
| `REWRITE_API_BASE` | 独立摄影提示服务地址；留空共用 `API_BASE` |
| `REWRITE_API_KEY` | 对应提示服务密钥；留空共用 `API_KEY` |
| `REWRITE_MODEL` | 服务支持的视觉模型 |
| `DATABASE_URL` | 账户模式的 Postgres 连接串，可选 |
| `SESSION_SECRET` | 随机长字符串；生产账户模式必填 |
| `APP_USERNAME` / `APP_PASSWORD` | 未使用账户模式时的登录配置 |
| `PINTEREST_SCRAPE` | 默认 `0`，开启要求见 README |

不需要摄影提示服务时设置 `REWRITE_ENABLED=0`，已有风格使用本地拍摄提示词。
密钥仅在托管平台环境变量中保存。不要把 Environment 页面截图、日志或配置导出提交到仓库。

## 2. 域名与持久化

如需自定义域名，在 Render 的 Custom Domains 中添加自己的域名，按照服务控制台给出的
DNS 记录配置并验证 HTTPS。`PUBLIC_BASE_URL` 可设为自己的 HTTPS 网站地址。

为运行数据库和用户新增图片配置持久化存储，或改用外部数据库与对象存储。
不要假定实例本地文件会跨部署保留；部署前备份运行时数据。

## 3. 核验

确认 HTTPS 首页与 `/api/auth-status` 可访问，登录和风格图库正常；
使用自己的服务配置验证一次图片生成。不要在核验日志中输出凭据或连接串。

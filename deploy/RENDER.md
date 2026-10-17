# 部署到 Render

**简体中文** | [English](RENDER.en.md)

本文使用通用示例，不包含私有服务标识或部署记录。

## 1. 创建 Web Service

连接自己的仓库，选择部署分支，并配置：

- Build Command：`pip install -r requirements.txt`
- Start Command：`python3 server.py`

根据托管平台要求配置 `PORT`，并填写自己的环境变量：

| 变量 | 配置 |
|---|---|
| `APP_ENV` | `production`。 |
| `API_BASE` | 图片服务基础地址，通常包含 `/v1`。 |
| `API_KEY` | 图片服务密钥。 |
| `IMAGE_API_PROVIDER` | `openai` 或 `toapis`，与服务接口对应。 |
| `MODEL` | 图片服务支持的模型。 |
| `IMAGE_SIZE_ENABLED` | 默认 `0`，根据服务能力调整。 |
| `REWRITE_API_BASE` | 可选的独立摄影指导地址，留空共用 `API_BASE`。 |
| `REWRITE_API_KEY` | 对应的摄影指导密钥，留空共用 `API_KEY`。 |
| `REWRITE_MODEL` | 摄影指导服务支持的视觉模型。 |
| `DATABASE_URL` | 可选，账户模式的 Postgres 连接串。 |
| `SESSION_SECRET` | 随机长密钥，生产账户模式必填。 |
| `APP_USERNAME` / `APP_PASSWORD` | 未启用账户模式时的共享认证配置。 |
| `PINTEREST_SCRAPE` | 默认 `0`，开启要求见 [README](../README.md#可选图源)。 |

不需要摄影指导时，设置 `REWRITE_ENABLED=0`，已有风格使用本地描述。
密钥保存在平台环境变量中，不要将配置页面截图、日志或配置导出提交到仓库。

## 2. 域名与持久化

如需自定义域名，在 Custom Domains 中添加自己的域名，
按控制台给出的记录配置 DNS 并验证 HTTPS。必要时将 `PUBLIC_BASE_URL` 设置为自己的 HTTPS 地址。

为运行数据库和用户新增图片配置持久化存储，或将应用适配到外部数据库与对象存储。
不要假定实例本地文件可以跨部署保留，更新前应先备份运行数据。

## 3. 核验

检查 HTTPS 首页、`/api/auth-status`、登录和风格图库，
再使用自己的服务配置验证一次图片生成并检查结果。核验日志不应输出凭据或数据库连接串。

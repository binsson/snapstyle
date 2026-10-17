# SnapStyle

**简体中文** | [English](README.en.md)

**一个以用户体验为中心的 AI 图像产品，探索更直观的视觉创作方式。**

SnapStyle 希望把图像生成融入日常创作：从一张产品照片出发，浏览喜欢的视觉风格，
通过简单的操作生成新的商品摄影图。

我们关注完整的产品体验：如何发现风格、如何开始创作、如何比较不同结果，
以及如何通过清晰的交互和合理的默认设置，让图像模型的能力更容易被使用。

## 产品体验

- **从图片开始。** 上传产品照片，选择参考风格，无需编写复杂的提示词。
- **发现喜欢的风格。** 浏览内置风格图库，也可以接入可选的在线图源。
- **持续探索。** 生成不同版本，在个人图库中查看和保存创作结果。

## 我们想探索的方向

SnapStyle 是一个持续演进的产品实践。我们希望围绕图像生成，逐步连接更多图源、
可复用的风格库、图像编辑流程和模型服务，构建更丰富的创意生态。

欢迎参与交互设计、视觉设计、创作流程、生态接入和代码改进。
也欢迎分享实际使用中的感受、问题和想法。

## 联系方式

合作、产品反馈或安全问题：[binsson@outlook.com](mailto:binsson@outlook.com)。
一般问题和功能建议，欢迎提交 [GitHub Issue](https://github.com/binsson/snapstyle/issues)。

## 快速开始

使用 Python 3.12，并准备自己的图像生成服务：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入自己的 API_BASE 和 API_KEY。
# .example 地址只是占位符，不能直接调用。
set -a && source .env && set +a
python3 server.py
# 打开 http://localhost:8080
```

当前应用界面和内置风格标签为中文。仓库文档提供中英文版本，可通过各文档顶部的链接切换。

## 工作方式

```text
上传产品照片
  → 浏览风格卡片并选择参考图
  → 后台生成新的图片
  → 在个人图库中查看已保存的结果
```

提交前，产品照片保存在内存中，刷新页面会清空上传内容。
生成结果通过浏览器 IndexedDB 保存在当前设备。主要创作流程由参考图驱动，
不需要打开提示词编辑器。目前只展示服装品类。

有参考图时，可选的视觉模型只分析参考图，用一两句话概括光线、构图和氛围。
图像模型同时接收产品原图和参考图，并附带简短的产品保真要求；
这条路径不再使用历史风格描述中的详细指令。再次生成时不会复用上一轮的摄影描述。

摄影指导服务失败或返回过长内容时，会回退到简短的本地描述。
默认图片配置不发送尺寸或分辨率参数，通过提示词表达画幅方向，最终尺寸由服务决定。
可选的 `toapis` 适配使用独立的参考图上传与异步任务流程。

## 可选图源

- **内置图库：** 随仓库提供，不依赖在线搜索。
- **Pexels：** 填入自己的 `PEXELS_API_KEY` 后启用，使用官方 API，密钥只在服务端保存。
- **Pinterest：** 保留 gallery-dl 适配，默认关闭。取得所需授权后，安装 `pip install -r requirements-pinterest.txt`，再在私密环境变量中设置 `PINTEREST_SCRAPE=1`；仅安装依赖不会自动开启。

Pinterest 的自动访问需要遵守其[服务条款](https://policy.pinterest.com/en/terms-of-service)。
Cookie、浏览器登录态和代理凭据不能提交到仓库。集成会忽略 gallery-dl 的全局配置；
如需使用浏览器登录态，通过本地 `PINTEREST_COOKIES_BROWSER` 配置。

## 环境配置

真实配置保存在私密的 `.env` 或托管平台的环境变量中。

| 变量 | 是否必需 | 用途 |
|---|---|---|
| `API_BASE` | 是 | 自己的图片服务基础地址，通常包含 `/v1`；代码不内置私有地址。 |
| `API_KEY` | 是 | 图片服务密钥。 |
| `IMAGE_API_PROVIDER` | 否 | 默认 `openai`；`toapis` 选择另一种服务适配。 |
| `MODEL` | 否 | 图片模型标识，默认 `gpt-image-2.5-sunburst`；请根据服务支持的模型调整。 |
| `IMAGE_SIZE_ENABLED` | 否 | 默认 `0`；设为 `1` 才向兼容图片接口发送像素尺寸。 |
| `IMAGE_RESOLUTION` | 否 | 默认留空；`toapis` 适配支持 `1k`、`2k`、`4k`。 |
| `REWRITE_API_BASE` | 否 | 视觉分析与摄影指导服务地址，留空共用 `API_BASE`。 |
| `REWRITE_API_KEY` | 否 | 摄影指导服务密钥，留空共用 `API_KEY`；服务不同时应单独配置。 |
| `REWRITE_MODEL` | 否 | 用于摄影指导、风格分析和标签生成，默认 `gpt-5.6-luna-official`。 |
| `REWRITE_ENABLED` | 否 | 设为 `0` 后，已有风格使用本地描述，并停用新参考图分析。 |
| `PEXELS_API_KEY` | 否 | 启用 Pexels 搜索。 |
| `PINTEREST_SCRAPE` | 否 | 默认 `0`；需要显式开启并安装可选依赖。 |
| `DATABASE_URL` | 否 | 启用邮箱账号与独立积分，见[账号配置](ACCOUNTS_SETUP.md)。 |
| `REGISTRATION_ENABLED` | 否 | 默认 `0`，控制是否开放注册。 |
| `FREE_SIGNUP_CREDITS` | 否 | 新账号初始积分，默认 `0`。 |
| `GENERATION_COST` | 否 | 每次生成消耗的积分，默认 `1`。 |
| `SESSION_SECRET` | 生产账户模式必需 | 随机签名密钥，更换后已有登录会失效。 |
| `APP_USERNAME` / `APP_PASSWORD` | 共享账号认证时需要 | 未启用账户模式时的共享登录配置。 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 否 | 管理员账号，绕过积分扣费和 IP 限额；密码留空时回退到 `APP_PASSWORD`。 |
| `APP_ENV` | 生产环境必需 | 设为 `production`，启用生产环境检查。 |
| `PORT` | 否 | 默认 `8080`。 |
| `PUBLIC_BASE_URL` | 否 | 自己的网站地址，留空时从请求推导。 |
| `STYLE_UPLOAD_CATEGORY` | 否 | 图库导入子目录，默认 `apparel`。 |

所有示例地址均为占位符，请填写适合自己部署的兼容服务和模型。
修改本地 `.env` 不会更新托管平台的配置。

## 部署

查看 [Linux 服务器部署](deploy/DEPLOY.md)或 [Render 部署](deploy/RENDER.md)。

- 通过私密环境变量配置 API 地址、密钥、数据库连接和账号。
- 生产环境设置 `APP_ENV=production` 并配置认证；生产账户模式还必须设置随机的 `SESSION_SECRET`。
- 仅提供前端文件和风格图片目录，不要将项目根目录作为静态站点目录。
- 为运行数据库和上传图片配置持久化存储，部署更新前先备份。

## 项目结构

| 文件 | 用途 |
|---|---|
| `server.py` | FastAPI 后端、图片服务适配、认证和静态文件白名单。 |
| `db.py` | SQLite 存储：旧版会话、限流、风格、收藏和生成任务。 |
| `userdb.py` | Postgres 账号、积分、认证限流和用户生成任务。 |
| `app.js` | 无框架前端和浏览器端结果存储。 |
| `index.html` / `style.css` | 应用页面与样式。 |
| `style_assets.json` | 内置风格图库的版本化数据源。 |
| `reference_uploads/styles/` | 内置风格图片和缩略图。 |
| `scripts/ingest_styles.py` | 压缩本地参考图，分析风格并写入 SQLite。 |
| `scripts/relabel_styles.py` | 为已有风格补充中文标签。 |
| `scripts/recompress_styles.py` | 将已有 PNG 参考图重新压缩为 JPEG。 |
| `scripts/gen_thumbnails.py` | 生成风格卡片缩略图。 |
| `scripts/export_styles.py` | 将 SQLite 风格数据导出为 `style_assets.json`。 |

## API 概览

| 路由 | 用途 |
|---|---|
| `GET /api/auth-status` | 认证要求和可用图源。 |
| `POST /api/login` | 共享账号、管理员或邮箱登录，返回会话令牌。 |
| `POST /api/register` | 开放注册时创建邮箱账号。 |
| `GET /api/account` | 当前账户信息与积分余额。 |
| `GET /api/style-assets` | 可见风格列表。 |
| `GET /api/favorite-counts?ids=a,b,c` | 获取展示基数与用户增量合成的收藏数。 |
| `POST /api/favorite-toggle` | 添加或取消收藏，返回更新后的数量。 |
| `GET /api/stock/search` | 搜索已配置的在线图源。 |
| `POST /api/stock/use` | 将在线参考图导入本地图库。 |
| `POST /api/generate` | 同步图片生成。 |
| `POST /api/generate-async` | 提交后台任务，返回任务 ID 与剩余额度。 |
| `GET /api/generate-result/{id}` | 轮询 `pending`、`done`、`error` 或 `not_found`。 |
| `GET /api/quota` | 剩余额度；账户模式使用积分。 |

## 数据与持久化

应用使用本地 SQLite（`aid.db`），邮箱账号模式另外使用 Postgres。
启动时通过 `db.migrate_from_json`，从版本化的 `style_assets.json` 同步内置风格数据。

| 数据 | 存储与行为 |
|---|---|
| 旧版会话 | SQLite；当前登录使用签名令牌，有效性取决于签名密钥和过期时间。 |
| IP 限流 | SQLite；持久性取决于底层磁盘。 |
| 内置风格 | Git 中的 JSON 与图片，启动时导入 SQLite。 |
| 收藏增量 | SQLite；持久性取决于底层磁盘。 |
| 在线导入参考图 | 本地图片与 SQLite 元数据，不提交到 Git。 |
| 邮箱账户与积分 | 配置 `DATABASE_URL` 后存入 Postgres。 |
| 账户生成任务 | Postgres，包含任务归属、扣费和退款状态。 |
| 个人图库 | 浏览器 IndexedDB，保存在用户当前设备。 |

展示收藏数由 30–280 的生成基数加上用户增量组成，不代表独立用户数。

## 维护风格图库

```text
将参考图放入 reference_uploads/styles_inbox/
  → python3 scripts/ingest_styles.py --concurrency 2
  → 压缩为最长边不超过 1024 px 的 JPEG
  → 分析风格，将元数据写入 SQLite
  → 图片保存至 reference_uploads/styles/<category>/
  → python3 scripts/relabel_styles.py（可选）
  → python3 scripts/export_styles.py
  → 检查并提交 style_assets.json 和对应图片
  → 部署，启动时从 JSON 同步 SQLite
```

遇到临时错误时，每张图片最多尝试三次，请根据服务限额调整并发。
卡片优先使用对应的 `.t400.webp` 缩略图，生成时仍传入原参考图。
名称来自 `styleSpec.chineseLabel`，缺失时由前端兜底；`shotIntent` 用于控制卡片比例和生成提示。

## 开发检查

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
python3 -m py_compile server.py db.py userdb.py
node --check app.js
```

测试使用占位凭据和模拟服务，不需要真实 API 调用。

## 已知限制

- 部署更新需要保留运行数据库和用户导入图片，并备份这些文件及外部数据库。
- 部分早期参考图没有生成标签，使用前端的通用标签兜底。
- 收藏数包含模拟基数，不能衡量独立用户数量。
- 旧版限流按 IP 计数，共享 NAT 的用户可能共用额度；需根据部署环境正确配置可信代理。
- 导入参考图最长边压缩到 1024 px，不适合作为印刷原图。
- 当前仅展示服装品类，其余类目仍为占位。
- 充值界面为占位，支付接入和邮件发送仍需结合实际部署完成。

## 路线图

- [ ] 优化视觉创作流程和参考图选择体验。
- [ ] 扩展更多产品品类的风格整理与发现能力。
- [ ] 增加蒙版编辑、换背景等局部编辑能力。
- [ ] 根据上传产品推荐合适风格。
- [ ] 探索多张候选图的比较体验，并明确展示生成成本。
- [ ] 完善账号体验，用有意义的真实活动数据替代模拟收藏基数。
- [ ] 加强备份与恢复流程。
- [ ] 扩展图源和模型接入，支持更丰富的创意生态。

## 发布说明

仓库包含应用代码和内置风格图库。用户上传、Cookie、凭据、日志、备份和运行数据库不随代码发布。
真实配置应通过环境变量管理，发布前检查暂存区内容。

本仓库从清理后的文件快照开始，请避免合并可能重新带入部署信息的私有开发历史。
更改仓库可见性前，还应单独检查托管平台的工作流日志和发布附件。

目前尚未选择开源许可证，正式以开源项目发布前需要添加 `LICENSE`。

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

## Development notes

The setup and implementation notes below are currently in Chinese.
Image generation services require your own API endpoints and credentials.
See the deployment examples for [Linux servers](deploy/DEPLOY.md) and [Render](deploy/RENDER.md).

---

## 用户视角:三个动作

```
上传产品图(内存,刷新就丢)
  ↓
挑一张风格卡(瀑布流,目前只开 apparel 类)
  ↓
后端异步生成,「我的」页看结果(IndexedDB 持久化)
```

没有文字输入。没有详情页多段规划。风格全靠卡片驱动。

---

## 关键文件

| 文件 | 角色 | 行数 |
|---|---|---|
| `server.py` | FastAPI 后端,API + 白名单静态文件 | ~930 |
| `db.py` | SQLite 封装,sessions / rate_limits / style_assets / favorite_deltas | ~290 |
| `app.js` | 前端单 JS,无框架 | ~1540 |
| `index.html` | 单页 HTML | ~130 |
| `style.css` | 全部样式 | ~250 |
| `style_assets.json` | **风格资产的 source of truth**,git tracked | ~280 KB |
| `scripts/ingest_styles.py` | 把图压缩 + 调 LLM 提取 styleSpec → 写 SQLite | — |
| `scripts/relabel_styles.py` | 给已有 asset 补 chineseLabel(只调 LLM,不重提 spec) | — |
| `scripts/recompress_styles.py` | 把磁盘上已有的 PNG 原地压成 JPEG | — |
| `scripts/export_styles.py` | SQLite → `style_assets.json`(commit 前跑) | — |

---

## API

| 路由 | 用途 |
|---|---|
| `GET /api/auth-status` | 是否需要登录 |
| `POST /api/login` | 共享账号登录,返回 session token |
| `GET /api/style-assets` | 列出所有动态 style asset(过滤 hidden) |
| `GET /api/favorite-counts?ids=a,b,c` | 批量取收藏数(`base + delta`) |
| `POST /api/favorite-toggle` | `{template_id, action: add/remove}` → +/-1,返回新 count |
| `POST /api/generate` | 同步生图入口(备用;web 端走 generate-async) |
| `POST /api/generate-async` | 提交后台生成任务,立即返回 `{job_id, remaining}` |
| `GET /api/generate-result/{id}` | 轮询任务:`pending` / `done`(含 image+brief) / `error` / `not_found` |
| `GET /api/quota` | 当日剩余生成次数(`remaining:-1` = admin/本地不限额) |

---

## 数据存储

| 表 | 用途 | 持久化 |
|---|---|---|
| `sessions` | 旧版会话表；当前认证使用签名 token | token 有效性取决于签名密钥和有效期 |
| `rate_limits` | IP × 时间戳,日限流 | 取决于 SQLite 所在磁盘是否持久化 |
| `style_assets` | 风格库 asset(id, image, styleSpec, hidden...) | 启动时从 `style_assets.json` 同步 |
| `favorite_deltas` | 用户点 ♥ 累积的 +/-1 | 取决于 SQLite 所在磁盘是否持久化 |

SQLite (`aid.db`) 和 `reference_uploads/` 需要持久磁盘和备份；EC2 方案使用
`/opt/ai-designer/app/`。账户与额度在配置 `DATABASE_URL` 后存入 Neon Postgres。
风格库通过 `style_assets.json` (git tracked) + 启动时 `db.migrate_from_json` 同步。

收藏数显示 = `hash(template_id)` 派生的伪随机 base(30-280)+ 用户 delta。
base 算法稳定,所以即使 delta 清空数字也不会归零。

---

## 风格库工作流

```
新图 → reference_uploads/styles_inbox/
  ↓ python3 scripts/ingest_styles.py --concurrency 2
压缩到 1024px JPEG 85,LLM 提取 styleSpec + chineseLabel
  ↓
写入 SQLite + 复制图到 reference_uploads/styles/<category>/
  ↓ python3 scripts/relabel_styles.py     (可选,补缺失的标签)
  ↓ python3 scripts/export_styles.py
SQLite → style_assets.json
  ↓
git add reference_uploads/styles/ style_assets.json
git commit && git push
  ↓
按实际托管方式部署代码（EC2 见 deploy/DEPLOY.md）
  ↓
服务启动 migrate_from_json → 数据上线
```

ingest 失败容错:脚本对每张图自带 3 次 retry，处理上游临时错误或超时；
并发太高(>=3)反而触发上游限流;`--concurrency 2` 是稳定甜点。

---

## 生成链路

```
用户点卡 (template) → startGeneration → enqueueGen
  ↓
取 refImage(产品图,内存)
fetch sampleImage(风格图,同源 URL → data URL)
  ↓
POST /api/generate-async {prompt, size, image: [product, style], art_direct, style_spec}
  ↓
Luna 仅看风格参考图 → 1–2 句光影、构图和美感方向
  ↓
POST ${API_BASE}/images/edits
  multipart image[] = [product, style]
  model = gpt-image-2.5-sunburst
  不发送 size / resolution，画幅方向由提示词表达
  ↓
接收图片 → 保存本地任务结果
  ↓
前端轮询 /api/generate-result/{job_id}
  ↓
前端写入 myItems (IndexedDB),「我的」页显示
```

产品图只交给图片模型，不由 Luna 描述衣服、格纹或印花。Luna 只看参考图，
用 1–2 句短提示概括光影、构图和整体美感，不指定详细姿势、道具或场景。
Sunburst 同时接收产品原图与参考原图，直接理解产品与摄影风格；提示词保留简短的产品保真要求。
参考图可用时不再注入历史 `styleSpec` 的详细描述；“换一张”也不回传旧拍摄描述。
辅助调用失败或返回过长内容时使用简短方向提示，继续双图生成。
新参考图的风格分析同样只提炼简短摄影方向。当前不发送 `size` / `resolution`；实际像素尺寸以返回文件为准。

---

## 风格卡片

风格卡片统一来自 SQLite + `style_assets.json`。图片位于
`reference_uploads/styles/<category>/`,卡片展示优先使用同名 `.t400.webp`
缩略图,生成时仍把原图作为风格参考发给模型。

名字来自 `styleSpec.chineseLabel`,缺失时前端用 `inferDynamicStyleName`
兜底。`shotIntent` 由 LLM 提取,前端用它控制卡片比例和生成提示。

---

## 部署

- 按自己的托管环境选择 [Linux 服务器部署](deploy/DEPLOY.md) 或 [Render 部署](deploy/RENDER.md)。
- 服务地址、数据库连接串、密钥和账号仅通过本地 `.env` 或托管平台的环境变量配置。
- 静态文件只白名单服务 `index.html` / `app.js` / `style.css` / `reference_uploads/styles/*`。
- 生产环境设置 `APP_ENV=production` 并配置身份验证；账户模式还必须配置随机的 `SESSION_SECRET`。
- 本地风格图库随代码提供；用户上传、Cookie、运行数据库和生成记录不随代码发布。

### 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `API_KEY` | ✅ | 图片服务 API Key |
| `API_BASE` | ✅ | 自行填写图片服务 API 基础地址，通常含 `/v1`；代码不内置私有地址 |
| `IMAGE_API_PROVIDER` | 可选 | 默认 `openai`，使用兼容 Images API；`toapis` 保留旧服务适配 |
| `MODEL` | 可选 | 默认 `gpt-image-2.5-sunburst` |
| `IMAGE_RESOLUTION` | 可选 | 默认留空；仅旧 ToAPIs 适配使用 `1k` / `2k` / `4k` |
| `IMAGE_SIZE_ENABLED` | 可选 | 默认 `0`；设为 `1` 才向 OpenAI 兼容图片接口发送像素尺寸 |
| `REWRITE_API_KEY` | 可选 | 摄影短提示服务密钥；服务不同时需独立填写，留空会共用图片 `API_KEY` |
| `REWRITE_API_BASE` | 可选 | 自行填写摄影提示服务地址；留空共用 `API_BASE` |
| `REWRITE_MODEL` | 可选 | 默认 `gpt-5.6-luna-official`，用于提示词、风格分析和标签生成 |
| `REWRITE_ENABLED` | 可选 | `0` 使用已有模板的本地拍摄提示词，停用新参考图分析 |
| `APP_USERNAME` | ⚠️ | 共享账号用户名(留空免登录) |
| `APP_PASSWORD` | ⚠️ | 共享账号密码 |
| `ADMIN_USERNAME` | 可选 | 绕过 IP 限流的管理员账号 |
| `ADMIN_PASSWORD` | 可选 | (留空 fallback `APP_PASSWORD`) |
| `PORT` | 可选 | 默认 8080；EC2 模板同样使用 8080 |
| `APP_ENV` | 生产必需 | 设为 `production`，启用生产环境检查 |
| `STYLE_UPLOAD_CATEGORY` | 可选 | ingest 写入的子目录,默认 `apparel` |

本地 `.env`(在 `.gitignore`):
```
API_BASE=https://image-provider.example/v1
API_KEY=
REWRITE_API_BASE=https://rewrite-provider.example/v1
REWRITE_API_KEY=
```

---

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入自己的 API_BASE 和 API_KEY；.example 地址是占位符。
set -a && source .env && set +a
python3 server.py
# http://localhost:8080
```

---

## 可选在线图库

- **Pexels**：配置自己的 `PEXELS_API_KEY` 后启用；代码使用官方 API，密钥仅在后端保存。
- **Pinterest**：保留 gallery-dl 适配，默认关闭。获得所需授权后，安装
  `pip install -r requirements-pinterest.txt`，并在私密环境变量中设置 `PINTEREST_SCRAPE=1`。
  安装依赖本身不会自动启用抓取。
- Pinterest 的自动访问须遵守 [平台条款](https://policy.pinterest.com/en/terms-of-service)。
  Cookie、浏览器登录态和代理凭据不得提交到仓库；集成不读取 gallery-dl 的全局配置。
- 本地风格图库独立于在线搜索；关闭在线图源不影响已有风格卡片。

## 开源发布

- 发布前检查暂存区，确认没有 `.env`、数据库、Cookie、私钥、日志或备份。
- 当前提交的清理不会修改旧 Git 历史。旧仓库若曾包含部署地址等私密信息，应继续保持私有；
  对外发布使用清理后的文件创建全新仓库，不复制旧 `.git`、分支或标签。
- GitHub 上的 Actions 日志、发布附件和其它远端内容也需要单独检查。
- 本项目尚未选择开源许可证，正式开源前需由维护者确定并添加 `LICENSE`。

---

## 已知限制 / 历史决策

- **数据持久化**:部署更新不能覆盖线上 `aid.db` 或用户导入图片；需定期备份。
  非账户模式的内存生成任务在服务重启后丢失，账户模式任务存入 Postgres。
- **24 张 asset 没有 chineseLabel**:早期导入时上游接口反复返回 400，
  这部分卡 fallback 到机械标签 `棚拍上身` 之类。
- **收藏数是 base + delta**:不是真实"多少独立用户"。base 是 hash 派生的伪随机数,
  用于社会证明(冷启动看着有人气);delta 才是真实用户点击累积。
- **聚类系统已删**:历史代码里有过 `style_clusters` + cosine_similarity + LLM 汇总,
  前端从未使用,2026-05 移除以节省 token。
- **限流粒度粗**:按 IP 限额,默认 20 次/天(`DAILY_GENERATION_LIMIT` 可调),失败自动退还,
  admin 绕过。共享 NAT 用户仍会互相挤额度;Cloudflare 前置时建议设
  `TRUSTED_IP_HEADER=CF-Connecting-IP`,避免伪造 X-Forwarded-For 绕过限额。
- **图片清晰度上限 1024px**:ingest 压缩到长边 1024px JPEG 85,够 LLM 视觉理解,
  但风格库不适合作为印刷物料源。
- **`bagshoe` / `jewelry` 等品类隐藏**:前端 `AID_CATEGORIES` 只开 apparel,
  其他类目占位但 sampleImage 多为空,暂不展示。

---

## Roadmap

**短期**(免费/低成本,先做)
- [ ] 简化 `buildStyleRefPrompt`(一整页 → 3-5 条核心硬约束)
- [ ] 风格图人工筛差(运营定期看着难看的标 `hidden=true`)

**中期**(本质改进)
- [ ] 图像编辑:`/images/edits` + mask UI,出图后局部重绘/换背景
- [ ] 智能推荐:用户传产品图后,基于品类反向推荐 top 5 风格卡
- [ ] A/B 种子:同一卡一次跑 2-3 张候选(成本翻倍,可选 toggle)

**长期**(需要架构升级)
- [ ] 个人账号 + 真实多用户收藏统计(替换共享账号 + 伪随机 base)
- [ ] 完善 SQLite 和用户图片的自动备份与恢复流程
- [ ] 多品类风格库扩展(bagshoe / jewelry / home / 家电...)

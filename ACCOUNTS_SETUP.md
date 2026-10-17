# 邮箱账户 + 积分 — 接入说明（第 1、2 步）

本次改动加了**邮箱注册/登录**和**按积分计费**。是否启用完全由一个环境变量
`DATABASE_URL` 决定：

| `DATABASE_URL` | 行为 |
|----------------|------|
| **已设置**（Neon） | 邮箱注册登录 + 每个用户独立积分余额，生成扣积分 |
| **未设置** | 保持旧的「共享账号 + 按 IP 每天限流」，**线上现状不受影响** |

> 第 4 步「充值」会在你办好第三方支付（虎皮椒等）后再接；现在「充值」按钮是占位，
> 点了会提示「即将开放」。支付接入前默认关闭公开注册，也不赠送免费试用额度。

---

## 一、创建 Neon 数据库（2 分钟，免费）

1. 打开 https://neon.tech ，用 GitHub/Google 登录。
2. 点 **Create project**，区域选离用户近的（如 `Asia Pacific (Singapore)`），其它默认。
3. 创建后页面会给一段 **Connection string**，点 **Pooled connection**（带 `-pooler` 的那个，
   更适合 Serverless），复制完整串，形如：
   ```
   postgresql://neondb_owner:xxxx@ep-xxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```

## 二、本地运行

把上面的串填进 `.env`（参考 `.env.example`）：

```bash
DATABASE_URL=postgresql://...你的串...?sslmode=require
API_KEY=...你的图像 key...
REGISTRATION_ENABLED=0
FREE_SIGNUP_CREDITS=0
```

然后：

```bash
pip install -r requirements.txt   # 会装上新依赖 psycopg
python3 server.py
```

启动日志看到 `🔐 Accounts mode ON` 就对了。表会在首次启动自动建好。
打开页面后默认只显示邮箱登录。支付接入前不开放公开注册。

## 三、生产环境账户配置

在当前托管环境中设置以下变量。采用 [EC2 部署方案](deploy/DEPLOY.md) 时，
编辑 `/etc/ai-designer.env`：

- `APP_ENV` = `production`

- `DATABASE_URL` = 你的 Neon 串
- `REGISTRATION_ENABLED` = `0`（支付上线后改为 `1` 才开放注册）
- `FREE_SIGNUP_CREDITS` = `0`（不赠送免费试用）
- 必须设 `SESSION_SECRET` = 一个随机长字符串（生产账户模式缺失时服务会拒绝启动；
  改动它会让所有人重新登录）
- 公开放注册前，建议同时配置 `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY`；
  只配置其中一个时服务会拒绝启动，防止验证静默失效

`APP_USERNAME` / `APP_PASSWORD` 可以删掉（不再需要共享账号）。`ADMIN_USERNAME` /
`ADMIN_PASSWORD` 可保留，管理员额度无限、绕过扣费，方便你自己测试。

配置完成后按当前托管方式重新部署或重启服务。EC2 方案执行
`sudo systemctl restart ai-designer`；更新代码见部署文档第 5 节，推送 GitHub 不会自动更新 EC2。

---

## 数据模型（已在 Postgres 自动建表）

- `users` — id / email / password_hash(scrypt) / **credits** / email_verified / created_at
- `credit_logs` — 每一次积分变动（注册赠送 / 生成扣除 / 失败退回 / 充值），可审计
- `orders` — 充值订单（第 4 步接支付时使用，已建表）
- `email_codes` — 邮箱验证码（开启 `REQUIRE_EMAIL_VERIFICATION` 时使用）
- `auth_rate_events` — 持久化登录/注册限流，服务重启不会清空窗口
- `generation_jobs` — 账户用户的生成任务、归属和退款状态；中断任务可自动退回积分

## 计费规则

- 公开注册默认关闭，新账号默认不赠送额度。
- 支付上线后设置 `REGISTRATION_ENABLED=1` 开放注册；若要做限时活动，再单独调整
  `FREE_SIGNUP_CREDITS`。
- 每生成一张扣 `GENERATION_COST`（默认 1）；扣费和任务创建在同一个数据库事务中。
- 生成失败会原子退款；部署/重启遗留的超时任务也会在启动、轮询或下次生成时退款。
- 管理员账号和本地开发不扣费。
- 额度为 0 时生成返回 `402`，前端提示「额度不足，请充值」。

## 相关接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/register` | 邮箱注册（仅 `REGISTRATION_ENABLED=1` 时开放） |
| POST | `/api/login` | 邮箱或管理员登录 |
| GET  | `/api/account` | 当前账户信息（邮箱、剩余积分） |
| GET  | `/api/quota` | 剩余额度（账户模式下即积分余额） |

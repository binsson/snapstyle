# 邮箱账号与积分

**简体中文** | [English](ACCOUNTS_SETUP.en.md)

设置 `DATABASE_URL` 后，启用邮箱注册、登录和独立的用户积分余额。

| 配置 | 行为 |
|---|---|
| 已设置 `DATABASE_URL` | 邮箱账号与独立积分余额，生成图片时扣除积分。 |
| 未设置 `DATABASE_URL` | 共享账号认证与按 IP 的每日限流。 |

默认不开放注册，新账号不赠送积分。充值按钮为占位，尚未接入支付处理。

## 1. 创建 Postgres 数据库

可以使用 [Neon](https://neon.tech) 等数据库服务，选择适合用户的区域，
在控制台复制连接串。需要连接池的部署应选择服务提供的连接池入口。

连接串示例：

```text
postgresql://user:password@database.example/dbname?sslmode=require
```

上面的域名和凭据均为占位符，请将真实连接串保存在私密配置中。

## 2. 本地运行

按照[快速开始](README.md#快速开始)创建虚拟环境并安装依赖。
如有需要，将 `.env.example` 复制为 `.env`，再编辑私密的 `.env`：

```bash
API_BASE=https://image-provider.example/v1
API_KEY=
DATABASE_URL=postgresql://user:password@database.example/dbname?sslmode=require
REGISTRATION_ENABLED=0
FREE_SIGNUP_CREDITS=0
```

将占位地址和凭据替换为自己的值，加载配置后启动：

```bash
set -a && source .env && set +a
python3 server.py
```

启动日志应包含 `Accounts mode ON`，首次启动会自动建表。
界面会显示邮箱登录，公开注册仍需要显式开启。

## 3. 生产环境配置

在托管平台的环境变量中配置；采用 [Linux 部署](deploy/DEPLOY.md)时，
编辑 `/etc/ai-designer.env`：

| 变量 | 推荐配置 |
|---|---|
| `APP_ENV` | `production`。 |
| `DATABASE_URL` | 私密的 Postgres 连接串。 |
| `REGISTRATION_ENABLED` | 准备好接受注册前保持 `0`。 |
| `FREE_SIGNUP_CREDITS` | 默认 `0`，仅在明确需要赠送额度时调整。 |
| `SESSION_SECRET` | 随机长密钥，生产账户模式必填。 |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | 使用注册验证时成对配置。 |

更换 `SESSION_SECRET` 会使已有登录令牌失效。Turnstile 只配置一个密钥时会拒绝启动，
避免验证配置不完整却被误认为已生效。

账户模式不再需要共享的 `APP_USERNAME` / `APP_PASSWORD`。
可以配置 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，用于绕过积分扣费和 IP 限额的管理员账号。
管理员凭据应保密。

修改环境变量后，需要重启或重新部署。Linux 示例：

```bash
sudo systemctl restart ai-designer
```

仅推送到 GitHub 不会更新手动维护的服务器，更新步骤见[部署指南](deploy/DEPLOY.md#5-更新与核验)。

## 数据模型

以下表在 Postgres 中创建：

| 表 | 用途 |
|---|---|
| `users` | 邮箱、scrypt 密码哈希、积分余额、验证状态和创建时间。 |
| `credit_logs` | 可审计的积分变动，包括生成扣费和退款。 |
| `orders` | 为支付接入预留的订单记录。 |
| `email_codes` | 邮箱验证码，发送流程需要额外接入。 |
| `auth_rate_events` | 跨重启保留的登录与注册限流记录。 |
| `generation_jobs` | 用户生成任务、归属、扣费和退款状态。 |

## 积分规则

- 默认不开放注册，新账号初始积分为零。
- 准备好后设置 `REGISTRATION_ENABLED=1`；促销赠送额度通过 `FREE_SIGNUP_CREDITS` 单独调整。
- 每次生成扣除 `GENERATION_COST` 积分，默认 `1`；扣费与任务创建在同一数据库事务中完成。
- 失败任务原子退款，因中断遗留的超时任务也可以在启动、轮询或后续请求时恢复并退款。
- 管理员不扣积分；未启用账户模式的本地开发可以不受额度限制。
- 积分不足返回 HTTP `402`，界面提示充值。
- 设置 `REQUIRE_EMAIL_VERIFICATION=1` 前，请先实现并配置邮件发送流程。

## 账号接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/register` | `REGISTRATION_ENABLED=1` 时开放邮箱注册。 |
| POST | `/api/login` | 邮箱、共享账号或管理员登录。 |
| GET | `/api/account` | 当前账户信息和积分余额。 |
| GET | `/api/quota` | 剩余额度，账户模式使用积分余额。 |

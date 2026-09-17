# Personal Multi-Search Gateway

个人使用的 **Tavily / Exa / Brave 搜索 API 聚合网关**。提供中文管理后台、多个授权账号与 Key 管理、优先级路由、自动切换和一个统一访问 Key。第一期只做 HTTP API，不做 MCP、商业计费或多租户。

> 这是可部署的第一版实现，不是完成生产验收的托管服务。实际完成的测试、未测试范围见 [测试报告](docs/TEST_REPORT.md)。没有内置供应商密钥，不会自动创建账号或购买额度。

## 功能

| 模块 | 已实现内容 |
| --- | --- |
| 管理后台 | 单管理员账密登录；概览、账号与密钥、路由、访问 API、请求日志、设置 |
| 上游管理 | Tavily / Exa / Brave；账号与 Key 分层；添加、停用、删除 Key、手动测试 |
| 路由 | 顺序优先；账号内 Key 切换；账号级额度/限流；跨平台故障转移；严格指定 Provider |
| 访问控制 | 独立网关 API Key；生成时只显示一次；轮换立即撤销旧 Key；可暂停访问 |
| 保护 | 请求频率、UTC 日请求上限、并发租约、账号本地调用上限、请求/响应大小限制 |
| 日志 | 每次请求的尝试轨迹与标准化错误；不保存搜索词明文或搜索结果；默认 30 天保留 |
| 云部署 | Docker Compose；Caddy HTTPS；PostgreSQL + Redis；仅反向代理映射公网端口 |

## 架构

```text
Internet :80 / :443
        |
      Caddy                 /admin/ -> Next.js 静态后台
        |                   /api/admin/* -> Session + CSRF
      FastAPI               /v1/search -> Bearer API Key
        |
  PostgreSQL + Redis
        |
  Tavily / Exa / Brave
```

前端使用 Next.js 静态导出，生产环境不需要常驻 Node 服务。后端不接受任意供应商 URL，也不提供任意网页抓取，避免把个人服务器变成开放代理。

## 部署到云服务器

准备一个指向服务器的域名、Docker Engine + Docker Compose，以及能够访问三家官方 API 的服务器网络。公网开放 TCP 80、443；SSH 仅允许必要来源。不要额外映射 8000、5432、6379。

```bash
git clone https://github.com/wang-love-yu/multi-search-gateway.git
cd multi-search-gateway

# 用你自己的域名替换示例。此命令生成权限为 0600 的 .env，并显示一次初始登录密码。
python3 scripts/init_env.py --domain search.example.com --username owner

# 检查配置但不打印密钥
docker compose config --quiet

docker compose up -d --build
```

打开 `https://search.example.com/admin/`，使用初始化命令显示的用户名、密码登录。Caddy 自动申请 HTTPS 证书，前提是域名解析和公网 80/443 可达。没有默认搜索 API Key，也没有 `admin/admin` 默认登录。

初始化脚本 **不会覆盖已有 `.env`**，防止意外更换主加密密钥导致所有供应商 Key 无法解密。首次启动后管理员密码保存在数据库中；修改 `.env` 的初始密码不会重置现有账号。

### 第一次使用

1. 在“账号与密钥”添加供应商账号。**同一个账号或团队的多个 Key 必须归入同一个账号记录**，不要把共享额度的 Key 当成独立额度。
2. 给账号添加上游 Key，按需点击“测试”。测试会发送真实搜索，可能消耗供应商额度，也受本地预算与限速约束。
3. 在“自动切换”调整 Tavily / Exa / Brave 的顺序。
4. 在“访问 API”生成自己的 `sk_live_...`，复制保存。完整上游 Key 不会再从后台返回。
5. 使用下面的 HTTP 接口调用。

### 统一 API

```bash
curl 'https://search.example.com/v1/search' \
  -H 'Authorization: Bearer YOUR_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "vector database documentation",
    "provider": "auto",
    "limit": 5,
    "freshness": "week"
  }'
```

| 字段 | 说明 |
| --- | --- |
| `query` | 必填，非空，最多 2000 字符 |
| `provider` | 默认 `auto`；或 `tavily` / `exa` / `brave` |
| `limit` | 1–10，默认 5 |
| `freshness` | 可选：`day` / `week` / `month` / `year` |

未知参数会被拒绝，不会悄悄丢弃。目前不接收 `language`、`country`、供应商专有选项或任意 URL。各家的时间过滤语义和索引不同，不保证相同参数得到完全相同范围；Exa 的 month/year 分别按 30/365 天转换。

```json
{
  "id": "request-id",
  "provider": "exa",
  "query": "vector database documentation",
  "results": [
    {
      "title": "Example documentation",
      "url": "https://example.com/docs",
      "snippet": "A relevant excerpt...",
      "published_at": null
    }
  ],
  "attempts": 2,
  "latency_ms": 1200
}
```

此处是结构示例，不是实际搜索结果。没有合并或重排三个平台的结果；返回第一家成功响应。`attempts` 是上游尝试次数，不代表官方账单 credits。空结果是成功响应，不自动再花额度搜索。

## 自动切换的准确规则

- `provider=auto`：按后台顺序尝试供应商，然后按账号优先级和 Key 最近使用时间选择。
- `provider=exa` 等明确指定：只尝试该供应商内部的候选账号/Key，**不会**跨平台切换。
- 上游 `401`：该 Key 标记无效，尝试同账号其他有效 Key。
- 明确额度耗尽：停用整个账号的自动路由，切下一个账号/平台；不继续试同账号其他 Key。
- `429`：通常按账号限流处理，尊重 `Retry-After` 冷却；已识别的额度错误码单独归类。
- `403`：账号标记 blocked，等待人工核对，不当作单纯 Key 错误反复轮换。
- 网络错误、超时、5xx、格式异常：该供应商短暂冷却，尝试其他平台。
- 上游参数错误：结束请求，不通过换平台掩盖错误。

默认单次上游调用最多 6 秒，整次搜索预算 15 秒、最多 4 次上游尝试。预算是路由层时间控制，不是包含所有数据库排队和网络传输的严格墙钟 SLA。

### 额度：必须区分两种数值

**官方余额**由供应商决定；本项目第一期没有实现三家的官方余额同步。

**本地调用上限**由你设置，用来保护账号。每次准备调用上游前，用数据库原子更新扣减一个本地尝试单位；失败和超时也保守计入，不退款。因此它可能高于实际计费次数，但不会把未知超时误认为“肯定未扣费”。同一账号所有 Key 共享这一计数。

某些请求或结果内容会使用不同数量的 credits，因此**本地调用数不等于免费额度、美元余额或账单**。请在供应商后台核对套餐、付费开关和余额；本项目不能保证供应商绝不扣费。

额度耗尽后，默认等待你人工核对并恢复。可以填写已确认的“一次性恢复时间”；届时仅重置一次，不猜测月初或账期，不会自动重新开启被 403 禁止的账号。恢复数据库备份后也应重新核对官方额度。

## 安全基线

账密登录，无公开注册、邮箱找回、OAuth 或 2FA。密码使用 Argon2id；会话保存在 Redis，生产 Cookie 使用 `__Host-`、HttpOnly、Secure、SameSite=Strict；管理写操作检查 Origin + CSRF。API Bearer Key 与后台会话互不授权。

上游 Key 使用 AES-256-GCM 加密，绑定供应商和记录 ID；网关 Key 只存 HMAC 摘要。主密钥只在服务器环境与独立备份中保存。加密主要保护数据库单独泄露，**不能防御服务器 root 或运行中的应用被完全攻破**。

登录限速、管理员接口限速、搜索限速/并发限制、请求体 64 KiB / 上游响应 2 MiB、固定上游域名、禁止 HTTP 重定向、SQL 参数化、脱敏日志、关闭公开 API schema 均已实现。Redis 故障时受保护接口拒绝服务，不退回不受限模式。

Caddy 只向后端传递它覆盖写入的客户端 IP Header。不要给后端额外配置公网 `ports`，否则会破坏代理信任边界。没有配置 CDN；接入 CDN 后需要按该 CDN 的正式 IP 范围另行配置可信代理，而不是信任任意 `X-Forwarded-For`。

静态后台部署时生成 inline script 的 CSP 哈希，不使用 `script-src 'unsafe-inline'`。数据库/Redis 位于独立 Docker 内网；后端和 Caddy 以非 root 运行。不要给容器挂载 Docker socket。

更多运维说明见 [部署与维护](docs/OPERATIONS.md)。

## 开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements-test.txt
cd backend
python -m pytest -q
```

单元/API 测试使用 SQLite、内存限流实现和 HTTP MockTransport，**不会调用真实搜索 API**。生产不能使用内存限流降级。真实 PostgreSQL/Redis 测试需要显式配置一次性测试服务，见测试文件及 CI。

```bash
cd frontend
npm install
npm run typecheck
npm run build
```

首次开发安装后保留 `package-lock.json`。本次离线开发环境不能解析 npm/PyPI 域名，因此尚未生成可验证的 npm 锁文件；Docker 构建支持有锁文件时用 `npm ci`，无锁文件时先安装生成。CI 会上传生成的锁文件和静态产物。提交锁文件后应统一使用 `npm ci`。

CI 配置包含后端测试、PostgreSQL/Redis 集成测试、前端类型检查与构建、Docker 镜像构建和 Caddy 配置校验。**配置存在不等于 CI 已通过**，以仓库 Actions 的具体运行结果为准。

## 目录

```text
backend/app/          配置、数据库、认证、适配器、路由、管理 API
backend/tests/        API、安全、故障转移、集成与初始化脚本测试
frontend/app/         中文 Next.js 管理后台
scripts/             初始化密钥、备份、CSP 哈希生成
deploy/              Caddy 配置与静态前端构建镜像
docs/                运维说明与测试报告
.github/workflows/   CI
```

## 当前边界

没有官方额度实时同步、原生供应商 API 透传、MCP、搜索结果缓存、计费、多租户、后台定时付费探测或付费套餐购买。没有幂等结果重放；客户端自动重试可能产生新的上游调用，应谨慎设置重试。

数据库由第一版模型初始化，并检查 schema version；尚未提供跨版本自动迁移。单实例启动是支持的部署方式，不能在未验证初始化/迁移流程的前提下直接扩成多个后端副本。

## 官方接口参考

- [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)：POST + Bearer；第一期固定 basic，关闭 auto_parameters 和完整正文。
- [Exa Search](https://exa.ai/docs/reference/search-api-guide-for-coding-agents)：POST + x-api-key；第一期 auto + highlights。
- [Brave Web Search](https://api-dashboard.search.brave.com/documentation/services/web-search)：**GET** + X-Subscription-Token。
- [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [Next.js Static Exports](https://nextjs.org/docs/app/guides/static-exports)
- [Caddy Configuration](https://caddyserver.com/docs/caddyfile/options)

仅用于你合法持有或获授权的账号。不要批量注册免费账号规避供应商限制；未来要对外转售服务时，应另行确认各家的许可条款。

# 第一版验证记录

## 在本次开发环境实际完成

命令：在 `backend/` 中执行 `python -m pytest -q --disable-warnings`。

结果：**53 passed, 2 skipped**（本地最近一次完整运行约 12 秒；耗时不是性能基准）。

覆盖：Tavily/Exa/Brave 请求方法、认证 Header 和基本参数；账号共享本地额度；432/433/429/403 账号级切换；401 Key 级切换；指定供应商不跨平台；服务级熔断；参数错误不重试；超时保守计数；单次恢复时间；API Key 撤销；Session/CSRF/Origin；账密和 API Key 认证隔离；密码更改撤销会话；加密/脱敏；重复 Key 拒绝；请求/响应大小限制；禁止上游重定向；Redis 故障拒绝访问；删除 Key 与在途请求的并发回归；环境初始化脚本不覆盖密钥。

Python 使用环境已有依赖运行：Python 3.13.5、FastAPI 0.128.2、Starlette 0.50.0、SQLAlchemy 2.0.50、Pydantic 2.13.4、httpx 0.28.1、cryptography 46.0.4、argon2-cffi 25.1.0、pytest 9.0.2。部署依赖中的 FastAPI 已选择较新版本，**本地结果不能等同于部署依赖集合验证**，需由 CI 安装 requirements 后再验证。

本地 TSX 语法检查：`page.tsx` 和 `layout.tsx` 各 **0 syntax diagnostics**。这是 TypeScript transpile 检查，不是完整 React/Next 类型检查或构建。

## 未在本地完成的验证

- 两项跳过测试分别需要真实 PostgreSQL 和 Redis。内存限流/SQLite 测试不能替代分布式服务验证。
- 环境无法解析 npm/PyPI 域名，安装依赖失败。因此 Next.js 依赖安装、完整类型检查、生产构建和 npm 锁文件生成未在本地完成。
- 本地没有 Docker、Caddy、PostgreSQL 或 Redis 可执行文件，无法实际运行 Compose 或校验 TLS/容器网络。
- 没有三家供应商真实 API Key，未调用真实搜索服务、未验证真实余额、实时故障或供应商账单。
- 未执行浏览器端到端测试、真实并发压力测试、依赖漏洞扫描或第三方安全审计。

## 已修复的问题

- 旧 README 将 Brave Web Search 写为 POST，已改为 GET，并增加三家请求契约测试。
- 账号额度耗尽或限流必须跳过同账号所有 Key，避免把共享额度错误当成多份额度。
- 在途搜索中删除 Key 时，完成日志不能假定 Key 记录仍存在，已修复并增加回归测试。
- 非预期适配器异常需记录为内部失败，避免日志永久显示正常执行中。
- 错误响应、校验响应不能回显 API Key 等输入，已做统一脱敏并测试。
- 增加主密钥数据库校验和初始化脚本的禁止覆盖保护，防止误换加密密钥。

## CI

仓库配置后端测试 + 真实 PostgreSQL/Redis 集成测试、前端类型检查与静态构建、两种 Docker 镜像构建及 Caddy 配置校验。请以 GitHub Actions 实际运行状态为准，本报告不会将“已配置 CI”写成“CI 已通过”。

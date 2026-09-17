# 部署与维护

## 启动与诊断

首次部署按 README 生成 `.env` 后运行 `docker compose up -d --build`。镜像构建需要能访问 npm、PyPI、Docker Hub；后端运行时需要能访问三家官方 API。域名解析、系统时间和 TLS 证书申请必须正常。

```bash
docker compose ps
docker compose logs --tail=100 backend
docker compose logs --tail=100 caddy
docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
```

不要把 `.env`、`docker compose config` 的展开输出或带 Authorization 的 curl 调试输出发到公开 Issue。检查 Compose 时使用 `docker compose config --quiet`。

资源限额在 Compose 中有默认设置，但没有经过真实压力测试，不能当作容量承诺。首次 Next.js 构建会额外使用内存，必要时在开发机/CI 构建镜像后再部署。镜像使用版本系列标签；生产升级前应记录实际镜像 digest，关键依赖升级先跑测试，不要盲目自动更新。

## 密码与会话

初始密码由脚本生成；后台可以修改密码。数据库已有管理员时不会被 BOOTSTRAP_PASSWORD 覆盖。

忘记密码时，通过服务器 SSH 交互恢复：

```bash
docker compose exec backend python -m app.cli reset-password
```

输入新密码两次，不把密码放在命令行参数中。修改密码或 CLI 重置会递增会话版本，使所有旧会话失效。会话固定 12 小时有效，未实现滑动续期。

## 密钥

`.env` 中的 `MASTER_KEY` 与数据库中的加密校验值绑定。使用错误主密钥启动会失败，不会继续以损坏配置运行。不要为了“更新密码”重建 MASTER_KEY。

上游 Key 在后台添加后仅显示尾号。网关 API Key 重新生成后旧 Key 立即失效，需要同步修改调用端配置。本期没有双 Key 过渡期。

主密钥轮换尚未实现；必须备份旧密钥，并在实现显式重新加密迁移前保持其不变。服务器权限丢失/主机被攻破时，应在供应商后台撤销上游 Key，再重建服务器和网关凭据。

## 备份与恢复

```bash
bash scripts/backup.sh
```

脚本生成私有 PostgreSQL dump，不包含 MASTER_KEY。数据库里仍有管理员密码哈希及加密 Key，dump 也属于敏感文件。将 dump 和主密钥分别安全备份，不要上传进 Git 仓库。

恢复前备份现有数据库，并停止写入。以下命令会清理和替换数据库对象，只在明确要恢复时执行：

```bash
docker compose stop backend caddy
cat backups/YOUR_BACKUP.dump | docker compose exec -T postgres pg_restore -U gateway -d gateway --clean --if-exists --no-owner
# 确认服务器使用与该备份匹配的 MASTER_KEY，再启动。
docker compose up -d backend caddy
```

需要恢复同版本 schema；本期没有跨版本迁移器。恢复旧快照会回滚本地额度计数，应先停用相关账号并核对供应商用量。不要直接 `docker compose down -v`，该命令会删除数据卷。

Redis 用 AOF 持久化，但异常断电可能损失最近一小段状态。网关日限额是保护措施而非严格账单；数据库里的账号尝试上限仍单独生效。

## 日志与失败

请求日志仅保存请求 ID、来源、结果状态、各次尝试、耗时与查询 HMAC，不保存查询明文、供应商原始响应、Key 或搜索结果。审计记录登录、密码、Key、路由和配置变更，不保存 IP 地理位置推断。

日志清理每小时执行一次，默认删除 30 天前的请求、尝试和审计记录。进程被强制终止时，已经发出的尝试可能保持 `in_flight`、请求保持 `running`；这意味着结果未知，不能据此判断上游没有收费。

`quota_exhausted` 应在供应商额度恢复后手动恢复或设置已确认的一次性恢复时间；`blocked` 需排查供应商权限/条款，不会自动按账期解除。服务级故障会短暂冷却 15 秒。Key 测试也受启用状态、本地预算和冷却限制。

## 上线前核对

确认后台只能通过 HTTPS 登录；匿名管理请求返回 401；不存在默认 API Key；数据库/Redis/后端没有公网端口；三家真实 Key 分别通过一次受控测试；手动制造停用/低本地预算后自动切换符合预期；备份可恢复。

真实供应商测试可能计费，应由你输入 Key 后在后台明确触发。本仓库不包含真实凭据，自动化测试也不读取个人搜索平台账户。

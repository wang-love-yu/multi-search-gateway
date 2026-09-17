'use client';

import { useEffect, useState, type FormEvent, type ReactNode } from 'react';

type ProviderName = 'tavily' | 'exa' | 'brave';
type Provider = { name: ProviderName; enabled: boolean; priority: number; cooldown_until: number };
type Account = { id: string; provider: ProviderName; name: string; enabled: boolean; status: string;
  priority: number; attempt_limit: number; attempts_used: number; rpm: number; cooldown_until: number; reset_at: number | null };
type Credential = { id: string; account_id: string; name: string; hint: string; enabled: boolean;
  status: string; last_used_at: number; last_error: string };
type Config = { api_key_hint: string; api_enabled: boolean; rpm: number; daily_limit: number;
  concurrency: number; max_attempts: number; timeout_seconds: number };
type Overview = { providers: Provider[]; accounts: Account[]; credentials: Credential[]; config: Config;
  stats: { requests_24h: number; success_24h: number; average_latency_ms: number } };
type Attempt = { provider: string; account_id: string; credential_id: string; outcome: string; http_status: number; latency_ms: number };
type Log = { id: string; created_at: number; source: string; status: string; provider: string; latency_ms: number; attempts: Attempt[] };
type Session = { username: string; csrf: string };
type Tab = 'overview' | 'providers' | 'routing' | 'api' | 'logs' | 'settings';
const names: Record<ProviderName, string> = { tavily: 'Tavily', exa: 'Exa', brave: 'Brave' };
const tabs: [Tab, string, string][] = [['overview', '总览', '01'], ['providers', '账号与密钥', '02'],
  ['routing', '自动切换', '03'], ['api', '访问 API', '04'], ['logs', '请求日志', '05'], ['settings', '设置', '06']];
const messages: Record<string, string> = {
  invalid_credentials: '用户名或密码不正确。', login_rate_limit: '登录尝试过于频繁，请稍后重试。',
  csrf_rejected: '页面验证已失效，请刷新后重试。', origin_rejected: '站点域名与 PUBLIC_ORIGIN 不一致，请检查服务器配置。',
  duplicate_or_conflicting_record: '账号名称或 Key 已存在，请勿重复添加。', invalid_request: '输入不符合要求，请检查字段。',
  no_provider_available: '没有可用的搜索源。请检查启用状态、额度、冷却时间和请求日志。',
  current_password_invalid: '当前密码不正确。', service_unavailable: '服务暂时不可用，请检查服务器日志。',
};
const when = (n: number | null) => n ? new Date(n * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
const statusNames: Record<string, string> = { active: '可用', quota_exhausted: '上游额度耗尽', blocked: '上游拒绝访问',
  invalid: 'Key 无效', success: '成功', no_provider_available: '无可用来源', running: '执行中', cancelled: '已取消' };
const statusName = (status: string) => statusNames[status] || status;

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}</label>;
}
function Badge({ ok, children }: { ok: boolean; children: ReactNode }) {
  return <span className={`badge ${ok ? 'good' : 'muted'}`}><i />{children}</span>;
}

export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [data, setData] = useState<Overview | null>(null);
  const [logs, setLogs] = useState<Log[]>([]);
  const [offset, setOffset] = useState(0);
  const [tab, setTab] = useState<Tab>('overview');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [newKey, setNewKey] = useState('');
  const [editing, setEditing] = useState<Account | null>(null);
  const [origin, setOrigin] = useState('https://your-domain.example');

  async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
    const response = await fetch('/api/admin' + path, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', ...(session ? { 'X-CSRF-Token': session.csrf } : {}) },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    let result;
    try { result = await response.json(); } catch { throw new Error('服务响应异常，请检查反向代理与后端状态。'); }
    if (!response.ok) {
      const code = result.error?.code || `HTTP ${response.status}`;
      if (response.status === 401 && path !== '/login') { setSession(null); setNewKey(''); setData(null); }
      throw new Error(messages[code] || code);
    }
    return result as T;
  }
  async function refresh(page = offset) {
    const [overview, history] = await Promise.all([api<Overview>('/overview'), api<{ items: Log[] }>(`/logs?offset=${page}&limit=30`)]);
    setData(overview); setLogs(history.items); setOffset(page);
  }
  async function act(operation: () => Promise<unknown>, success = '已保存') {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { const result = await operation(); setNotice(typeof result === 'string' ? result : success); } catch (e) { setError(e instanceof Error ? e.message : '操作失败'); }
    finally { setBusy(false); }
  }
  useEffect(() => {
    setOrigin(window.location.origin);
    api<Session>('/session').then(async s => { setSession(s); await refresh(0); })
      .catch(e => { if (e.message !== 'login_required') setError(e.message); })
      .finally(() => setChecking(false));
    // Session cookies, not browser storage, hold authentication state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const values = new FormData(form);
    void act(async () => {
      const s = await api<Session>('/login', 'POST', { username: values.get('username'), password: values.get('password') });
      setSession(s); form.reset(); await refresh(0);
    }, '登录成功');
  }
  function addAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const values = new FormData(form);
    void act(async () => {
      await api('/accounts', 'POST', { provider: values.get('provider'), name: values.get('name'),
        attempt_limit: Number(values.get('attempt_limit')), rpm: Number(values.get('rpm')) });
      form.reset(); await refresh();
    }, '账号已添加，接下来为它添加 Key');
  }
  function addKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const values = new FormData(form);
    void act(async () => {
      await api('/credentials', 'POST', { account_id: values.get('account_id'), name: values.get('name'), key: values.get('key') });
      form.reset(); await refresh();
    }, 'Key 已加密保存');
  }
  async function updateRouting(providers: Provider[]) {
    await api('/routing', 'PUT', { providers: providers.map(p => ({ name: p.name, enabled: p.enabled })) });
    await refresh();
  }
  function saveAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!editing) return;
    const values = new FormData(event.currentTarget); const accountId = editing.id;
    const reset = String(values.get('reset_at') || '');
    void act(async () => {
      await api(`/accounts/${accountId}`, 'PATCH', {
        priority: Number(values.get('priority')), attempt_limit: Number(values.get('attempt_limit')),
        rpm: Number(values.get('rpm')), reset_at: reset ? new Date(reset).getTime() / 1000 : null,
      });
      setEditing(null); await refresh();
    });
  }
  function settings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!data) return;
    const values = new FormData(event.currentTarget);
    void act(async () => {
      await api('/settings', 'PUT', { api_enabled: data.config.api_enabled,
        ...Object.fromEntries(['rpm', 'daily_limit', 'concurrency', 'max_attempts', 'timeout_seconds'].map(k => [k, Number(values.get(k))])) });
      await refresh();
    });
  }
  function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const values = new FormData(form);
    void act(async () => {
      await api('/password', 'POST', { current_password: values.get('current_password'), new_password: values.get('new_password') });
      form.reset(); setSession(null); setData(null); setNewKey('');
    }, '密码已修改，请重新登录');
  }

  if (checking) return <div className="loading" role="status">正在连接控制台…</div>;
  if (!session) return <div className="login-shell">
    <section className="login-story"><span className="eyebrow">PERSONAL SEARCH INFRASTRUCTURE</span>
      <div className="brand large">S<span>/</span>G</div><h1>一个入口。<br />多个搜索来源。</h1>
      <p>管理你的搜索密钥，让请求在可用来源之间自动接力。</p>
      <div className="provider-chips"><span>Tavily</span><span>Exa</span><span>Brave</span></div>
      <small>PERSONAL EDITION · HTTP API</small>
    </section>
    <section className="login-panel"><span className="eyebrow">WELCOME BACK</span><h2>登录管理后台</h2>
      <p className="subtle">使用服务器初始化时设置的管理员账号。</p>
      <form onSubmit={login}><fieldset disabled={busy}>
        <Field label="用户名"><input name="username" required maxLength={80} autoComplete="username" autoFocus /></Field>
        <Field label="密码"><input name="password" type="password" required maxLength={128} autoComplete="current-password" /></Field>
        <button className="primary wide" type="submit">{busy ? '正在验证…' : '登录控制台 →'}</button>
      </fieldset></form>
      {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="notice">{notice}</p>}
      <p className="login-foot">单管理员 · 无开放注册</p>
    </section>
  </div>;

  return <div className="app-shell"><aside className="sidebar">
    <div className="wordmark"><div className="brand">S<span>/</span>G</div><div>Search Gateway<small>PERSONAL CONSOLE</small></div></div>
    <nav aria-label="主导航">{tabs.map(([id, label, no]) => <button key={id} className={tab === id ? 'nav active' : 'nav'}
      aria-current={tab === id ? 'page' : undefined} onClick={() => setTab(id)}><span>{no}</span>{label}</button>)}</nav>
    <div className="sidebar-foot"><span className="dot" /> 私人工作空间<small>v0.1 · API Gateway</small></div>
  </aside><div className="workspace"><header className="topbar"><span>工作空间 <b>/ {tabs.find(t => t[0] === tab)?.[1]}</b></span>
    <div><span className="owner">{session.username}</span><button disabled={busy} onClick={() => void act(async () => {
      await api('/logout', 'POST'); setSession(null); setData(null); setNewKey('');
    }, '已退出')}>退出</button></div></header>
    <main aria-busy={busy}><div className="page-heading"><div><span className="eyebrow">YOUR SEARCH, UNDER CONTROL</span>
      <h1>{tabs.find(t => t[0] === tab)?.[1]}</h1></div><button disabled={busy} onClick={() => void act(() => refresh(), '数据已刷新')}>↻ 刷新</button></div>
      {error && <div role="alert" className="error">{error}</div>}{notice && <div role="status" className="notice">{notice}</div>}
      {!data ? <div className="empty">暂未取得数据。请检查服务状态后点击刷新。</div> : <>
      {tab === 'overview' && <>
        <div className="metrics"><article><span>过去 24 小时请求</span><strong>{data.stats.requests_24h.toLocaleString()}</strong><small>包括手动 Key 测试</small></article>
          <article><span>请求成功率</span><strong>{data.stats.requests_24h ? `${(100 * data.stats.success_24h / data.stats.requests_24h).toFixed(1)}%` : '—'}</strong><small>{data.stats.success_24h} 次成功</small></article>
          <article><span>成功请求平均耗时</span><strong>{data.stats.average_latency_ms}<em> ms</em></strong><small>含自动切换耗时</small></article></div>
        <section className="panel"><div className="section-heading"><h2>搜索来源</h2><span className="subtle">当前配置，不代表实时探测结果</span></div>
          <div className="provider-grid">{data.providers.map((p, i) => <article key={p.name} className="provider-card">
            <div className="section-heading"><span className={`provider-letter ${p.name}`}>{names[p.name][0]}</span><span className="subtle">优先级 {i + 1}</span></div>
            <h3>{names[p.name]}</h3><p>{data.accounts.filter(a => a.provider === p.name).length} 个账号 · {data.credentials.filter(c => data.accounts.some(a => a.id === c.account_id && a.provider === p.name)).length} 个 Key</p>
            <Badge ok={p.enabled && p.cooldown_until < Date.now() / 1000}>{!p.enabled ? '已停用' : p.cooldown_until > Date.now() / 1000 ? '冷却中' : '已启用'}</Badge>
          </article>)}</div></section>
        <section className="panel callout"><div><h2>开始使用</h2><p>添加账号 → 添加上游 Key → 生成网关 Key → 调用统一 API。</p></div><button onClick={() => setTab('providers')}>管理账号 →</button></section>
      </>}
      {tab === 'providers' && <>
        <p className="subtle">同一供应商账号下的多个 Key 共享本地预算与限速。只接入你拥有或获授权的账号。</p>
        <div className="two-columns"><section className="panel"><h2>01 / 添加供应商账号</h2><form onSubmit={addAccount}><fieldset disabled={busy}>
          <div className="form-row"><Field label="供应商"><select name="provider">{Object.entries(names).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
            <Field label="账号名称"><input name="name" placeholder="例如：个人主账号" required maxLength={80} /></Field></div>
          <div className="form-row"><Field label="本地调用上限"><input name="attempt_limit" type="number" min={1} max={10000000} defaultValue={1000} required /></Field>
            <Field label="账号每分钟上限"><input name="rpm" type="number" min={1} max={1000} defaultValue={30} required /></Field></div>
          <p className="hint">这是网关保护上限，不是官方余额。失败尝试也计入，避免误估费用。</p><button className="primary" type="submit">添加账号</button>
        </fieldset></form></section>
        <section className="panel"><h2>02 / 添加上游 Key</h2><form onSubmit={addKey}><fieldset disabled={busy || !data.accounts.length}>
          <Field label="所属账号"><select name="account_id" required>{data.accounts.map(a => <option key={a.id} value={a.id}>{names[a.provider]} / {a.name}</option>)}</select></Field>
          <div className="form-row"><Field label="Key 名称"><input name="name" required maxLength={80} placeholder="例如：主密钥" /></Field>
            <Field label="API Key"><input name="key" type="password" required minLength={8} maxLength={512} autoComplete="off" placeholder="粘贴供应商 Key" /></Field></div>
          <p className="hint">加密保存；保存后只显示尾号，不再返回明文。</p><button className="primary" type="submit">加密保存</button>
        </fieldset></form></section></div>
        {!data.accounts.length && <div className="empty">还没有账号，从上面的表单开始。</div>}
        {data.accounts.map(a => <section className="panel" key={a.id}><div className="section-heading"><div><h2>{names[a.provider]} <span className="subtle">/</span> {a.name}</h2>
          <p className="hint">本地调用 {a.attempts_used} / {a.attempt_limit} · {a.rpm} 次/分钟 · {statusName(a.status)}{a.reset_at ? ` · 一次性恢复时间 ${when(a.reset_at)}` : ''}{a.cooldown_until > Date.now()/1000 ? ` · 冷却至 ${when(a.cooldown_until)}` : ''}</p></div>
          <div className="actions"><button disabled={busy} onClick={() => setEditing(a)}>编辑</button><button disabled={busy} onClick={() => void act(async () => { await api(`/accounts/${a.id}`, 'PATCH', { enabled: !a.enabled }); await refresh(); })}>{a.enabled ? '停用账号' : '启用账号'}</button>
            <button disabled={busy} onClick={() => { if (confirm('确认已核对官方额度并恢复该账号？这只会重置本地调用计数，不会增加供应商额度。')) void act(async () => { await api(`/accounts/${a.id}/reset`, 'POST'); await refresh(); }); }}>重置本地计数</button></div></div>
          <div className="table-wrap"><table><thead><tr><th>密钥</th><th>状态</th><th>最近使用</th><th>最近错误</th><th>操作</th></tr></thead><tbody>
            {data.credentials.filter(c => c.account_id === a.id).map(c => <tr key={c.id}><td><b>{c.name}</b><small>{c.hint}</small></td><td><Badge ok={c.enabled && c.status === 'active'}>{c.enabled ? statusName(c.status) : '已停用'}</Badge></td>
              <td>{when(c.last_used_at)}</td><td><code>{c.last_error || '—'}</code></td><td><div className="actions">
                <button disabled={busy} onClick={() => { if (confirm('执行一次真实搜索测试，可能消耗供应商额度。继续？')) void act(async () => {
                  const r = await api<{ latency_ms: number }>(`/credentials/${c.id}/test`, 'POST'); await refresh(); return `测试成功 · ${r.latency_ms} ms`;
                }, '测试完成，请查看请求日志'); }}>测试</button>
                <button disabled={busy} onClick={() => void act(async () => { await api(`/credentials/${c.id}`, 'PATCH', { enabled: !c.enabled }); await refresh(); })}>{c.enabled ? '停用' : '启用'}</button>
                {c.status !== 'active' && <button disabled={busy} onClick={() => void act(async () => { await api(`/credentials/${c.id}/resume`, 'POST'); await refresh(); })}>重新尝试</button>}
                <button className="danger-link" disabled={busy} onClick={() => { if (confirm('删除此 Key？该操作会删除加密密钥，但保留历史调用日志。')) void act(async () => { await api(`/credentials/${c.id}`, 'DELETE'); await refresh(); }); }}>删除</button>
              </div></td></tr>)}
            {!data.credentials.some(c => c.account_id === a.id) && <tr><td colSpan={5} className="subtle">此账号还没有 Key。</td></tr>}
          </tbody></table></div></section>)}
      </>}
      {tab === 'routing' && <section className="panel"><h2>顺序优先，失败后接力</h2><p className="subtle">仅 provider=auto 时跨平台切换；指定某家时只使用该平台，不悄悄更换来源。</p>
        <div className="routing-list">{data.providers.map((p, index) => <div className="routing-row" key={p.name}><span className="rank">0{index + 1}</span><div className="grow"><h3>{names[p.name]}</h3><span className="subtle">{p.enabled ? '参与自动路由' : '不参与路由'}</span></div>
          <button disabled={busy || index === 0} aria-label={`${p.name} 上移`} onClick={() => { const rules = [...data.providers]; [rules[index-1], rules[index]] = [rules[index], rules[index-1]]; void act(() => updateRouting(rules)); }}>↑</button>
          <button disabled={busy || index === data.providers.length - 1} aria-label={`${p.name} 下移`} onClick={() => { const rules = [...data.providers]; [rules[index+1], rules[index]] = [rules[index], rules[index+1]]; void act(() => updateRouting(rules)); }}>↓</button>
          <button disabled={busy} onClick={() => void act(() => updateRouting(data.providers.map(x => x.name === p.name ? { ...x, enabled: !x.enabled } : x)))}>{p.enabled ? '停用' : '启用'}</button></div>)}</div>
        <div className="callout"><p>Key 认证失败 → 同账号下一个 Key。账号额度耗尽或限流 → 跳过整个账号。超时或服务故障 → 下一个供应商。最多尝试 {data.config.max_attempts} 次，总预算 {data.config.timeout_seconds} 秒。</p></div></section>}
      {tab === 'api' && <><section className="panel"><div className="section-heading"><div><h2>你的统一 API Key</h2><p className="subtle">后台登录与 API 调用使用独立凭据。</p></div><Badge ok={data.config.api_enabled}>{data.config.api_enabled ? '已启用' : '已停用'}</Badge></div>
        <div className="key-display"><code>{data.config.api_key_hint}</code><div className="actions"><button className="primary" disabled={busy} onClick={() => {
          if (confirm('生成新 Key 后，旧 Key 将立即失效。继续？')) void act(async () => { const r = await api<{ key: string }>('/api-key/rotate', 'POST'); setNewKey(r.key); await refresh(); }, '新 Key 已生成，请复制保存');
        }}>{data.config.api_key_hint === '未生成' ? '生成 Key' : '重新生成'}</button>
          <button disabled={busy} onClick={() => void act(async () => { const { api_key_hint: _, ...cfg } = data.config; await api('/settings', 'PUT', { ...cfg, api_enabled: !cfg.api_enabled }); await refresh(); })}>{data.config.api_enabled ? '停用访问' : '启用访问'}</button></div></div>
        <p className="hint">{data.config.rpm} 次/分钟 · {data.config.daily_limit} 次/UTC 日 · 最大并发 {data.config.concurrency}</p></section>
        <section className="panel"><h2>通过 HTTP 调用</h2><p className="subtle">将 YOUR_GATEWAY_KEY 替换为上方生成的密钥，不要填写供应商 Key。</p>
          <pre>{`curl '${origin}/v1/search' \\\n  -H 'Authorization: Bearer YOUR_GATEWAY_KEY' \\\n  -H 'Content-Type: application/json' \\\n  -d '{"query":"vector database documentation","provider":"auto","limit":5}'`}</pre>
          <p className="hint">provider：auto / tavily / exa / brave · limit：1–10 · freshness：day / week / month / year（可选）。一期不提供原生 API 透传、MCP 或结果融合。</p></section></>}
      {tab === 'logs' && <section className="panel"><div className="section-heading"><h2>请求与切换轨迹</h2><span className="subtle">不保存搜索词明文或搜索结果</span></div>
        {!logs.length ? <div className="empty">暂无请求记录。</div> : <div className="log-list">{logs.map(row => <details key={row.id} className="log-row"><summary>
          <Badge ok={row.status === 'success'}>{statusName(row.status)}</Badge><span>{when(row.created_at)}</span><code>{row.id.slice(0, 12)}</code><span>{row.source === 'key_test' ? 'Key 测试' : 'API'} · {row.provider || '—'}</span><b>{row.latency_ms} ms</b></summary>
          <div className="attempts">{row.attempts.map((a, i) => <div key={i}><b>尝试 {i+1} · {a.provider}</b><code>{a.outcome}</code><span>HTTP {a.http_status || '—'} · {a.latency_ms} ms</span></div>)}{!row.attempts.length && <p className="subtle">没有满足条件的账号，未调用上游。</p>}</div>
        </details>)}</div>}<div className="pagination"><button disabled={busy || offset === 0} onClick={() => void act(() => refresh(Math.max(0, offset-30)), '已加载')}>上一页</button><span>第 {offset / 30 + 1} 页</span><button disabled={busy || logs.length < 30} onClick={() => void act(() => refresh(offset+30), '已加载')}>下一页</button></div></section>}
      {tab === 'settings' && <div className="two-columns"><section className="panel"><h2>调用保护</h2><form onSubmit={settings} key={JSON.stringify(data.config)}><fieldset disabled={busy}>
        <div className="form-row"><Field label="每分钟请求上限"><input name="rpm" type="number" min={1} max={600} required defaultValue={data.config.rpm}/></Field><Field label="每日请求上限（UTC）"><input name="daily_limit" type="number" min={1} max={100000} required defaultValue={data.config.daily_limit}/></Field></div>
        <Field label="最大并发"><input name="concurrency" type="number" min={1} max={20} required defaultValue={data.config.concurrency}/></Field>
        <div className="form-row"><Field label="最多上游尝试次数"><input name="max_attempts" type="number" min={1} max={10} required defaultValue={data.config.max_attempts}/></Field><Field label="搜索总预算（秒）"><input name="timeout_seconds" type="number" min={3} max={30} required defaultValue={data.config.timeout_seconds}/></Field></div>
        <p className="hint">单次上游调用最多 6 秒；网络超时不代表供应商未扣费。</p><button type="submit" className="primary">保存设置</button></fieldset></form></section>
        <section className="panel"><h2>修改管理员密码</h2><form onSubmit={changePassword}><fieldset disabled={busy}>
          <Field label="当前密码"><input name="current_password" type="password" required maxLength={128} autoComplete="current-password" /></Field>
          <Field label="新密码（至少 16 位）"><input name="new_password" type="password" required minLength={16} maxLength={128} autoComplete="new-password" /></Field>
          <p className="hint">修改后所有已登录会话将失效。此应用不提供公开注册与邮件找回密码。</p><button type="submit">更新密码并退出</button>
        </fieldset></form></section></div>}
      </>}
      <footer>PERSONAL MULTI-SEARCH GATEWAY <span>仅管理你拥有或获授权的搜索账号</span></footer>
    </main></div>
    {newKey && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="key-title"><h2 id="key-title">复制你的新 API Key</h2><p>只显示这一次。关闭后无法查看明文；旧 Key 已失效。</p><textarea readOnly value={newKey} aria-label="新 API Key" rows={3}/><div className="actions"><button className="primary" onClick={() => void act(() => navigator.clipboard.writeText(newKey), '已复制到剪贴板')}>复制</button><button onClick={() => setNewKey('')}>我已保存，关闭</button></div></section></div>}
    {editing && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="account-title"><h2 id="account-title">编辑 {editing.name}</h2><form onSubmit={saveAccount}><fieldset disabled={busy}>
      <Field label="账号内优先级（越小越优先）"><input type="number" name="priority" min={0} max={1000} defaultValue={editing.priority} required/></Field>
      <div className="form-row"><Field label="本地调用上限"><input type="number" name="attempt_limit" min={1} max={10000000} defaultValue={editing.attempt_limit} required/></Field><Field label="账号每分钟上限"><input type="number" name="rpm" min={1} max={1000} defaultValue={editing.rpm} required/></Field></div>
      <Field label="一次性恢复时间（本地时区，可留空）"><input type="datetime-local" name="reset_at" defaultValue={editing.reset_at ? new Date(editing.reset_at * 1000 - new Date(editing.reset_at*1000).getTimezoneOffset()*60000).toISOString().slice(0,16) : ''}/></Field>
      <p className="hint">仅在你确认的官方额度恢复时间重置一次；不会按猜测的账期循环重置。</p><div className="actions"><button type="submit" className="primary">保存</button><button type="button" onClick={() => setEditing(null)}>取消</button></div>
    </fieldset></form></section></div>}
  </div>;
}

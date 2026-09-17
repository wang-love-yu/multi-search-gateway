"""HTTP API and single-administrator management API. Run with app.main:create_app --factory."""
import asyncio
import hmac
import logging
import secrets
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from .core import Account, Admin, Attempt, Audit, Config, Credential, Crypto, Database, Provider, RequestLog, Settings, uid
from .providers import Adapters, SearchInput
from .router import GatewayError, Router
from .schemas import AccountInput, AccountUpdate, ConfigInput, CredentialInput, Login, PasswordChange, RoutingInput, Toggle
from .state import MemoryState, RedisState

log = logging.getLogger('gateway')


def public_config(config):
    return {k: getattr(config, k) for k in ('api_key_hint', 'api_enabled', 'rpm', 'daily_limit',
            'concurrency', 'max_attempts', 'timeout_seconds')}


def public_credential(c):
    return {k: getattr(c, k) for k in ('id', 'account_id', 'name', 'hint', 'enabled', 'status', 'last_used_at', 'last_error')}


def public_account(a):
    return {k: getattr(a, k) for k in ('id', 'provider', 'name', 'priority', 'enabled', 'status',
            'attempt_limit', 'attempts_used', 'rpm', 'cooldown_until', 'reset_at')}


def create_app(settings=None, database=None, state=None, adapters=None):
    settings = settings or Settings()
    crypto = Crypto(settings.master_key.get_secret_value())
    database = database or Database(settings.database_url)
    state = state or RedisState(settings.redis_url)
    if settings.environment == 'production' and isinstance(state, MemoryState):
        raise RuntimeError('MemoryState is test-only')
    client = None
    if adapters is None:
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False, limits=httpx.Limits(max_connections=30))
        adapters = Adapters(client)
    router = Router(database, state, crypto, adapters)
    password_slots = asyncio.Semaphore(2)

    async def prune_logs():
        while True:
            await asyncio.sleep(3600)
            try:
                cutoff = time.time() - settings.log_retention_days * 86400
                with database.session() as db:
                    old = select(RequestLog.id).where(RequestLog.created_at < cutoff)
                    db.execute(delete(Attempt).where(Attempt.request_id.in_(old)))
                    db.execute(delete(RequestLog).where(RequestLog.created_at < cutoff))
                    db.execute(delete(Audit).where(Audit.created_at < cutoff))
            except Exception as error:
                log.warning('Log cleanup failed: %s', type(error).__name__)

    @asynccontextmanager
    async def lifespan(app):
        database.initialize(settings, crypto)
        await state.ping()
        cleanup = asyncio.create_task(prune_logs())
        yield
        cleanup.cancel()
        try:
            await cleanup
        except asyncio.CancelledError:
            pass
        if client:
            await client.aclose()
        await state.close()
        database.engine.dispose()

    app = FastAPI(title='Personal Multi-Search Gateway', version='0.1.0', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database, app.state.crypto, app.state.runtime = database, crypto, state

    def ip(request):
        if settings.trust_gateway_ip:
            return request.headers.get('x-gateway-client-ip', 'unknown')[:100]
        return request.client.host if request.client else 'unknown'

    def require_origin(request):
        if request.headers.get('origin') != settings.public_origin:
            raise HTTPException(403, 'origin_rejected')

    async def admin(request: Request):
        token = request.cookies.get(settings.cookie_name, '')
        if not token or len(token) > 200:
            raise HTTPException(401, 'login_required')
        session_key = 'session:' + crypto.digest(token, 'session')
        data = await state.get(session_key)
        with database.session() as db:
            owner = db.get(Admin, 1)
        if not data or not owner or data['version'] != owner.session_version:
            raise HTTPException(401, 'login_required')
        if not await state.allow('admin:' + session_key, 120, 60):
            raise HTTPException(429, 'admin_rate_limit')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            require_origin(request)
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), data['csrf']):
                raise HTTPException(403, 'csrf_rejected')
        request.state.session_key = session_key
        return data

    async def public_auth(request: Request):
        if not await state.allow('auth:' + crypto.digest(ip(request), 'ip'), 120, 60):
            raise HTTPException(429, 'authentication_rate_limit')
        header = request.headers.get('authorization', '')
        with database.session() as db:
            config = db.get(Config, 1)
        token = header[7:] if header.startswith('Bearer ') else ''
        if not token or len(token) > 200 or not config.api_enabled or not config.api_key_hash or not hmac.compare_digest(
                crypto.digest(token, 'api'), config.api_key_hash):
            raise HTTPException(401, 'invalid_api_key')
        if not await state.allow('api:minute', config.rpm, 60):
            raise HTTPException(429, 'api_minute_limit')
        if not await state.allow('api:day', config.daily_limit, 86400):
            raise HTTPException(429, 'api_daily_limit')

    @app.middleware('http')
    async def boundary(request, call_next):
        request.state.request_id = uid()
        try:
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise HTTPException(413, 'request_too_large')
                request._body = bytes(body)
                if body and request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
                    raise HTTPException(415, 'json_required')
            response = await call_next(request)
        except HTTPException as error:
            response = JSONResponse({'error': {'code': str(error.detail)}}, status_code=error.status_code)
        except Exception as error:
            log.error('Request failed id=%s class=%s', request.state.request_id, type(error).__name__)
            response = JSONResponse({'error': {'code': 'service_unavailable'}}, status_code=503)
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse({'error': {'code': str(error.detail)}}, status_code=error.status_code,
                            headers={'Retry-After': '60'} if error.status_code == 429 else {})

    @app.exception_handler(GatewayError)
    async def gateway_error(request, error):
        return JSONResponse({'error': {'code': error.code, 'request_id': request.state.request_id}},
                            status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # FastAPI's default validation body includes rejected inputs, which can contain credentials.
        return JSONResponse({'error': {'code': 'invalid_request', 'fields': [
            {'location': list(e['loc']), 'type': e['type']} for e in error.errors()]}}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def duplicate_error(request, error):
        return JSONResponse({'error': {'code': 'duplicate_or_conflicting_record'}}, status_code=409)

    @app.get('/healthz')
    async def health():
        await state.ping()
        with database.session() as db:
            db.execute(text('SELECT 1'))
        return {'status': 'ok'}

    @app.post('/api/admin/login')
    async def login(payload: Login, request: Request, response: Response):
        require_origin(request)
        if not await state.allow('login:' + crypto.digest(ip(request), 'ip'), 5, 60) or not await state.allow('login:global', 20, 900):
            raise HTTPException(429, 'login_rate_limit')
        with database.session() as db:
            owner = db.get(Admin, 1)
        async with password_slots:
            valid = await run_in_threadpool(crypto.verify_password, owner.password_hash, payload.password.get_secret_value())
        if not valid or not hmac.compare_digest(owner.username.encode(), payload.username.encode()):
            database.audit('login_failed')
            raise HTTPException(401, 'invalid_credentials')
        old = request.cookies.get(settings.cookie_name)
        if old:
            await state.delete('session:' + crypto.digest(old, 'session'))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        await state.put('session:' + crypto.digest(token, 'session'),
                        {'username': owner.username, 'version': owner.session_version, 'csrf': csrf},
                        settings.session_hours * 3600)
        response.set_cookie(settings.cookie_name, token, httponly=True, secure=settings.secure_cookie,
                            samesite='strict', path='/', max_age=settings.session_hours * 3600)
        database.audit('login_success')
        return {'username': owner.username, 'csrf': csrf}

    @app.get('/api/admin/session')
    async def current_session(data=Depends(admin)):
        return {'username': data['username'], 'csrf': data['csrf']}

    @app.post('/api/admin/logout', dependencies=[Depends(admin)])
    async def logout(request: Request, response: Response):
        await state.delete(request.state.session_key)
        response.delete_cookie(settings.cookie_name, path='/', secure=settings.secure_cookie,
                               httponly=True, samesite='strict')
        return {'ok': True}

    @app.post('/api/admin/password', dependencies=[Depends(admin)])
    async def password(payload: PasswordChange, request: Request):
        if not await state.allow('password-change', 5, 300):
            raise HTTPException(429, 'password_rate_limit')
        with database.session() as db:
            owner = db.get(Admin, 1)
        async with password_slots:
            valid = await run_in_threadpool(crypto.verify_password, owner.password_hash, payload.current_password.get_secret_value())
        if not valid:
            raise HTTPException(403, 'current_password_invalid')
        async with password_slots:
            password_hash = await run_in_threadpool(crypto.passwords.hash, payload.new_password.get_secret_value())
        with database.session() as db:
            owner = db.get(Admin, 1)
            owner.password_hash = password_hash
            owner.session_version += 1
        await state.delete(request.state.session_key)
        database.audit('password_changed')
        return {'ok': True, 'login_required': True}

    @app.get('/api/admin/overview', dependencies=[Depends(admin)])
    async def overview():
        with database.session() as db:
            providers = [{k: getattr(p, k) for k in ('name', 'enabled', 'priority', 'cooldown_until')}
                         for p in db.scalars(select(Provider).order_by(Provider.priority))]
            accounts = [public_account(a) for a in db.scalars(select(Account).order_by(Account.provider, Account.priority, Account.name))]
            credentials = [public_credential(c) for c in db.scalars(select(Credential).order_by(Credential.name))]
            cutoff = time.time() - 86400
            count = db.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= cutoff))
            success = db.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= cutoff, RequestLog.status == 'success'))
            avg = db.scalar(select(func.avg(RequestLog.latency_ms)).where(RequestLog.created_at >= cutoff, RequestLog.status == 'success'))
            return {'providers': providers, 'accounts': accounts, 'credentials': credentials,
                    'config': public_config(db.get(Config, 1)),
                    'stats': {'requests_24h': count, 'success_24h': success, 'average_latency_ms': int(avg or 0)}}

    @app.post('/api/admin/accounts', dependencies=[Depends(admin)])
    async def add_account(payload: AccountInput):
        with database.session() as db:
            account = Account(**payload.model_dump())
            db.add(account)
            db.flush()
            result = public_account(account)
        database.audit('account_created', result['id'])
        return result

    @app.patch('/api/admin/accounts/{account_id}', dependencies=[Depends(admin)])
    async def update_account(account_id: str, payload: AccountUpdate):
        with database.session() as db:
            account = db.get(Account, account_id)
            if not account:
                raise HTTPException(404, 'account_not_found')
            for key, value in payload.model_dump(exclude_unset=True).items():
                if value is None and key != 'reset_at':
                    raise HTTPException(422, 'null_not_allowed')
                setattr(account, key, value)
        database.audit('account_updated', account_id)
        return {'ok': True}

    @app.post('/api/admin/accounts/{account_id}/reset', dependencies=[Depends(admin)])
    async def reset_account(account_id: str):
        with database.session() as db:
            account = db.get(Account, account_id)
            if not account:
                raise HTTPException(404, 'account_not_found')
            account.attempts_used, account.status, account.cooldown_until = 0, 'active', 0
            account.reset_at = None
        database.audit('account_local_budget_reset', account_id)
        return {'ok': True}

    @app.post('/api/admin/credentials', dependencies=[Depends(admin)])
    async def add_credential(payload: CredentialInput):
        with database.session() as db:
            account = db.get(Account, payload.account_id)
            if not account:
                raise HTTPException(404, 'account_not_found')
            key, identifier = payload.key.get_secret_value(), uid()
            credential = Credential(id=identifier, account_id=account.id, name=payload.name,
                                    encrypted_key=crypto.encrypt(key, account.provider + ':' + identifier),
                                    fingerprint=crypto.digest(key, 'provider-key'), hint='••••' + key[-4:])
            db.add(credential)
            db.flush()
            result = public_credential(credential)
        database.audit('credential_created', identifier)
        return result

    @app.patch('/api/admin/credentials/{key_id}', dependencies=[Depends(admin)])
    async def toggle_credential(key_id: str, payload: Toggle):
        with database.session() as db:
            credential = db.get(Credential, key_id)
            if not credential:
                raise HTTPException(404, 'credential_not_found')
            credential.enabled = payload.enabled
        database.audit('credential_toggled', key_id)
        return {'ok': True}

    @app.delete('/api/admin/credentials/{key_id}', dependencies=[Depends(admin)])
    async def delete_credential(key_id: str):
        with database.session() as db:
            credential = db.get(Credential, key_id)
            if not credential:
                raise HTTPException(404, 'credential_not_found')
            db.delete(credential)
        database.audit('credential_deleted', key_id)
        return {'ok': True}

    @app.post('/api/admin/credentials/{key_id}/resume', dependencies=[Depends(admin)])
    async def resume_credential(key_id: str):
        with database.session() as db:
            credential = db.get(Credential, key_id)
            if not credential:
                raise HTTPException(404, 'credential_not_found')
            credential.status, credential.last_error = 'active', ''
        database.audit('credential_resumed', key_id)
        return {'ok': True}

    @app.post('/api/admin/credentials/{key_id}/test', dependencies=[Depends(admin)])
    async def test_credential(key_id: str, request: Request):
        if not await state.allow('key-tests', 5, 60):
            raise HTTPException(429, 'test_rate_limit')
        with database.session() as db:
            if not db.get(Credential, key_id):
                raise HTTPException(404, 'credential_not_found')
        result = await router.search(SearchInput(query='connection test', limit=1),
                                     request.state.request_id, source='key_test', target_key=key_id)
        database.audit('credential_tested', key_id)
        return {k: result[k] for k in ('id', 'provider', 'latency_ms', 'attempts')}

    @app.put('/api/admin/routing', dependencies=[Depends(admin)])
    async def routing(payload: RoutingInput):
        with database.session() as db:
            for index, rule in enumerate(payload.providers):
                provider = db.get(Provider, rule.name)
                provider.priority, provider.enabled = index, rule.enabled
        database.audit('routing_updated')
        return {'ok': True}

    @app.post('/api/admin/api-key/rotate', dependencies=[Depends(admin)])
    async def rotate():
        token = 'sk_live_' + secrets.token_urlsafe(32)
        with database.session() as db:
            config = db.get(Config, 1)
            config.api_key_hash = crypto.digest(token, 'api')
            config.api_key_hint = token[:12] + '…' + token[-4:]
            config.api_enabled = True
        database.audit('api_key_rotated')
        return {'key': token, 'notice': 'Only shown once. The previous key is now invalid.'}

    @app.put('/api/admin/settings', dependencies=[Depends(admin)])
    async def update_settings(payload: ConfigInput):
        with database.session() as db:
            config = db.get(Config, 1)
            for key, value in payload.model_dump().items():
                setattr(config, key, value)
        database.audit('settings_updated')
        return {'ok': True}

    @app.get('/api/admin/logs', dependencies=[Depends(admin)])
    async def logs(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        with database.session() as db:
            rows = list(db.scalars(select(RequestLog).order_by(RequestLog.created_at.desc()).limit(limit).offset(offset)))
            ids = [row.id for row in rows]
            attempts = list(db.scalars(select(Attempt).where(Attempt.request_id.in_(ids)).order_by(Attempt.created_at)))
            items = [{k: getattr(row, k) for k in ('id', 'created_at', 'source', 'status', 'provider', 'latency_ms')} for row in rows]
            for item in items:
                item['attempts'] = [{k: getattr(a, k) for k in ('provider', 'account_id', 'credential_id', 'outcome', 'http_status', 'latency_ms')}
                                    for a in attempts if a.request_id == item['id']]
            return {'items': items, 'offset': offset, 'limit': limit}

    @app.get('/api/admin/audit', dependencies=[Depends(admin)])
    async def audit_logs():
        with database.session() as db:
            return {'items': [{k: getattr(row, k) for k in ('created_at', 'event', 'target')}
                             for row in db.scalars(select(Audit).order_by(Audit.created_at.desc()).limit(100))]}

    @app.post('/v1/search', dependencies=[Depends(public_auth)])
    async def search(payload: SearchInput, request: Request):
        return await router.search(payload, request.state.request_id)

    return app

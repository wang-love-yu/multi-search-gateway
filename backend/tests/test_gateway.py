import asyncio
import base64
import json
import time

import httpx
import pytest
from sqlalchemy import select

from app.core import Account, Admin, Attempt, Config, Credential, Crypto, Settings
from app.providers import SearchInput, classify_error, normalize, request_spec, retry_seconds
from app.state import MemoryState
from conftest import ORIGIN, PASSWORD


def test_tavily_search_and_private_logs(gateway):
    account = gateway.account()
    gateway.key(account)
    response = gateway.search()
    assert response.status_code == 200, response.text
    assert response.json()['provider'] == 'tavily'
    assert response.json()['results'][0]['snippet'] == 'tavily'
    assert response.json()['attempts'] == 1
    request = gateway.calls[0]
    assert request.method == 'POST'
    assert request.headers['authorization'] == 'Bearer test-provider-key'
    assert json.loads(request.content)['auto_parameters'] is False
    logs = gateway.admin('GET', '/logs').json()
    assert logs['items'][0]['attempts'][0]['outcome'] == 'success'
    assert 'hello world' not in json.dumps(logs)
    assert 'test-provider-key' not in json.dumps(logs)


@pytest.mark.parametrize('provider,method,host,header', [
    ('tavily', 'POST', 'api.tavily.com', 'authorization'),
    ('exa', 'POST', 'api.exa.ai', 'x-api-key'),
    ('brave', 'GET', 'api.search.brave.com', 'x-subscription-token'),
])
def test_each_adapter(gateway, provider, method, host, header):
    gateway.key(gateway.account(provider))
    result = gateway.search(provider=provider, freshness='week', limit=3)
    assert result.status_code == 200, result.text
    assert result.json()['provider'] == provider
    call = gateway.calls[0]
    assert call.method == method and call.url.host == host and header in call.headers
    if provider == 'brave':
        assert call.url.params['count'] == '3' and call.url.params['freshness'] == 'pw'
    if provider == 'exa':
        assert json.loads(call.content)['numResults'] == 3


@pytest.mark.parametrize('status,payload,kind', [
    (432, {'detail': 'limit'}, 'quota_exhausted'),
    (433, {'detail': 'payg'}, 'quota_exhausted'),
    (429, {'detail': 'rate'}, 'rate_limited'),
    (403, {'detail': 'policy'}, 'account_forbidden'),
])
def test_account_scope_failover(gateway, status, payload, kind):
    a = gateway.account()
    gateway.key(a, 'test-provider-aaa')
    gateway.key(a, 'test-provider-bbb')
    gateway.key(gateway.account('exa'), 'exa-test-credential')
    gateway.responses = [httpx.Response(status, json=payload, headers={'Retry-After': '120'})]
    response = gateway.search()
    assert response.status_code == 200, response.text
    assert response.json()['provider'] == 'exa'
    assert len(gateway.calls) == 2  # Never rotate through all keys sharing an exhausted/rate-limited account.
    with gateway.db.session() as db:
        account = db.get(Account, a)
        assert account.attempts_used == 1
        if kind == 'rate_limited':
            assert account.cooldown_until > time.time() + 100
        else:
            assert account.status == ('blocked' if status == 403 else 'quota_exhausted')


def test_invalid_key_tries_next_credential(gateway):
    a = gateway.account()
    gateway.key(a, 'bad-credential-000')
    gateway.key(a, 'good-credential-00')
    gateway.responses = [httpx.Response(401, json={'error': 'invalid'})]
    response = gateway.search()
    assert response.status_code == 200 and response.json()['attempts'] == 2
    with gateway.db.session() as db:
        keys = list(db.scalars(select(Credential)))
        assert sum(c.status == 'invalid' for c in keys) == 1
        assert db.get(Account, a).status == 'active'


def test_no_cross_provider_fallback_when_pinned(gateway):
    gateway.key(gateway.account('tavily'), 'tavily-credential')
    gateway.key(gateway.account('exa'), 'exa-credential')
    gateway.responses = [httpx.Response(432, json={})]
    response = gateway.search(provider='tavily')
    assert response.status_code == 503
    assert len(gateway.calls) == 1


def test_server_error_skips_whole_provider(gateway):
    gateway.key(gateway.account('tavily', 'a'), 'tavily-key-a00')
    gateway.key(gateway.account('tavily', 'b'), 'tavily-key-b00')
    gateway.key(gateway.account('exa'), 'exa-key-0000')
    gateway.responses = [httpx.Response(503, json={'error': 'unavailable'})]
    assert gateway.search().json()['provider'] == 'exa'
    assert len(gateway.calls) == 2


def test_invalid_query_does_not_fallback(gateway):
    gateway.key(gateway.account())
    gateway.key(gateway.account('exa'), 'exa-key-0000')
    gateway.responses = [httpx.Response(400, json={'error': 'bad parameters'})]
    assert gateway.search().status_code == 422
    assert len(gateway.calls) == 1


def test_timeout_consumes_local_attempt_and_falls_back(gateway):
    a = gateway.account()
    gateway.key(a)
    gateway.key(gateway.account('exa'), 'exa-test-key-0')
    gateway.responses = [httpx.ReadTimeout('timeout')]
    assert gateway.search().json()['provider'] == 'exa'
    with gateway.db.session() as db:
        assert db.get(Account, a).attempts_used == 1


def test_attempt_cap_is_shared_by_keys(gateway):
    a = gateway.account(attempt_limit=1)
    gateway.key(a, 'test-key-0001')
    gateway.key(a, 'test-key-0002')
    assert gateway.search().status_code == 200
    assert gateway.search().status_code == 503
    assert len(gateway.calls) == 1


def test_reset_time_is_one_shot(gateway):
    a = gateway.account(attempt_limit=1)
    gateway.key(a)
    assert gateway.search().status_code == 200
    gateway.admin('PATCH', '/accounts/' + a, {'reset_at': time.time() - 10})
    assert gateway.search().status_code == 200
    assert gateway.search().status_code == 503
    with gateway.db.session() as db:
        assert db.get(Account, a).reset_at is None


def test_rotation_revokes_old_key(gateway):
    old = gateway.token
    gateway.rotate()
    response = gateway.client.post('/v1/search', json={'query': 'test'}, headers={'Authorization': 'Bearer ' + old})
    assert response.status_code == 401
    assert gateway.search().status_code == 503


def test_cookie_is_not_search_auth(gateway):
    assert gateway.client.post('/v1/search', json={'query': 'test'}).status_code == 401


def test_api_key_is_not_admin_auth(gateway):
    gateway.client.cookies.clear()
    response = gateway.client.get('/api/admin/overview', headers={'Authorization': 'Bearer ' + gateway.token})
    assert response.status_code == 401


def test_csrf_and_origin(gateway):
    response = gateway.client.post('/api/admin/api-key/rotate', headers={'Origin': ORIGIN}, json={})
    assert response.status_code == 403
    response = gateway.client.post('/api/admin/api-key/rotate',
                                    headers={'Origin': 'https://attacker.invalid', 'X-CSRF-Token': gateway.csrf}, json={})
    assert response.status_code == 403
    response = gateway.client.post('/api/admin/login', headers={'Origin': 'https://attacker.invalid'},
                                   json={'username': 'owner', 'password': PASSWORD})
    assert response.status_code == 403


def test_logout_and_session_cookie(gateway):
    response = gateway.login()
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert gateway.admin('POST', '/logout').status_code == 200
    assert gateway.admin('GET', '/overview').status_code == 401


def test_password_change_revokes_sessions(gateway):
    response = gateway.admin('POST', '/password', {'current_password': PASSWORD, 'new_password': 'new-long-test-password-12345'})
    assert response.status_code == 200
    assert gateway.admin('GET', '/overview').status_code == 401
    response = gateway.client.post('/api/admin/login', headers={'Origin': ORIGIN},
                                   json={'username': 'owner', 'password': 'new-long-test-password-12345'})
    assert response.status_code == 200


def test_duplicate_provider_key_rejected(gateway):
    a = gateway.account()
    gateway.key(a)
    response = gateway.admin('POST', '/credentials', {'account_id': a, 'name': 'duplicate', 'key': 'test-provider-key'})
    assert response.status_code == 409


def test_credentials_encrypted_and_never_echoed(gateway):
    a = gateway.account()
    key_id = gateway.key(a)
    overview = gateway.admin('GET', '/overview')
    assert 'test-provider-key' not in overview.text and 'encrypted_key' not in overview.text
    with gateway.db.session() as db:
        credential = db.get(Credential, key_id)
        assert 'test-provider-key' not in credential.encrypted_key
        assert db.get(Config, 1).api_key_hash != gateway.token
    crypto = gateway.client.app.state.crypto
    with pytest.raises(Exception):
        crypto.decrypt(credential.encrypted_key, 'tavily:wrong-row')


def test_validation_never_echoes_secret(gateway):
    secret = 'secret-with\ninvalid-line'
    response = gateway.admin('POST', '/credentials', {'account_id': 'a' * 32, 'name': 'bad', 'key': secret})
    assert response.status_code == 422
    assert secret not in response.text and 'secret-with' not in response.text


@pytest.mark.parametrize('body', [{'query': ''}, {'query': '   '}, {'query': 'x', 'limit': 11},
                                {'query': 'x', 'base_url': 'http://127.0.0.1'}, {'query': 'x', 'limit': '5'}])
def test_strict_input_validation(gateway, body):
    response = gateway.client.post('/v1/search', headers={'Authorization': 'Bearer ' + gateway.token}, json=body)
    assert response.status_code == 422
    assert not gateway.calls


def test_request_body_size_limit(gateway):
    response = gateway.client.post('/v1/search', headers={'Authorization': 'Bearer ' + gateway.token},
                                   json={'query': 'x' * 70000})
    assert response.status_code == 413
    assert not gateway.calls


def test_provider_disabled_and_test_button(gateway):
    key_id = gateway.key(gateway.account())
    result = gateway.admin('POST', '/credentials/' + key_id + '/test')
    assert result.status_code == 200
    assert 'results' not in result.json()
    gateway.admin('PATCH', '/credentials/' + key_id, {'enabled': False})
    assert gateway.search().status_code == 503


def test_no_public_api_schema(gateway):
    for path in ['/docs', '/redoc', '/openapi.json']:
        assert gateway.client.get(path).status_code == 404


def test_api_daily_limit(gateway):
    cfg = gateway.admin('GET', '/overview').json()['config']
    cfg.pop('api_key_hint')
    cfg['daily_limit'] = 1
    assert gateway.admin('PUT', '/settings', cfg).status_code == 200
    gateway.key(gateway.account())
    assert gateway.search().status_code == 200
    assert gateway.search().status_code == 429


def test_login_limit_does_not_trust_forwarded_ip(gateway):
    results = []
    for i in range(8):
        results.append(gateway.client.post('/api/admin/login', headers={'Origin': ORIGIN, 'X-Gateway-Client-IP': str(i)},
                                          json={'username': 'wrong', 'password': 'wrong'}).status_code)
    assert results[-1] == 429


def test_error_messages_cannot_leak_provider_secret(gateway):
    gateway.key(gateway.account())
    gateway.responses = [httpx.Response(401, text='Invalid test-provider-key Authorization secret')]
    response = gateway.search()
    assert 'test-provider-key' not in response.text
    assert 'test-provider-key' not in gateway.admin('GET', '/logs').text


def test_retry_header_and_error_classification():
    assert retry_seconds('120') == 120
    assert retry_seconds('not-a-number') == 30
    assert retry_seconds('inf') == 60
    assert classify_error('brave', 429, '{"code":"QUOTA_LIMIT_EXCEEDED"}').kind == 'quota_exhausted'
    assert classify_error('brave', 429, '{}').kind == 'rate_limited'
    assert classify_error('exa', 402, '{}').kind == 'quota_exhausted'
    assert classify_error('exa', 403, '{}').kind == 'account_forbidden'


def test_result_normalizer_rejects_unsafe_urls():
    data = {'results': [{'url': 'javascript:alert(1)'}, {'url': 'https://u:p@example.org'},
                        {'url': 'https://example.org', 'content': '<script>not html</script>'},
                        {'url': 'https://example.org'}]}
    result = normalize('tavily', data, 5)
    assert len(result) == 1 and result[0]['url'] == 'https://example.org'


async def test_concurrency_lease_is_atomic_and_releasable():
    state = MemoryState()
    acquired = await asyncio.gather(*(state.acquire('test', str(i), 4, 60) for i in range(20)))
    assert sum(acquired) == 4
    await state.release('test', '0')
    assert await state.acquire('test', 'new', 4, 60)


def test_production_settings_reject_unsafe_defaults():
    with pytest.raises(ValueError):
        Settings(master_key=base64.b64encode(b't' * 32).decode(), bootstrap_password=PASSWORD,
                 public_origin='http://example.org')
    with pytest.raises(ValueError):
        Settings(master_key='invalid', bootstrap_password=PASSWORD)


def test_delete_key_while_request_in_flight(gateway):
    key_id = gateway.key(gateway.account())
    async def delete_during_request(request):
        with gateway.db.session() as db:
            db.delete(db.get(Credential, key_id))
        return httpx.Response(200, json={'results': [{'url': 'https://example.org/result', 'title': 'ok'}]})
    gateway.responses = [delete_during_request]
    assert gateway.search().status_code == 200
    assert gateway.admin('GET', '/logs').json()['items'][0]['status'] == 'success'


def test_delete_endpoint_removes_secret_but_keeps_history(gateway):
    key_id = gateway.key(gateway.account())
    assert gateway.search().status_code == 200
    assert gateway.admin('DELETE', '/credentials/' + key_id).status_code == 200
    with gateway.db.session() as db:
        assert db.get(Credential, key_id) is None
        assert db.scalar(select(Attempt)) is not None


def test_unexpected_adapter_failure_is_logged(gateway):
    gateway.key(gateway.account())
    gateway.responses = [ValueError('never include this secret in logs')]
    response = gateway.search()
    assert response.status_code == 500
    logs = gateway.admin('GET', '/logs').json()['items']
    assert logs[0]['status'] == 'internal_error'
    assert logs[0]['attempts'][0]['outcome'] == 'internal_error'
    assert 'never include' not in json.dumps(logs)


def test_empty_results_do_not_trigger_retry(gateway):
    gateway.key(gateway.account())
    gateway.responses = [httpx.Response(200, json={'results': []})]
    response = gateway.search()
    assert response.status_code == 200 and response.json()['results'] == []
    assert len(gateway.calls) == 1


def test_do_not_follow_upstream_redirects(gateway):
    gateway.key(gateway.account())
    gateway.responses = [httpx.Response(302, headers={'Location': 'http://169.254.169.254/latest/meta-data/'})]
    assert gateway.search().status_code == 503
    assert len(gateway.calls) == 1


def test_upstream_response_size_limit(gateway):
    gateway.key(gateway.account())
    gateway.responses = [httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1))]
    assert gateway.search().status_code == 503
    assert gateway.admin('GET', '/logs').json()['items'][0]['attempts'][0]['outcome'] == 'response_too_large'


def test_wrong_master_key_fails_startup(gateway):
    bad_crypto = Crypto(base64.b64encode(b'X' * 32).decode())
    settings = Settings(environment='test', public_origin=ORIGIN,
                        master_key=base64.b64encode(b'X' * 32).decode(), bootstrap_password=PASSWORD)
    with pytest.raises(Exception):
        gateway.db.initialize(settings, bad_crypto)


def test_null_account_fields_are_rejected(gateway):
    account = gateway.account()
    assert gateway.admin('PATCH', '/accounts/' + account, {'rpm': None}).status_code == 422
    assert gateway.admin('PATCH', '/accounts/' + account, {'reset_at': None}).status_code == 200


def test_production_cookie_name_is_host_scoped():
    settings = Settings(master_key=base64.b64encode(b'T' * 32).decode(), bootstrap_password=PASSWORD,
                        public_origin='https://search.example.com')
    assert settings.secure_cookie and settings.cookie_name == '__Host-msg_session'


def test_config_cannot_reveal_hash(gateway):
    body = gateway.admin('GET', '/overview').text
    for forbidden in ['api_key_hash', 'fingerprint', 'master_check', 'password_hash']:
        assert forbidden not in body


def test_redis_failure_does_not_bypass_auth(gateway):
    async def broken(*args, **kwargs):
        raise ConnectionError('Redis unavailable')
    gateway.state.allow = broken
    assert gateway.search().status_code == 503
    assert not gateway.calls


def test_deleted_candidate_is_skipped_during_failover(gateway):
    account = gateway.account()
    gateway.key(account, 'test-key-candidate-a')
    gateway.key(account, 'test-key-candidate-b')
    gateway.key(gateway.account('exa'), 'test-exa-candidate')
    async def remove_other_key(request):
        with gateway.db.session() as db:
            credentials = list(db.scalars(select(Credential).where(Credential.account_id == account)))
            for credential in credentials:
                if credential.last_used_at == 0:
                    db.delete(credential)
        return httpx.Response(401, json={'error': 'invalid'})
    gateway.responses = [remove_other_key]
    response = gateway.search()
    assert response.status_code == 200 and response.json()['provider'] == 'exa'
    assert len(gateway.calls) == 2

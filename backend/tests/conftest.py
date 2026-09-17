import base64
import json
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import Database, Settings
from app.main import create_app
from app.providers import Adapters
from app.state import MemoryState

ORIGIN = 'http://testserver'
PASSWORD = 'test-only-password-very-long'


@dataclass
class Harness:
    client: TestClient | None = None
    calls: list = field(default_factory=list)
    responses: list = field(default_factory=list)
    csrf: str = ''
    token: str = ''
    db: Database | None = None
    state: MemoryState | None = None

    async def upstream(self, request):
        self.calls.append(request)
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            if callable(result):
                return await result(request)
            return result
        if request.url.host == 'api.search.brave.com':
            return httpx.Response(200, json={'web': {'results': [{'title': 'B', 'url': 'https://example.org/b', 'description': 'brave'}]}})
        if request.url.host == 'api.exa.ai':
            return httpx.Response(200, json={'results': [{'title': 'E', 'url': 'https://example.org/e', 'highlights': ['exa']}]})
        return httpx.Response(200, json={'results': [{'title': 'T', 'url': 'https://example.org/t', 'content': 'tavily'}]})

    def login(self):
        response = self.client.post('/api/admin/login', headers={'Origin': ORIGIN},
                                    json={'username': 'owner', 'password': PASSWORD})
        assert response.status_code == 200, response.text
        self.csrf = response.json()['csrf']
        return response

    def admin(self, method, path, body=None):
        return self.client.request(method, '/api/admin' + path,
                                   headers={'Origin': ORIGIN, 'X-CSRF-Token': self.csrf}, json=body)

    def account(self, provider='tavily', name='main', **kwargs):
        response = self.admin('POST', '/accounts', {'provider': provider, 'name': name, **kwargs})
        assert response.status_code == 200, response.text
        return response.json()['id']

    def key(self, account, key='test-provider-key', name='primary'):
        response = self.admin('POST', '/credentials', {'account_id': account, 'name': name, 'key': key})
        assert response.status_code == 200, response.text
        return response.json()['id']

    def rotate(self):
        response = self.admin('POST', '/api-key/rotate')
        assert response.status_code == 200, response.text
        self.token = response.json()['key']
        return self.token

    def search(self, **kwargs):
        return self.client.post('/v1/search', headers={'Authorization': 'Bearer ' + self.token},
                                json={'query': 'hello world', **kwargs})


@pytest.fixture
def gateway(tmp_path):
    harness = Harness()
    settings = Settings(environment='test', public_origin=ORIGIN,
                        database_url='sqlite:///' + str(tmp_path / 'test.db'),
                        master_key=base64.b64encode(b'T' * 32).decode(), bootstrap_password=PASSWORD)
    db = Database(settings.database_url)
    state = MemoryState()
    transport = httpx.MockTransport(harness.upstream)
    app = create_app(settings, db, state, Adapters(httpx.AsyncClient(transport=transport)))
    harness.db, harness.state = db, state
    with TestClient(app) as client:
        harness.client = client
        harness.login()
        harness.rotate()
        yield harness

"""Fixed-endpoint provider adapters. No arbitrary URLs or automatic HTTP retries."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

ProviderName = Literal['tavily', 'exa', 'brave']


class SearchInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=2000)
    provider: Literal['auto', 'tavily', 'exa', 'brave'] = 'auto'
    limit: int = Field(default=5, ge=1, le=10)
    freshness: Literal['day', 'week', 'month', 'year'] | None = None

    @field_validator('query')
    @classmethod
    def clean_query(cls, value):
        if not value.strip() or any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise ValueError('query is blank or contains control characters')
        return value.strip()


class UpstreamError(Exception):
    def __init__(self, kind: str, status: int = 0, retry_after: int = 30):
        super().__init__(kind)
        self.kind, self.status, self.retry_after = kind, status, retry_after


def retry_seconds(value: str | None) -> int:
    try:
        n = float(value or '30')
    except ValueError:
        try:
            n = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            n = 30
    if not 0 <= n < 86400:
        n = 60
    return max(1, min(3600, int(n)))


def classify_error(provider: str, status: int, body: str, retry_after: str | None = None):
    text = body[:16000].lower()
    if provider == 'tavily' and status in (432, 433):
        kind = 'quota_exhausted'
    elif status == 402 or (status == 429 and any(code in text for code in (
            'quota_limit_exceeded', 'usage_limit_exceeded', 'insufficient_credits', 'insufficient balance'))):
        kind = 'quota_exhausted'
    elif status == 401:
        kind = 'invalid_key'
    elif status == 403:
        kind = 'account_forbidden'
    elif status == 429:
        kind = 'rate_limited'
    elif status in (400, 422):
        kind = 'invalid_request'
    elif status >= 500:
        kind = 'upstream_error'
    else:
        kind = 'upstream_protocol_error'
    return UpstreamError(kind, status, retry_seconds(retry_after))


def request_spec(provider: str, key: str, query: SearchInput):
    headers = {'Accept': 'application/json', 'User-Agent': 'personal-search-gateway/0.1'}
    if provider == 'tavily':
        headers['Authorization'] = 'Bearer ' + key
        payload = dict(query=query.query, max_results=query.limit, search_depth='basic',
                       auto_parameters=False, include_answer=False, include_raw_content=False, include_usage=True)
        if query.freshness:
            payload['time_range'] = query.freshness
        return 'POST', 'https://api.tavily.com/search', headers, {'json': payload}
    if provider == 'exa':
        headers['x-api-key'] = key
        payload = dict(query=query.query, numResults=query.limit, type='auto', contents={'highlights': True})
        if query.freshness:
            days = {'day': 1, 'week': 7, 'month': 30, 'year': 365}[query.freshness]
            payload['startPublishedDate'] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return 'POST', 'https://api.exa.ai/search', headers, {'json': payload}
    if provider == 'brave':
        headers['X-Subscription-Token'] = key
        params = {'q': query.query, 'count': query.limit, 'text_decorations': 'false'}
        if query.freshness:
            params['freshness'] = {'day': 'pd', 'week': 'pw', 'month': 'pm', 'year': 'py'}[query.freshness]
        return 'GET', 'https://api.search.brave.com/res/v1/web/search', headers, {'params': params}
    raise ValueError('Unknown provider')


def normalize(provider: str, data: dict, limit: int):
    rows = data.get('web', {}).get('results', []) if provider == 'brave' else data.get('results')
    if not isinstance(rows, list):
        raise UpstreamError('upstream_protocol_error')
    output = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get('url', '')
        if not isinstance(url, str) or len(url) > 8000:
            continue
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                continue
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        snippet = row.get('content') if provider == 'tavily' else row.get('description')
        if provider == 'exa':
            highlights = row.get('highlights') or []
            snippet = '\n'.join(x for x in highlights if isinstance(x, str)) if isinstance(highlights, list) else ''
        published = row.get('publishedDate') or row.get('published_date')
        output.append({'title': str(row.get('title') or '')[:1000], 'url': url,
                       'snippet': str(snippet or '')[:8000],
                       'published_at': published if isinstance(published, str) else None})
        if len(output) >= limit:
            break
    return output


class Adapters:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def search(self, provider: str, key: str, query: SearchInput, timeout: float):
        method, url, headers, options = request_spec(provider, key, query)
        try:
            async with asyncio.timeout(timeout):
                async with self.client.stream(method, url, headers=headers, **options,
                                              timeout=httpx.Timeout(timeout, connect=min(3, timeout)),
                                              follow_redirects=False) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 2 * 1024 * 1024:
                            raise UpstreamError('response_too_large', response.status_code)
                        chunks.append(chunk)
                    raw = b''.join(chunks)
                    if response.status_code != 200:
                        raise classify_error(provider, response.status_code, raw.decode('utf-8', 'replace'),
                                             response.headers.get('retry-after'))
                    try:
                        data = json.loads(raw)
                        if not isinstance(data, dict) or 'error' in data:
                            raise ValueError('Unexpected response')
                        return normalize(provider, data, query.limit)
                    except (ValueError, TypeError, AttributeError):
                        raise UpstreamError('upstream_protocol_error', 200) from None
        except (TimeoutError, httpx.TimeoutException):
            raise UpstreamError('timeout') from None
        except httpx.RequestError:
            raise UpstreamError('connection_error') from None

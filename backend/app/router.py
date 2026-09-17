"""Sequential failover with account-scoped limits and conservative attempt accounting."""
import asyncio
import time
from dataclasses import dataclass

from sqlalchemy import select, update

from .core import Account, Attempt, Config, Credential, Crypto, Database, Provider, RequestLog, uid
from .providers import Adapters, SearchInput, UpstreamError
from .state import State


@dataclass
class GatewayError(Exception):
    code: str
    status: int = 503


class Router:
    def __init__(self, database: Database, state: State, crypto: Crypto, adapters: Adapters):
        self.database, self.state, self.crypto, self.adapters = database, state, crypto, adapters

    def _candidates(self, query: SearchInput, target_key: str | None):
        now = time.time()
        with self.database.session() as db:
            # reset_at is an operator-supplied, one-shot UTC timestamp, not a guessed billing date.
            db.execute(update(Account).where(Account.reset_at <= now,
                       Account.status.in_(['active', 'quota_exhausted'])).values(
                           attempts_used=0, status='active', reset_at=None, cooldown_until=0))
            stmt = (select(Credential.id).join(Account).join(Provider)
                    .where(Credential.enabled.is_(True), Credential.status == 'active',
                           Account.enabled.is_(True), Account.status == 'active',
                           Account.cooldown_until <= now, Account.attempts_used < Account.attempt_limit,
                           Provider.enabled.is_(True), Provider.cooldown_until <= now)
                    .order_by(Provider.priority, Account.priority, Account.id, Credential.last_used_at, Credential.id))
            if query.provider != 'auto':
                stmt = stmt.where(Provider.name == query.provider)
            if target_key:
                stmt = stmt.where(Credential.id == target_key)
            return list(db.scalars(stmt))

    def _reserve(self, key_id: str, request_id: str):
        now = time.time()
        with self.database.session() as db:
            credential = db.get(Credential, key_id)
            if not credential or not credential.enabled or credential.status != 'active':
                return None
            account = db.get(Account, credential.account_id)
            provider = db.get(Provider, account.provider)
            if not provider.enabled or provider.cooldown_until > now:
                return None
            # Atomic SQL update prevents different keys from overspending one account's local cap.
            changed = db.execute(update(Account).where(
                Account.id == account.id, Account.enabled.is_(True), Account.status == 'active',
                Account.cooldown_until <= now, Account.attempts_used < Account.attempt_limit
            ).values(attempts_used=Account.attempts_used + 1)).rowcount
            if not changed:
                return None
            credential.last_used_at = now
            attempt = Attempt(id=uid(), request_id=request_id, provider=account.provider,
                              account_id=account.id, credential_id=credential.id)
            db.add(attempt)
            return account, credential, attempt.id

    def _complete(self, attempt_id, account_id, key_id, provider_name, started, error=None):
        with self.database.session() as db:
            attempt = db.get(Attempt, attempt_id)
            attempt.latency_ms = int((time.monotonic() - started) * 1000)
            attempt.outcome = error.kind if error else 'success'
            attempt.http_status = error.status if error else 200
            credential = db.get(Credential, key_id)
            if credential:
                credential.last_error = error.kind if error else ''
            if not error:
                return
            account = db.get(Account, account_id)
            if error.kind == 'invalid_key':
                if credential:
                    credential.status = 'invalid'
            elif error.kind in ('quota_exhausted', 'account_forbidden'):
                account.status = 'quota_exhausted' if error.kind == 'quota_exhausted' else 'blocked'
            elif error.kind == 'rate_limited':
                account.cooldown_until = max(account.cooldown_until, time.time() + error.retry_after)
            elif error.kind not in ('invalid_request', 'cancelled_unknown_billing'):
                provider = db.get(Provider, provider_name)
                provider.cooldown_until = max(provider.cooldown_until, time.time() + 15)

    def _finish(self, request_id, status, started, provider=''):
        with self.database.session() as db:
            log = db.get(RequestLog, request_id)
            log.status, log.provider = status, provider
            log.latency_ms = int((time.monotonic() - started) * 1000)

    async def search(self, query: SearchInput, request_id: str, source='api', target_key=None):
        with self.database.session() as db:
            config = db.get(Config, 1)
        if not await self.state.acquire('search', request_id, config.concurrency, config.timeout_seconds + 30):
            raise GatewayError('concurrency_limit', 429)
        started = time.monotonic()
        deadline = started + config.timeout_seconds
        count, skipped_accounts, skipped_providers = 0, set(), set()
        try:
            with self.database.session() as db:
                db.add(RequestLog(id=request_id, query_digest=self.crypto.digest(query.query, 'query'), source=source))
            for key_id in self._candidates(query, target_key):
                remaining = deadline - time.monotonic()
                if remaining < 0.1 or count >= (1 if target_key else config.max_attempts):
                    break
                with self.database.session() as db:
                    credential = db.get(Credential, key_id)
                    if not credential:
                        continue
                    account = db.get(Account, credential.account_id)
                    if not account:
                        continue
                if account.id in skipped_accounts or account.provider in skipped_providers:
                    continue
                if not await self.state.allow('upstream:' + account.id, account.rpm, 60):
                    skipped_accounts.add(account.id)
                    continue
                reserved = self._reserve(key_id, request_id)
                if not reserved:
                    continue
                account, credential, attempt_id = reserved
                count += 1
                attempt_start = time.monotonic()
                try:
                    key = self.crypto.decrypt(credential.encrypted_key, account.provider + ':' + credential.id)
                    results = await self.adapters.search(account.provider, key, query, min(6.0, remaining))
                except asyncio.CancelledError:
                    self._complete(attempt_id, account.id, key_id, account.provider, attempt_start,
                                   UpstreamError('cancelled_unknown_billing'))
                    self._finish(request_id, 'cancelled', started)
                    raise
                except UpstreamError as error:
                    self._complete(attempt_id, account.id, key_id, account.provider, attempt_start, error)
                    if error.kind == 'invalid_request':
                        self._finish(request_id, error.kind, started)
                        raise GatewayError('upstream_rejected_parameters', 422) from None
                    if error.kind in ('quota_exhausted', 'account_forbidden', 'rate_limited'):
                        skipped_accounts.add(account.id)
                    elif error.kind != 'invalid_key':
                        skipped_providers.add(account.provider)
                    continue
                except Exception:
                    self._complete(attempt_id, account.id, key_id, account.provider, attempt_start,
                                   UpstreamError('internal_error'))
                    self._finish(request_id, 'internal_error', started)
                    raise GatewayError('internal_error', 500) from None
                self._complete(attempt_id, account.id, key_id, account.provider, attempt_start)
                self._finish(request_id, 'success', started, account.provider)
                return {'id': request_id, 'provider': account.provider, 'query': query.query, 'results': results,
                        'attempts': count, 'latency_ms': int((time.monotonic() - started) * 1000)}
            self._finish(request_id, 'no_provider_available', started)
            raise GatewayError('no_provider_available')
        finally:
            # An expiring lease bounds damage even if Redis becomes unavailable during cleanup.
            try:
                await self.state.release('search', request_id)
            except Exception:
                pass

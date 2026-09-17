"""Live PostgreSQL/Redis tests. Use disposable CI services, never a production database."""
import asyncio
import os
import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, update

from app.core import Account, Base, Database, Provider, uid
from app.state import RedisState


@pytest.mark.skipif(not os.getenv('TEST_REDIS_URL'), reason='Disposable Redis not available')
async def test_real_redis_limits_and_sessions():
    state = RedisState(os.environ['TEST_REDIS_URL'])
    name = 'ci:' + secrets.token_hex(10)
    try:
        await state.ping()
        limits = await asyncio.gather(*(state.allow(name, 5, 60) for _ in range(20)))
        assert sum(limits) == 5
        leases = await asyncio.gather(*(state.acquire(name, str(i), 3, 10) for i in range(15)))
        assert sum(leases) == 3
        for i, acquired in enumerate(leases):
            if acquired:
                await state.release(name, str(i))
        assert await state.acquire(name, 'after-release', 3, 10)
        await state.release(name, 'after-release')
        await state.put(name + ':session', {'version': 1}, 10)
        assert await state.get(name + ':session') == {'version': 1}
        await state.delete(name + ':session')
        assert await state.get(name + ':session') is None
    finally:
        await state.close()


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='Disposable PostgreSQL not available')
def test_real_postgres_atomic_account_budget():
    database = Database(os.environ['TEST_POSTGRES_URL'])
    Base.metadata.create_all(database.engine)
    account_id = uid()
    with database.session() as db:
        if not db.get(Provider, 'tavily'):
            db.add(Provider(name='tavily', priority=0))
            db.flush()
        db.add(Account(id=account_id, provider='tavily', name='ci-' + account_id, attempt_limit=3))
    def reserve(_):
        with database.session() as db:
            return db.execute(update(Account).where(Account.id == account_id,
                Account.attempts_used < Account.attempt_limit).values(attempts_used=Account.attempts_used + 1)).rowcount
    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            assert sum(pool.map(reserve, range(30))) == 3
        with database.session() as db:
            assert db.get(Account, account_id).attempts_used == 3
    finally:
        with database.session() as db:
            db.delete(db.get(Account, account_id))
        database.engine.dispose()

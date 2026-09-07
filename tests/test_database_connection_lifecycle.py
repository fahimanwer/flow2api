"""Real-thread regressions for the September 7 database thread exhaustion."""
import asyncio
import threading
from unittest.mock import patch

import aiosqlite
import anyio
import pytest

from src.core.database import Database


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 5)


@pytest.fixture
def connections():
    original = aiosqlite.connect
    opened = []

    def tracked(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    with patch('src.core.database.aiosqlite.connect', tracked):
        yield opened
    # Ensure a regressed implementation cannot leave pytest hanging on leaked
    # non-daemon threads. Tests assert termination before this fallback cleanup.
    for conn in opened:
        if conn.is_alive():
            conn._stop_running()
            conn.join(5)


@pytest.mark.asyncio
async def test_cancel_during_open_closes_thread_and_preserves_cancellation(connections):
    db = Database(':memory:', max_connections=1)
    started, release = threading.Event(), threading.Event()
    original = aiosqlite.connect

    def delayed(*args, **kwargs):
        conn = original(*args, **kwargs)
        connector = conn._connector
        def open_sqlite():
            started.set()
            release.wait(5)
            return connector()
        conn._connector = open_sqlite
        return conn

    async def request():
        async with db._connect():
            pytest.fail('Cancelled acquisition must not reach request body')

    with patch('src.core.database.aiosqlite.connect', delayed):
        task = asyncio.create_task(request())
        await until(started.is_set)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()  # A second cancellation must not interrupt cleanup either.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert all(not c.is_alive() for c in connections)
    async with db._connect() as conn:
        assert (await (await conn.execute('SELECT 1')).fetchone())[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('during_close', [False, True])
async def test_cancel_during_query_or_close_stops_thread(connections, during_close):
    db = Database(':memory:')
    running, release = threading.Event(), threading.Event()

    def slow_sql():
        running.set()
        release.wait(5)
        return 1

    async def request():
        async with db._connect() as conn:
            await conn.create_function('slow_sql', 0, slow_sql)
            if during_close:
                close = conn.close
                async def delayed_close():
                    running.set()
                    await until(release.is_set)
                    await close()
                conn.close = delayed_close
            else:
                await conn.execute('SELECT slow_sql()')

    task = asyncio.create_task(request())
    await until(running.is_set)
    task.cancel()
    await asyncio.sleep(.01)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert all(not c.is_alive() for c in connections)


@pytest.mark.asyncio
async def test_anyio_disconnect_scope_closes_connection(connections):
    db = Database(':memory:')
    with anyio.CancelScope() as scope:
        async with db._connect() as conn:
            await conn.execute('SELECT 1')
            scope.cancel()
            await anyio.sleep(0)
    assert scope.cancelled_caught
    assert all(not c.is_alive() for c in connections)


@pytest.mark.asyncio
async def test_anyio_cancel_during_open(connections):
    db = Database(':memory:')
    started, release = threading.Event(), threading.Event()
    original = aiosqlite.connect
    def delayed(*args, **kwargs):
        conn = original(*args, **kwargs)
        connector = conn._connector
        def open_sqlite():
            started.set()
            release.wait(5)
            return connector()
        conn._connector = open_sqlite
        return conn
    scope_ready = asyncio.Future()
    async def request():
        with anyio.CancelScope() as scope:
            scope_ready.set_result(scope)
            async with db._connect():
                pytest.fail('Cancelled scope must not reach request body')
        assert scope.cancelled_caught
    with patch('src.core.database.aiosqlite.connect', delayed):
        task = asyncio.create_task(request())
        scope = await scope_ready
        await until(started.is_set)
        scope.cancel()
        release.set()
        await asyncio.wait_for(task, 5)
    assert all(not c.is_alive() for c in connections)


@pytest.mark.asyncio
async def test_burst_is_bounded_and_cancelled_waiters_release_capacity(connections):
    db = Database(':memory:', max_connections=3)
    release = asyncio.Event()
    entered = 0
    async def request():
        nonlocal entered
        async with db._connect():
            entered += 1
            await release.wait()
    tasks = [asyncio.create_task(request()) for _ in range(100)]
    await until(lambda: entered == 3)
    assert len(connections) == 3
    assert sum(c.is_alive() for c in connections) == 3
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(not c.is_alive() for c in connections)
    async with db._connect():
        pass


@pytest.mark.asyncio
async def test_failed_open_and_failed_configuration_release_capacity(connections, tmp_path):
    db = Database(str(tmp_path / 'missing' / 'db'), max_connections=1)
    with pytest.raises(aiosqlite.OperationalError):
        async with db._connect():
            pass
    assert all(not c.is_alive() for c in connections)
    db.db_path = ':memory:'
    async def bad_config(conn):
        raise ValueError('configuration failed')
    with patch.object(db, '_configure_connection', bad_config):
        with pytest.raises(ValueError, match='configuration failed'):
            async with db._connect():
                pass
    assert all(not c.is_alive() for c in connections)
    async with db._connect(write=True):
        pass


@pytest.mark.asyncio
async def test_repeated_disconnects_do_not_accumulate_threads(connections):
    db = Database(':memory:', max_connections=4)
    async def request():
        with anyio.CancelScope() as scope:
            async with db._connect():
                scope.cancel()
                await anyio.sleep(0)
    for _ in range(10):
        await asyncio.gather(*(request() for _ in range(20)))
        assert all(not c.is_alive() for c in connections)
    assert len(connections) == 200

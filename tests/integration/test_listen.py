"""LISTEN/NOTIFY: delivery, many channels and notifiers, and the listening connection goes
back to the pool clean (also after cancellation or when the server kills it)."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

import pytest

from mizzle import ConnectionError, Database, Notification, QueryError, sql
from mizzle.dialects.postgres.driver import PostgresDriver
from tests.integration.test_stress import assert_no_leak

APP = "mizzle_listen"


@pytest.fixture
async def db(pg_url: str) -> AsyncGenerator[Database]:
    url = pg_url + ("&" if "?" in pg_url else "?") + f"application_name={APP}"
    async with await Database.connect(url, pool_size=2, reset_connections="auto") as database:
        yield database
        assert_no_leak(database)


async def take(events: AsyncIterator[Notification], count: int) -> list[Notification]:
    async with asyncio.timeout(5):
        return [await anext(events) for _ in range(count)]


async def test_notifications_on_several_channels(db: Database) -> None:
    payload = "it's \"quoted\" \\ and 'tricky'; DROP TABLE x; -- ünïcode"
    async with db.listen("orders", "Mixed Case", "orders") as events:
        await db.notify("orders", payload)
        await db.notify("Mixed Case")
        await db.notify("other", "not listened to")
        await db.notify("orders", "last")
        got = await take(events, 3)
    assert [(e.channel, e.payload) for e in got] == [
        ("orders", payload),
        ("Mixed Case", ""),
        ("orders", "last"),
    ]
    assert all(isinstance(e.pid, int) for e in got)


async def test_concurrent_notifiers(db: Database, pg_url: str) -> None:
    async with (
        await Database.connect(pg_url, pool_size=5) as sender,
        db.listen("a", "b") as events,
    ):
        sends = [sender.notify("ab"[i % 2], str(i)) for i in range(200)]
        await asyncio.gather(*sends)
        got = await take(events, 200)
    assert sorted(int(e.payload) for e in got) == list(range(200))
    assert {e.channel for e in got if int(e.payload) % 2} == {"b"}


async def test_notify_in_a_transaction_is_sent_on_commit(db: Database) -> None:
    async with db.listen("tx") as events:
        with pytest.raises(RuntimeError):
            async with db.transaction() as tx:
                await tx.notify("tx", "rolled back")
                raise RuntimeError
        async with db.transaction() as tx:
            await tx.notify("tx", "committed")
            with pytest.raises(TimeoutError):  # nothing arrives before COMMIT
                async with asyncio.timeout(0.2):
                    await anext(events)
        await db.notify("tx", "after")
        assert [e.payload for e in await take(events, 2)] == ["committed", "after"]


async def test_connection_goes_back_clean(pg_url: str) -> None:
    # One pooled connection and "auto" reset: the next user must not still be listening.
    async with (
        await Database.connect(pg_url, pool_size=1, reset_connections="auto") as db,
        await Database.connect(pg_url, pool_size=1) as sender,
    ):
        async with db.listen("clean") as events:
            await sender.notify("clean", "x")
            [event] = await take(events, 1)
            assert event.payload == "x"
        [row] = await db.execute(sql("select count(*) as n from pg_listening_channels()"))
        assert row["n"] == 0
        assert_no_leak(db)


async def test_cancelled_listener_releases_its_connection(db: Database) -> None:
    listening = asyncio.Event()

    async def listen() -> None:
        async with db.listen("cancel") as events:
            listening.set()
            async for _ in events:
                pass

    task = asyncio.create_task(listen())
    await listening.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_no_leak(db)
    await db.notify("cancel", "nobody listens")


async def test_killed_listener_raises_connection_error(
    db: Database, driver: PostgresDriver
) -> None:
    with pytest.raises(ConnectionError, match="listen"):
        async with db.listen("killed") as events:
            await db.notify("killed", "before")
            assert (await take(events, 1))[0].payload == "before"
            async with driver.acquire() as conn:
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE application_name = '{APP}' AND query LIKE 'LISTEN%'",
                    [],
                )
            await take(events, 1)
    # A new listener works.
    async with db.listen("killed") as events:
        await db.notify("killed", "after")
        assert (await take(events, 1))[0].payload == "after"


async def test_listen_misuse(db: Database) -> None:
    with pytest.raises(QueryError, match="at least one"):
        async with db.listen():
            pass
    async with db.transaction() as tx:
        with pytest.raises(QueryError, match="outside a transaction"):
            async with tx.listen("x"):
                pass

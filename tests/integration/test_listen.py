"""LISTEN/NOTIFY: delivery, JSON payloads, transactions, reconnecting after the server
kills the listener, the queue limit, and the listener never holding a pooled connection."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from decimal import Decimal

import pytest

from mizzle import Database, ListenerOverflow, Notification, QueryError, Reconnected, sql
from mizzle.dialects.postgres.driver import PostgresDriver

APP = "mizzle_listener"


def with_app(url: str, app: str) -> str:
    return url + ("&" if "?" in url else "?") + f"application_name={app}"


@pytest.fixture
async def db(pg_url: str) -> AsyncGenerator[Database]:
    async with await Database.connect(pg_url, pool_size=2) as database:
        yield database


async def take[T](events: AsyncIterator[T], count: int) -> list[T]:
    async with asyncio.timeout(10):
        return [await anext(events) for _ in range(count)]


def payloads(events: list[Notification[str] | Reconnected]) -> list[str]:
    return [e.payload for e in events if isinstance(e, Notification)]


async def kill_listener(driver: PostgresDriver) -> None:
    async with driver.acquire() as conn:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE application_name = '{APP}'",
            [],
        )


async def test_notifications_on_several_channels(db: Database) -> None:
    payload = "it's \"quoted\" \\ and 'tricky'; DROP TABLE x; -- ünïcode"
    async with db.listen("orders", "Mixed Case", "orders") as events:
        await db.notify("orders", payload)
        await db.notify("Mixed Case")
        await db.notify("other", "not listened to")
        await db.notify("orders", "last")
        got = await take(events, 3)
    assert [(e.channel, e.payload) for e in got if isinstance(e, Notification)] == [
        ("orders", payload),
        ("Mixed Case", ""),
        ("orders", "last"),
    ]
    assert all(isinstance(e, Notification) and e.data == e.payload and e.pid for e in got)


async def test_json_payloads_and_decode(db: Database) -> None:
    async with db.listen("json", decode=json.loads) as events:
        await db.notify("json", {"id": 7, "price": Decimal("12345678901234567890.123")})
        [event] = await take(events, 1)
    assert isinstance(event, Notification)
    assert event.data == {"id": 7, "price": 1.2345678901234568e19}  # json.loads: a float
    assert '"price": 12345678901234567890.123' in event.payload  # sent exactly


async def test_a_decode_error_fails_one_step_only(db: Database) -> None:
    async with db.listen("dec", decode=int) as events:
        await db.notify("dec", "x")
        await db.notify("dec", "2")
        with pytest.raises(ValueError, match="invalid literal"):
            await take(events, 1)
        [event] = await take(events, 1)
    assert isinstance(event, Notification) and event.data == 2


async def test_concurrent_notifiers(db: Database, pg_url: str) -> None:
    async with (
        await Database.connect(pg_url, pool_size=5) as sender,
        db.listen("a", "b") as events,
    ):
        await asyncio.gather(*(sender.notify("ab"[i % 2], str(i)) for i in range(200)))
        got = await take(events, 200)
    assert sorted(int(p) for p in payloads(got)) == list(range(200))


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
        assert payloads(await take(events, 2)) == ["committed", "after"]


async def test_the_listener_holds_no_pooled_connection(db: Database, pg_url: str) -> None:
    async with db.listen("pool", url=with_app(pg_url, APP)) as events:
        assert db.pool_stats().in_use == 0
        await db.notify("pool", "x")
        assert payloads(await take(events, 1)) == ["x"]
        [row] = await db.execute(sql("SELECT count(*) AS n FROM pg_listening_channels()"))
        assert row["n"] == 0  # pooled connections never LISTEN
    async with asyncio.timeout(5):
        while True:  # the listener's own connection is closed on exit
            [row] = await db.execute(
                sql("SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = {}", APP)
            )
            if row["n"] == 0:
                break
            await asyncio.sleep(0.05)


async def test_a_cancelled_listener_closes_its_connection(db: Database, pg_url: str) -> None:
    listening = asyncio.Event()

    async def listen() -> None:
        async with db.listen("cancel", url=with_app(pg_url, APP)) as events:
            listening.set()
            async for _ in events:
                pass

    task = asyncio.create_task(listen())
    await listening.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [row] = await db.execute(
        sql("SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = {}", APP)
    )
    assert row["n"] == 0


async def test_a_killed_listener_reconnects_and_says_so(
    db: Database, pg_url: str, driver: PostgresDriver
) -> None:
    async with db.listen("killed", url=with_app(pg_url, APP)) as events:
        await db.notify("killed", "before")
        assert payloads(await take(events, 1)) == ["before"]
        await kill_listener(driver)
        [gap] = await take(events, 1)
        assert isinstance(gap, Reconnected)
        await db.notify("killed", "after")
        assert payloads(await take(events, 1)) == ["after"]


async def test_keepalive_checks_keep_the_listener_working(db: Database) -> None:
    async with db.listen("quiet", keepalive=0.05) as events:
        waiting = asyncio.ensure_future(anext(events))
        await asyncio.sleep(0.3)  # several SELECT 1 checks while nothing arrives
        assert not waiting.done()
        await db.notify("quiet", "late")
        async with asyncio.timeout(5):
            event = await waiting
    assert isinstance(event, Notification) and event.payload == "late"


async def test_too_many_unread_notifications_raise(db: Database) -> None:
    async with db.listen("full", max_queue=2) as events:
        for i in range(5):
            await db.notify("full", str(i))
        await asyncio.sleep(0.2)
        assert payloads(await take(events, 2)) == ["0", "1"]  # queued ones first
        with pytest.raises(ListenerOverflow, match="2 notifications behind"):
            await anext(events)


async def test_listen_misuse(db: Database) -> None:
    with pytest.raises(QueryError, match="at least one channel"):
        db.listen()
    with pytest.raises(QueryError, match="max_queue"):
        db.listen("x", max_queue=0)
    with pytest.raises(QueryError, match="keepalive"):
        db.listen("x", keepalive=0)


async def test_channel_names_and_payload_sizes_are_checked(db: Database) -> None:
    # H13-1..3: a 70-byte channel was cut to 63 by LISTEN but refused by pg_notify with a
    # hidden message; an 8000-byte payload too; an empty name raised a raw driver error.
    for bad in ("", "c" * 64, "é" * 32):
        with pytest.raises(QueryError, match="1 to 63 bytes"):
            db.listen(bad)
        with pytest.raises(QueryError, match="1 to 63 bytes"):
            await db.notify(bad)
    with pytest.raises(QueryError, match="under 8000 bytes"):
        await db.notify("big", "x" * 8000)
    await db.notify("c" * 63, "x" * 7999)  # the limits themselves are fine


async def test_reading_after_the_block_raises(db: Database) -> None:
    # H13-4: it waited forever.
    async with db.listen("after", keepalive=0.1) as events:
        pass
    with pytest.raises(QueryError, match="listener is closed"):
        async with asyncio.timeout(2):
            await anext(events)

"""Streaming reads from a server-side cursor: correct rows, bounded memory, and the
connection is always released (loop end, break, error, cancellation, lost connection)."""

import asyncio
import gc
import tracemalloc
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import final

import pytest

from mizzle import (
    ConnectionError,
    Database,
    DatabaseError,
    QueryError,
    QueryEvent,
    Table,
    asc,
    eq,
    gt,
    sql,
)
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.pg import integer, serial, text
from tests.integration.test_stress import assert_no_leak, pool_of
from tests.models import Posts, Users

APP = "mizzle_stream"


@final
class Items(Table, name="stream_items"):
    id = serial().primary_key()
    n = integer().not_null()
    label = text().not_null()


DDL = """
DROP TABLE IF EXISTS stream_items;
CREATE TABLE stream_items (id serial PRIMARY KEY, n integer NOT NULL, label text NOT NULL);
INSERT INTO stream_items (n, label) SELECT g, 'item ' || g FROM generate_series(1, 2500) g;
"""


def with_app(url: str) -> str:
    return url + ("&" if "?" in url else "?") + f"application_name={APP}"


@pytest.fixture
async def db(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    async with driver.acquire() as conn:
        await conn.execute(DDL, [])
    async with await Database.connect(
        with_app(pg_url), schema=[Items, Users, Posts], pool_size=4, reset_connections="auto"
    ) as database:
        yield database
        assert_no_leak(database)
    async with driver.acquire() as conn:
        await conn.execute("DROP TABLE stream_items", [])


async def released(db: Database) -> None:
    """Wait for a stream dropped without aclosing() to be finalized by the event loop."""
    pool = pool_of(db)
    for _ in range(100):
        gc.collect()
        await asyncio.sleep(0.01)
        if pool.get_idle_size() == pool.get_size():
            return
    raise AssertionError("the stream's connection was never released")


async def test_stream_matches_a_plain_select(db: Database) -> None:
    query = db.select().from_(Items).order_by(asc(Items.id))
    streamed = [row async for row in query.stream(batch_size=1000)]
    assert [(r.id, r.n, r.label) for r in streamed] == [(r.id, r.n, r.label) for r in await query]
    assert len(streamed) == 2500
    # Projections decode like a plain select; a batch larger than the result is fine.
    pairs = db.select(Items.n, Items.label).from_(Items).where(gt(Items.n, 2490))
    assert [p async for p in pairs.order_by(Items.n).stream(batch_size=50)] == [
        (n, f"item {n}") for n in range(2491, 2501)
    ]
    assert [r async for r in db.select().from_(Items).where(eq(Items.n, -1)).stream()] == []


async def test_relational_stream(db: Database) -> None:
    users = await db.insert_many(
        Users, [{"email": f"u{i}@x.io"} for i in range(30)], returning=True
    )
    await db.insert_many(Posts, [{"author_id": u.id, "title": f"t{u.id}"} for u in users])
    query = db.query(Users).find_many(with_={"posts": True}, order_by=Users.id)
    streamed = [u async for u in query.stream(batch_size=7)]
    assert [(u.email, [p.title for p in u.posts]) for u in streamed] == [
        (u.email, [f"t{u.id}"]) for u in users
    ]


async def test_batch_size_must_be_positive(db: Database) -> None:
    with pytest.raises(QueryError, match="batch_size"):
        [r async for r in db.select().from_(Items).stream(batch_size=0)]


async def test_breaking_out_releases_the_connection(db: Database) -> None:
    async with aclosing(db.select().from_(Items).stream(batch_size=100)) as rows:
        async for row in rows:
            if row.n == 150:
                break
    assert_no_leak(db)  # aclosing(): released right away

    async for row in db.select().from_(Items).stream(batch_size=100):
        if row.n == 150:
            break
    await released(db)  # plain break: released when the loop finalizes the generator


async def test_cancelled_stream_releases_the_connection(db: Database) -> None:
    started = asyncio.Event()

    async def consume() -> None:
        async for _ in db.select().from_(Items).stream(batch_size=10):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await released(db)
    assert len(await db.select().from_(Items)) == 2500


async def test_error_mid_stream_releases_the_connection(db: Database) -> None:
    # Fails on the row with n = 1500, after the first batches were already yielded (no
    # ORDER BY: a sort would compute every row before the first batch).
    query = db.select({"x": sql("1 / (n - 1500)")}).from_(Items)
    seen = 0
    with pytest.raises(DatabaseError, match="DivisionByZero"):
        async for _ in query.stream(batch_size=100):
            seen += 1
    assert seen == 1400
    assert_no_leak(db)


async def test_stream_inside_a_transaction(db: Database) -> None:
    # The loop body runs other statements on the same connection while the cursor is open.
    async with db.transaction() as tx:
        async for row in tx.select().from_(Items).where(gt(Items.n, 2400)).stream(batch_size=7):
            await tx.update(Items).set(label=f"seen {row.n}").where(eq(Items.id, row.id))
        # An early break leaves the cursor until the transaction ends; the tx still works.
        async for _ in tx.select().from_(Items).stream(batch_size=5):
            break
        assert len(await tx.select().from_(Items).where(gt(Items.n, 2400))) == 100
    labels = {r.label for r in await db.select().from_(Items).where(gt(Items.n, 2400))}
    assert labels == {f"seen {n}" for n in range(2401, 2501)}

    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            async for row in tx.select().from_(Items).stream(batch_size=500):
                await tx.delete(Items).where(eq(Items.id, row.id))
            raise RuntimeError
    assert len(await db.select().from_(Items)) == 2500


async def test_auto_reset_cursor_leaves_no_state(pg_url: str, driver: PostgresDriver) -> None:
    async with driver.acquire() as conn:
        await conn.execute(DDL, [])
    async with await Database.connect(
        pg_url, schema=[Items], pool_size=1, reset_connections="auto"
    ) as db:
        async for _ in db.select().from_(Items).stream(batch_size=10):
            break
        await released(db)
        [row] = await db.execute(sql("select count(*) as n from pg_cursors where name <> ''"))
        assert row["n"] == 0
        assert len([r async for r in db.select().from_(Items).stream()]) == 2500


async def test_lost_connection_mid_stream(db: Database, driver: PostgresDriver) -> None:
    rows = db.select().from_(Items).stream(batch_size=10)
    with pytest.raises(ConnectionError):
        async with aclosing(rows):
            async for _ in rows:
                async with driver.acquire() as conn:
                    await conn.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        f"WHERE application_name = '{APP}' AND pid <> pg_backend_pid()",
                        [],
                    )
    assert len(await db.select().from_(Items)) == 2500  # the pool was replaced


async def test_logger_sees_one_event_per_stream(pg_url: str, driver: PostgresDriver) -> None:
    async with driver.acquire() as conn:
        await conn.execute(DDL, [])
    events: list[QueryEvent] = []
    async with await Database.connect(pg_url, schema=[Items], logger=events.append) as db:
        assert len([r async for r in db.select().from_(Items).stream(batch_size=100)]) == 2500
        async with aclosing(db.select().from_(Items).stream()) as rows:
            async for _ in rows:
                break
    assert [e.error for e in events] == [None, None]
    assert "stream_items" in events[0].sql


async def test_streaming_200k_rows_keeps_memory_bounded(db: Database) -> None:
    async with db.driver.acquire() as conn:
        await conn.execute(
            "INSERT INTO stream_items (n, label) "
            "SELECT g, repeat('x', 200) FROM generate_series(1, 200000) g",
            [],
        )
    query = db.select().from_(Items).where(gt(Items.id, 2500))
    tracemalloc.start()
    try:
        count = 0
        async for row in query.stream(batch_size=1000):
            count += len(row.label) > 0
        _, streamed_peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        rows = await query
        _, loaded_peak = tracemalloc.get_traced_memory()
        del rows
    finally:
        tracemalloc.stop()
    assert count == 200_000
    # ~1000 rows in flight at a time vs. all 200k: well over 50 MB when loaded at once.
    assert streamed_peak < 5_000_000, streamed_peak
    assert loaded_peak > 10 * streamed_peak


REPLICA_DDL = """
DROP SCHEMA IF EXISTS stream_replica CASCADE;
CREATE SCHEMA stream_replica;
CREATE TABLE stream_replica.stream_items (LIKE public.stream_items INCLUDING ALL);
INSERT INTO stream_replica.stream_items (n, label) VALUES (-1, 'replica');
"""


async def test_plain_streams_read_from_replicas(db: Database, pg_url: str) -> None:
    async with db.driver.acquire() as conn:
        await conn.execute(REPLICA_DDL, [])
    replica = pg_url + ("&" if "?" in pg_url else "?") + "search_path=stream_replica"
    try:
        async with await Database.connect(pg_url, schema=[Items], replicas=[replica]) as rdb:
            labels = [r.label async for r in rdb.select().from_(Items).stream()]
            assert labels == ["replica"]
            assert len([r async for r in rdb.primary.select().from_(Items).stream()]) == 2500
            locked = rdb.select().from_(Items).for_update()
            assert len([r async for r in locked.stream()]) == 2500  # FOR UPDATE: primary
    finally:
        async with db.driver.acquire() as conn:
            await conn.execute("DROP SCHEMA stream_replica CASCADE", [])

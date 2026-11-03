"""What production traffic does to a pool: cancellation, timeouts, overload, killed
connections, real serialization conflicts and deadlocks, racing upserts. After each test the
pool must hold no leaked connection and still work.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import final

import asyncpg
import pytest

from mizzle import (
    ConnectionError,
    Database,
    QueryTimeout,
    SerializationFailure,
    Table,
    UniqueViolation,
    eq,
    excluded,
    sql,
)
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.pg import integer, serial, text

APP = "mizzle_stress"


@final
class Counters(Table, name="stress_counters"):
    id = serial().primary_key()
    key = text().not_null().unique()
    value = integer().not_null()


DDL = """
DROP TABLE IF EXISTS stress_counters;
CREATE TABLE stress_counters (
    id serial PRIMARY KEY, key text NOT NULL UNIQUE, value integer NOT NULL
);
"""


@pytest.fixture
async def db(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    async with driver.acquire() as conn:
        await conn.execute(DDL, [])
    url = pg_url + ("&" if "?" in pg_url else "?") + f"application_name={APP}"
    async with await Database.connect(url, schema=[Counters], pool_size=4) as database:
        yield database
        assert_no_leak(database)
    async with driver.acquire() as conn:
        await conn.execute("DROP TABLE stress_counters", [])


def pool_of(db: Database) -> asyncpg.Pool:
    driver = db.driver
    assert isinstance(driver, PostgresDriver)
    return driver._pool


def assert_no_leak(db: Database) -> None:
    pool = pool_of(db)
    assert pool.get_idle_size() == pool.get_size(), "a connection was not returned to the pool"


async def healthy(db: Database) -> None:
    assert await db.execute(sql("select 1 as one")) == [{"one": 1}]
    assert_no_leak(db)


async def test_cancelled_queries_free_their_connections(db: Database) -> None:
    tasks = [asyncio.create_task(db.execute(sql("select pg_sleep(5)"))) for _ in range(8)]
    await asyncio.sleep(0.2)
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    async with asyncio.timeout(3):  # the server-side sleeps were cancelled, not awaited
        await healthy(db)


async def test_cancel_inside_transaction_rolls_back(db: Database) -> None:
    async def writer() -> None:
        async with db.transaction() as tx:
            await tx.insert(Counters).values(key="ghost", value=1)
            await tx.execute(sql("select pg_sleep(5)"))

    task = asyncio.create_task(writer())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db.select().from_(Counters) == []
    await healthy(db)


async def test_overload_queues_instead_of_failing(db: Database) -> None:
    # 200 queries, 4 connections: everyone waits their turn.
    results = await asyncio.gather(
        *(db.execute(sql("select pg_sleep(0.005), {}::int as i", i)) for i in range(200))
    )
    assert [r[0]["i"] for r in results] == list(range(200))
    await healthy(db)


async def test_timeouts_under_load_leave_pool_healthy(db: Database) -> None:
    fast = db.with_timeout(0.05)
    results = await asyncio.gather(
        *(fast.execute(sql("select pg_sleep({})", 0.2 if i % 2 else 0)) for i in range(40)),
        return_exceptions=True,
    )
    timeouts = [r for r in results if isinstance(r, QueryTimeout)]
    assert len(timeouts) == 20
    assert all(isinstance(r, list) for r in results if not isinstance(r, QueryTimeout))
    await healthy(db)


async def test_killed_connections_recover(db: Database, driver: PostgresDriver) -> None:
    await asyncio.gather(*(db.execute(sql("select pg_sleep(0.05)")) for _ in range(4)))
    async with driver.acquire() as admin:
        killed = await admin.fetch(
            "select pg_terminate_backend(pid) from pg_stat_activity where application_name = $1",
            [APP],
        )
    assert killed
    # Plain reads retry once on a fresh connection: no error reaches the app.
    for _ in range(10):
        assert await db.select().from_(Counters) == []
    await healthy(db)


async def test_killed_connections_fail_writes_with_connection_error(
    db: Database, driver: PostgresDriver
) -> None:
    await asyncio.gather(*(db.execute(sql("select pg_sleep(0.05)")) for _ in range(4)))
    async with driver.acquire() as admin:
        await admin.fetch(
            "select pg_terminate_backend(pid) from pg_stat_activity where application_name = $1",
            [APP],
        )
    # Writes are never retried (they may have run); they fail with mizzle's ConnectionError.
    outcomes: list[str] = []
    for i in range(6):
        try:
            await db.insert(Counters).values(key=f"w{i}", value=i)
            outcomes.append("ok")
        except ConnectionError:
            outcomes.append("lost")
    assert outcomes.count("lost") <= 1  # the pool is replaced after the first loss
    assert outcomes[-1] == "ok"
    await healthy(db)


async def test_serialization_conflicts_retry_to_the_right_total(db: Database) -> None:
    await db.insert(Counters).values(key="hits", value=0)

    async def increment() -> None:
        async with db.transaction("serializable") as tx:
            [row] = await tx.select().from_(Counters).where(eq(Counters.key, "hits"))
            await asyncio.sleep(0.001)  # widen the race window
            await tx.update(Counters).set(value=row.value + 1).where(eq(Counters.id, row.id))

    await asyncio.gather(*(db.retry(increment, attempts=50, backoff=0.001) for _ in range(20)))
    [row] = await db.select().from_(Counters).where(eq(Counters.key, "hits"))
    assert row.value == 20
    await healthy(db)


async def test_deadlock_is_a_retryable_serialization_failure(db: Database) -> None:
    await db.insert(Counters).values([{"key": "a", "value": 0}, {"key": "b", "value": 0}])
    both_locked_one = asyncio.Barrier(2)

    async def lock(first: str, second: str) -> None:
        async with db.transaction() as tx:
            await tx.update(Counters).set(value=1).where(eq(Counters.key, first))
            await both_locked_one.wait()
            await tx.update(Counters).set(value=2).where(eq(Counters.key, second))

    results = await asyncio.gather(lock("a", "b"), lock("b", "a"), return_exceptions=True)
    assert sum(isinstance(r, SerializationFailure) for r in results) == 1
    await healthy(db)


async def test_racing_upserts_never_fail(db: Database) -> None:
    async def bump() -> None:
        await (
            db.insert(Counters)
            .values(key="race", value=1)
            .on_conflict_do_update(
                target=Counters.key,
                set_={"value": sql("{} + 1", Counters.value)},
            )
        )

    await asyncio.gather(*(bump() for _ in range(100)))
    [row] = await db.select().from_(Counters).where(eq(Counters.key, "race"))
    assert row.value == 100
    await healthy(db)


async def test_racing_plain_inserts_raise_unique_violation(db: Database) -> None:
    results = await asyncio.gather(
        *(db.insert(Counters).values(key="once", value=i) for i in range(20)),
        return_exceptions=True,
    )
    assert sum(r == 1 for r in results) == 1
    assert all(isinstance(r, UniqueViolation) for r in results if r != 1)
    await healthy(db)


async def test_errors_inside_transactions_do_not_poison_the_pool(db: Database) -> None:
    for i in range(20):
        with pytest.raises(UniqueViolation):
            async with db.transaction() as tx:
                await tx.insert(Counters).values(key=f"k{i}", value=1)
                await tx.insert(Counters).values(key=f"k{i}", value=2)
    assert await db.select().from_(Counters) == []
    await healthy(db)


async def test_excluded_upsert_values(db: Database) -> None:
    await db.insert(Counters).values(key="x", value=1)
    await (
        db.insert(Counters)
        .values(key="x", value=5)
        .on_conflict_do_update(target=Counters.key, set_={"value": excluded(Counters.value)})
    )
    [row] = await db.select().from_(Counters)
    assert row.value == 5

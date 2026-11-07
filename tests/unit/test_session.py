from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import pytest

from mizzle import (
    ConnectionError,
    Database,
    QueryError,
    SerializationFailure,
    eq,
    exists,
    placeholder,
)
from mizzle.dialects.base import Connection, Driver, PoolStats
from tests.models import Posts, Users


class FakeConnection:
    def __init__(self, name: str, seen: list[tuple[str, object]] | None = None) -> None:
        self.name, self.seen = name, seen

    async def fetch(
        self, sql: str, params: object, timeout: float | None = None, *, session_safe: bool = False
    ) -> list[object]:
        return []

    async def fetch_dicts(
        self, sql: str, params: object, timeout: float | None = None
    ) -> list[object]:
        if self.seen is not None:
            self.seen.append((sql, params))
        return []

    async def execute(
        self, sql: str, params: object, timeout: float | None = None, *, session_safe: bool = False
    ) -> int:
        return 0


class FakeDriver:
    def __init__(
        self,
        name: str,
        log: list[str],
        down: bool = False,
        seen: list[tuple[str, object]] | None = None,
    ) -> None:
        self.name, self.log, self.down, self.seen = name, log, down, seen

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Connection]:
        if self.down:
            raise ConnectionError("down")
        self.log.append(self.name)
        # Only fetch() and fetch_dicts() are used here.
        yield cast(Connection, cast(object, FakeConnection(self.name, self.seen)))

    def stats(self) -> PoolStats:
        return PoolStats(size=1, idle=1, in_use=0, max_size=1, min_size=1, wait_ms=0.0)

    async def close(self) -> None:
        pass

    async def expire_connections(self) -> None:
        pass


def database(log: list[str], *replicas: FakeDriver) -> Database:
    db = Database(schema=[Users, Posts], driver=cast(Driver, FakeDriver("primary", log)))
    db._swap_drivers(db.driver, cast(list[Driver], list(replicas)))
    return db


async def test_replicas_take_turns_and_fall_back_to_primary() -> None:
    log: list[str] = []
    db = database(log, FakeDriver("r1", log), FakeDriver("r2", log), FakeDriver("r3", log, True))
    for _ in range(3):
        await db.select().from_(Users)
    await db.primary.select().from_(Users)
    assert log == ["r1", "r2", "primary", "primary"]


async def test_row_locks_anywhere_use_the_primary() -> None:
    log: list[str] = []
    db = database(log, FakeDriver("r1", log))
    await db.select().from_(Users).for_update()
    locked = db.select().from_(Posts).for_update()
    await db.select().from_(Users).where(exists(locked))  # lock inside a subquery
    await db.with_(locked.cte("p")).select().from_(Users)  # lock inside a CTE
    await db.select().from_(Users)
    assert log == ["primary", "primary", "primary", "r1"]


def test_evolve_rejects_unknown_fields() -> None:
    from mizzle.core.query.base import evolve

    with pytest.raises(TypeError, match="no field"):
        evolve(Database().select().from_(Users), nope=1)


async def test_explain_compiles_explain_and_guards_writes() -> None:
    log: list[str] = []
    seen: list[tuple[str, object]] = []
    db = Database(schema=[Users, Posts], driver=cast(Driver, FakeDriver("primary", log, seen=seen)))

    await db.explain(db.select().from_(Users).where(eq(Users.id, 1)))
    await db.explain(db.select().from_(Users), analyze=True)
    assert seen[0][0].startswith("EXPLAIN (FORMAT TEXT) SELECT")
    assert seen[1][0].startswith("EXPLAIN (ANALYZE, FORMAT TEXT) SELECT")

    with pytest.raises(QueryError, match="only selects"):
        await db.explain(db.insert(Users).values(email="a@b"), analyze=True)
    with pytest.raises(QueryError, match="placeholder"):
        await db.explain(db.select().from_(Users).where(eq(Users.id, placeholder("id"))))


async def test_retry() -> None:
    db = Database()
    calls: list[int] = []

    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise SerializationFailure("conflict")
        return "ok"

    assert await db.retry(flaky, backoff=0) == "ok"
    calls.clear()
    with pytest.raises(SerializationFailure):
        await db.retry(flaky, attempts=2, backoff=0)
    assert len(calls) == 2
    with pytest.raises(QueryError):
        await db.retry(flaky, attempts=0)


async def test_ping_and_pool_stats() -> None:
    log: list[str] = []
    db = database(log)
    assert await db.ping() is True
    stats = db.pool_stats()
    assert stats == PoolStats(size=1, idle=1, in_use=0, max_size=1, min_size=1, wait_ms=0.0)

    down = Database(driver=cast(Driver, FakeDriver("x", log, down=True)))
    assert await down.ping() is False

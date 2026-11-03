from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import pytest

from mizzle import ConnectionError, Database, QueryError, SerializationFailure
from mizzle.dialects.base import Connection, Driver
from tests.models import Posts, Users


class FakeConnection:
    def __init__(self, name: str) -> None:
        self.name = name

    async def fetch(self, sql: str, params: object, timeout: float | None = None) -> list[object]:
        return []


class FakeDriver:
    def __init__(self, name: str, log: list[str], down: bool = False) -> None:
        self.name, self.log, self.down = name, log, down

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Connection]:
        if self.down:
            raise ConnectionError("down")
        self.log.append(self.name)
        # Only fetch() is used here.
        yield cast(Connection, cast(object, FakeConnection(self.name)))

    async def close(self) -> None:
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

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest

from mizzle import (
    ConnectionError,
    Database,
    Notification,
    QueryError,
    Reconnected,
    SerializationFailure,
    exists,
)
from mizzle.dialects.base import Connection, Driver
from tests.models import Posts, Users


class FakeConnection:
    def __init__(self, name: str) -> None:
        self.name = name

    async def fetch(
        self, sql: str, params: object, timeout: float | None = None, *, session_safe: bool = False
    ) -> list[object]:
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

    async def expire_connections(self) -> None:
        pass

    async def notify_unpooled(self, channel: str, payload: str = "") -> None:
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


class FlakyListenDriver:
    """acquire() fails ``down`` times after the first listener is lost."""

    def __init__(self, down: int) -> None:
        self.down = down
        self.opened = 0
        self.expired = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Connection]:
        if self.opened and self.down:
            self.down -= 1
            raise ConnectionError("down")
        yield cast(Connection, cast(object, self))

    async def notify_unpooled(self, channel: str, payload: str = "") -> None:
        pass

    @asynccontextmanager
    async def listen(self, *_: object) -> AsyncGenerator[AsyncIterator[Notification]]:
        self.opened += 1
        number = self.opened

        async def events() -> AsyncGenerator[Notification]:
            yield Notification("c", str(number), 1)
            raise ConnectionError("lost")

        yield events()

    async def close(self) -> None:
        pass

    async def expire_connections(self) -> None:
        self.expired += 1


async def test_listen_reconnect_retries_until_the_database_is_back() -> None:
    driver = FlakyListenDriver(down=3)
    db = Database(driver=cast(Driver, driver))
    async with db.listen("c", reconnect=True) as events:
        got = [await anext(events) for _ in range(5)]
    assert got == [
        Notification("c", "1", 1),
        Reconnected(),
        Notification("c", "2", 1),
        Reconnected(),
        Notification("c", "3", 1),
    ]
    assert driver.down == 0 and driver.expired == 2

"""Runtime and operations: ping, pool stats, connect hooks, slow queries, retries."""

import json
import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest

from mizzle import ConnectionError, Database, DatabaseError, QueryError, eq, placeholder, sql
from mizzle.dialects.base import Connection
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from tests.models import Posts, Users


async def setting(db: Database, name: str) -> str:
    [row] = await db.execute(sql(f"SELECT current_setting('{name}') AS value"))
    return str(row["value"])


async def test_ping_and_pool_stats(pg_url: str) -> None:
    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=3) as db:
        assert await db.ping() is True
        stats = db.pool_stats()
        assert stats.max_size == 3
        assert stats.min_size == 1
        assert stats.in_use == stats.size - stats.idle
        assert stats.wait_ms >= 0


async def test_on_connect_runs_for_each_new_connection(pg_url: str) -> None:
    opened = 0

    async def hook(conn: Connection) -> None:
        nonlocal opened
        opened += 1
        await conn.execute("SET application_name = 'hooked'", [])

    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=1, on_connect=hook
    ) as db:
        assert await setting(db, "application_name") == "hooked"
    assert opened >= 1


async def test_server_settings_apply_to_every_connection(pg_url: str) -> None:
    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=2, server_settings={"application_name": "srv"}
    ) as db:
        assert await setting(db, "application_name") == "srv"


async def test_slow_query_warning(pg_url: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="mizzle"):
        async with await Database.connect(pg_url, schema=[Users, Posts], slow_query_ms=0.0) as db:
            await db.execute(sql("SELECT 1"))
    assert any("slow query" in record.getMessage() for record in caplog.records)


async def test_connect_retries_until_the_database_is_back(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PostgresDialect.connect
    attempts = 0

    async def flaky(self: PostgresDialect, url: str, **kwargs: Any) -> PostgresDriver:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("not up yet")
        return await original(self, url, **kwargs)

    monkeypatch.setattr(PostgresDialect, "connect", flaky)
    db = await Database.connect(
        pg_url, schema=[Users, Posts], connect_retries=5, connect_backoff=0.001
    )
    async with db:
        assert await db.ping() is True
    assert attempts == 3


async def test_listen_decodes_json_payloads(pg_url: str) -> None:
    async with (
        await Database.connect(pg_url, schema=[Users, Posts]) as db,
        db.listen("orders", decoder=json.loads) as events,
    ):
        await db.notify("orders", json.dumps({"id": 7}))
        event = await anext(events)
        assert event.data == {"id": 7}
        assert event.payload == '{"id": 7}'


async def test_transaction_callbacks(db: Database) -> None:
    events: list[str] = []

    async def commit() -> None:
        events.append("commit")

    async def rollback() -> None:
        events.append("rollback")

    async with db.transaction(after_commit=commit, after_rollback=rollback) as tx:
        await tx.insert(Users).values(email="rt8@x.io")
    assert events == ["commit"]

    with pytest.raises(DatabaseError):  # duplicate email -> rollback
        async with db.transaction(after_commit=commit, after_rollback=rollback) as tx:
            await tx.insert(Users).values(email="rt8@x.io")
    assert events == ["commit", "rollback"]
    assert await db.select().from_(Users).where(eq(Users.email, "rt8@x.io")) != []


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}


class RecordingTracer:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.transactions = 0

    @contextmanager
    def statement(self, sql: str, params: tuple[object, ...] | None) -> Generator[_Span]:
        self.statements.append(sql)
        yield _Span()

    @contextmanager
    def transaction(self, isolation: object = None) -> Generator[_Span]:
        self.transactions += 1
        yield _Span()


async def test_tracer_wraps_statements_and_transactions(pg_url: str) -> None:
    tracer = RecordingTracer()
    async with (
        await Database.connect(pg_url, schema=[Users, Posts], tracer=tracer) as db,
        db.transaction(),
    ):
        await db.execute(sql("SELECT 1"))
    assert tracer.transactions == 1
    assert "SELECT 1" in tracer.statements


async def test_executemany_pipelines_many_sets(db: Database) -> None:
    ran = await db.executemany(
        sql("INSERT INTO users (email) VALUES ($1)"), [("e1@x.io",), ("e2@x.io",), ("e3@x.io",)]
    )
    assert ran == 3
    found = await db.select(Users.email).from_(Users).order_by(Users.email)
    assert [email for (email,) in found] == ["e1@x.io", "e2@x.io", "e3@x.io"]

    with pytest.raises(QueryError):  # mizzle values, not external parameter sets
        await db.executemany(
            sql("INSERT INTO users (email) VALUES (", placeholder("email"), ")"), [("x",)]
        )

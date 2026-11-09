"""migrate.py: target resolution and how statements are batched into transactions."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest

from mizzle.core.errors import MigrationError
from mizzle.core.steps import Statement
from mizzle.dialects.base import Connection
from mizzle.kit.journal import Migration
from mizzle.kit.migrate import apply_statements, resolve_target


def migration(index: int, name: str) -> Migration:
    return Migration(index=index, name=name, path=Path(f"/migrations/{name}"))


def test_resolve_target_by_name_prefix_and_number() -> None:
    migrations = [migration(1, "0001_create_users"), migration(2, "0002_add_age")]
    assert resolve_target(migrations, "0001_create_users") == 1
    assert resolve_target(migrations, "0002") == 2
    assert resolve_target(migrations, "2") == 2
    with pytest.raises(MigrationError, match="no migration matches"):
        resolve_target(migrations, "9999")


class FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        self.transactions += 1
        yield

    async def execute(self, sql: str, params: object, **_kwargs: object) -> int:
        self.calls.append((sql, params))
        return 0


def fake_conn() -> tuple[Connection, FakeConn]:
    fake = FakeConn()
    return cast(Connection, cast(object, fake)), fake


async def test_apply_statements_transactional_runs_once() -> None:
    conn, fake = fake_conn()
    statements = [Statement("CREATE TABLE a (id int)"), Statement("CREATE TABLE b (id int)")]
    await apply_statements(conn, statements, ("insert history", ["x"]))

    assert fake.transactions == 1
    assert [sql for sql, _ in fake.calls] == [
        "CREATE TABLE a (id int)",
        "CREATE TABLE b (id int)",
        "insert history",
    ]


async def test_apply_statements_batches_around_nontransactional() -> None:
    conn, fake = fake_conn()
    statements = [
        Statement("CREATE TABLE a (id int)"),
        Statement("CREATE TABLE b (id int)"),
        Statement("ALTER TYPE t ADD VALUE 'x'", transactional=False),
        Statement("CREATE TABLE d (id int)"),
    ]
    await apply_statements(conn, statements, ("insert history", []))

    assert [sql for sql, _ in fake.calls] == [
        "CREATE TABLE a (id int)",
        "CREATE TABLE b (id int)",
        "ALTER TYPE t ADD VALUE 'x'",
        "CREATE TABLE d (id int)",
        "insert history",
    ]
    assert fake.transactions == 2  # two transactional batches; the odd one runs alone

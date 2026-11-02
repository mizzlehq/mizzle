"""End-to-end tests of the `mizzle` CLI against a fresh database per test."""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import asyncpg
import pytest

from mizzle.core.errors import MigrationTampered
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.cli import main
from mizzle.kit.journal import MigrationsDir
from mizzle.kit.migrate import run_migrations

V1 = """
import enum
from mizzle import Table, sql, index
from mizzle.pg import serial, varchar, text, integer, timestamp, pg_enum

class Role(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"

class Users(Table, name="users"):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    name = text()
    role = pg_enum(Role).not_null().default(Role.USER)
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    title = text().not_null()

    @classmethod
    def config(cls):
        return [index("posts_author_idx").on(cls.author_id)]
"""


@pytest.fixture
async def kit_url(pg_url: str) -> AsyncGenerator[str]:
    name = f"kit_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg_url)
    await admin.execute(f'CREATE DATABASE "{name}"')
    base, _, _ = pg_url.rpartition("/")
    yield f"{base}/{name}"
    await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    await admin.close()


@pytest.fixture
def project(tmp_path: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    module = f"schema_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module}.py").write_text(V1)
    (tmp_path / "mizzle.toml").write_text(f'schema = "{module}"\nout = "migrations"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", kit_url)
    yield tmp_path


def schema_file(project: Path) -> Path:
    return next(p for p in project.glob("schema_*.py") if not p.stem.endswith("_types"))


def rewrite_schema(project: Path, source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write a new schema under a new module name (Python caches imported modules)."""
    old = schema_file(project)
    old.unlink()
    module = f"schema_{uuid.uuid4().hex[:8]}"
    (project / f"{module}.py").write_text(source)
    (project / "mizzle.toml").write_text(f'schema = "{module}"\nout = "migrations"\n')


async def columns(url: str, table: str) -> list[str]:
    conn = await asyncpg.connect(url)
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $1 "
        "ORDER BY ordinal_position",
        table,
    )
    await conn.close()
    return [r[0] for r in rows]


def run(*args: str) -> int:
    # The CLI uses asyncio.run, so call it from a worker thread inside async tests.
    return main(list(args))


async def cli(*args: str) -> int:
    return await asyncio.to_thread(run, *args)


async def test_generate_migrate_cycle(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli("generate") == 0
    sql = next(project.glob("migrations/0001_*/migration.sql")).read_text()
    assert "CREATE TYPE" in sql and "CREATE TABLE" in sql and "REFERENCES" in sql
    assert await cli("generate") == 0
    assert "No schema changes." in capsys.readouterr().out

    assert await cli("migrate") == 0
    assert await cli("migrate") == 0
    assert "Database is up to date." in capsys.readouterr().out
    assert await columns(kit_url, "users") == ["id", "email", "name", "role", "created_at"]

    # Rename a column (explicit flag, since tests are not interactive) and add one.
    rewrite_schema(
        project,
        V1.replace("    name = text()", "    full_name = text()\n    age = integer()"),
        monkeypatch,
    )
    assert await cli("generate") == 1  # ambiguous: rename or drop+add?
    assert await cli("generate", "--rename", "users.name=full_name", "--name", "rename name") == 0
    assert (project / "migrations" / "0002_rename_name").exists()
    assert await cli("migrate") == 0
    assert await columns(kit_url, "users") == [
        "id",
        "email",
        "full_name",
        "role",
        "created_at",
        "age",
    ]
    assert await cli("check") == 0


async def test_data_loss_needs_flag(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await cli("generate") == 0
    rewrite_schema(project, V1.replace("    name = text()\n", ""), monkeypatch)
    assert await cli("generate") == 1
    assert await cli("generate", "--allow-data-loss") == 0


async def test_tamper_and_concurrency(project: Path, kit_url: str) -> None:
    assert await cli("generate") == 0
    directory = MigrationsDir(project / "migrations", "postgresql")
    dialect = PostgresDialect()
    drivers = [await PostgresDriver.connect(kit_url, pool_size=1) for _ in range(5)]
    try:
        results = await asyncio.gather(
            *(run_migrations(d, dialect, directory, lock_timeout=10) for d in drivers)
        )
        assert sorted(len(r) for r in results) == [0, 0, 0, 0, 1]  # applied exactly once

        sql_path = directory.migrations()[0].sql_path
        sql_path.write_text(sql_path.read_text() + "\n-- edited\n")
        with pytest.raises(MigrationTampered):
            await run_migrations(drivers[0], dialect, directory)
    finally:
        for d in drivers:
            await d.close()


async def test_drop(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    assert await cli("drop") == 1  # applied: refuse
    rewrite_schema(
        project,
        V1.replace("    name = text()", "    name = text()\n    age = integer()"),
        monkeypatch,
    )
    assert await cli("generate") == 0
    assert await cli("drop") == 0
    assert len(MigrationsDir(project / "migrations", "postgresql").migrations()) == 1


async def test_check_detects_fork(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await cli("generate") == 0
    migrations = project / "migrations"
    (migrations / "0002_other_branch").mkdir()
    assert await cli("check") == 1


async def test_push_then_pull_roundtrip(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli("push", "--force") == 0
    assert await cli("push", "--force") == 0
    assert "Database is up to date." in capsys.readouterr().out

    # Pull into a fresh project; generating from the pulled schema must find no changes.
    assert await cli("pull") == 0
    pulled = (project / "pulled_schema.py").read_text()
    assert "class Users(Table" in pulled and "pg_enum(Role" in pulled
    module = f"pulled_{uuid.uuid4().hex[:8]}"
    (project / f"{module}.py").write_text(pulled)
    (project / "mizzle.toml").write_text(f'schema = "{module}"\nout = "migrations"\n')
    assert await cli("generate") == 0
    assert "No schema changes." in capsys.readouterr().out


async def test_typegen(project: Path) -> None:
    assert await cli("typegen") == 0
    types = next(project.glob("schema_*_types.py")).read_text()
    assert "class UsersInsert(TypedDict):" in types
    assert "    email: str\n" in types
    assert "    id: NotRequired[int]\n" in types
    assert "    name: NotRequired[str | None]\n" in types
    assert "class UsersUpdate(TypedDict, total=False):" in types
    compile(types, "types.py", "exec")


def test_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert run("generate") == 1


def test_env_url_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "mizzle.toml").write_text('schema = "x"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert run("migrate") == 1

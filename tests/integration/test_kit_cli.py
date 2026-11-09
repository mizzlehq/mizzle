"""End-to-end tests of the `mizzle` CLI against a fresh database per test."""

import asyncio
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import asyncpg
import pytest

import mizzle.kit.cli as kit_cli
from mizzle.core.errors import MigrationError, MigrationTampered
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
        return [
            index("posts_author_idx").on(cls.author_id),
            index("posts_title_lower_idx").on(sql("lower(title)")),
        ]
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


async def test_migrate_reports_missing_on_disk(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await cli("generate") == 0
    rewrite_schema(
        project,
        V1.replace("    name = text()", "    name = text()\n    age = integer()"),
        monkeypatch,
    )
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    directory = MigrationsDir(project / "migrations", "postgresql")
    directory.drop_latest()  # 0002 leaves disk and the journal, but stays applied in the DB
    driver = await PostgresDriver.connect(kit_url, pool_size=1)
    try:
        with pytest.raises(MigrationError, match="missing on disk"):
            await run_migrations(driver, PostgresDialect(), directory)
    finally:
        await driver.close()


async def test_migrate_reports_a_forked_history(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await cli("generate") == 0
    rewrite_schema(
        project,
        V1.replace("    name = text()", "    name = text()\n    age = integer()"),
        monkeypatch,
    )
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    directory = MigrationsDir(project / "migrations", "postgresql")
    driver = await PostgresDriver.connect(kit_url, pool_size=1)
    try:
        async with driver.acquire() as conn:  # forget 0001 while 0002 stays applied
            await conn.execute(
                "DELETE FROM public.__mizzle_migrations WHERE name = $1",
                [directory.migrations()[0].name],
            )
        with pytest.raises(MigrationError, match="not applied but a later"):
            await run_migrations(driver, PostgresDialect(), directory)
    finally:
        await driver.close()


async def test_migrate_survives_a_failed_lock_release(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await cli("generate") == 0
    directory = MigrationsDir(project / "migrations", "postgresql")
    dialect = PostgresDialect()

    async def boom(_conn: object) -> None:
        raise RuntimeError("the connection is already broken")

    monkeypatch.setattr(dialect, "release_migration_lock", boom)
    driver = await PostgresDriver.connect(kit_url, pool_size=1)
    try:
        applied = await run_migrations(driver, dialect, directory)
        assert applied == [directory.migrations()[0].name]
    finally:
        await driver.close()


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


async def test_check_accepts_written_custom_migrations(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli("generate") == 0
    assert await cli("generate", "--custom", "--name", "seed") == 0
    custom = next(project.glob("migrations/0002_seed/migration.sql"))
    custom.write_text(custom.read_text() + "INSERT INTO users (email) VALUES ('a@b.c');\n")
    capsys.readouterr()
    assert await cli("check") == 0
    assert "edited" not in capsys.readouterr().out  # written by hand on purpose
    generated = next(project.glob("migrations/0001_*/migration.sql"))
    generated.write_text(generated.read_text() + "-- edited\n")
    assert await cli("check") == 0
    assert "0001" in capsys.readouterr().out  # a generated one still warns


async def test_check_detects_fork(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await cli("generate") == 0
    migrations = project / "migrations"
    (migrations / "0002_other_branch").mkdir()
    assert await cli("check") == 1


async def test_check_fix_repairs_journal(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli("generate") == 0
    sql = next(project.glob("migrations/0001_*/migration.sql"))
    sql.write_text(sql.read_text() + "-- edited\n")
    snapshot = next(project.glob("migrations/0001_*/snapshot.json"))
    data = json.loads(snapshot.read_text())
    data["prev_id"] = "bogus"
    snapshot.write_text(json.dumps(data, indent=2) + "\n")
    capsys.readouterr()
    assert await cli("check") == 1  # a broken prev_id is a problem
    # B5: an unknown parent means the snapshot may be stale: refuse instead of re-linking,
    # and never refresh the hash of an edited migration (that would hide the edit).
    assert await cli("check", "--fix") == 1
    assert "generate" in capsys.readouterr().err
    data["prev_id"] = None
    snapshot.write_text(json.dumps(data, indent=2) + "\n")
    assert await cli("check", "--fix") == 0
    assert "hash refreshed" not in capsys.readouterr().out
    assert await cli("check") == 0
    assert "edited after generate" in capsys.readouterr().out


async def test_custom_migration_template(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    template = project / "seed.sql"
    template.write_text("-- seed data\nSELECT 1;\n")
    assert await cli("generate", "--custom", "--name", "seed", "--template", str(template)) == 0
    custom = next(project.glob("migrations/0001_seed/migration.sql"))
    assert custom.read_text().startswith("-- seed data\nSELECT 1;\n")
    custom.write_text(custom.read_text() + "SELECT 2;\n")  # editing it is expected
    capsys.readouterr()
    assert await cli("check") == 0
    assert "edited" not in capsys.readouterr().out
    assert await cli("generate", "--template", str(template)) == 1  # needs --custom


async def test_reset_reruns_every_migration(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    conn = await asyncpg.connect(kit_url)
    await conn.execute("INSERT INTO users (email, name) VALUES ('a@b.c', 'a')")
    assert await conn.fetchval("SELECT count(*) FROM users") == 1
    await conn.close()
    capsys.readouterr()

    database = kit_url.rsplit("/", 1)[-1]
    assert await cli("reset") == 1  # non-interactive: needs an explicit confirmation
    assert await cli("reset", "--force") == 1  # --force alone no longer confirms a reset
    assert await cli("reset", "--force", "--database", "wrong") == 1
    assert await cli("reset", "--force", "--database", database) == 0
    assert "Reset: applied 1 migration(s)." in capsys.readouterr().out
    conn = await asyncpg.connect(kit_url)
    assert await conn.fetchval("SELECT count(*) FROM users") == 0  # dropped and recreated
    await conn.close()


async def test_migrate_to_stops_early(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await cli("generate") == 0
    rewrite_schema(
        project,
        V1.replace("    name = text()", "    name = text()\n    age = integer()"),
        monkeypatch,
    )
    assert await cli("generate") == 0  # 0002 adds the age column

    assert await cli("migrate", "--to", "0001") == 0
    assert "age" not in await columns(kit_url, "users")
    assert await cli("migrate", "--to", "9999") == 1  # no such migration
    assert await cli("migrate", "--to", "0002") == 0
    assert "age" in await columns(kit_url, "users")


async def test_squash_collapses_history(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli("generate") == 0
    assert await cli("generate", "--custom", "--name", "extra") == 0
    assert len(list(project.glob("migrations/0[0-9][0-9][0-9]_*"))) == 2
    capsys.readouterr()

    assert await cli("squash", "--name", "baseline") == 0
    assert "Squashed 2 migration(s)" in capsys.readouterr().out
    assert (project / "migrations" / "0001_baseline").exists()
    assert len(list(project.glob("migrations/0[0-9][0-9][0-9]_*"))) == 1
    assert await cli("check") == 0
    assert await cli("generate", "--sql") == 0
    assert "No schema changes." in capsys.readouterr().out
    assert (
        "CREATE TABLE" in (project / "migrations" / "0001_baseline" / "migration.sql").read_text()
    )


async def test_seed_runs_sql_scripts(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    seeds = project / "seeds"
    seeds.mkdir()
    (seeds / "001_users.sql").write_text("INSERT INTO users (email, name) VALUES ('a@b.c', 'A');\n")
    (seeds / "002_more.sql").write_text("INSERT INTO users (email, name) VALUES ('c@d.e', 'C');\n")
    capsys.readouterr()

    assert await cli("seed") == 0
    out = capsys.readouterr().out
    assert "001_users.sql" in out and "002_more.sql" in out and "Ran 2 seed script(s)." in out
    conn = await asyncpg.connect(kit_url)
    assert await conn.fetchval("SELECT count(*) FROM users") == 2
    await conn.close()

    (seeds / "001_users.sql").unlink()
    (seeds / "002_more.sql").unlink()
    seeds.rmdir()
    capsys.readouterr()
    assert await cli("seed") == 0
    assert "No seed scripts" in capsys.readouterr().out


async def test_status(project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli("status") == 0
    assert "No migrations." in capsys.readouterr().out

    assert await cli("generate") == 0
    capsys.readouterr()
    assert await cli("status") == 0
    out = capsys.readouterr().out
    assert "0001_" in out and "pending" in out and "0 applied, 1 pending." in out
    conn = await asyncpg.connect(kit_url)
    created = await conn.fetchval("SELECT to_regclass('public.__mizzle_migrations')")
    await conn.close()
    assert created is None  # status only reads

    assert await cli("migrate") == 0
    capsys.readouterr()
    assert await cli("status") == 0
    out = capsys.readouterr().out
    assert "applied" in out and "1 applied, 0 pending." in out

    # An applied migration edited on disk is reported.
    sql = next(project.glob("migrations/0001_*/migration.sql"))
    sql.write_text(sql.read_text() + "\n-- edited\n")
    assert await cli("status") == 0
    assert "changed after it was applied" in capsys.readouterr().out


async def test_generate_sql_prints_without_writing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli("generate", "--sql") == 0
    out = capsys.readouterr().out
    assert "CREATE TYPE" in out and "CREATE TABLE" in out and out.rstrip().endswith(";")
    assert not (project / "migrations").exists()  # nothing written


async def test_migrate_dry_run(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli("generate") == 0
    capsys.readouterr()
    assert await cli("migrate", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "0001_" in out and "CREATE TABLE" in out and "nothing was applied" in out
    conn = await asyncpg.connect(kit_url)
    users = await conn.fetchval("SELECT to_regclass('public.users')")
    history = await conn.fetchval("SELECT to_regclass('public.__mizzle_migrations')")
    await conn.close()
    assert users is None and history is None  # dry-run touched nothing

    assert await cli("migrate") == 0
    assert await cli("migrate", "--dry-run") == 0
    assert "Database is up to date." in capsys.readouterr().out


async def test_status_json_and_check(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli("status", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "migrations": [],
        "applied": 0,
        "pending": 0,
        "changed": [],
        "missing": [],
    }
    assert await cli("status", "--check") == 0

    assert await cli("generate") == 0
    capsys.readouterr()
    assert await cli("status", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pending"] == 1
    assert data["migrations"][0]["state"] == "pending"
    assert await cli("status", "--check") == 1  # pending -> nonzero

    assert await cli("migrate") == 0
    capsys.readouterr()
    assert await cli("status", "--check") == 0
    capsys.readouterr()
    assert await cli("status", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] == 1 and data["pending"] == 0


async def test_info_and_doctor(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli("generate") == 0
    capsys.readouterr()
    assert await cli("doctor") == 1  # not applied yet
    out = capsys.readouterr().out
    assert "pending" in out and "PostgreSQL" in out and "drift:" in out

    assert await cli("migrate") == 0
    capsys.readouterr()
    assert await cli("info") == 0
    out = capsys.readouterr().out
    assert "0 pending" in out and "drift:       none" in out
    assert await cli("doctor") == 0

    # Without a URL it still describes the project, but doctor fails.
    monkeypatch.delenv("DATABASE_URL")
    assert await cli("info") == 0
    assert "set the DATABASE_URL" in capsys.readouterr().out
    assert await cli("doctor") == 1


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


async def test_pull_into_new_project_writes_the_schema_module(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli("push", "--force") == 0
    # A second project on the same database, with no schema file yet.
    fresh = project / "fresh"
    fresh.mkdir()
    module = f"fresh_{uuid.uuid4().hex[:8]}"
    (fresh / "mizzle.toml").write_text(f'schema = "{module}.db.schema"\nout = "migrations"\n')
    monkeypatch.chdir(fresh)
    assert await cli("pull") == 0
    schema = fresh / module / "db" / "schema.py"
    assert "class Users(Table" in schema.read_text()
    assert not (fresh / module / "db" / "pulled_schema.py").exists()
    assert await cli("generate") == 0
    assert "No schema changes." in capsys.readouterr().out
    # Pulling again keeps the schema module and writes next to it.
    assert await cli("pull") == 0
    assert (fresh / module / "db" / "pulled_schema.py").exists()


async def test_pull_baseline_creates_tables_on_an_empty_database(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(K7) The pulled baseline is only recorded where its tables exist."""
    assert await cli("push", "--force") == 0
    fresh = project / "fresh"
    fresh.mkdir()
    module = f"fresh_{uuid.uuid4().hex[:8]}"
    (fresh / "mizzle.toml").write_text(f'schema = "{module}"\nout = "migrations"\n')
    monkeypatch.chdir(fresh)
    assert await cli("pull") == 0
    assert await cli("migrate") == 0  # the source database: recorded, nothing run
    # A reset database: migrate creates the pulled tables.
    conn = await asyncpg.connect(kit_url)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    await conn.close()
    assert await cli("migrate") == 0
    assert await columns(kit_url, "users") == ["id", "email", "name", "role", "created_at"]


async def execute(url: str, sql: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _pull_fresh(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = project / "fresh"
    fresh.mkdir()
    module = f"fresh_{uuid.uuid4().hex[:8]}"
    (fresh / "mizzle.toml").write_text(f'schema = "{module}"\nout = "migrations"\n')
    monkeypatch.chdir(fresh)
    assert await cli("pull") == 0


async def test_pull_baseline_checks_every_object_kind(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # K4-1: views and enums were not checked; a DB missing them only recorded it.
    await execute(
        kit_url,
        "CREATE TYPE mood AS ENUM ('ok'); CREATE TABLE t (n integer);"
        " CREATE VIEW one AS SELECT 1 AS n",
    )
    await _pull_fresh(project, monkeypatch)
    # K4-18: dry-run on the source database shows the record, not the CREATE statements.
    capsys.readouterr()
    assert await cli("migrate", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "record baseline" in out and "CREATE TABLE" not in out
    await execute(kit_url, "DROP VIEW one")
    assert await cli("migrate") == 1
    assert "missing view" in capsys.readouterr().err
    await execute(kit_url, "DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    assert await cli("migrate") == 0  # nothing there: the baseline runs
    assert await fetchval(kit_url, "SELECT n FROM one") == 1


async def test_pull_baseline_without_tables_runs_on_an_empty_database(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # K4-1: a baseline with no tables, views or enums was recorded on any database.
    await execute(kit_url, "CREATE TYPE mood AS ENUM ('ok')")
    await _pull_fresh(project, monkeypatch)
    await execute(kit_url, "DROP TYPE mood")
    assert await cli("migrate") == 0
    assert await fetchval(kit_url, "SELECT 'ok'::mood::text") == "ok"


async def test_crlf_migration_applied_by_an_older_mizzle(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # K4-16: the stored hash is sha256 of the CRLF bytes; migrate and status accept it.
    assert await cli("generate") == 0
    sql = next(project.glob("migrations/0001_*/migration.sql"))
    sql.write_bytes(sql.read_bytes().replace(b"\n", b"\r\n"))
    assert await cli("migrate") == 0
    raw = hashlib.sha256(sql.read_bytes()).hexdigest()
    await execute(kit_url, f"UPDATE __mizzle_migrations SET hash = '{raw}'")
    assert await cli("migrate") == 0
    capsys.readouterr()
    assert await cli("status") == 0
    assert "changed" not in capsys.readouterr().out


async def test_non_migration_folders_are_ignored(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # K4-11: .cache or __pycache__ blocked every command; check called it unlisted.
    assert await cli("generate") == 0
    (project / "migrations" / ".cache").mkdir()
    (project / "migrations" / "__pycache__").mkdir()
    assert await cli("migrate") == 0
    assert await cli("check") == 0


async def test_pull_warns_about_what_it_skips(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """(K8) What mizzle does not manage is named: procedures, functions, triggers, standalone
    sequences, inheritance, partitions and grants."""
    conn = await asyncpg.connect(kit_url)
    await conn.execute(
        "CREATE PROCEDURE p_noop() LANGUAGE sql AS 'SELECT 1';"
        "CREATE SEQUENCE s_alone;"
        "CREATE TABLE base_t (id int); CREATE TABLE child_t () INHERITS (base_t);"
        "GRANT SELECT (id) ON base_t TO public;"
        "CREATE TABLE ev (id int, at date, PRIMARY KEY (id, at)) PARTITION BY RANGE (at);"
        "CREATE TABLE ev_2026 PARTITION OF ev FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');"
        "CREATE FUNCTION f_one() RETURNS int LANGUAGE sql AS 'SELECT 1';"
        "CREATE DOMAIN posint AS integer CHECK (VALUE > 0)"
    )
    await conn.close()
    assert await cli("pull") == 0
    err = capsys.readouterr().err
    assert "ev_2026_pkey" not in err, err  # its index is no partition (K4-10)
    for what in [
        "procedure p_noop()",
        "function f_one()",
        "domain posint",
        "sequence s_alone",
        "table inheritance child_t of base_t",
        "partition ev_2026 of ev",
        "grants on base_t",
    ]:
        assert f"warning: pull skips {what} (not managed by mizzle)" in err, err


async def test_pull_update_follows_a_schema_another_app_owns(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The owner app (this project) generates and applies migrations.
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    owner = project
    # A second service on the same database follows the schema; it never migrates.
    follower = project / "follower"
    follower.mkdir()
    module = f"follower_{uuid.uuid4().hex[:8]}"
    (follower / "mizzle.toml").write_text(f'schema = "{module}"\nout = "migrations"\n')
    monkeypatch.chdir(follower)
    assert await cli("pull", "--update") == 0  # first time: the schema module and a baseline
    assert len(list(follower.glob("migrations/0*"))) == 1
    assert await cli("pull", "--update") == 0
    assert "Up to date with the database." in capsys.readouterr().out

    # The owner adds a column and a table, and applies it.
    monkeypatch.chdir(owner)
    rewrite_schema(
        owner,
        V1.replace("    name = text()", "    name = text()\n    nickname = text()")
        + '\nclass Tags(Table, name="tags"):\n    id = serial().primary_key()\n',
        monkeypatch,
    )
    assert await cli("generate", "--name", "tags") == 0
    assert await cli("migrate") == 0

    # The follower pulls the change: the module is rewritten, the migration is a record.
    monkeypatch.chdir(follower)
    assert await cli("pull", "--update", "--name", "tags") == 0
    recorded = follower / "migrations" / "0002_tags" / "migration.sql"
    sql = recorded.read_text()
    assert '-- ALTER TABLE "users" ADD COLUMN "nickname" text' in sql
    assert '-- CREATE TABLE "tags"' in sql
    assert all(line.startswith("--") for line in sql.splitlines() if line.strip())
    text = (follower / f"{module}.py").read_text()
    assert "class Tags(Table" in text and "nickname = text()" in text
    for name in [m for m in sys.modules if m.startswith(module)]:
        monkeypatch.delitem(sys.modules, name)  # the CLI is a new process in real use
    capsys.readouterr()
    assert await cli("generate") == 0
    assert "No schema changes." in capsys.readouterr().out
    assert await cli("check") == 0
    assert await cli("pull", "--update") == 0
    assert "Up to date with the database." in capsys.readouterr().out
    # A recorded migration runs nothing.
    directory = MigrationsDir(follower / "migrations", "postgresql")
    assert directory.migrations()[-1].statements() == []


async def test_pull_columns_named_like_types_and_helpers(
    tmp_path: Path,
    kit_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Columns named like a constructor the class body calls (text(), sql(...)) must not
    # shadow it: "text = text()" then "note = text()" would call a Column.
    conn = await asyncpg.connect(kit_url)
    await conn.execute(
        """
        CREATE TYPE "Mood" AS ENUM ('ok', 'bad');
        CREATE TABLE clock (
            id serial PRIMARY KEY, text text NOT NULL, note text, date date, time time,
            integer integer, sql text, one text, "Mood" "Mood", mood "Mood"
        );
        """
    )
    await conn.close()
    module = f"pulled_{uuid.uuid4().hex[:8]}"
    (tmp_path / "mizzle.toml").write_text(f'schema = "{module}"\nout = "migrations"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", kit_url)
    assert await cli("pull") == 0
    source = (tmp_path / f"{module}.py").read_text()
    assert "    text_ = text(name='text').not_null()" in source
    assert "    note = text()" in source
    mod = importlib.import_module(module)  # the class body runs
    assert mod.Clock.text_.name == "text" and mod.Clock.Mood_.name == "Mood"
    capsys.readouterr()
    assert await cli("generate") == 0
    assert "No schema changes." in capsys.readouterr().out
    checked = subprocess.run(
        [sys.executable, "-m", "basedpyright", str(tmp_path / f"{module}.py")],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stdout


async def test_pull_nasty_names_round_trip(
    tmp_path: Path,
    kit_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every pulled Python name is a valid, unique, unmangled identifier that keeps its SQL
    name (K4-5, K4-6, K4-8, K4-12, K4-14, K4-15)."""
    conn = await asyncpg.connect(kit_url)
    await conn.execute(
        """
        CREATE SCHEMA s;
        CREATE TYPE "Mood" AS ENUM ('happy');
        CREATE TYPE odd AS ENUM ('a', 'A', 'a b', 'a_b', '__x', '_a_', 'None', 'ß', 'SS');
        CREATE TYPE "table" AS ENUM ('x');
        CREATE DOMAIN d AS int; CREATE DOMAIN s.d AS int;
        CREATE TYPE c AS (x int); CREATE TYPE s.c AS (x int);
        CREATE TABLE t (
            id int PRIMARY KEY, "__x" int, "__y__" int, "a-b" int, a_b int, "class" int,
            class_ int, kind "table" DEFAULT 'x', o odd DEFAULT 'A', o2 odd DEFAULT 'a_b',
            dd d, sd s.d, cc c, sc s.c, "query" int, "override" int
        );
        CREATE TABLE s.t (id int PRIMARY KEY);
        CREATE FUNCTION tf() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END';
        CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION tf();
        CREATE TRIGGER tr BEFORE INSERT ON s.t FOR EACH ROW EXECUTE FUNCTION tf();
        """
    )
    await conn.close()
    module = f"pulled_{uuid.uuid4().hex[:8]}"
    (tmp_path / "mizzle.toml").write_text(
        f'schema = "{module}"\nout = "migrations"\nschema_filter = ["public", "s"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", kit_url)
    assert await cli("pull") == 0
    source = (tmp_path / f"{module}.py").read_text()
    assert "    __" not in source  # no mangled class attributes (K4-14)
    mod = importlib.import_module(module)
    odd = [m.value for m in mod.Odd]
    assert odd == ["a", "A", "a b", "a_b", "__x", "_a_", "None", "ß", "SS"]  # (K4-6)
    names = sorted(c.db_name(None) for c in mod.T.__table__.columns.values())
    assert len(names) == 16 and "__x" in names and "a-b" in names  # (K4-14, K4-15)
    capsys.readouterr()
    assert await cli("generate") == 0
    out = capsys.readouterr().out
    assert "No schema changes." in out, out


OBJECTS = (
    V1.replace(
        "from mizzle import Table, sql, index",
        "from typing import override\n"
        "from mizzle import Table, sql, index, eq, View, QueryBuilder, policy, rls",
    ).replace(
        'index("posts_author_idx").on(cls.author_id),',
        'index("posts_author_idx").on(cls.author_id), rls(), '
        'policy("visible", for_="select", using=sql("author_id > 0")),',
    )
    + """
class Admins(View, name="admins"):
    id = integer()
    email = varchar(255)

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return db.select(Users.id, Users.email).from_(Users).where(eq(Users.role, Role.ADMIN))
"""
)


async def test_objects_generate_migrate_push_pull(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rewrite_schema(project, OBJECTS, monkeypatch)
    assert await cli("generate") == 0
    sql = next(project.glob("migrations/0001_*/migration.sql")).read_text()
    assert sql.index("CREATE TABLE") < sql.index("CREATE VIEW")
    assert await cli("migrate") == 0
    assert await cli("check") == 0
    assert await cli("generate") == 0
    assert await cli("push", "--dry-run") == 0  # the database's view text compares equal
    out = capsys.readouterr().out
    assert "No schema changes." in out and "Database is up to date." in out

    # Change the view query; drop the unapplied migration once.
    evolved = OBJECTS.replace("Role.ADMIN", "Role.USER")
    rewrite_schema(project, evolved, monkeypatch)
    assert await cli("generate") == 0
    sql = next(project.glob("migrations/0002_*/migration.sql")).read_text()
    assert 'DROP VIEW "admins"' in sql
    assert await cli("drop") == 0
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    assert await cli("push", "--dry-run") == 0
    assert "Database is up to date." in capsys.readouterr().out

    # Pull into a fresh project: its schema builds the snapshot of the database.
    fresh = project / "fresh"
    fresh.mkdir()
    module = f"fresh_{uuid.uuid4().hex[:8]}"
    (fresh / "mizzle.toml").write_text(f'schema = "{module}"\nout = "migrations"\n')
    monkeypatch.chdir(fresh)
    assert await cli("pull") == 0
    pulled = (fresh / f"{module}.py").read_text()
    assert "class Admins(View" in pulled and "policy(" in pulled
    assert await cli("generate") == 0
    assert await cli("push", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "No schema changes." in out and "Database is up to date." in out


async def test_reset_reruns_when_history_lives_in_another_schema(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # K1: the history schema is not in schema_filter, so a schema drop never cleared it.
    toml = project / "mizzle.toml"
    toml.write_text(toml.read_text() + 'migrations_schema = "mizzle_meta"\n')
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    capsys.readouterr()

    database = kit_url.rsplit("/", 1)[-1]
    assert await cli("reset", "--force", "--database", database) == 0
    assert "Reset: applied 1 migration(s)." in capsys.readouterr().out
    conn = await asyncpg.connect(kit_url)
    assert await conn.fetchval("SELECT count(*) FROM users") == 0  # really re-ran
    await conn.close()


async def test_reset_keeps_unmanaged_objects(project: Path, kit_url: str) -> None:
    # K3: reset drops what mizzle manages, never an unmanaged table in public.
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    conn = await asyncpg.connect(kit_url)
    await conn.execute("CREATE TABLE public.keepme (id int)")
    await conn.execute("INSERT INTO public.users (email) VALUES ('a@b.c')")
    await conn.close()

    database = kit_url.rsplit("/", 1)[-1]
    assert await cli("reset", "--force", "--database", database) == 0
    conn = await asyncpg.connect(kit_url)
    assert await conn.fetchval("SELECT to_regclass('public.keepme')") is not None
    assert await conn.fetchval("SELECT count(*) FROM users") == 0
    await conn.close()


async def test_squash_refuses_to_lose_hand_written_sql(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # K6: collapsing history must not silently delete a custom migration's SQL.
    assert await cli("generate") == 0
    assert await cli("generate", "--custom", "--name", "seed") == 0
    custom = next(project.glob("migrations/0002_seed/migration.sql"))
    custom.write_text(custom.read_text() + "INSERT INTO users (email) VALUES ('a@b.c');\n")
    capsys.readouterr()
    assert await cli("squash", "--name", "baseline") == 1
    assert "hand-written SQL" in capsys.readouterr().err
    assert len(list(project.glob("migrations/0[0-9][0-9][0-9]_*"))) == 2  # untouched


async def test_squash_is_adopted_by_a_database_that_applied_the_old_history(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # K7: after a squash, an existing database must not fail "missing on disk".
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    old_name = MigrationsDir(project / "migrations", "postgresql").migrations()[0].name
    capsys.readouterr()

    assert await cli("squash", "--name", "baseline") == 0
    assert "0001_baseline" in next(iter(project.glob("migrations/0001_*"))).name

    assert await cli("migrate") == 0  # adopted, nothing re-run
    assert "Database is up to date." in capsys.readouterr().out
    assert await cli("status", "--check") == 0
    assert await cli("doctor") == 0
    conn = await asyncpg.connect(kit_url)
    assert await conn.fetchval("SELECT count(*) FROM users") == 0
    names = await conn.fetch("SELECT name FROM public.__mizzle_migrations ORDER BY id")
    await conn.close()
    assert old_name not in [r["name"] for r in names]
    assert "0001_baseline" in [r["name"] for r in names]


async def test_check_fix_refuses_an_ambiguous_branch(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # K8: two folders with the same number are ambiguous; never swap one journal entry.
    assert await cli("generate") == 0
    (project / "migrations" / "0001_other_branch").mkdir()
    capsys.readouterr()
    assert await cli("check", "--fix") == 1
    assert "share number" in capsys.readouterr().err


async def test_check_fix_reports_a_missing_file(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # K8: a deleted migration.sql must be reported, not silently dropped from the journal.
    assert await cli("generate") == 0
    sql = next(project.glob("migrations/0001_*/migration.sql"))
    sql.unlink()
    capsys.readouterr()
    assert await cli("check", "--fix") == 1
    assert "left as is" in capsys.readouterr().out
    assert len(MigrationsDir(project / "migrations", "postgresql").migrations()) == 1


async def test_migrate_dry_run_reports_tampering(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # K9: --dry-run makes the same checks a real migrate makes.
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    sql = next(project.glob("migrations/0001_*/migration.sql"))
    sql.write_text(sql.read_text() + "\n-- edited\n")
    capsys.readouterr()
    assert await cli("migrate", "--dry-run") == 1
    assert "changed after it was applied" in capsys.readouterr().err


async def test_status_check_and_doctor_fail_when_a_migration_changed(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # K11: an edited applied migration is a failure, not just a note.
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    sql = next(project.glob("migrations/0001_*/migration.sql"))
    sql.write_text(sql.read_text() + "\n-- edited\n")
    capsys.readouterr()
    assert await cli("status", "--check") == 1
    capsys.readouterr()
    # K13: an edited migration is still applied, and the JSON names it.
    assert await cli("status", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] == 1 and data["pending"] == 0
    assert data["changed"] == [sql.parent.name]
    assert await cli("doctor") == 1
    assert "edited after they were applied" in capsys.readouterr().out


async def test_to_requires_an_exact_name_or_number(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # K12: a typo or a loose prefix must not pick a neighbouring migration.
    assert await cli("generate") == 0
    rewrite_schema(
        project,
        V1.replace("    name = text()", "    name = text()\n    age = integer()"),
        monkeypatch,
    )
    assert await cli("generate") == 0

    directory = MigrationsDir(project / "migrations", "postgresql")
    first = directory.migrations()[0].name
    assert await cli("migrate", "--to", first) == 0
    assert await cli("migrate", "--to", "000") == 1  # no such number
    assert await cli("migrate", "--to", "0001_age") == 1  # no such exact name
    assert "age" not in await columns(kit_url, "users")

    assert await cli("migrate") == 0
    capsys.readouterr()
    assert await cli("migrate", "--to", "0001") == 0  # already passed: says so
    assert "already applied" in capsys.readouterr().out


async def test_generate_sql_custom_writes_nothing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # K13: --sql never writes, not even the folder a --custom migration would make.
    assert await cli("generate", "--custom", "--sql") == 0
    assert "Custom migration" in capsys.readouterr().out
    assert not (project / "migrations").exists()


async def test_malformed_url_and_missing_template_are_clean_errors(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # K14: no traceback for a bad URL or a --template file that is not there.
    assert await cli("generate") == 0
    monkeypatch.setenv("DATABASE_URL", kit_url + "?sslmode=bogus")
    capsys.readouterr()
    assert await cli("info") == 1
    assert "error: the database URL is malformed" in capsys.readouterr().err

    monkeypatch.setenv("DATABASE_URL", kit_url)
    capsys.readouterr()
    assert await cli("generate", "--custom", "--template", "/no/such/file.sql") == 1
    assert "cannot read --template" in capsys.readouterr().err


async def test_doctor_does_not_report_role_drift(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # K10: info/doctor must read the role options, or every role looks like drift.
    admin = f"mizzle_kit_{uuid.uuid4().hex[:8]}"
    source = f'''
from mizzle import Constraint, Table, policy, rls, role, sql
from mizzle.pg import serial, text

ADMIN = role("{admin}")


class Users(Table, name="users"):
    id = serial().primary_key()
    email = text().not_null()

    @classmethod
    def config(cls) -> list[Constraint]:
        return [rls(), policy("users_admins", to=[ADMIN], using=sql("true"))]
'''
    rewrite_schema(project, source, monkeypatch)
    try:
        assert await cli("generate") == 0
        capsys.readouterr()
        assert await cli("doctor") == 1  # not applied yet
        assert await cli("migrate") == 0
        capsys.readouterr()
        assert await cli("doctor") == 0
        assert "drift:       none" in capsys.readouterr().out
    finally:
        conn = await asyncpg.connect(kit_url)
        await conn.execute(f'DROP OWNED BY "{admin}"')
        await conn.execute(f'DROP ROLE IF EXISTS "{admin}"')
        await conn.close()


# -- BUGS.md: reset, squash, check --fix, migrate --to ----------------------------------


@pytest.fixture
async def other_url(pg_url: str) -> AsyncGenerator[str]:
    name = f"kit_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg_url)
    await admin.execute(f'CREATE DATABASE "{name}"')
    base, _, _ = pg_url.rpartition("/")
    yield f"{base}/{name}"
    await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    await admin.close()


async def fetchval(url: str, query: str) -> object:
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetchval(query)
    finally:
        await conn.close()


WITH_AGE = V1.replace("    name = text()", "    name = text()\n    age = integer()")
WITH_BIO = WITH_AGE.replace("    age = integer()", "    age = integer()\n    bio = text()")


async def test_reset_prompt_requires_the_database_name(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # K4: the prompt makes you type the database name; B5: no --force and no TTY refuses.
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    await fetchval(kit_url, "INSERT INTO users (email) VALUES ('a@b.c')")
    database = kit_url.rsplit("/", 1)[-1]
    assert await cli("reset", "--database", database) == 1  # no TTY to ask on
    assert await fetchval(kit_url, "SELECT count(*) FROM users") == 1

    monkeypatch.setattr(kit_cli, "_interactive", lambda: True)
    answers = iter(["y", database])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert await cli("reset") == 1  # "y" is not the database name
    assert await fetchval(kit_url, "SELECT count(*) FROM users") == 1
    assert await cli("reset") == 0
    assert await fetchval(kit_url, "SELECT count(*) FROM users") == 0


async def test_reset_with_a_malformed_url_is_a_clean_error(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B31: no traceback, and the password is never printed.
    assert await cli("generate") == 0
    for url in ("postgresql://u:secretpw@localhost:badport/db", "postgresql://u:secretpw@[::1/db"):
        monkeypatch.setenv("DATABASE_URL", url)
        capsys.readouterr()
        assert await cli("reset", "--force", "--database", "db") == 1
        err = capsys.readouterr().err
        assert err.startswith("error:") and "secretpw" not in err


async def test_reset_failure_leaves_the_database_untouched(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # B26: an unmanaged view on a managed table stops the drops; nothing may be dropped.
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    await fetchval(kit_url, "CREATE VIEW zv AS SELECT * FROM users")
    database = kit_url.rsplit("/", 1)[-1]
    capsys.readouterr()
    assert await cli("reset", "--force", "--database", database) == 1
    assert "rolled back" in capsys.readouterr().err
    assert await fetchval(kit_url, "SELECT to_regclass('public.posts')::text") == "posts"
    assert await fetchval(kit_url, "SELECT count(*) FROM public.__mizzle_migrations") == 1


async def test_reset_with_custom_sql_is_never_half_done(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B25: a custom migration creates an object reset does not manage.
    assert await cli("generate") == 0
    assert await cli("generate", "--custom", "--name", "extra") == 0
    custom = next(project.glob("migrations/0002_extra/migration.sql"))
    custom.write_text(custom.read_text() + "CREATE TABLE public.custom_made (id int);\n")
    assert await cli("migrate") == 0
    await fetchval(kit_url, "INSERT INTO users (email) VALUES ('a@b.c')")
    database = kit_url.rsplit("/", 1)[-1]
    capsys.readouterr()
    assert await cli("reset", "--force", "--database", database) == 1
    err = capsys.readouterr().err
    assert "rolled back" in err and "0002_extra" in err
    assert await fetchval(kit_url, "SELECT count(*) FROM users") == 1
    assert await fetchval(kit_url, "SELECT count(*) FROM public.__mizzle_migrations") == 2

    # A new enum value cannot run in one transaction: refuse before dropping anything.
    rewrite_schema(
        project, V1.replace('USER = "user"', 'USER = "user"\n    GUEST = "guest"'), monkeypatch
    )
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    capsys.readouterr()
    assert await cli("reset", "--force", "--database", database) == 1
    assert "0002_extra" in capsys.readouterr().err
    assert await fetchval(kit_url, "SELECT count(*) FROM users") == 1


async def test_squash_refuses_sql_edited_after_generate(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # B27: SQL added to a generated migration would be lost; check --fix must not hide it.
    assert await cli("generate") == 0
    sql = next(project.glob("migrations/0001_*/migration.sql"))
    sql.write_text(sql.read_text() + "CREATE INDEX users_email_custom ON users (email);\n")
    capsys.readouterr()
    assert await cli("squash") == 1
    assert "hand-written SQL" in capsys.readouterr().err
    assert await cli("check", "--fix") == 0
    out = capsys.readouterr().out
    assert "hash refreshed" not in out and "edited after generate" in out
    assert await cli("squash") == 1
    assert "users_email_custom" in sql.read_text()


async def test_a_second_squash_is_adopted(
    project: Path,
    kit_url: str,
    other_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # B28: a unique name, and supersedes carries the first squash's own list.
    assert await cli("generate") == 0
    rewrite_schema(project, WITH_AGE, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    monkeypatch.setenv("DATABASE_URL", other_url)
    assert await cli("migrate") == 0  # other_url stays on the original history
    monkeypatch.setenv("DATABASE_URL", kit_url)

    assert await cli("squash") == 0
    assert await cli("migrate") == 0  # kit_url adopts the first squash
    rewrite_schema(project, WITH_BIO, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    first = MigrationsDir(project / "migrations", "postgresql").migrations()[0].name

    capsys.readouterr()
    assert await cli("squash") == 0
    [second] = MigrationsDir(project / "migrations", "postgresql").migrations()
    assert second.name != first
    assert first in second.supersedes and len(second.supersedes) == 4
    assert await cli("migrate", "--dry-run") == 0  # adopted: nothing to run
    assert "Database is up to date." in capsys.readouterr().out
    assert await cli("migrate") == 0
    assert "Database is up to date." in capsys.readouterr().out
    assert await cli("status", "--check") == 0

    # other_url never applied the bio migration: it cannot adopt, and says why.
    monkeypatch.setenv("DATABASE_URL", other_url)
    assert await cli("migrate") == 1
    assert "only part of the history" in capsys.readouterr().err

    # A history that is one squash already has nothing to squash.
    assert await cli("squash") == 0
    assert "Nothing to squash" in capsys.readouterr().out
    assert [m.name for m in MigrationsDir(project / "migrations", "postgresql").migrations()] == [
        second.name
    ]


async def test_squash_of_a_partly_migrated_database(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B29: a database at 0001 of 2 cannot adopt the squash: warn, then refuse clearly.
    assert await cli("generate") == 0
    rewrite_schema(project, WITH_AGE, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate", "--to", "1") == 0
    capsys.readouterr()
    # B63: squash refuses instead of warning and squashing anyway.
    assert await cli("squash") == 1
    assert "only part of the history" in capsys.readouterr().err
    assert len(MigrationsDir(project / "migrations", "postgresql").migrations()) == 2
    monkeypatch.delenv("DATABASE_URL")  # squashed where the database cannot be checked
    assert await cli("squash") == 0
    monkeypatch.setenv("DATABASE_URL", kit_url)
    capsys.readouterr()
    for args in (("migrate", "--dry-run"), ("migrate",), ("status",)):
        assert await cli(*args) == 1
        assert "only part of the history" in capsys.readouterr().err
    # B62: info and doctor report it too.
    assert await cli("doctor") == 1
    assert "squash:" in capsys.readouterr().out


async def test_migrate_to_the_last_applied_migration_says_so(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B5: `--to` naming the last applied migration (a later one pending) is not "up to date".
    assert await cli("generate") == 0
    rewrite_schema(project, WITH_AGE, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate", "--to", "1") == 0
    capsys.readouterr()
    assert await cli("migrate", "--to", "1") == 0
    out = capsys.readouterr().out
    assert "already applied" in out and "up to date" not in out


def _branch_migrations(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """0001, then two branches that each generated 0002 on top of it: 0002_alpha is in the
    journal, the other branch's folder is copied in as 0003_beta."""
    migrations = project / "migrations"
    alpha = "\nclass Alpha(Table, name='alpha'):\n    id = serial().primary_key()\n"
    beta = "\nclass Beta(Table, name='beta'):\n    id = serial().primary_key()\n"
    run("generate")
    rewrite_schema(project, V1 + beta, monkeypatch)
    run("generate", "--name", "beta")
    stash = project / "beta_branch"
    shutil.copytree(migrations / "0002_beta", stash)
    MigrationsDir(migrations, "postgresql").drop_latest()
    rewrite_schema(project, V1 + alpha, monkeypatch)
    run("generate", "--name", "alpha")
    shutil.copytree(stash, migrations / "0003_beta")
    rewrite_schema(project, V1 + alpha + beta, monkeypatch)
    return migrations / "0002_alpha", migrations / "0003_beta"


async def test_check_fix_refuses_to_relink_a_stale_branch(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B32 / B5: re-linking 0003_beta would hide that its snapshot lacks alpha.
    _, beta = await asyncio.to_thread(_branch_migrations, project, monkeypatch)
    before = (beta / "snapshot.json").read_text()
    capsys.readouterr()
    assert await cli("check", "--fix") == 1
    assert "generate" in capsys.readouterr().err
    assert (beta / "snapshot.json").read_text() == before
    assert await cli("check") == 1


async def test_check_fix_relinks_when_the_parent_has_the_same_schema(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A branch generated on 0001 while 0002 (custom) changed no schema: safe to re-link.
    assert await cli("generate") == 0
    assert await cli("generate", "--custom", "--name", "noop") == 0
    rewrite_schema(project, WITH_AGE, monkeypatch)
    assert await cli("generate") == 0
    directory = MigrationsDir(project / "migrations", "postgresql")
    first, _, third = directory.migrations()
    data = third.snapshot()
    data["prev_id"] = first.snapshot()["id"]
    third.snapshot_path.write_text(json.dumps(data, indent=2) + "\n")
    assert await cli("check") == 1
    capsys.readouterr()
    assert await cli("check", "--fix") == 0
    assert "prev_id re-linked" in capsys.readouterr().out
    assert await cli("check") == 0


async def test_reset_with_a_new_enum_value(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Adding an enum value cannot run in a transaction: reset re-runs the history after the
    # drops (no hand-written SQL, so nothing can fail on objects reset did not drop).
    assert await cli("generate") == 0
    rewrite_schema(
        project, V1.replace('USER = "user"', 'USER = "user"\n    GUEST = "guest"'), monkeypatch
    )
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    await fetchval(kit_url, "INSERT INTO users (email, role) VALUES ('a@b.c', 'guest')")
    database = kit_url.rsplit("/", 1)[-1]
    assert await cli("reset", "--force", "--database", database) == 0
    assert await fetchval(kit_url, "SELECT count(*) FROM users") == 0


async def test_squash_without_a_reachable_database(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The partial-history warning is best effort: squash itself needs no database.
    assert await cli("generate") == 0
    monkeypatch.setenv("DATABASE_URL", kit_url + "?sslmode=bogus")
    assert await cli("squash") == 0


# -- round 2: reset, squash, check --fix ------------------------------------------------

TMP = "\nclass Tmp(Table, name='tmp_t'):\n    id = serial().primary_key()\n"
GUEST = V1.replace('USER = "user"', 'USER = "user"\n    GUEST = "guest"')


async def test_reset_after_a_non_transactional_step_says_it_is_partly_done(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B51: a failure after the enum value migration left the database half reset, silently.
    assert await cli("generate") == 0
    rewrite_schema(project, GUEST, monkeypatch)
    assert await cli("generate") == 0  # 0002: ADD VALUE, not in a transaction
    rewrite_schema(project, GUEST + TMP, monkeypatch)
    assert await cli("generate") == 0  # 0003 creates tmp_t
    rewrite_schema(project, GUEST, monkeypatch)
    assert await cli("generate", "--allow-data-loss") == 0  # 0004 drops it
    assert await cli("migrate") == 0
    await fetchval(kit_url, "CREATE TABLE tmp_t (x int)")  # unmanaged, in 0003's way
    database = kit_url.rsplit("/", 1)[-1]
    capsys.readouterr()
    assert await cli("reset", "--force", "--database", database) == 1
    out, err = capsys.readouterr()
    assert "cannot run in a transaction" in out
    assert "partly reset" in err and "re-ran 2 migration" in err and "0003" in err
    assert "rolled back" not in err.split("partly")[0]


async def test_reset_refuses_transaction_control_in_a_migration(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # B52: COMMIT in a custom migration ended reset's transaction; reset then claimed the
    # database was unchanged.
    assert await cli("generate") == 0
    assert await cli("generate", "--custom", "--name", "tx") == 0
    custom = next(project.glob("migrations/0002_tx/migration.sql"))
    header = custom.read_text()
    custom.write_text(header + "BEGIN;\nINSERT INTO users (email) VALUES ('x@y.z');\nCOMMIT;\n")
    # C28: migrate refuses it too (it would commit part of the migration).
    capsys.readouterr()
    assert await cli("migrate") == 1
    assert "0002_tx contains BEGIN" in capsys.readouterr().err
    # A history an older version applied: reset still refuses it.
    custom.write_text(header + "INSERT INTO users (email) VALUES ('x@y.z');\n")
    assert await cli("migrate") == 0
    custom.write_text(header + "BEGIN;\nINSERT INTO users (email) VALUES ('x@y.z');\nCOMMIT;\n")
    database = kit_url.rsplit("/", 1)[-1]
    capsys.readouterr()
    assert await cli("reset", "--force", "--database", database) == 1
    assert "0002_tx contains BEGIN" in capsys.readouterr().err
    assert await fetchval(kit_url, "SELECT count(*) FROM users") == 1


async def test_reset_needs_a_database_in_the_url(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B56: with no path the confirmation compared with '' while ?dbname= picked the target.
    assert await cli("generate") == 0
    base, _, database = kit_url.rpartition("/")
    monkeypatch.setenv("DATABASE_URL", f"{base}?dbname={database}")
    capsys.readouterr()
    assert await cli("reset", "--force", "--database", "") == 1
    assert "name the database" in capsys.readouterr().err


async def test_ctrl_d_at_the_reset_prompt_cancels(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B60: EOFError escaped as a traceback.
    assert await cli("generate") == 0

    def eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(kit_cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", eof)
    assert await cli("reset") == 1
    assert "Cancelled" in capsys.readouterr().out


async def test_check_fix_keeps_a_readded_custom_migration_custom(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # B53: re-adding a folder recorded it as generated, so squash deleted its SQL.
    assert await cli("generate") == 0
    assert await cli("generate", "--custom", "--name", "hand") == 0
    custom = next(project.glob("migrations/0002_hand/migration.sql"))
    custom.write_text(custom.read_text() + "SELECT 1;\n")
    journal = project / "migrations" / "meta" / "journal.json"
    data = json.loads(journal.read_text())
    data["entries"] = data["entries"][:1]  # a merge conflict resolved by dropping it
    journal.write_text(json.dumps(data))
    capsys.readouterr()
    assert await cli("check", "--fix") == 0
    assert "0002_hand: added to the journal as a custom migration" in capsys.readouterr().out
    assert await cli("squash") == 1
    assert "hand-written SQL in 0002_hand" in capsys.readouterr().err
    assert "SELECT 1;" in custom.read_text()


async def test_squash_refuses_a_folder_the_journal_does_not_list(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B54: squash deleted every folder, including a merged branch's unlisted one.
    assert await cli("generate") == 0
    rewrite_schema(project, WITH_AGE, monkeypatch)
    assert await cli("generate") == 0
    stray = project / "migrations" / "0003_backfill"
    stray.mkdir()
    (stray / "migration.sql").write_text("UPDATE users SET name = 'x';\n")
    capsys.readouterr()
    assert await cli("squash") == 1
    assert "0003_backfill is not in the journal" in capsys.readouterr().err
    assert (stray / "migration.sql").exists()


async def test_check_fix_names_a_missing_snapshot(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # B58: the advice was to delete the next migration, which may be applied.
    assert await cli("generate") == 0
    rewrite_schema(project, WITH_AGE, monkeypatch)
    assert await cli("generate") == 0
    rewrite_schema(project, WITH_BIO, monkeypatch)
    assert await cli("generate") == 0
    next(project.glob("migrations/0002_*/snapshot.json")).unlink()
    capsys.readouterr()
    assert await cli("check", "--fix") == 1
    err = capsys.readouterr().err
    assert "snapshot.json is missing in 0002_" in err and "Delete" not in err


async def test_a_missing_migration_file_is_a_clean_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # B61: FileNotFoundError tracebacks in squash, reset, migrate and status.
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    next(project.glob("migrations/0001_*/migration.sql")).unlink()
    for args in (("status",), ("migrate",), ("squash",)):
        capsys.readouterr()
        assert await cli(*args) == 1
        assert "migration.sql is missing" in capsys.readouterr().err


async def test_drop_refuses_an_adopted_squash(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # C21: drop deleted a squash the database had adopted: the only copy of the history.
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    assert await cli("squash") == 0
    capsys.readouterr()
    assert await cli("drop") == 1
    assert "already applied" in capsys.readouterr().err
    assert len(MigrationsDir(project / "migrations", "postgresql").migrations()) == 1


async def test_a_column_can_change_from_one_enum_to_another(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C23: USING "role"::user_role failed: there is no cast between two enum types.
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    await fetchval(kit_url, "INSERT INTO users (email, role) VALUES ('a@b.c', 'admin')")
    rewrite_schema(
        project, V1.replace("pg_enum(Role)", 'pg_enum(Role, type_name="user_role")'), monkeypatch
    )
    assert await cli("generate", "--allow-data-loss") == 0
    assert await cli("migrate") == 0
    assert await fetchval(kit_url, "SELECT role::text FROM users") == "admin"
    assert await fetchval(kit_url, "SELECT pg_typeof(role)::text FROM users") == "user_role"


async def test_broken_migration_files_are_clean_errors(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # C27: merge-conflict markers in journal.json or a bad mizzle.toml gave a traceback.
    assert await cli("generate") == 0
    journal = project / "migrations" / "meta" / "journal.json"
    text = journal.read_text()
    journal.write_text("<<<<<<< HEAD\n" + text)
    capsys.readouterr()
    assert await cli("status") == 1
    assert "is not valid JSON" in capsys.readouterr().err
    journal.write_text(text)
    (project / "mizzle.toml").write_text('schema = "x\n')
    assert await cli("status") == 1
    assert "is not valid TOML" in capsys.readouterr().err


async def test_status_shows_applied_migrations_missing_on_disk(
    project: Path, kit_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # C29: with an empty migrations folder status said only "No migrations.".
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    name = MigrationsDir(project / "migrations", "postgresql").migrations()[0].name
    shutil.rmtree(project / "migrations")
    capsys.readouterr()
    assert await cli("status", "--check") == 1
    assert f"{name}  applied but missing on disk" in capsys.readouterr().out


TWO_SCHEMAS = """
from mizzle import Table
from mizzle.pg import serial, text

class T(Table, name="t", schema="app"):
    id = serial().primary_key()
    note = text()
"""


async def test_a_rename_across_schemas_is_a_move_not_a_drop(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # C20: --rename app.t=arch.t2 was ignored (DROP + CREATE); a --rename that matched
    # nothing was accepted; a cross-schema drop + create was never asked about.
    rewrite_schema(project, TWO_SCHEMAS, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    await fetchval(kit_url, "INSERT INTO app.t (note) VALUES ('kept')")
    moved = TWO_SCHEMAS.replace('name="t", schema="app"', 'name="t2", schema="arch"').replace(
        "note = text()", "note = text()\n    extra = text()"
    )
    rewrite_schema(project, moved, monkeypatch)
    capsys.readouterr()
    assert await cli("generate") == 1  # in CI: asks for a --rename
    assert "--rename" in capsys.readouterr().err
    assert await cli("generate", "--rename", "app.t=nonexistent.zzz") == 1
    assert await cli("generate", "--rename", "app.typo=arch.t2") == 1
    assert await cli("generate", "--rename", "app.t=arch.t2") == 0
    assert await cli("migrate") == 0
    assert await fetchval(kit_url, "SELECT note FROM arch.t2") == "kept"
    capsys.readouterr()
    assert await cli("generate", "--rename", "app.gone=arch.x") == 1
    assert "matched no" in capsys.readouterr().err


EXPRESSIONS = """
from mizzle import Table, check, index, sql
from mizzle.pg import serial, integer, text, double_precision, interval, timestamp

class Item(Table, name="items"):
    id = serial().primary_key()
    qty = integer().not_null().default(0)
    ratio = double_precision().default(1e-7)
    wait = interval().default(sql("'1 day 5 seconds'::interval"))
    label = text().default(sql("('a' || 'b')"))
    due = timestamp(tz=True).default(sql("now() + interval '1 day'"))
    twice = integer().generated_always_as(sql("qty * 2"))

    @classmethod
    def config(cls):
        return [
            check("items_qty", sql("qty >= 0")),
            index("items_big").on(cls.qty).where(sql("qty > 10")),
        ]
"""


async def test_doctor_and_push_compare_expressions(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # C24: right after migrate, doctor reported drift for defaults it could not match, and
    # push repeated them; a changed CHECK, index WHERE or generated expression was missed.
    rewrite_schema(project, EXPRESSIONS, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    capsys.readouterr()
    assert await cli("doctor") == 0, capsys.readouterr().out
    assert await cli("push", "--dry-run") == 0
    assert "up to date" in capsys.readouterr().out

    for change, undo in (
        (
            "ALTER TABLE items DROP CONSTRAINT items_qty, ADD CONSTRAINT items_qty "
            "CHECK (qty >= 5)",
            "ALTER TABLE items DROP CONSTRAINT items_qty, ADD CONSTRAINT items_qty "
            "CHECK (qty >= 0)",
        ),
        (
            "DROP INDEX items_big; CREATE INDEX items_big ON items (qty) WHERE qty > 99",
            "DROP INDEX items_big; CREATE INDEX items_big ON items (qty) WHERE qty > 10",
        ),
        (
            "ALTER TABLE items DROP COLUMN twice, "
            "ADD COLUMN twice integer GENERATED ALWAYS AS (qty * 3) STORED",
            "ALTER TABLE items DROP COLUMN twice, "
            "ADD COLUMN twice integer GENERATED ALWAYS AS (qty * 2) STORED",
        ),
    ):
        conn = await asyncpg.connect(kit_url)
        try:
            await conn.execute(change)
            assert await cli("doctor") == 1, change
            await conn.execute(undo)
            assert await cli("doctor") == 0, undo
        finally:
            await conn.close()


async def test_unjournaled_folder_is_an_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # (K5) a folder merged without its journal entry was skipped silently.
    assert await cli("generate") == 0
    (project / "migrations" / "0002_teammate").mkdir()
    capsys.readouterr()
    for command in ("generate", "migrate", "status", "check"):
        assert await cli(command) == 1, command
        captured = capsys.readouterr()
        assert "0002_teammate" in captured.err + captured.out, command


async def test_os_errors_are_clean_cli_errors(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # (K10) no traceback for a missing --config or an unwritable --out.
    assert await cli("--config", "missing.toml", "status") == 1
    assert "missing.toml" in capsys.readouterr().err
    assert await cli("typegen", "--out", str(project / "no" / "such" / "dir.py")) == 1
    assert "error:" in capsys.readouterr().err


async def test_a_custom_migration_keeps_the_previous_snapshot(project: Path) -> None:
    # What the tables need (an extension, a function) goes in a custom migration before
    # them: the schema changes still go into the next generated migration.
    assert await cli("generate", "--custom", "--name", "setup") == 0
    assert await cli("generate") == 0
    [generated] = project.glob("migrations/0002_*/migration.sql")
    assert "CREATE TABLE" in generated.read_text()


NEWLINE_NAME = """
from mizzle import Table
from mizzle.pg import serial

class Items(Table, name="items\\nCREATE TABLE pwned (x int);\\n--"):
    id = serial().primary_key()
"""


async def test_a_name_with_a_newline_stays_in_its_comment(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # K8-5: the name ended the "-- create table ..." header and the rest ran as SQL.
    rewrite_schema(project, NEWLINE_NAME, monkeypatch)
    assert await cli("generate") == 0
    [path] = project.glob("migrations/0001_*/migration.sql")
    assert path.read_text().startswith("-- create table items\\nCREATE TABLE pwned (x int);\\n--\n")
    assert await cli("migrate") == 0
    assert await columns(kit_url, "pwned") == []
    assert await columns(kit_url, "items\nCREATE TABLE pwned (x int);\n--") == ["id"]

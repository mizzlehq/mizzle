"""End-to-end tests of the `mizzle` CLI against a fresh database per test."""

import asyncio
import importlib
import subprocess
import sys
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


OBJECTS = (
    V1.replace(
        "from mizzle import Table, sql, index",
        "from typing import override\n"
        "from mizzle import Table, sql, index, eq, View, QueryBuilder, extension, function, "
        "trigger, policy, rls",
    ).replace(
        'index("posts_author_idx").on(cls.author_id),',
        'index("posts_author_idx").on(cls.author_id), rls(), '
        'policy("visible", for_="select", using=sql("author_id > 0")),',
    )
    + """
trgm = extension("pg_trgm")
lower_name = function(
    "lower_name", returns="trigger", body="BEGIN NEW.name = lower(NEW.name); RETURN NEW; END"
)
users_lower = trigger("users_lower", on=Users, execute=lower_name, events=["insert", "update"])

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
    assert sql.index("CREATE EXTENSION") < sql.index("CREATE TABLE") < sql.index("CREATE VIEW")
    assert await cli("migrate") == 0
    assert await cli("check") == 0
    assert await cli("generate") == 0
    assert await cli("push", "--dry-run") == 0  # the database's view text compares equal
    out = capsys.readouterr().out
    assert "No schema changes." in out and "Database is up to date." in out

    # Change the view query and the function body; drop the unapplied migration once.
    evolved = OBJECTS.replace("Role.ADMIN", "Role.USER").replace("lower(", "upper(")
    rewrite_schema(project, evolved, monkeypatch)
    assert await cli("generate") == 0
    sql = next(project.glob("migrations/0002_*/migration.sql")).read_text()
    assert 'DROP VIEW "admins"' in sql and "CREATE OR REPLACE FUNCTION" in sql
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
    assert "class Admins(View" in pulled and "trigger(" in pulled and "policy(" in pulled
    assert await cli("generate") == 0
    assert await cli("push", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "No schema changes." in out and "Database is up to date." in out

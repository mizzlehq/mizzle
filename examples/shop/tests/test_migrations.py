"""The migration kit on the shop schema, through the real CLI: generate from zero, migrate
from several processes at once, evolve (rename, add, change type, view, policy), check,
drop, push to a fresh database, and pull back into code that imports and has no drift."""

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conftest import admin_connect, drop_database, recreate_database, with_database
from tests.versions import schema_v1

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "shop" / "schema.py").read_text()


@dataclass
class Result:
    code: int
    out: str


@dataclass
class Project:
    path: Path
    url: str

    def write_schema(self, source: str) -> None:
        (self.path / "shop" / "schema.py").write_text(source)

    def run(self, *args: str, config: str = "mizzle.toml") -> Result:
        env = {**os.environ, "DATABASE_URL": self.url, "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run(
            [sys.executable, "-m", "mizzle", "--config", config, *args],
            cwd=self.path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return Result(done.returncode, done.stdout + done.stderr)

    async def run_async(self, *args: str) -> Result:
        return await asyncio.to_thread(self.run, *args)

    def sql(self, name: str) -> str:
        [folder] = (self.path / "migrations").glob(f"*_{name}")
        return (folder / "migration.sql").read_text()


async def new_project(tmp_path: Path, admin_url: str, db_name: str) -> Project:
    await recreate_database(admin_url, db_name)
    (tmp_path / "shop").mkdir(parents=True)
    (tmp_path / "shop" / "__init__.py").write_text("")
    shutil.copy(ROOT / "mizzle.toml", tmp_path / "mizzle.toml")
    return Project(tmp_path, with_database(admin_url, db_name))


async def fetch(url: str, query: str) -> list[tuple[object, ...]]:
    conn = await admin_connect(url)
    try:
        return [tuple(r) for r in await conn.fetch(query)]
    finally:
        await conn.close()


async def execute(url: str, script: str) -> None:
    conn = await admin_connect(url)
    try:
        await conn.execute(script)
    finally:
        await conn.close()


@pytest.fixture
async def project(tmp_path: Path, admin_url: str) -> AsyncGenerator[Project]:
    yield await new_project(tmp_path, admin_url, "shop_mig")
    await drop_database(admin_url, "shop_mig")


def repo_sql(name: str) -> str:
    [folder] = (ROOT / "migrations").glob(f"*_{name}")
    return (folder / "migration.sql").read_text()


def custom_migration(p: Project, name: str) -> None:
    """``generate --custom``, filled with the repository's SQL for it."""
    assert p.run("generate", "--custom", "--name", name).code == 0
    [folder] = (p.path / "migrations").glob(f"*_{name}")
    (folder / "migration.sql").write_text(repo_sql(name))


async def test_generate_migrate_evolve_check_drop(project: Project) -> None:
    p = project
    p.write_schema(schema_v1(SCHEMA))
    # Extensions and the auth.uid() function first: an index and the policies use them.
    custom_migration(p, "extensions_and_auth")
    r = p.run("generate", "--name", "shop_schema")
    assert r.code == 0 and "(45 changes)" in r.out, r.out
    assert p.sql("shop_schema") == repo_sql("shop_schema")  # deterministic
    custom_migration(p, "triggers_and_grants")  # what mizzle does not manage

    # Four app servers start at once: the advisory lock runs each migration exactly once.
    results = await asyncio.gather(*(p.run_async("migrate") for _ in range(4)))
    assert all(x.code == 0 for x in results), [x.out for x in results]
    outputs = "".join(x.out for x in results)
    for name in ("0001_extensions_and_auth", "0002_shop_schema", "0003_triggers_and_grants"):
        assert outputs.count(f"Applying {name}") == 1
    applied = await fetch(p.url, "SELECT count(*) FROM __mizzle_migrations")
    assert applied == [(3,)]

    # Data written under version 1 survives version 2.
    await fetch(
        p.url,
        "WITH u AS (INSERT INTO users (email, name) VALUES ('m@x.io', 'm') RETURNING id),"
        " s AS (INSERT INTO stores (owner_id, slug, name) SELECT id, 'm', 'm' FROM u RETURNING id)"
        " INSERT INTO products (store_id, sku, name, description, price)"
        " SELECT id, 'k', 'Kettle', 'boils water', 9.5 FROM s RETURNING id",
    )

    p.write_schema(SCHEMA)
    # Widening varchar(40) to varchar(64) cannot lose data: no --allow-data-loss needed.
    r = p.run("generate", "--name", "catalog_v2", "--rename", "products.description=details")
    assert r.code == 0 and "(7 changes)" in r.out, r.out
    assert p.sql("catalog_v2") == repo_sql("catalog_v2")
    r = p.run("migrate")
    assert r.code == 0 and "Applying 0004_catalog_v2" in r.out, r.out
    rows = await fetch(p.url, "SELECT details, search::text FROM products")
    assert rows == [("boils water", "'boil':2B 'kettl':1A 'water':3B")]
    assert await fetch(p.url, "SELECT count(*) FROM top_products") == [(0,)]
    policies = await fetch(p.url, "SELECT policyname FROM pg_policies WHERE tablename = 'orders'")
    assert policies == [("orders_owner",)]
    assert await fetch(
        p.url,
        "SELECT character_maximum_length FROM information_schema.columns "
        "WHERE table_name = 'stores' AND column_name = 'slug'",
    ) == [(64,)]

    r = p.run("check")
    assert r.code == 0 and "4 migration(s) OK." in r.out, r.out
    assert p.run("generate").out.strip() == "No schema changes."
    assert p.run("migrate").out.strip() == "Database is up to date."

    # An unapplied migration can be dropped; an applied one cannot.
    p.write_schema(
        SCHEMA.replace(
            "    phone = text()  # v2\n", "    phone = text()  # v2\n    nickname = text()\n"
        )
    )
    r = p.run("generate", "--name", "nickname")
    assert r.code == 0 and "0005_nickname" in r.out, r.out
    r = p.run("drop")
    assert r.code == 0 and "Dropped 0005_nickname" in r.out, r.out
    assert not list((p.path / "migrations").glob("0005_*"))
    assert "4 migration(s) OK." in p.run("check").out
    r = p.run("drop")
    assert r.code == 1 and "0004_catalog_v2 is already applied" in r.out, r.out

    # Editing an applied migration is caught.
    applied_sql = next((p.path / "migrations").glob("0004_*")) / "migration.sql"
    applied_sql.write_text(applied_sql.read_text() + "\nSELECT 1;\n")
    r = p.run("migrate")
    assert r.code == 1 and "changed after it was applied" in r.out, r.out


async def test_push_then_pull_without_drift(tmp_path: Path, admin_url: str) -> None:
    p = await new_project(tmp_path / "app", admin_url, "shop_push")
    try:
        p.write_schema(SCHEMA)
        # push applies the schema; what mizzle does not manage is SQL run around it.
        await execute(p.url, repo_sql("extensions_and_auth"))
        r = p.run("push", "--force")
        assert r.code == 0 and "Applied" in r.out, r.out
        assert await fetch(
            p.url, "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'"
        ) == [(7,)]
        r = p.run("push", "--dry-run")
        assert r.out.strip() == "Database is up to date.", r.out
        await execute(p.url, repo_sql("triggers_and_grants"))
        r = p.run("push", "--dry-run")  # triggers, functions and grants are left alone
        assert r.out.strip() == "Database is up to date.", r.out

        # pull into a scratch project: the module imports, and generating from it (on top
        # of the baseline pull writes) finds nothing to change.
        scratch = tmp_path / "pulled"
        (scratch / "db").mkdir(parents=True)
        (scratch / "db" / "__init__.py").write_text("")
        (scratch / "mizzle.toml").write_text(
            'schema = "db.schema"\nout = "migrations"\nurl_env = "DATABASE_URL"\n'
            'schema_filter = ["public", "shop_audit", "auth"]\n'
        )
        pulled = Project(scratch, p.url)
        r = pulled.run("pull")
        assert r.code == 0 and "Created baseline" in r.out, r.out
        assert "pull skips trigger orders_audit on orders (not managed by mizzle)" in r.out, r.out
        source = (scratch / "db" / "schema.py").read_text()
        for name in (
            "class Users(Table",
            "class AuditLog(Table",
            "schema='shop_audit'",
            "class StoreSales(View",
            "search = tsvector()",
        ):
            assert name in source, name
        imported = subprocess.run(
            [sys.executable, "-c", "import db.schema"], cwd=scratch, capture_output=True, text=True
        )
        assert imported.returncode == 0, imported.stderr
        r = pulled.run("generate")
        assert r.out.strip() == "No schema changes.", r.out
        r = pulled.run("push", "--dry-run")
        assert r.out.strip() == "Database is up to date.", r.out
    finally:
        await drop_database(admin_url, "shop_push")


async def test_vector_migrations_have_no_drift(tmp_path: Path, admin_url: str) -> None:
    available = await fetch(
        admin_url, "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
    )
    if not available:
        pytest.skip("no pgvector on this server")
    p = await new_project(tmp_path, admin_url, "shop_vec")
    try:
        p.write_schema(SCHEMA)
        shutil.copy(ROOT / "shop" / "vectors.py", tmp_path / "shop" / "vectors.py")
        shutil.copy(ROOT / "mizzle.vector.toml", tmp_path / "mizzle.vector.toml")
        shutil.copytree(ROOT / "migrations_vector", tmp_path / "migrations_vector")
        r = p.run("migrate", config="mizzle.vector.toml")
        assert r.code == 0 and "Applied 3 migration(s)." in r.out, r.out
        assert p.run("generate", config="mizzle.vector.toml").out.strip() == "No schema changes."
        r = p.run("push", "--dry-run", config="mizzle.vector.toml")
        assert r.out.strip() == "Database is up to date.", r.out
        indexes = await fetch(
            p.url, "SELECT indexdef FROM pg_indexes WHERE indexname = 'product_embeddings_hnsw'"
        )
        assert "hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='64')" in str(
            indexes[0][0]
        )
    finally:
        await drop_database(admin_url, "shop_vec")


def test_typegen_is_up_to_date(tmp_path: Path) -> None:
    out = tmp_path / "types.py"
    done = subprocess.run(
        [sys.executable, "-m", "mizzle", "typegen", "--out", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert out.read_text() == (ROOT / "shop" / "schema_types.py").read_text()

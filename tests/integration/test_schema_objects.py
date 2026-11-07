"""Views and row-level security against a real database: applied, introspected without
drift, evolved, pulled and dropped. Objects mizzle does not manage are left alone."""

import importlib.util
import re
import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import final, override

import pytest

from mizzle import (
    Constraint,
    Database,
    QueryBuilder,
    SchemaError,
    Table,
    View,
    count,
    eq,
    index,
    policy,
    rls,
    sql,
    unique_index,
)
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.codegen import render_schema_module
from mizzle.kit.diff import diff
from mizzle.kit.migrate import apply_statements
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import bigint, boolean, integer, serial, text, timestamp

dialect = PostgresDialect()
SCHEMAS = ["public", "so_app"]
ROLE = "mizzle_so_reader"


# -- version 1 -------------------------------------------------------------------------


@final
class Accounts(Table, name="so_accounts"):
    id = serial().primary_key()
    email = text().not_null()
    active = boolean().not_null().default(True)
    owner = text()
    score = integer()
    updated_at = timestamp(tz=True)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            index("so_accounts_email_hash").using("hash").on(cls.email),
            rls(),
            policy("own", for_="select", to=[ROLE], using=sql("owner = current_user")),
            policy("add", for_="insert", with_check=sql("owner = current_user AND score > 0")),
        ]


@final
class Active(View, name="so_active", schema="so_app"):
    id = integer().not_null()
    email = text().not_null()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return (
            db.select(Accounts.id, Accounts.email).from_(Accounts).where(eq(Accounts.active, True))
        )


@final
class Counts(View, name="so_counts", materialized=True):
    active = boolean().not_null()
    n = bigint().not_null()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return (
            db.select({"active": Accounts.active, "n": count()})
            .from_(Accounts)
            .group_by(Accounts.active)
        )

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [unique_index("so_counts_active").on(cls.active)]


V1_TABLES: list[type[Table]] = [Accounts, Active, Counts]


# -- version 2: every kind of object changes -------------------------------------------


@final
class Accounts2(Table, name="so_accounts"):  # score is dropped (used by a policy)
    id = serial().primary_key()
    email = text().not_null()
    active = boolean().not_null().default(True)
    owner = text()
    updated_at = timestamp(tz=True)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            rls(force=True),
            policy("own", for_="select", to=[ROLE, "public"], using=sql("owner = session_user")),
            policy("edit", for_="update", as_="restrictive", using=sql("active")),
        ]


@final
class Active2(View, name="so_active", schema="so_app"):  # one more column
    id = integer().not_null()
    email = text().not_null()
    owner = text()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return (
            db.select(Accounts2.id, Accounts2.email, Accounts2.owner)
            .from_(Accounts2)
            .where(eq(Accounts2.active, True))
        )


V2_TABLES: list[type[Table]] = [Accounts2, Active2, Counts]


def v1() -> Snapshot:
    return build_snapshot(V1_TABLES, dialect)


def v2() -> Snapshot:
    return build_snapshot(V2_TABLES, dialect)


RESET = f"""
DROP SCHEMA IF EXISTS so_app CASCADE;
DROP MATERIALIZED VIEW IF EXISTS so_counts;
DROP TABLE IF EXISTS so_accounts CASCADE;
DROP TABLE IF EXISTS so_ignored_part, so_ignored CASCADE;
DROP FUNCTION IF EXISTS so_touch();
DROP DOMAIN IF EXISTS so_email;
DROP TYPE IF EXISTS so_pair;
DROP ROLE IF EXISTS {ROLE};
"""


@pytest.fixture
async def driver(pg_url: str) -> AsyncGenerator[PostgresDriver]:
    drv = await PostgresDriver.connect(pg_url, pool_size=2)
    async with drv.acquire() as conn:
        await conn.execute(RESET, [])
        await conn.execute(f"CREATE ROLE {ROLE}", [])
    yield drv
    async with drv.acquire() as conn:
        await conn.execute(RESET, [])
    await drv.close()


async def migrate(driver: PostgresDriver, old: Snapshot, new: Snapshot) -> None:
    async with driver.acquire() as conn:
        await apply_statements(conn, dialect.render(diff(old, new)))


async def live_and_expected(driver: PostgresDriver, desired: Snapshot) -> tuple[Snapshot, Snapshot]:
    async with driver.acquire() as conn:
        live = await dialect.introspect(conn, SCHEMAS)
        expected = await dialect.normalize_snapshot(conn, desired)
    # Only this test's objects: other tests leave their own behind.
    ours = ("public.so_", "so_app.")
    live["enums"] = {}
    live["tables"] = {k: t for k, t in live["tables"].items() if k.startswith(ours)}
    live["views"] = {k: v for k, v in live.get("views", {}).items() if k.startswith(ours)}
    return live, expected


def assert_no_drift(live: Snapshot, expected: Snapshot, strict: bool = True) -> None:
    """``strict=False`` is what push compares."""
    remaining = diff(live, expected, compare_expressions=strict)
    assert remaining == [], [s.describe() for s in remaining]


async def test_create_query_evolve_and_drop(driver: PostgresDriver, pg_url: str) -> None:
    await migrate(driver, empty_snapshot("postgresql"), v1())
    live, expected = await live_and_expected(driver, v1())
    assert_no_drift(live, expected)
    # The database's own text of the view query and policies.
    view = live.get("views", {})["so_app.so_active"]["definition"]
    assert re.match(r"SELECT (so_accounts\.)?id,", view), view  # 14-15 qualify columns
    policies = live["tables"]["public.so_accounts"].get("policies", {})
    assert policies["own"]["using"] == "(owner = CURRENT_USER)"

    async with await Database.connect(pg_url, schema=V1_TABLES) as db:
        await db.insert(Accounts).values(email="a@x", owner="me", score=1)
        await db.insert(Accounts).values(email="b@x", active=False)
        rows = await db.select().from_(Active)
        assert [(r.id, r.email) for r in rows] == [(1, "a@x")]
        assert await db.select().from_(Counts) == []  # not refreshed yet
        await db.refresh(Counts)
        await db.refresh(Counts, concurrently=True)  # needs the unique index
        assert sorted((r.active, r.n) for r in await db.select().from_(Counts)) == [
            (False, 1),
            (True, 1),
        ]
        with pytest.raises(Exception, match="not a materialized view"):
            await db.refresh(Active)

    # v1 -> v2 as a generated migration (snapshot to snapshot), with rows in place.
    await migrate(driver, v1(), v2())
    live, expected = await live_and_expected(driver, v2())
    assert_no_drift(live, expected)
    table = live["tables"]["public.so_accounts"]
    assert table.get("rls") == {"enabled": True, "force": True}
    assert set(table.get("policies", {})) == {"own", "edit"}
    assert list(live.get("views", {})["so_app.so_active"]["columns"]) == ["id", "email", "owner"]

    # Dropping everything leaves nothing behind.
    await migrate(driver, v2(), empty_snapshot("postgresql"))
    live, _ = await live_and_expected(driver, empty_snapshot("postgresql"))
    assert live["tables"] == {} and live.get("views", {}) == {}


async def test_push_path_and_pull_round_trip(driver: PostgresDriver, tmp_path: Path) -> None:
    """``push`` diffs the live database against a normalized snapshot."""
    await migrate(driver, empty_snapshot("postgresql"), v1())
    for desired in (v2(), v1()):
        async with driver.acquire() as conn:
            live = await dialect.introspect(conn, SCHEMAS)
            target = await dialect.normalize_snapshot(conn, desired)
            steps = diff(live, target, compare_expressions=False)
            assert steps, "something to do"
            await apply_statements(conn, dialect.render(steps))
        live, expected = await live_and_expected(driver, desired)
        assert_no_drift(live, expected, strict=False)

    # pull: the generated module builds the snapshot it was generated from.
    live, _ = await live_and_expected(driver, v1())
    source = render_schema_module(live)
    module_name = f"pulled_{uuid.uuid4().hex[:8]}"
    path = tmp_path / f"{module_name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    members = list(vars(module).values())
    tables = [
        m
        for m in members
        if isinstance(m, type) and issubclass(m, Table) and "__table__" in vars(m)
    ]
    pulled = build_snapshot(tables, dialect)
    assert diff(live, pulled) == [], [s.describe() for s in diff(live, pulled)]
    assert "class SoActive(View, name='so_active', schema='so_app')" in source
    assert "materialized=True" in source


async def test_view_columns_are_checked(driver: PostgresDriver) -> None:
    @final
    class Wrong(View, name="so_wrong"):
        missing = integer()

        @classmethod
        @override
        def query(cls, db: QueryBuilder):
            return sql("SELECT 1 AS id")

    async with driver.acquire() as conn:
        with pytest.raises(SchemaError, match="missing"):
            await dialect.normalize_snapshot(conn, build_snapshot([Wrong], dialect))


async def test_view_options_survive_recreate_and_pull(driver: PostgresDriver) -> None:
    """(K6) security_invoker / security_barrier / check option: written, read back,
    kept when the view is re-created, and written by pull."""
    async with driver.acquire() as conn:
        [row] = await conn.fetch("SHOW server_version_num", [])
    if int(str(row[0])) < 150000:
        pytest.skip("security_invoker needs PostgreSQL 15")

    @final
    class Secure(
        View,
        name="so_secure",
        schema="so_app",
        security_invoker=True,
        security_barrier=True,
        check_option="local",
    ):
        id = integer().not_null()
        email = text().not_null()

        @classmethod
        @override
        def query(cls, db: QueryBuilder):
            return db.select(Accounts.id, Accounts.email).from_(Accounts)

    @final
    class Secure2(  # a new query: dropped and created again
        View,
        name="so_secure",
        schema="so_app",
        security_invoker=True,
        security_barrier=True,
        check_option="local",
    ):
        id = integer().not_null()

        @classmethod
        @override
        def query(cls, db: QueryBuilder):
            return db.select(Accounts.id).from_(Accounts)

    one = build_snapshot([Accounts, Secure], dialect)
    two = build_snapshot([Accounts, Secure2], dialect)
    await migrate(driver, empty_snapshot("postgresql"), one)
    await migrate(driver, one, two)
    live, expected = await live_and_expected(driver, two)
    assert_no_drift(live, expected)
    view = live.get("views", {})["so_app.so_secure"]
    assert view.get("security_invoker") and view.get("security_barrier")
    assert view.get("check_option") == "local"
    # An option no longer declared is a change.
    del expected.get("views", {})["so_app.so_secure"]["security_invoker"]
    assert diff(live, expected)
    source = render_schema_module(live)
    assert "security_invoker=True, security_barrier=True, check_option='local'" in source


async def test_normalize_twice_on_one_connection(driver: PostgresDriver) -> None:
    """The server's text comes back each time, for the objects of that call."""
    await migrate(driver, empty_snapshot("postgresql"), v1())
    async with driver.acquire() as conn:  # one connection: one statement cache
        for tables in ([Accounts, Active], [Accounts2, Active2]):
            out = await dialect.normalize_snapshot(conn, build_snapshot(tables, dialect))
            definition = out.get("views", {})["so_app.so_active"]["definition"]
            # The server's text (14-15 qualify columns).
            assert re.match(r"SELECT (so_accounts\.)?id,", definition), definition
            assert ("owner" in definition) == (tables[1] is Active2), definition


UNMANAGED = f"""
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE DOMAIN so_email AS text CHECK (VALUE LIKE '%@%');
CREATE TYPE so_pair AS (a integer, b text);
CREATE FUNCTION so_touch() RETURNS trigger LANGUAGE plpgsql
    AS $$ BEGIN NEW.updated_at = now(); RETURN NEW; END $$;
CREATE TRIGGER so_touch BEFORE UPDATE ON so_accounts
    FOR EACH ROW EXECUTE FUNCTION so_touch();
ALTER TABLE so_accounts ADD CONSTRAINT so_no_overlap EXCLUDE USING gist (email WITH =);
GRANT SELECT ON so_accounts TO {ROLE};
GRANT SELECT ON so_app.so_active TO {ROLE};
CREATE TABLE so_ignored (id integer, at date, email so_email, pair so_pair)
    PARTITION BY RANGE (at);
CREATE TABLE so_ignored_part PARTITION OF so_ignored
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
"""


async def test_unmanaged_objects_are_left_alone(driver: PostgresDriver, tmp_path: Path) -> None:
    """Functions, triggers, domains, composites, extensions, exclusion constraints,
    partitions and grants: not read, so never dropped nor reported as drift, and pull
    writes columns of other types as custom()."""
    await migrate(driver, empty_snapshot("postgresql"), v1())
    async with driver.acquire() as conn:
        await conn.execute(UNMANAGED, [])
        assert set(await dialect.skipped_objects(conn, SCHEMAS)) >= {
            "function so_touch()",
            "trigger so_touch on so_accounts",
            "domain so_email",
            "composite type so_pair",
            "extension btree_gist",
            "exclusion constraint so_no_overlap on so_accounts",
            "partition so_ignored_part of so_ignored",
            "grants on so_accounts",
            "grants on so_app.so_active",
        }
    live, expected = await live_and_expected(driver, v1())
    ignored = live["tables"].pop("public.so_ignored")  # the partitioned parent is a table
    assert "public.so_ignored_part" not in live["tables"]
    assert_no_drift(live, expected)
    assert_no_drift(live, expected, strict=False)
    accounts = live["tables"]["public.so_accounts"]
    assert set(accounts) <= {*expected["tables"]["public.so_accounts"], "comment"}
    assert "so_no_overlap" not in accounts["indexes"]

    # pull writes the partitioned table plainly, its domain and composite as custom().
    alone: Snapshot = {**live, "tables": {"public.so_ignored": ignored}, "views": {}}
    source = render_schema_module(alone)
    assert "custom('so_email')" in source and "custom('so_pair')" in source
    module_name = f"pulled_{uuid.uuid4().hex[:8]}"
    path = tmp_path / f"{module_name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    [pulled_table] = [
        m
        for m in vars(module).values()
        if isinstance(m, type) and issubclass(m, Table) and "__table__" in vars(m)
    ]
    pulled = build_snapshot([pulled_table], dialect)
    assert diff(alone, pulled) == []

    # Dropping mizzle's objects leaves the others (dropped with their table) alone.
    await migrate(driver, v1(), empty_snapshot("postgresql"))
    async with driver.acquire() as conn:
        [row] = await conn.fetch(
            "SELECT to_regprocedure('so_touch()') IS NOT NULL, to_regtype('so_email') IS NOT"
            " NULL, to_regtype('so_pair') IS NOT NULL, to_regclass('so_ignored') IS NOT NULL",
            [],
        )
    assert tuple(row) == (True, True, True, True)

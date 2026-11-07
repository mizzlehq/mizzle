"""Identity, generated and on_update columns against a real database."""

import datetime as dt
import importlib.util
import sys
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import final, override

import pytest

from mizzle import Constraint, Database, QueryError, Table, eq, excluded, index, sql
from mizzle.core.schema.relations import many, one
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.dialects.base import Driver
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.codegen import render_schema_module
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import array, bigint, integer, text, timestamp

dialect = PostgresDialect()
SCHEMA = "gencols"
STAMP = dt.datetime(2026, 1, 2, 3, 4, 5)


@final
class Authors(Table, name="authors", schema=SCHEMA):
    id = integer().primary_key().generated_always_as_identity()
    name = text().not_null()
    articles = many(lambda: Articles)


@final
class Articles(Table, name="articles", schema=SCHEMA):
    id = bigint().primary_key().generated_by_default_as_identity(start=1000, increment=10)
    author_id = integer().not_null().references(lambda: Authors.id)
    title = text().not_null()
    body = text().not_null().default("")
    search = array(text()).generated_always_as(
        sql("string_to_array(lower(coalesce(title, '') || ' ' || body), ' ')")
    )
    title_length = integer().generated_always_as(sql("length(title)"))
    edited = timestamp().on_update(lambda: STAMP)
    changed = timestamp(tz=True).on_update_now()
    author = one(
        lambda: Authors, fields=lambda: [Articles.author_id], references=lambda: [Authors.id]
    )

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [index("articles_search_idx").using("gin").on(cls.search)]


TABLES: Sequence[type[Table]] = [Authors, Articles]


async def setup(driver: Driver, snapshot: Snapshot) -> None:
    async with driver.acquire() as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE", [])
        for statement in dialect.render(diff(empty_snapshot("postgresql"), snapshot)):
            await conn.execute(statement.sql, [])


async def introspect(driver: PostgresDriver) -> Snapshot:
    async with driver.acquire() as conn:
        return await dialect.introspect(conn, [SCHEMA])


@pytest.fixture
async def gen_db(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    await setup(driver, build_snapshot(TABLES, dialect))
    async with await Database.connect(pg_url, schema=list(TABLES)) as database:
        yield database
    async with driver.acquire() as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE", [])


async def server_version(driver: PostgresDriver) -> int:
    async with driver.acquire() as conn:
        [row] = await conn.fetch("SHOW server_version_num", [])
        return int(str(row[0]))


def load(source: str, tmp_path: Path, name: str) -> list[type[Table]]:
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return [
        v
        for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, Table) and v is not Table
    ]


async def test_no_drift_and_pull_round_trip(
    gen_db: Database, driver: PostgresDriver, tmp_path: Path
) -> None:
    del gen_db
    expected = build_snapshot(TABLES, dialect)
    live = await introspect(driver)
    assert diff(live, expected, compare_expressions=False) == []
    columns = live["tables"][f"{SCHEMA}.articles"]["columns"]
    assert columns["id"].get("identity") == {"always": False, "start": 1000, "increment": 10}
    assert columns["search"].get("generated", {}).get("stored") is True
    assert (
        live["tables"][f"{SCHEMA}.articles"]["indexes"]["articles_search_idx"].get("method")
        == "gin"
    )

    source = render_schema_module(live)
    assert "generated_by_default_as_identity(increment=10, start=1000)" in source
    assert "array(text()).generated_always_as(sql(" in source
    pulled = build_snapshot(load(source, tmp_path, "pulled_gencols"), dialect)
    assert diff(live, pulled) == []  # the database's own expression text, exactly


async def test_identity_and_generated_values(gen_db: Database) -> None:
    [ann] = await gen_db.insert(Authors).values(name="ann").returning()
    [bob] = await gen_db.insert(Authors).values(name="bob").returning()
    assert bob.id == ann.id + 1
    [a, b] = (
        await gen_db.insert(Articles)
        .values(
            [
                {"author_id": ann.id, "title": "Cats sat", "body": "on mats"},
                {"author_id": ann.id, "title": "Dogs", "id": 5},  # BY DEFAULT: explicit id is fine
            ]
        )
        .returning()
    )
    assert (a.id, b.id) == (1000, 5)
    assert a.search == ["cats", "sat", "on", "mats"]
    assert a.title_length == 8

    with pytest.raises(QueryError, match="GENERATED ALWAYS AS IDENTITY"):
        await gen_db.insert(Authors).values(id=9, name="x")
    with pytest.raises(QueryError, match="generated column"):
        await gen_db.insert(Articles).values(author_id=ann.id, title="x", search=["x"])
    with pytest.raises(QueryError, match="generated column"):
        await gen_db.update(Articles).set(title_length=1).where(eq(Articles.id, a.id))

    found = await gen_db.select().from_(Articles).where(sql("{} @> ARRAY['cats']", Articles.search))
    assert [r.id for r in found] == [a.id]

    # Nested rows travel as JSON: generated values come back the same.
    [loaded] = await gen_db.query(Authors).find_many(
        with_={"articles": {"order_by": Articles.id}}, where=eq(Authors.id, ann.id)
    )
    direct = await gen_db.select().from_(Articles).order_by(Articles.id)
    assert loaded.articles == direct


async def test_on_update_in_update_and_upsert(gen_db: Database) -> None:
    [ann] = await gen_db.insert(Authors).values(name="ann").returning()
    [art] = await gen_db.insert(Articles).values(author_id=ann.id, title="t").returning()
    assert (art.edited, art.changed) == (None, None)

    [updated] = (
        await gen_db.update(Articles).set(title="u").where(eq(Articles.id, art.id)).returning()
    )
    assert updated.edited == STAMP
    assert updated.changed is not None
    assert updated.title_length == 1

    later = dt.datetime(2030, 1, 1)
    [explicit] = (
        await gen_db.update(Articles).set(edited=later).where(eq(Articles.id, art.id)).returning()
    )
    assert explicit.edited == later

    await gen_db.update(Articles).set(edited=None, changed=None).where(eq(Articles.id, art.id))
    [upserted] = await (
        gen_db.insert(Articles)
        .values(id=art.id, author_id=ann.id, title="v")
        .on_conflict_do_update(target=Articles.id, set_={"title": excluded(Articles.title)})
        .returning()
    )
    assert (upserted.title, upserted.edited) == ("v", STAMP)
    assert upserted.changed is not None


async def test_identity_migrations(driver: PostgresDriver) -> None:
    def table(**columns: object) -> type[Table]:
        return type("Ids", (Table,), {"__module__": __name__, **columns}, name="ids", schema=SCHEMA)

    steps = [
        table(a=integer().not_null(), b=integer()),
        table(
            a=integer().generated_always_as_identity(start=5),
            b=integer().generated_by_default_as_identity(),
        ),
        table(a=integer().generated_by_default_as_identity(start=7, increment=2), b=integer()),
        table(
            a=bigint().generated_by_default_as_identity(start=7, increment=2),
            b=integer().not_null(),
        ),
    ]
    await setup(driver, build_snapshot([steps[0]], dialect))
    try:
        previous = build_snapshot([steps[0]], dialect)
        for cls in steps[1:]:
            target = build_snapshot([cls], dialect)
            async with driver.acquire() as conn:
                for statement in dialect.render(diff(previous, target)):
                    await conn.execute(statement.sql, [])
            assert diff(await introspect(driver), target, compare_expressions=False) == []
            previous = target
    finally:
        async with driver.acquire() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE", [])


async def test_generated_migrations(driver: PostgresDriver) -> None:
    version = await server_version(driver)

    def table(**columns: object) -> type[Table]:
        return type("Gen", (Table,), {"__module__": __name__, **columns}, name="gen", schema=SCHEMA)

    steps = [
        table(a=integer(), g=integer().generated_always_as(sql("a * 2"))),
        table(
            a=integer(),
            g=integer(),
            t=text().generated_always_as(sql("a::text")),
        ),
    ]
    if version >= 170000:  # ALTER COLUMN ... SET EXPRESSION
        steps.append(
            table(
                a=integer(),
                g=integer(),
                t=text().generated_always_as(sql("(a + 1)::text")),
            )
        )
    if version >= 180000:  # VIRTUAL generated columns
        steps.append(
            table(
                a=integer(), g=integer(), v=bigint().generated_always_as(sql("a + 1"), stored=False)
            )
        )
    await setup(driver, build_snapshot([steps[0]], dialect))
    try:
        async with driver.acquire() as conn:
            await conn.execute(f"INSERT INTO {SCHEMA}.gen (a) VALUES (21)", [])
        previous = build_snapshot([steps[0]], dialect)
        for cls in steps[1:]:
            target = build_snapshot([cls], dialect)
            async with driver.acquire() as conn:
                for statement in dialect.render(diff(previous, target)):
                    await conn.execute(statement.sql, [])
            assert diff(await introspect(driver), target, compare_expressions=False) == []
            previous = target
        async with driver.acquire() as conn:
            [row] = await conn.fetch(f"SELECT g FROM {SCHEMA}.gen", [])
            assert row[0] == 42  # DROP EXPRESSION kept the computed value
    finally:
        async with driver.acquire() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE", [])

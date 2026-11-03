"""Identity, generated and on_update columns, and tsvector, against a real database."""

import asyncio
import datetime as dt
import importlib.util
import os
import sys
from collections.abc import AsyncGenerator, Generator, Sequence
from pathlib import Path
from typing import final, override

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizzle import Constraint, Database, QueryError, Table, eq, excluded, index, sql
from mizzle.core.schema.relations import many, one
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.dialects.base import Driver
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.codegen import render_schema_module
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import bigint, integer, text, timestamp, tsquery, tsvector

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
    search = tsvector().generated_always_as(
        sql("to_tsvector('english', coalesce(title, '') || ' ' || body)")
    )
    query = tsquery()
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
    assert "tsvector().generated_always_as(sql(" in source
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
                {"author_id": ann.id, "title": "Cats sat", "body": "on mats", "query": "cat & mat"},
                {"author_id": ann.id, "title": "Dogs", "id": 5},  # BY DEFAULT: explicit id is fine
            ]
        )
        .returning()
    )
    assert (a.id, b.id) == (1000, 5)
    assert a.search == "'cat':1 'mat':4 'sat':2"
    assert a.query == "'cat' & 'mat'"
    assert a.title_length == 8

    with pytest.raises(QueryError, match="GENERATED ALWAYS AS IDENTITY"):
        await gen_db.insert(Authors).values(id=9, name="x")
    with pytest.raises(QueryError, match="generated column"):
        await gen_db.insert(Articles).values(author_id=ann.id, title="x", search="'x'")
    with pytest.raises(QueryError, match="generated column"):
        await gen_db.update(Articles).set(title_length=1).where(eq(Articles.id, a.id))

    found = (
        await gen_db.select()
        .from_(Articles)
        .where(sql("{} @@ to_tsquery('english', 'cats')", Articles.search))
    )
    assert [r.id for r in found] == [a.id]

    # Nested rows travel as JSON: tsvector / tsquery / generated values come back the same.
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
            t=tsvector().generated_always_as(sql("to_tsvector('simple', a::text)")),
        ),
    ]
    if version >= 170000:  # ALTER COLUMN ... SET EXPRESSION
        steps.append(
            table(
                a=integer(),
                g=integer(),
                t=tsvector().generated_always_as(sql("to_tsvector('english', a::text)")),
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


# -- tsvector / tsquery values through every read path (fuzzed) -------------------------


@final
class Docs(Table, name="docs", schema=SCHEMA):
    id = integer().primary_key().generated_always_as_identity()
    doc = tsvector()
    query = tsquery()
    texts = many(lambda: DocTexts)


@final
class DocTexts(Table, name="doc_texts", schema=SCHEMA):
    id = integer().primary_key().generated_always_as_identity()
    doc_id = integer().not_null().references(lambda: Docs.id)
    doc = tsvector()
    query = tsquery()
    parent = one(lambda: Docs, fields=lambda: [DocTexts.doc_id], references=lambda: [Docs.id])


def _quoted(word: str) -> str:
    return "'" + word.replace("\\", "\\\\").replace("'", "''") + "'"


lexemes = st.text(
    st.characters(exclude_characters="\x00", exclude_categories=["Cs"]), min_size=1, max_size=8
)
positions = st.lists(
    st.tuples(st.integers(1, 16383), st.sampled_from(["", "A", "B", "C"])), max_size=3
)
documents = st.lists(st.tuples(lexemes, positions), max_size=5).map(
    lambda items: " ".join(
        _quoted(w) + (":" + ",".join(f"{p}{wt}" for p, wt in pos) if pos else "")
        for w, pos in items
    )
)
queries = st.lists(lexemes, min_size=1, max_size=4).flatmap(
    lambda words: st.sampled_from([" & ", " | ", " <-> "]).map(
        lambda op: op.join(_quoted(w) for w in words)
    )
)


@pytest.fixture(scope="module")
def search_runner() -> Generator[tuple[asyncio.Runner, Database]]:
    url = os.environ.get("MIZZLE_TEST_URL")
    if not url:
        pytest.skip("set MIZZLE_TEST_URL to run integration tests")
    with asyncio.Runner() as loop:
        db = loop.run(Database.connect(url, schema=[Docs, DocTexts], pool_size=2))
        loop.run(setup(db.driver, build_snapshot([Docs, DocTexts], dialect)))
        yield loop, db

        async def teardown() -> None:
            async with db.driver.acquire() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE", [])
            await db.close()

        loop.run(teardown())


@settings(
    max_examples=int(os.environ.get("MIZZLE_FUZZ_EXAMPLES", "200")),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(doc=st.none() | documents, query=st.none() | queries)
def test_search_types_round_trip(
    search_runner: tuple[asyncio.Runner, Database], doc: str | None, query: str | None
) -> None:
    loop, db = search_runner

    async def scenario() -> None:
        [parent] = await db.insert(Docs).values(doc=doc, query=query).returning()
        [child] = (
            await db.insert(DocTexts).values(doc_id=parent.id, doc=doc, query=query).returning()
        )
        # PostgreSQL normalizes the text (sorts, merges); every path must agree on it.
        [selected] = await db.select().from_(DocTexts).where(eq(DocTexts.id, child.id))
        [loaded] = await db.query(Docs).find_many(
            where=eq(Docs.id, parent.id), with_={"texts": True}
        )
        assert (
            (selected.doc, selected.query) == (child.doc, child.query) == (parent.doc, parent.query)
        )
        assert loaded.texts == [selected]
        # Writing back what was read keeps it.
        [again] = (
            await db.update(DocTexts)
            .set(doc=selected.doc, query=selected.query)
            .where(eq(DocTexts.id, child.id))
            .returning()
        )
        assert again == selected
        await db.delete(DocTexts).where(eq(DocTexts.id, child.id))
        await db.delete(Docs).where(eq(Docs.id, parent.id))

    loop.run(scenario())

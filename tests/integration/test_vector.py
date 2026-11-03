"""pgvector columns, distances and indexes. Needs a server with the extension:

    docker compose -f tests/infra/compose.yml up -d --wait vector
    MIZZLE_VECTOR_URL=postgresql://mizzle:mizzle@localhost:54335/mizzle \\
    uv run pytest tests/integration/test_vector.py
"""

import asyncio
import importlib.util
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import final, override

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizzle import Constraint, Database, Table, eq, index, many, one
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.dialects.postgres import PostgresDialect
from mizzle.kit.codegen import render_schema_module
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import (
    cosine_distance,
    halfvec,
    inner_product,
    integer,
    l2_distance,
    text,
    vector,
)

dialect = PostgresDialect()
SCHEMA = "vec"


@final
class Shelves(Table, name="shelves", schema=SCHEMA):
    id = integer().primary_key().generated_always_as_identity()
    items = many(lambda: Items)


@final
class Items(Table, name="items", schema=SCHEMA):
    id = integer().primary_key().generated_always_as_identity()
    shelf_id = integer().not_null().references(lambda: Shelves.id)
    label = text()
    embedding = vector(3)
    half = halfvec(2)
    shelf = one(lambda: Shelves, fields=lambda: [Items.shelf_id], references=lambda: [Shelves.id])

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            index("items_embedding_hnsw")
            .using("hnsw", "vector_cosine_ops")
            .on(cls.embedding)
            .with_(m=16, ef_construction=64),
            index("items_embedding_ivf").using("ivfflat").on(cls.embedding).with_(lists=1),
            index("items_half_hnsw").using("hnsw", "halfvec_l2_ops").on(cls.half),
        ]


TABLES: list[type[Table]] = [Shelves, Items]


async def reset(db: Database) -> None:
    async with db.driver.acquire() as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE", [])


async def introspect(db: Database) -> Snapshot:
    async with db.driver.acquire() as conn:
        return await dialect.introspect(conn, [SCHEMA])


@pytest.fixture(scope="module")
def runner() -> Generator[tuple[asyncio.Runner, Database]]:
    url = os.environ.get("MIZZLE_VECTOR_URL")
    if not url:
        pytest.skip("set MIZZLE_VECTOR_URL to a server with pgvector (tests/infra/compose.yml)")
    with asyncio.Runner() as loop:
        db = loop.run(Database.connect(url, schema=TABLES, pool_size=2))

        async def setup() -> None:
            await reset(db)
            async with db.driver.acquire() as conn:
                await conn.execute("DROP EXTENSION IF EXISTS vector CASCADE", [])
                # The migration creates the extension itself.
                for statement in dialect.render(
                    diff(empty_snapshot("postgresql"), build_snapshot(TABLES, dialect))
                ):
                    await conn.execute(statement.sql, [])

        loop.run(setup())
        yield loop, db
        loop.run(reset(db))
        loop.run(db.close())


def test_no_drift_and_pull_round_trip(
    runner: tuple[asyncio.Runner, Database], tmp_path: Path
) -> None:
    loop, db = runner
    live = loop.run(introspect(db))
    expected = build_snapshot(TABLES, dialect)
    assert diff(live, expected, compare_expressions=False) == []
    assert diff(live, expected) == []
    indexes = live["tables"][f"{SCHEMA}.items"]["indexes"]
    assert indexes["items_embedding_hnsw"].get("options") == {"m": "16", "ef_construction": "64"}

    source = render_schema_module(live)
    assert "embedding = vector(3)" in source and "half = halfvec(2)" in source
    path = tmp_path / "pulled_vec.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("pulled_vec", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pulled_vec"] = module
    spec.loader.exec_module(module)
    pulled = [
        v
        for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, Table) and v is not Table
    ]
    assert diff(live, build_snapshot(pulled, dialect)) == []


def test_nearest_neighbours(runner: tuple[asyncio.Runner, Database]) -> None:
    loop, db = runner

    async def scenario() -> None:
        [shelf] = await db.insert(Shelves).values({}).returning()
        points = {"x": [1.0, 0.0, 0.0], "y": [0.0, 1.0, 0.0], "xy": [1.0, 1.0, 0.0]}
        await db.insert(Items).values(
            [{"shelf_id": shelf.id, "label": k, "embedding": v} for k, v in points.items()]
        )
        query = [0.9, 0.1, 0.0]
        nearest = await db.select().from_(Items).order_by(l2_distance(Items.embedding, query))
        assert [i.label for i in nearest] == ["x", "xy", "y"]
        rows = await (
            db.select({"label": Items.label, "d": cosine_distance(Items.embedding, query)})
            .from_(Items)
            .order_by(cosine_distance(Items.embedding, query))
            .limit(1)
        )
        assert rows[0]["label"] == "x"
        assert isinstance(rows[0]["d"], float)
        best = await (
            db.select()
            .from_(Items)
            .order_by(inner_product(Items.embedding, [1, 1, 0]))  # negative: best first
            .limit(1)
        )
        assert best[0].label == "xy"
        await db.delete(Items).where(eq(Items.shelf_id, shelf.id))
        await db.delete(Shelves).where(eq(Shelves.id, shelf.id))

    loop.run(scenario())


finite4 = st.floats(width=32, allow_nan=False, allow_infinity=False)
finite2 = st.floats(width=16, allow_nan=False, allow_infinity=False)


@settings(
    max_examples=int(os.environ.get("MIZZLE_FUZZ_EXAMPLES", "200")),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    embedding=st.none() | st.lists(finite4, min_size=3, max_size=3),
    halves=st.none() | st.lists(finite2, min_size=2, max_size=2),
)
def test_vectors_round_trip(
    runner: tuple[asyncio.Runner, Database],
    embedding: list[float] | None,
    halves: list[float] | None,
) -> None:
    loop, db = runner

    async def scenario() -> None:
        [shelf] = await db.insert(Shelves).values({}).returning()
        [row] = await (
            db.insert(Items).values(shelf_id=shelf.id, embedding=embedding, half=halves).returning()
        )
        assert row.embedding == embedding  # float4 values come back exactly
        assert row.half == halves
        [selected] = await db.select().from_(Items).where(eq(Items.id, row.id))
        [loaded] = await db.query(Shelves).find_many(
            where=eq(Shelves.id, shelf.id), with_={"items": True}
        )
        assert loaded.items == [selected] == [row]  # also through nested JSON
        await db.delete(Items).where(eq(Items.id, row.id))
        await db.delete(Shelves).where(eq(Shelves.id, shelf.id))

    loop.run(scenario())

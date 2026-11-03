"""Bulk writes and exports: copy_into (COPY FROM), copy_from (COPY TO) and insert_many."""

import asyncio
import io
import time
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any, final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizzle import (
    Database,
    DatabaseError,
    QueryError,
    Table,
    UniqueViolation,
    asc,
    eq,
    placeholder,
    sql,
)
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.pg import integer, jsonb, serial, smallint, text
from tests.integration.test_fuzz_types import Owners, Things, check, runner, thing_values
from tests.integration.test_stress import assert_no_leak
from tests.models import Posts, Role, Users

__all__ = ["runner"]  # the fuzz harness fixture, shared with test_fuzz_types


@final
class Rows(Table, name="bulk_rows"):
    id = serial().primary_key()
    n = integer().not_null().unique()
    label = text().not_null()
    small = smallint()
    doc = jsonb[Any]()


DDL = """
DROP TABLE IF EXISTS bulk_rows;
CREATE TABLE bulk_rows (
    id serial PRIMARY KEY, n integer NOT NULL UNIQUE, label text NOT NULL, small smallint,
    doc jsonb
);
"""


@pytest.fixture
async def db(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    async with driver.acquire() as conn:
        await conn.execute(DDL, [])
    async with await Database.connect(
        pg_url, schema=[Rows, Users, Posts], pool_size=4, reset_connections="auto"
    ) as database:
        yield database
        assert_no_leak(database)
    async with driver.acquire() as conn:
        await conn.execute("DROP TABLE bulk_rows", [])


def rows(count: int, start: int = 0) -> Iterator[dict[str, object]]:
    return ({"n": i, "label": f"row {i}"} for i in range(start, start + count))


async def count(db: Database) -> int:
    [row] = await db.execute(sql("select count(*) as n from bulk_rows"))
    return int(str(row["n"]))


# -- copy_into ----------------------------------------------------------------------


async def test_copy_into_round_trips(db: Database) -> None:
    sent = [
        {"n": 1, "label": "plain", "small": 7, "doc": {"a": [1, None, "x"]}},
        {"n": 2, "label": "tab\tnew\nline\\ 'q' \"dq\"", "small": None, "doc": None},
        {"n": 3, "label": "", "small": -32768, "doc": [1.5, True]},
    ]
    assert await db.copy_into(Rows, sent) == 3
    got = await db.select().from_(Rows).order_by(asc(Rows.n))
    assert [{k: getattr(r, k) for k in sent[0]} for r in got] == sent
    # Enums (and server defaults for columns left out) through the shared test schema.
    assert await db.copy_into(Users, [{"email": "a@x.io", "role": Role.ADMIN}]) == 1
    [user] = await db.select().from_(Users)
    assert user.role is Role.ADMIN and user.created_at is not None


async def test_copy_into_columns_and_generators(db: Database) -> None:
    assert await db.copy_into(Rows, rows(500), columns=[Rows.n, Rows.label]) == 500
    assert await db.copy_into(Rows, iter([])) == 0
    assert await count(db) == 500


async def test_copy_into_inside_a_transaction(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.copy_into(Rows, rows(10))
            assert await tx.select().from_(Rows).where(eq(Rows.n, 3))
            raise RuntimeError
    assert await count(db) == 0


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"n": 1, "label": "x", "nope": 1}, "has no column"),
        ({"n": 1, "label": None}, "NOT NULL"),
        ({"n": "1", "label": "x"}, "expects int"),
        ({"n": 1}, "required"),
        ({"n": 1, "label": "x", "small": 1}, "sets small"),
        ({"label": "x"}, "required"),
        ({"n": 1, "label": sql("'x'")}, "SQL expressions"),
    ],
)
async def test_copy_into_checks_rows_like_insert(
    db: Database, bad: dict[str, object], message: str
) -> None:
    # The bad row comes after good ones, mid-COPY: nothing is stored.
    with pytest.raises(QueryError, match=message):
        await db.copy_into(Rows, [*rows(1000, start=10), bad])
    assert await count(db) == 0


async def test_copy_into_column_checks(db: Database) -> None:
    with pytest.raises(QueryError, match="required"):
        await db.copy_into(Rows, rows(1), columns=[Rows.n])
    with pytest.raises(QueryError, match="not a column"):
        await db.copy_into(Rows, rows(1), columns=[Users.email])
    with pytest.raises(QueryError, match="has no value for small"):
        await db.copy_into(Rows, rows(1), columns=[Rows.n, Rows.label, Rows.small])


async def test_copy_into_errors_hide_values(db: Database) -> None:
    secret = "s3cret-value"
    await db.copy_into(Rows, [{"n": 1, "label": secret}])
    with pytest.raises(UniqueViolation) as unique:
        await db.copy_into(Rows, [{"n": 1, "label": secret}])
    with pytest.raises(DatabaseError, match="value hidden") as overflow:
        await db.copy_into(Rows, [{"n": 2, "label": "x", "small": 99999}])
    for error in (unique.value, overflow.value):
        assert secret not in str(error) and "99999" not in str(error)
    assert await count(db) == 1


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(sent=st.lists(thing_values, min_size=1, max_size=20))
def test_copy_into_every_type_round_trips(
    runner: tuple[asyncio.Runner, Database], sent: list[dict[str, object]]
) -> None:
    loop, db = runner
    columns = [c for c in Things.__table__.columns.values() if c.attr != "id"]

    async def scenario() -> None:
        [owner] = await db.insert(Owners).values({}).returning()
        full = [{c.attr: row.get(c.attr) for c in columns} | {"owner_id": owner.id} for row in sent]
        assert await db.copy_into(Things, full) == len(full)
        got = (
            await db.select().from_(Things).where(eq(Things.owner_id, owner.id)).order_by(Things.id)
        )
        for values, row in zip(sent, got, strict=True):
            check(values, row, "copy_into")
        await db.delete(Owners).where(eq(Owners.id, owner.id))

    loop.run(scenario())


# -- copy_from ----------------------------------------------------------------------


async def test_copy_from_exports(db: Database, tmp_path: Path) -> None:
    await db.copy_into(Rows, [{"n": 1, "label": 'a,"b"', "doc": {"k": 1}}])
    await db.copy_into(Rows, rows(3, start=2))
    query = db.select(Rows.n, Rows.label).from_(Rows).where(eq(Rows.label, 'a,"b"'))

    buffer = io.BytesIO()
    assert await db.copy_from(query, buffer, header=True) == 1
    assert buffer.getvalue() == b'n,label\n1,"a,""b"""\n'

    path = tmp_path / "rows.csv"
    assert await db.copy_from(db.select().from_(Rows).order_by(Rows.n), str(path)) == 4
    assert path.read_text().splitlines()[0] == '1,1,"a,""b""",,"{""k"": 1}"'

    chunks: list[bytes] = []

    async def write(chunk: bytes) -> None:
        chunks.append(chunk)

    exported = await db.copy_from(db.query(Rows).find_many(), write, format="text")
    assert exported == 4 and b"".join(chunks).count(b"\n") == 4
    assert await db.copy_from(sql("select 1 as one"), write, format="binary") == 1


async def test_copy_from_errors(db: Database) -> None:
    with pytest.raises(DatabaseError, match="DivisionByZero"):
        await db.copy_from(sql("select 1 / 0"), io.BytesIO())
    with pytest.raises(QueryError, match="placeholders"):
        await db.copy_from(
            db.select().from_(Rows).where(eq(Rows.n, placeholder("n"))), io.BytesIO()
        )


# -- insert_many --------------------------------------------------------------------


async def test_insert_many_beyond_the_parameter_limit(db: Database) -> None:
    # 2 values per row: 40k rows need 80k parameters, more than one statement can take.
    assert await db.insert_many(Rows, rows(40_000)) == 40_000
    assert await count(db) == 40_000
    inserted = await db.insert_many(Rows, rows(5, start=50_000), batch_size=2, returning=True)
    assert [r.n for r in inserted] == list(range(50_000, 50_005))
    assert all(isinstance(r.id, int) for r in inserted)
    assert await db.insert_many(Rows, []) == 0
    assert await db.insert_many(Rows, [], returning=True) == []


async def test_insert_many_is_one_transaction(db: Database) -> None:
    with pytest.raises(UniqueViolation):
        await db.insert_many(Rows, [*rows(30_000), {"n": 5, "label": "duplicate"}])
    with pytest.raises(QueryError, match="NOT NULL"):
        await db.insert_many(Rows, [*rows(30_000), {"n": -1, "label": None}])
    with pytest.raises(QueryError, match="batch_size"):
        await db.insert_many(Rows, rows(1), batch_size=0)
    assert await count(db) == 0


async def test_insert_many_inside_a_transaction(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.insert_many(Rows, rows(20_000))
            raise RuntimeError
    assert await count(db) == 0


async def test_copy_is_fast_for_a_million_rows(db: Database) -> None:
    async with db.driver.acquire() as conn:
        await conn.execute("ALTER TABLE bulk_rows SET UNLOGGED", [])
    start = time.perf_counter()
    assert await db.copy_into(Rows, rows(1_000_000)) == 1_000_000
    copy_rate = 1_000_000 / (time.perf_counter() - start)

    start = time.perf_counter()
    assert await db.insert_many(Rows, rows(200_000, start=1_000_000)) == 200_000
    insert_rate = 200_000 / (time.perf_counter() - start)

    print(f"copy_into: {copy_rate:,.0f} rows/s, insert_many: {insert_rate:,.0f} rows/s")
    assert copy_rate > insert_rate
    assert await count(db) == 1_200_000


async def test_copy_into_text_only_types_falls_back_to_insert(
    driver: PostgresDriver, pg_url: str
) -> None:
    from mizzle import Table
    from mizzle.pg import tsvector

    @final
    class Docs(Table, name="bulk_docs"):
        id = serial().primary_key()
        terms = tsvector()

    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS bulk_docs;"
            " CREATE TABLE bulk_docs (id serial PRIMARY KEY, terms tsvector)",
            [],
        )
    try:
        async with await Database.connect(pg_url, schema=[Docs]) as db:
            # asyncpg's binary COPY has no tsvector encoder: copy_into() uses INSERT instead.
            assert await db.copy_into(Docs, [{"terms": "a b"}, {"terms": "c"}]) == 2
            assert sorted(d.terms or "" for d in await db.select().from_(Docs)) == [
                "'a' 'b'",
                "'c'",
            ]
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP TABLE bulk_docs", [])

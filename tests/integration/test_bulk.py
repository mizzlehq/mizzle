"""Bulk writes: insert_many."""

import datetime as dt
from collections.abc import AsyncGenerator, Iterator
from typing import Any, final

import pytest

from mizzle import (
    Database,
    QueryError,
    Table,
    UniqueViolation,
    eq,
    excluded,
    ne,
    sql,
)
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.pg import integer, jsonb, serial, smallint, text
from tests.integration.test_stress import assert_no_leak
from tests.models import Posts, Users


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


async def test_insert_many_on_conflict(db: Database) -> None:
    await db.insert_many(Rows, rows(3))
    skip = db.insert(Rows).on_conflict_do_nothing(Rows.n)
    # 20k rows: several statements, each with the conflict clause.
    assert await db.insert_many(Rows, rows(20_000), on_conflict=skip) == 19_997
    upsert = db.insert(Rows).on_conflict_do_update(
        target=Rows.n, set_={"label": excluded(Rows.label), "small": 7}
    )
    changed = await db.insert_many(
        Rows,
        [{"n": 1, "label": "new 1"}, {"n": 30_000, "label": "new"}],
        on_conflict=upsert,
        batch_size=1,
        returning=True,
    )
    assert [(r.n, r.label, r.small) for r in changed] == [(1, "new 1", 7), (30_000, "new", None)]
    assert await count(db) == 20_001
    with pytest.raises(QueryError, match="insert into Rows"):
        await db.insert_many(Rows, rows(1), on_conflict=db.insert(Users).on_conflict_do_nothing())  # pyright: ignore[reportArgumentType]
    with pytest.raises(QueryError, match="values"):
        await db.insert_many(Rows, rows(1), on_conflict=skip.values(n=1, label="x"))


async def test_insert_many_on_conflict_near_the_parameter_limit(db: Database) -> None:
    # Each row takes 2 parameters; the DO UPDATE clause takes more. Batches leave room.
    upsert = db.insert(Rows).on_conflict_do_update(
        target=Rows.n, set_={"label": "x", "small": 1}, where=ne(Rows.label, "y")
    )
    assert await db.insert_many(Rows, rows(16_383), on_conflict=upsert) == 16_383


async def test_timestamptz_infinity_is_aware_everywhere(db: Database) -> None:
    # asyncpg reads timestamptz infinity as a naive datetime.max; mizzle makes it aware.
    top, bottom = dt.datetime.max.replace(tzinfo=dt.UTC), dt.datetime.min.replace(tzinfo=dt.UTC)
    [a] = await db.insert(Users).values(email="a", created_at=top).returning()
    await db.insert(Users).values(email="b", created_at=bottom)
    await db.execute(sql("insert into users (email, created_at) values ('c', 'infinity')"))
    await db.insert_many(Users, [{"email": "d", "created_at": dt.datetime.max}])  # naive too
    [b] = await db.select().from_(Users).where(eq(Users.email, "b"))
    assert a.created_at == top and a.created_at.tzinfo is not None
    assert b.created_at == bottom and b.created_at.tzinfo is not None
    await db.insert(Posts).values([{"author_id": a.id, "title": "t"}])

    def stamps(users: list[Users]) -> list[dt.datetime]:
        return [u.created_at for u in sorted(users, key=lambda u: u.email)]

    expected = [top, bottom, top, top]
    assert all(s.tzinfo is not None for s in expected)
    assert stamps(await db.select().from_(Users)) == expected
    streamed = [u async for u in db.select().from_(Users).stream(batch_size=2)]
    assert stamps(streamed) == expected
    [post] = await db.query(Posts).find_many(with_={"author": True})
    assert post.author is not None
    assert post.author.created_at == top and post.author.created_at.tzinfo is not None
    selected = (
        await db.select({"at": Users.created_at}).from_(Users).where(eq(Users.created_at, top))
    )
    assert len(selected) == 3  # stored as infinity, so equal to the infinity read back

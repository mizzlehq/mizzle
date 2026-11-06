"""Round-2 query fixes against a live database: IntEnum labels, JSON null, per-run defaults."""

import enum
import itertools
from collections.abc import AsyncGenerator

import pytest

from mizzle import Database, eq, jsonb_build_object, placeholder
from mizzle.core.schema.table import Table
from mizzle.core.schema.types import text
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.dialects.postgres.types import pg_enum, serial
from tests.models import Users


class Lvl(enum.IntEnum):
    LOW = 1
    HIGH = 2


_ticket = itertools.count(1)


class Things(Table, name="qc_things"):
    id = serial().primary_key()
    color = text()
    level = pg_enum(Lvl)
    tag = text().not_null().unique().default_fn(lambda: f"tag-{next(_ticket)}")


@pytest.fixture
async def things(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS qc_things; DROP TYPE IF EXISTS lvl; "
            "CREATE TYPE lvl AS ENUM ('1', '2'); "
            "CREATE TABLE qc_things (id serial PRIMARY KEY, color text, level lvl, "
            "tag text NOT NULL UNIQUE)",
            [],
        )
    yield await Database.connect(pg_url, schema=[Things], pool_size=2)
    async with driver.acquire() as conn:  # other tests introspect the whole schema
        await conn.execute("DROP TABLE qc_things; DROP TYPE lvl", [])


async def test_an_int_enum_round_trips(things: Database) -> None:
    # B49: the int value was sent to a text enum: "a query parameter has the wrong type".
    async with things as db:
        await db.insert(Things).values({"level": Lvl.HIGH})
        [row] = await db.select(Things.level).from_(Things).where(eq(Things.level, Lvl.HIGH))
        assert row == (Lvl.HIGH,)


async def test_prepared_and_reawaited_inserts_get_fresh_defaults(things: Database) -> None:
    # B43: default_fn ran once in values(), so the second run hit the unique constraint.
    async with things as db:
        prepared = db.insert(Things).values({"color": placeholder("c")}).prepare()
        await prepared.execute(c="p1")
        await prepared.execute(c="p2")
        query = db.insert(Things).values({"color": "q"})
        await query
        await query
        tags = await db.select(Things.tag).from_(Things)
        assert len({t for (t,) in tags}) == 4


async def test_jsonb_build_object_takes_none(db: Database) -> None:
    # B50: "could not determine data type of parameter $2".
    await db.insert(Users).values({"email": "json@x"})
    rows = await db.select(jsonb_build_object("k", None)).from_(Users).limit(1)
    assert rows == [({"k": None},)]

"""Round-2 query fixes against a live database: IntEnum labels, JSON null, per-run defaults."""

import enum
import itertools
from collections.abc import AsyncGenerator

import pytest

from mizzle import Database, eq, jsonb_build_object, many, one, placeholder
from mizzle.core.schema.table import Table
from mizzle.core.schema.types import integer, text
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.dialects.postgres.types import (
    array,
    box,
    cidr,
    circle,
    daterange,
    hstore,
    int4multirange,
    int4range,
    line,
    lseg,
    numrange,
    path,
    pg_enum,
    point,
    polygon,
    serial,
    tstzrange,
    varbit,
)
from tests.models import Users


class Lvl(enum.IntEnum):
    LOW = 1
    HIGH = 2


_ticket = itertools.count(1)


class Things(Table, name="qc_things"):
    id = serial().primary_key()
    color = text()
    level = pg_enum(Lvl)
    levels = array(pg_enum(Lvl))
    tag = text().not_null().unique().default_fn(lambda: f"tag-{next(_ticket)}")


@pytest.fixture
async def things(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS qc_things; DROP TYPE IF EXISTS lvl; "
            "CREATE TYPE lvl AS ENUM ('1', '2'); "
            "CREATE TABLE qc_things (id serial PRIMARY KEY, color text, level lvl, levels lvl[], "
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


async def test_array_functions_decode_enum_elements(things: Database) -> None:
    # C31: unnest/array_append/array_cat/array_remove returned labels ('1'), not members.
    from mizzle import array_append, array_cat, array_remove, unnest

    async with things as db:
        await db.insert(Things).values({"levels": [Lvl.LOW, Lvl.HIGH]})
        rows = await db.select(unnest(Things.levels)).from_(Things)
        assert rows == [(Lvl.LOW,), (Lvl.HIGH,)]
        [(appended, joined, removed)] = await db.select(
            array_append(Things.levels, Lvl.LOW),
            array_cat(Things.levels, Things.levels),
            array_remove(Things.levels, Lvl.LOW),
        ).from_(Things)
        assert appended == [Lvl.LOW, Lvl.HIGH, Lvl.LOW]
        assert joined == [Lvl.LOW, Lvl.HIGH, Lvl.LOW, Lvl.HIGH]
        assert removed == [Lvl.HIGH]


class Nest(Table, name="qc_nest"):
    id = serial().primary_key()
    kids = many(lambda: NestKid)


class NestKid(Table, name="qc_nest_kid"):
    id = serial().primary_key()
    nest_id = integer().not_null().references(lambda: Nest.id)
    ir = int4range()
    nr = numrange()
    tz = tstzrange()
    dr = daterange()
    mr = int4multirange()
    net = cidr()
    pt = point()
    bx = box()
    ln = line()
    ls = lseg()
    pa = path()
    pg = polygon()
    ci = circle()
    bits = varbit()
    hs = hstore()
    nest = one(lambda: Nest, fields=lambda: [NestKid.nest_id], references=lambda: [Nest.id])


async def test_nested_rows_decode_like_plain_rows(driver: PostgresDriver, pg_url: str) -> None:
    # C30: in a nested (relational) row these came back as str (hstore as dict), while a
    # plain select gives Range, IPv4Network, Point, BitString...
    async with driver.acquire() as conn:
        await conn.execute(
            "CREATE EXTENSION IF NOT EXISTS hstore; "
            "DROP TABLE IF EXISTS qc_nest_kid, qc_nest; "
            "CREATE TABLE qc_nest (id serial PRIMARY KEY); "
            "CREATE TABLE qc_nest_kid (id serial PRIMARY KEY, "
            "nest_id int NOT NULL REFERENCES qc_nest, ir int4range, nr numrange, "
            "tz tstzrange, dr daterange, mr int4multirange, net cidr, pt point, bx box, "
            "ln line, ls lseg, pa path, pg polygon, ci circle, bits varbit, hs hstore); "
            "INSERT INTO qc_nest DEFAULT VALUES; "
            "INSERT INTO qc_nest_kid (nest_id, ir, nr, tz, dr, mr, net, pt, bx, ln, ls, pa, "
            "pg, ci, bits, hs) VALUES (1, '[1,5)', '(1.25,2.5]', "
            "'[2026-01-01 10:00+00,)', 'empty', '{[1,3),[5,7)}', '10.0.0.0/8', '(1.5,2)', "
            "'(3,4),(1,2)', '{1,2,3}', '[(1,2),(3,4)]', '[(0,0),(1,1)]', "
            "'((0,0),(1,0),(1,1))', '<(1,2),3>', B'0101', 'a=>1'), "
            "(1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
            "NULL, NULL, NULL)",
            [],
        )
    try:
        async with await Database.connect(pg_url, schema=[Nest, NestKid], pool_size=2) as db:
            plain = await db.query(NestKid).find_many(order_by=NestKid.id)
            [parent] = await db.query(Nest).find_many(with_={"kids": True})
            assert parent.kids == plain
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP TABLE qc_nest_kid, qc_nest", [])

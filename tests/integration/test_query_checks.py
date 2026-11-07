"""Round-2 query fixes against a live database: IntEnum labels, JSON null, per-run defaults."""

import datetime
import enum
import itertools
import types
from collections.abc import AsyncGenerator, Callable

import pytest

from mizzle import Database, eq, jsonb_build_object, many, one, placeholder, sql
from mizzle.core.schema.column import ColumnBase
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
    json,
    jsonb,
    line,
    lseg,
    numrange,
    path,
    pg_composite,
    pg_domain,
    pg_enum,
    point,
    polygon,
    serial,
    tsrange,
    tstzrange,
    varbit,
)
from mizzle.pg import (
    bit,
    bytea,
    date,
    datemultirange,
    double_precision,
    inet,
    interval,
    numeric,
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


class Boxed(Table, name="qc_boxed", schema="qc_app"):
    id = serial().primary_key()
    levels = array(pg_enum(Lvl, type_name="LvlType", schema="qc_app"))
    r = int4range()
    s = text()


async def test_container_operators_on_enum_arrays_and_ranges(
    driver: PostgresDriver, pg_url: str
) -> None:
    # C38: contains() cast to an unquoted, unqualified type: type "lvltype[]" does not exist.
    # C39: contains(range, 3) cast 3 to bigint: operator does not exist: int4range @> bigint.
    from asyncpg import Range

    from mizzle import contained_by, contains, overlaps, string_agg

    async with driver.acquire() as conn:
        await conn.execute(
            "DROP SCHEMA IF EXISTS qc_app CASCADE; CREATE SCHEMA qc_app; "
            "CREATE TYPE qc_app.\"LvlType\" AS ENUM ('1', '2'); "
            "CREATE TABLE qc_app.qc_boxed (id serial PRIMARY KEY, "
            'levels qc_app."LvlType"[], r int4range, s text)',
            [],
        )
    try:
        async with await Database.connect(pg_url, schema=[Boxed], pool_size=1) as db:
            await db.insert(Boxed).values({"levels": [Lvl.LOW], "r": Range(1, 5), "s": "a"})
            for cond in (
                contains(Boxed.levels, [Lvl.LOW]),
                contained_by(Boxed.levels, [Lvl.LOW, Lvl.HIGH]),
                overlaps(Boxed.levels, [Lvl.LOW]),
                contains(Boxed.r, 3),
            ):
                assert await db.select(Boxed.id).from_(Boxed).where(cond) == [(1,)]
            assert await db.select(Boxed.id).from_(Boxed).where(contains(Boxed.r, 9)) == []
            assert await db.select(string_agg(Boxed.s, ",")).from_(Boxed) == [("a",)]
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP SCHEMA qc_app CASCADE", [])


async def test_writes_through_an_alias_and_union_order_by_a_dict_key(db: Database) -> None:
    # C41: UPDATE/DELETE/INSERT on an alias wrote UPDATE "boss": relation does not exist.
    # C42: a UNION of dict projections ordered by a column wrote ORDER BY "email".
    from mizzle import alias, desc, lower

    boss = alias(Users, "boss")
    await db.insert(boss).values({"email": "b@x"})
    await db.insert(Users).values({"email": "a@x"})
    await db.update(boss).set({"bio": "hi"}).where(eq(boss.email, "b@x"))
    assert await db.select(Users.bio).from_(Users).where(eq(Users.email, "b@x")) == [("hi",)]
    left = db.select({"mail": Users.email}).from_(Users)
    right = db.select({"mail": Users.email}).from_(Users)
    rows = await left.union(right).order_by(desc(Users.email))
    assert rows == [{"mail": "b@x"}, {"mail": "a@x"}]
    rows = await left.union(right).order_by(lower(Users.email))
    assert rows == [{"mail": "a@x"}, {"mail": "b@x"}]
    await db.delete(boss).where(eq(boss.email, "b@x"))
    assert await db.select(Users.email).from_(Users) == [("a@x",)]


class Camel(Table, name="qc_camel"):
    id = serial().primary_key()
    userLevel = pg_enum(Lvl)  # camelCase on purpose: snake_case casing


async def test_cte_columns_decode_under_snake_case(driver: PostgresDriver, pg_url: str) -> None:
    # C40: with casing="snake_case" the CTE column user_level was not traced back to
    # userLevel, so it came back as the label '2', not Lvl.HIGH.
    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS qc_camel; DROP TYPE IF EXISTS lvl; "
            "CREATE TYPE lvl AS ENUM ('1', '2'); "
            "CREATE TABLE qc_camel (id serial PRIMARY KEY, user_level lvl)",
            [],
        )
    try:
        async with await Database.connect(
            pg_url, schema=[Camel], pool_size=1, casing="snake_case"
        ) as db:
            await db.insert(Camel).values({"userLevel": Lvl.HIGH})
            c = db.select(Camel.id, Camel.userLevel).from_(Camel).cte("c")
            assert await db.with_(c).select(c.c("user_level", Lvl)).from_(c) == [(Lvl.HIGH,)]
            r = db.insert(Camel).values({"userLevel": Lvl.LOW}).returning().cte("r")
            assert await db.with_(r).select(r.c("user_level", Lvl)).from_(r) == [(Lvl.LOW,)]
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP TABLE qc_camel; DROP TYPE lvl", [])


class Dom(Table, name="qc_dom"):
    id = serial().primary_key()
    kids = many(lambda: DomKid)


class DomKid(Table, name="qc_dom_kid"):
    id = serial().primary_key()
    dom_id = integer().not_null().references(lambda: Dom.id)
    day = pg_domain("qc_day", datetime.date)
    at = pg_composite("qc_addr", dict)
    dom = one(lambda: Dom, fields=lambda: [DomKid.dom_id], references=lambda: [Dom.id])


@pytest.fixture
async def doms(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS qc_dom_kid, qc_dom; DROP DOMAIN IF EXISTS qc_day; "
            "DROP TYPE IF EXISTS qc_addr; "
            "CREATE DOMAIN qc_day AS date; CREATE TYPE qc_addr AS (city text, zip int); "
            "CREATE TABLE qc_dom (id serial PRIMARY KEY); "
            "CREATE TABLE qc_dom_kid (id serial PRIMARY KEY, "
            "dom_id int NOT NULL REFERENCES qc_dom, day qc_day, at qc_addr); "
            "INSERT INTO qc_dom DEFAULT VALUES; "
            "INSERT INTO qc_dom_kid (dom_id, day, at) VALUES (1, '2026-01-02', ROW('Oslo', 150))",
            [],
        )
    try:
        async with await Database.connect(pg_url, schema=[Dom, DomKid], pool_size=1) as db:
            yield db
    finally:
        async with driver.acquire() as conn:
            await conn.execute(
                "DROP TABLE qc_dom_kid, qc_dom; DROP DOMAIN qc_day; DROP TYPE qc_addr", []
            )


async def test_a_date_domain_decodes(doms: Database) -> None:
    # Q1: the decoder ran date(date(...)): TypeError on every read.
    assert await doms.select(DomKid.day).from_(DomKid) == [(datetime.date(2026, 1, 2),)]


async def test_a_composite_in_a_nested_row_decodes(doms: Database) -> None:
    # Q2: the composite came back as a JSON object, which _from_json refused (QueryError).
    await doms.insert(DomKid).values({"dom_id": 1, "at": {"city": "Bergen", "zip": 5003}})  # Q9
    plain = await doms.query(DomKid).find_many(order_by=DomKid.id)
    [parent] = await doms.query(Dom).find_many(with_={"kids": True})
    assert parent.kids == plain
    assert plain[0].at == {"city": "Oslo", "zip": 150}


async def test_alias_caches_returning_and_merge(db: Database) -> None:
    # Q4: each alias() call made a new class, so the decoder/INSERT caches grew per call.
    # Q6: returning() on an alias gave alias-class rows. Q7: MERGE INTO "boss" (no table).
    from mizzle import alias
    from mizzle.core.compiler import _INSERT_SQL
    from mizzle.core.query.base import _ROW_DECODERS

    sizes: list[tuple[int, int]] = []
    for i in range(2):
        boss = alias(Users, "boss")
        [row] = await db.insert(boss).values({"email": f"{i}@x"}).returning()
        assert type(row) is Users
        await db.select(Users, boss).from_(Users).join(boss, eq(boss.id, Users.id))
        sizes.append((len(_ROW_DECODERS), len(_INSERT_SQL)))
    assert sizes[0] == sizes[1]
    boss = alias(Users, "boss")
    source = db.select(Users.id).from_(Users).subquery("src")
    sid = source.c("id", int)
    [version] = await db.execute(sql("SHOW server_version_num"))
    if int(str(version["server_version_num"])) < 150000:
        return  # MERGE needs PostgreSQL 15
    merge = db.merge(boss).using(source).on(eq(boss.id, sid)).when_matched_then_delete()
    assert await merge == 2


async def test_placeholders_in_any_in_and_containers_are_encoded(things: Database) -> None:
    # Q5: the placeholder list was sent raw ([2] to an enum array) and in_() gave IN ($1).
    from mizzle import any_, contains, in_, not_in

    async with things as db:
        await db.insert(Things).values({"level": Lvl.HIGH, "levels": [Lvl.HIGH]})
        for cond, found in (
            (in_(Things.level, placeholder("p")), [(1,)]),
            (not_in(Things.level, placeholder("p")), []),
            (eq(Things.level, any_(placeholder("p"))), [(1,)]),
            (contains(Things.levels, placeholder("p")), [(1,)]),
        ):
            prepared = db.select(Things.id).from_(Things).where(cond).prepare()
            assert await prepared.execute(p=[Lvl.HIGH]) == found


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


async def test_enum_and_json_values_without_a_column_are_typed(db: Database) -> None:
    # Q8: an IntEnum in CASE came back as int; a plain Enum, dict or list was an untyped
    # parameter ("could not determine data type").
    from mizzle import case_, is_not_null, jsonb_build_array

    await db.insert(Users).values({"email": "q8@x"})
    yes = is_not_null(Users.id)
    rows = await db.select(
        case_().when(yes, Lvl.HIGH).else_(Lvl.LOW),
        case_().when(yes, Color.RED).else_(Color.BLUE),
        case_().when(yes, {"a": 1}).else_({"b": 2}),
        jsonb_build_object("c", Color.RED, "d", {"x": [1]}),
        jsonb_build_array(Lvl.HIGH, [1, 2]),
    ).from_(Users)
    assert rows == [(Lvl.HIGH, Color.RED, {"a": 1}, {"c": "red", "d": {"x": [1]}}, [2, [1, 2]])]
    # Q4-13: a tuple was an untyped parameter too (DataError)
    assert await db.select(case_().when(yes, (1, 2))).from_(Users) == [([1, 2],)]


class DateNest(Table, name="qc_date_nest"):
    id = serial().primary_key()
    kids = many(lambda: DateKid)


class DateKid(Table, name="qc_date_kid"):
    id = serial().primary_key()
    nest_id = integer().not_null().references(lambda: DateNest.id)
    ts = tsrange()
    dr = daterange()
    nest = one(lambda: DateNest, fields=lambda: [DateKid.nest_id], references=lambda: [DateNest.id])


async def test_nested_ranges_ignore_the_date_style(driver: PostgresDriver, pg_url: str) -> None:
    # Q13: range bounds were sent as lower(r)::text, which follows DateStyle ('02/01/2026').
    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS qc_date_kid, qc_date_nest; "
            "CREATE TABLE qc_date_nest (id serial PRIMARY KEY); "
            "CREATE TABLE qc_date_kid (id serial PRIMARY KEY, "
            "nest_id int NOT NULL REFERENCES qc_date_nest, ts tsrange, dr daterange); "
            "INSERT INTO qc_date_nest DEFAULT VALUES; "
            "INSERT INTO qc_date_kid (nest_id, ts, dr) "
            "VALUES (1, '[2026-01-02 10:00,2026-03-04)', '[2026-01-02,2026-03-04)')",
            [],
        )
    try:
        async with await Database.connect(
            pg_url,
            schema=[DateNest, DateKid],
            pool_size=1,
            server_settings={"DateStyle": "SQL, DMY"},
        ) as db:
            plain = await db.query(DateKid).find_many()
            [parent] = await db.query(DateNest).find_many(with_={"kids": True})
            assert parent.kids == plain
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP TABLE qc_date_kid, qc_date_nest", [])


class Hue(enum.Enum):
    RED = "red"
    BLUE = "blue"


_BITS = "101100111010"
_ROUND_TRIPS: list[tuple[str, Callable[[], ColumnBase], str]] = [
    ("int4range", int4range, "'[1,5)'"),
    ("int4range", int4range, "'empty'"),
    ("numrange", numrange, "'[1.25,2.5]'"),
    ("daterange", daterange, "'[2026-01-01,infinity)'"),
    ("tstzrange", tstzrange, "'[2026-01-01 00:00+00,)'"),
    ("int4multirange", int4multirange, "'{[1,3),[5,8)}'"),
    ("int4multirange", int4multirange, "'{}'"),
    ("datemultirange", datemultirange, "'{[2026-01-01,2026-02-01)}'"),
    ("int4range[]", lambda: array(int4range()), "ARRAY['[1,2)'::int4range, 'empty']"),
    *((f"varbit({n})", varbit, f"B'{_BITS[:n]}'") for n in range(1, 13)),
    ("bit(5)", lambda: bit(5), "B'10101'"),
    ("varbit[]", lambda: array(varbit()), "ARRAY[B'10101'::varbit, B'']"),
    ("point", point, "'(1.5,-2)'"),
    ("point[]", lambda: array(point()), "ARRAY['(1,2)'::point]"),
    ("line", line, "'{1,2,3}'"),
    ("lseg", lseg, "'[(0,0),(1.25,1)]'"),
    ("box", box, "'((3,4),(1,2))'"),
    ("path", path, "'[(0,0),(1,1)]'"),
    ("path", path, "'((0,0),(1,1),(1,0))'"),
    ("polygon", polygon, "'((0,0),(1,1),(1,0))'"),
    ("circle", circle, "'<(1,2),3>'"),
    ("numeric", numeric, "'NaN'"),
    ("numeric", numeric, "'Infinity'"),
    ("numeric", numeric, "'-Infinity'"),
    ("numeric", numeric, "'1.50'"),
    ("numeric(10,2)", lambda: numeric(10, 2), "'NaN'"),
    ("numeric[]", lambda: array(numeric()), "ARRAY['NaN'::numeric, 2]"),
    ("double precision", double_precision, "'Infinity'"),
    ("double precision", double_precision, "'NaN'"),
    ("date", date, "'infinity'"),
    ("hue", lambda: pg_enum(Hue), "'red'"),
    ("hue[]", lambda: array(pg_enum(Hue)), "ARRAY['red', 'blue']::hue[]"),
    ("jsonb", jsonb, """'{"a": [1, null, "x"]}'"""),
    ("json", json, """'[1, "two"]'"""),
    ("cidr", cidr, "'10.0.0.0/8'"),
    ("inet", inet, "'10.1.2.3/16'"),
    ("interval", interval, "'-1 day 00:00:01'"),
    ("bytea", bytea, "'\\x00ff27'::bytea"),
]


@pytest.mark.parametrize(("sql_type", "column", "literal"), _ROUND_TRIPS)
async def test_select_models_round_trip_every_column_through_json(
    driver: PostgresDriver,
    pg_url: str,
    sql_type: str,
    column: Callable[[], ColumnBase],
    literal: str,
) -> None:
    # Q4-9/Q4-10/Q4-13: multiranges, BitStrings ('1010 1'), Point/Box, Decimal('NaN') broke
    # the JSON round trip of a value read from the database.
    from mizzle.pydantic import select_model

    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS qc_rt; DROP TYPE IF EXISTS hue; "
            "CREATE TYPE hue AS ENUM ('red', 'blue'); "
            f"CREATE TABLE qc_rt (id serial PRIMARY KEY, v {sql_type}); "
            f"INSERT INTO qc_rt (v) VALUES ({literal})",
            [],
        )
    body: dict[str, object] = {"id": serial().primary_key(), "v": column()}
    rt = types.new_class("Rt", (Table,), {"name": "qc_rt"}, lambda ns: ns.update(body))
    try:
        async with await Database.connect(pg_url, schema=[rt], pool_size=1) as db:
            [(value,)] = await db.select(rt.v).from_(rt)
        model = select_model(rt)
        dumped = model.model_validate({"id": 1, "v": value}).model_dump_json()
        back = model.model_validate_json(dumped).model_dump()["v"]
        assert back == value or repr(back) == repr(value), dumped
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP TABLE qc_rt; DROP TYPE hue", [])


class JsonArrays(Table, name="qc_json_arrays"):
    id = serial().primary_key()
    jb = array(jsonb())
    js = array(json())


async def test_each_json_array_element_is_one_document(driver: PostgresDriver, pg_url: str) -> None:
    # Q4-5: a list element became an array dimension: [[1]] was stored as a 2-D jsonb[]
    # (jb[1] IS NULL) and [{"a": 1}, [1]] failed with a DataError.
    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS qc_json_arrays; "
            "CREATE TABLE qc_json_arrays (id serial PRIMARY KEY, jb jsonb[], js json[])",
            [],
        )
    try:
        async with await Database.connect(pg_url, schema=[JsonArrays], pool_size=1) as db:
            for value in ([[1]], [{"a": 1}, [1]], [[1, [2]], None, "x", 1], []):
                [row] = await db.insert(JsonArrays).values({"jb": value, "js": value}).returning()
                assert (row.jb, row.js) == (value, value)
                [dims] = await db.execute(
                    sql(f"SELECT array_ndims(jb) AS n FROM qc_json_arrays WHERE id = {row.id}")
                )
                assert dims == {"n": 1 if value else None}
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP TABLE qc_json_arrays", [])

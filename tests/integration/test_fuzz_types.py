"""Random values for every column type must come back unchanged through every read path:
SELECT, RETURNING, and nested relational rows (which travel as JSON and are converted back).
"""

import asyncio
import datetime as dt
import enum
import ipaddress
import math
import os
import struct
from collections.abc import Generator
from decimal import Decimal
from typing import Any, final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizzle import Database, Table, eq, many, one
from mizzle.pg import (
    array,
    bigint,
    boolean,
    bytea,
    date,
    double_precision,
    inet,
    integer,
    interval,
    json,
    jsonb,
    numeric,
    pg_enum,
    real,
    serial,
    smallint,
    text,
    time,
    timestamp,
    uuid,
    varchar,
)


class Mood(enum.Enum):
    HAPPY = "happy"
    SAD = "sad"
    WEIRD = "we ird'\"\\"


@final
class Owners(Table, name="fuzz_owners"):
    id = serial().primary_key()
    things = many(lambda: Things)


@final
class Things(Table, name="fuzz_things"):
    id = serial().primary_key()
    owner_id = integer().not_null().references(lambda: Owners.id, on_delete="cascade")
    small = smallint()
    int_ = integer("int_")
    big = bigint()
    txt = text()
    short = varchar(20)
    flag = boolean()
    f4 = real()
    f8 = double_precision()
    money = numeric(30, 8)
    any_num = numeric()
    day = date()
    at = timestamp()
    at_tz = timestamp(tz=True)
    clock = time()
    clock_tz = time(tz=True)
    span = interval()
    uid = uuid()
    blob = bytea()
    addr = inet()
    doc = jsonb[Any]()
    raw_doc = json[Any]()
    mood = pg_enum(Mood, type_name="fuzz_mood")
    ints = array(integer())
    texts = array(text())
    moods = array(pg_enum(Mood, type_name="fuzz_mood"))
    owner = one(lambda: Owners, fields=lambda: [Things.owner_id], references=lambda: [Owners.id])


DDL = """
DROP TABLE IF EXISTS fuzz_things, fuzz_owners;
DROP TYPE IF EXISTS fuzz_mood;
CREATE TYPE fuzz_mood AS ENUM ('happy', 'sad', 'we ird''"\\');
CREATE TABLE fuzz_owners (id serial PRIMARY KEY);
CREATE TABLE fuzz_things (
    id serial PRIMARY KEY,
    owner_id integer NOT NULL REFERENCES fuzz_owners(id) ON DELETE CASCADE,
    small smallint, int_ integer, big bigint, txt text, short varchar(20), flag boolean,
    f4 real, f8 double precision, money numeric(30, 8), any_num numeric, day date,
    at timestamp, at_tz timestamptz, clock time, clock_tz timetz, span interval, uid uuid,
    blob bytea, addr inet, doc jsonb, raw_doc json, mood fuzz_mood, ints integer[],
    texts text[], moods fuzz_mood[]
);
"""

# -- value strategies (the full ranges PostgreSQL and Python can both hold) --------------

# PostgreSQL text cannot hold NUL; UTF-8 cannot hold lone surrogates.
no_nul = st.characters(exclude_characters="\x00", exclude_categories=["Cs"])
texts = st.text(no_nul)
utc_offsets = st.integers(-14 * 60, 14 * 60).map(lambda m: dt.timezone(dt.timedelta(minutes=m)))
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**63), 2**63)
    | st.floats(allow_nan=False, allow_infinity=False)
    | texts,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(texts, inner, max_size=4),
    max_leaves=12,
)
microsecond_timedeltas = st.timedeltas(
    min_value=dt.timedelta(days=-(10**6)), max_value=dt.timedelta(days=10**6)
)


def nullable[T](strategy: st.SearchStrategy[T]) -> st.SearchStrategy[T | None]:
    return st.none() | strategy


thing_values = st.fixed_dictionaries(
    {},
    optional={
        "small": nullable(st.integers(-(2**15), 2**15 - 1)),
        "int_": nullable(st.integers(-(2**31), 2**31 - 1)),
        "big": nullable(st.integers(-(2**63), 2**63 - 1)),
        "txt": nullable(texts),
        "short": nullable(st.text(no_nul, max_size=20)),
        "flag": nullable(st.booleans()),
        "f4": nullable(st.floats(width=32)),
        "f8": nullable(st.floats()),
        "money": nullable(
            st.decimals(
                min_value=Decimal("-1e21"),
                max_value=Decimal("1e21"),
                places=8,
                allow_nan=False,
            )
        ),
        "any_num": nullable(st.decimals(allow_infinity=False) | st.integers()),
        "day": nullable(st.dates()),
        "at": nullable(st.datetimes(min_value=dt.datetime(1, 1, 2))),
        "at_tz": nullable(st.datetimes(min_value=dt.datetime(1, 1, 2), timezones=utc_offsets)),
        "clock": nullable(st.times()),
        "clock_tz": nullable(st.times(timezones=utc_offsets)),
        "span": nullable(microsecond_timedeltas),
        "uid": nullable(st.uuids()),
        "blob": nullable(st.binary()),
        "addr": nullable(st.ip_addresses()),
        "doc": nullable(json_values),
        "raw_doc": nullable(json_values),
        "mood": nullable(st.sampled_from(Mood)),
        "ints": nullable(st.lists(nullable(st.integers(-(2**31), 2**31 - 1)))),
        "texts": nullable(st.lists(nullable(texts))),
        "moods": nullable(st.lists(nullable(st.sampled_from(Mood)))),
    },
)


# -- comparison ---------------------------------------------------------------------


def as_float4(value: float) -> float:
    (rounded,) = struct.unpack("f", struct.pack("f", value))
    return float(rounded)


def same_json(sent: object, got: object) -> bool:
    """jsonb keeps numbers as exact decimals: 1e16 comes back as the int 10000000000000000.
    Equal means equal as a number of the type that was sent."""
    if isinstance(sent, float) and isinstance(got, int | float) and not isinstance(got, bool):
        return sent == float(got)
    if isinstance(sent, list) and isinstance(got, list):
        return len(sent) == len(got) and all(
            same_json(a, b) for a, b in zip(sent, got, strict=True)
        )
    if isinstance(sent, dict) and isinstance(got, dict):
        return sent.keys() == got.keys() and all(same_json(sent[k], got[k]) for k in sent)
    return type(sent) is type(got) and sent == got


def same(column: str, sent: object, got: object) -> bool:
    if column in ("doc", "raw_doc"):
        return same_json(sent, got)
    if isinstance(sent, float) and isinstance(got, float):
        if math.isnan(sent):
            return math.isnan(got)
        return (as_float4(sent) if column == "f4" else sent) == got
    if isinstance(sent, Decimal) and sent.is_nan():
        return isinstance(got, Decimal) and got.is_nan()
    if isinstance(sent, int) and not isinstance(sent, bool) and isinstance(got, Decimal):
        return Decimal(sent) == got
    if isinstance(sent, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return got in (sent, ipaddress.ip_interface(sent))
    if isinstance(sent, dt.time) and isinstance(got, dt.time) and sent.tzinfo is not None:
        return sent.replace(tzinfo=None) == got.replace(tzinfo=None) and (
            sent.utcoffset() == got.utcoffset()
        )
    if isinstance(sent, list) and isinstance(got, list):
        return len(sent) == len(got) and all(
            same(column, a, b) for a, b in zip(sent, got, strict=True)
        )
    return sent == got


def check(sent: dict[str, object], row: Things, how: str) -> None:
    for column, value in sent.items():
        got: object = getattr(row, column)
        assert same(column, value, got), f"{how}: {column} sent {value!r}, got {got!r}"


# -- harness: one event loop and pool for all examples ------------------------------


@pytest.fixture(scope="module")
def runner() -> Generator[tuple[asyncio.Runner, Database]]:
    url = os.environ.get("MIZZLE_TEST_URL")
    if not url:
        pytest.skip("set MIZZLE_TEST_URL to run integration tests")
    with asyncio.Runner() as loop:
        db = loop.run(Database.connect(url, schema=[Owners, Things], pool_size=2))

        async def setup() -> None:
            async with db.driver.acquire() as conn:
                await conn.execute(DDL, [])

        loop.run(setup())
        yield loop, db

        async def teardown() -> None:
            async with db.driver.acquire() as conn:
                await conn.execute("DROP TABLE fuzz_things, fuzz_owners; DROP TYPE fuzz_mood", [])
            await db.close()

        loop.run(teardown())


@settings(
    max_examples=int(os.environ.get("MIZZLE_FUZZ_EXAMPLES", "300")),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(rows=st.lists(thing_values, min_size=1, max_size=4))
def test_every_type_round_trips(
    runner: tuple[asyncio.Runner, Database], rows: list[dict[str, object]]
) -> None:
    loop, db = runner

    async def scenario() -> None:
        [owner] = await db.insert(Owners).values({}).returning()
        inserted = (
            await db.insert(Things)
            .values([{**row, "owner_id": owner.id} for row in rows])
            .returning()
        )
        for sent, got in zip(rows, inserted, strict=True):
            check(sent, got, "RETURNING")

        selected = await db.select().from_(Things).where(eq(Things.owner_id, owner.id))
        selected.sort(key=lambda t: t.id)
        for sent, got in zip(rows, selected, strict=True):
            check(sent, got, "SELECT")

        [loaded] = await db.query(Owners).find_many(
            where=eq(Owners.id, owner.id), with_={"things": {"order_by": Things.id}}
        )
        for sent, got in zip(rows, loaded.things, strict=True):
            check(sent, got, "relational JSON")

        # Writing back what was read must not change it (UPDATE path).
        if rows[0]:
            first = selected[0]
            await db.update(Things).set(rows[0]).where(eq(Things.id, first.id))
            [again] = await db.select().from_(Things).where(eq(Things.id, first.id))
            check(rows[0], again, "UPDATE")

        await db.delete(Owners).where(eq(Owners.id, owner.id))

    loop.run(scenario())

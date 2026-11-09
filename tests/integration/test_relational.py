import datetime
import enum
import ipaddress
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from asyncpg import Box, Range, Record

from mizzle import (
    Database,
    QueryError,
    RelationNotLoaded,
    Table,
    desc,
    eq,
    excluded,
    gt,
    in_,
    like,
    sql,
)
from mizzle.core.query.select import Select
from mizzle.core.schema.column import Column
from mizzle.core.schema.relations import many, one
from mizzle.core.snapshot import empty_snapshot
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import (
    array,
    bytea,
    custom,
    date,
    inet,
    integer,
    interval,
    jsonb,
    numeric,
    pg_enum,
    real,
    serial,
    text,
    time,
    timestamp,
)
from mizzle.pg import (
    uuid as uuid_col,
)
from tests.models import Posts, Users


async def seed(db: Database) -> tuple[Users, Users]:
    [ann] = await db.insert(Users).values(email="ann").returning()
    [bob] = await db.insert(Users).values(email="bob").returning()
    await db.insert(Posts).values(
        [{"author_id": ann.id, "title": t} for t in ("a1", "a2", "a3")]
        + [{"author_id": bob.id, "title": "b1"}]
    )
    return ann, bob


async def test_find_many_with_nested_relations(db: Database) -> None:
    await seed(db)
    users = await db.query(Users).find_many(
        with_={
            "posts": {
                "order_by": desc(Posts.title),
                "limit": 2,
                "with_": {"author": True},  # back to users: same table, needs aliasing
            }
        },
        order_by=Users.email,
    )
    assert [u.email for u in users] == ["ann", "bob"]
    assert [p.title for p in users[0].posts] == ["a3", "a2"]
    assert users[0].posts[0].author is not None
    assert users[0].posts[0].author.email == "ann"
    assert [p.title for p in users[1].posts] == ["b1"]


async def test_nested_order_without_limit(db: Database) -> None:
    await seed(db)
    [ann] = await db.query(Users).find_many(
        where=eq(Users.email, "ann"), with_={"posts": {"order_by": desc(Posts.title)}}
    )
    assert [p.title for p in ann.posts] == ["a3", "a2", "a1"]


async def test_filters_at_each_level(db: Database) -> None:
    await seed(db)
    users = await db.query(Users).find_many(
        where=eq(Users.email, "ann"),
        with_={"posts": {"where": like(Posts.title, "%2")}},
    )
    assert len(users) == 1
    assert [p.title for p in users[0].posts] == ["a2"]


async def test_empty_relation_and_find_first(db: Database) -> None:
    [cat] = await db.insert(Users).values(email="cat").returning()
    user = await db.query(Users).find_first(where=eq(Users.id, cat.id), with_={"posts": True})
    assert user is not None and user.posts == []
    assert await db.query(Users).find_first(where=eq(Users.id, -1)) is None


async def test_one_relation_and_not_loaded(db: Database) -> None:
    await seed(db)
    posts = await db.query(Posts).find_many(with_={"author": True}, where=gt(Posts.id, 0))
    assert {p.author.email for p in posts if p.author} == {"ann", "bob"}
    with pytest.raises(RelationNotLoaded, match="add 'posts' to with_"):
        _ = posts[0].author.posts if posts[0].author else None


async def test_columns_load_only_what_is_asked(db: Database) -> None:
    await seed(db)
    [ann] = await db.query(Users).find_many(
        columns=[Users.email, Users.id],
        where=eq(Users.email, "ann"),
        with_={"posts": {"columns": [Posts.title], "order_by": Posts.title}},
    )
    assert (ann.id, ann.email) == (ann.id, "ann")
    assert [p.title for p in ann.posts] == ["a1", "a2", "a3"]
    with pytest.raises(AttributeError, match=r"Users\.bio was not selected"):
        _ = ann.bio
    with pytest.raises(AttributeError, match=r"Posts\.id was not selected"):
        _ = ann.posts[0].id


def test_bad_with_options(db: Database) -> None:
    with pytest.raises(QueryError, match="no relation 'comments'"):
        db.query(Users).find_many(with_={"comments": True})
    with pytest.raises(QueryError, match="not a column of Users"):
        db.query(Users).find_many(columns=[Posts.id])
    with pytest.raises(QueryError, match="must not be empty"):
        db.query(Users).find_many(columns=[])
    with pytest.raises(QueryError, match="limit/order_by need many"):
        db.query(Posts).find_many(with_={"author": {"limit": 1}})


class Mood(enum.StrEnum):
    UP = "up"
    DOWN = "down"


class Owner(Table, name="rel_owner"):
    id = serial().primary_key()
    things = many(lambda: Thing)


class Thing(Table, name="rel_thing"):
    id = serial().primary_key()
    owner_id = integer().not_null()
    n = numeric(20, 10)
    r = real()
    d = date()
    ts = timestamp(tz=True)
    t = time()
    u = uuid_col()
    b = bytea()
    ip = inet()
    span = interval()
    j = jsonb[dict[str, Any]]()
    mood = pg_enum(Mood, type_name="rel_mood")
    moods = array(pg_enum(Mood, type_name="rel_mood").not_null())
    words = array(text().not_null())
    amounts = array(numeric(12, 4))
    blobs = array(bytea().not_null())
    spans = array(interval().not_null())
    owner = one(lambda: Owner, fields=lambda: [Thing.owner_id], references=lambda: [Owner.id])


async def test_every_type_survives_json(driver: PostgresDriver, pg_url: str) -> None:
    from mizzle.dialects.postgres import PostgresDialect

    dialect = PostgresDialect()
    async with driver.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS rel_thing, rel_owner, rel_mood CASCADE", [])
        await conn.execute("DROP TYPE IF EXISTS rel_mood", [])
        snapshot = build_snapshot([Owner, Thing], dialect)
        for statement in dialect.render(diff(empty_snapshot("postgresql"), snapshot)):
            await conn.execute(statement.sql, [])

    values = {
        "n": Decimal("12345678.1234567891"),
        "r": 1.5,
        "d": datetime.date(2024, 2, 29),
        "ts": datetime.datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=datetime.UTC),
        "t": datetime.time(10, 11, 12, 500),
        "u": uuid.uuid4(),
        "b": b"\x00\xffbytes",
        "ip": ipaddress.ip_address("10.1.2.3"),
        "span": datetime.timedelta(days=1, seconds=3.5),
        "j": {"nested": [1, "two", None]},
        "mood": Mood.DOWN,
        "moods": [Mood.UP, Mood.DOWN],
        "words": ["a", "b"],
        "amounts": [Decimal("1.0001"), None, Decimal("99999999.9999")],
        "blobs": [b"\x00\x01", b"\xff"],
        "spans": [datetime.timedelta(seconds=1.5), datetime.timedelta(days=2)],
    }
    async with await Database.connect(pg_url, schema=[Owner, Thing]) as db:
        [owner] = await db.insert(Owner).values({}).returning()
        await db.insert(Thing).values(owner_id=owner.id, **values)
        [loaded] = await db.query(Owner).find_many(with_={"things": True})
        [direct] = await db.select().from_(Thing)
        thing = loaded.things[0]
        for key, expected in values.items():
            assert getattr(thing, key) == expected, key
            assert getattr(thing, key) == getattr(direct, key), key
        assert thing.mood is Mood.DOWN

        await db.insert(Thing).values(owner_id=owner.id)  # every array column NULL
        [reloaded] = await db.query(Owner).find_many(with_={"things": {"order_by": Thing.id}})
        empty = reloaded.things[1]
        assert (empty.amounts, empty.blobs, empty.spans, empty.words) == (None, None, None, None)

        await db.execute(sql("DROP TABLE rel_thing, rel_owner"))
        await db.execute(sql("DROP TYPE rel_mood"))


class CusOwner(Table, name="rel_cus_owner"):
    id = serial().primary_key()
    rows = many(lambda: CusRow)


@dataclass(frozen=True)
class Label:
    text: str


def _label_text(label: Label) -> str:
    return label.text


class CusRow(Table, name="rel_cus_row"):
    id = serial().primary_key()
    owner_id = integer()
    ci = custom("citext")
    num = custom("rel_c_num")  # a domain over numeric
    mail = custom("rel_c_mail")  # a domain over text
    mood = custom("rel_c_mood")  # an enum mizzle does not manage
    doc = custom("tsvector")
    pair = custom("rel_c_pair")  # a composite
    span = custom("int4range")
    rect = custom("box")
    rects = array(custom("box"))
    label = custom("citext", py_type=Label, decode=Label, encode=_label_text)
    owner = one(
        lambda: CusOwner, fields=lambda: [CusRow.owner_id], references=lambda: [CusOwner.id]
    )


class CusCopy(Table, name="rel_cus_copy"):
    id = serial().primary_key()
    ci = custom("citext")
    span = custom("int4range")


_CUS_DROP = (
    "DROP TABLE IF EXISTS rel_cus_row, rel_cus_owner, rel_cus_copy;"
    " DROP TYPE IF EXISTS rel_c_pair, rel_c_mood; DROP DOMAIN IF EXISTS rel_c_num, rel_c_mail"
)
_CUS_DDL = """
CREATE EXTENSION IF NOT EXISTS citext;
CREATE DOMAIN rel_c_num AS numeric;
CREATE DOMAIN rel_c_mail AS text CHECK (VALUE LIKE '%@%');
CREATE TYPE rel_c_mood AS ENUM ('sad', 'ok', 'happy');
CREATE TYPE rel_c_pair AS (amount numeric, day date);
CREATE TABLE rel_cus_owner (id serial PRIMARY KEY);
CREATE TABLE rel_cus_row (id serial PRIMARY KEY, owner_id int REFERENCES rel_cus_owner,
  ci citext, num rel_c_num, mail rel_c_mail, mood rel_c_mood, doc tsvector, pair rel_c_pair,
  span int4range, rect box, rects box[], label citext);
CREATE TABLE rel_cus_copy (id serial PRIMARY KEY, ci citext, span int4range);
"""
_DAY = datetime.date(2020, 1, 2)
_BOX = Box((2, 2), (0, 0))


@pytest.fixture
async def cus_db(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    """An owner with three rows: citext Abc/abc/Z, numeric domain 1.0/1.00/2."""
    async with driver.acquire() as conn:
        await conn.execute(_CUS_DROP + ";" + _CUS_DDL, [])
    try:
        async with await Database.connect(pg_url, schema=[CusOwner, CusRow, CusCopy]) as db:
            [owner] = await db.insert(CusOwner).values({}).returning()
            first = {
                "owner_id": owner.id,
                "ci": "Abc",
                "num": Decimal("1.0"),
                "mail": "a@x",
                "mood": "happy",
                "doc": "b a",
                "pair": (Decimal("1.5"), _DAY),
                "span": Range(1, 5),
                "rect": _BOX,
                "rects": [_BOX, None],
                "label": Label("Hi"),
            }
            more = [
                {"owner_id": owner.id, "ci": "abc", "num": Decimal("1.00"), "span": Range(10, 12)},
                {"owner_id": owner.id, "ci": "Z", "num": Decimal(2), "span": Range(2, 4)},
            ]
            await db.insert(CusRow).values([first, *more])  # insert_many
            yield db
    finally:
        async with driver.acquire() as conn:
            await conn.execute(_CUS_DROP + "; DROP EXTENSION IF EXISTS citext", [])


def _check_first(row: CusRow) -> None:
    assert (row.ci, row.num, row.mail, row.mood, row.doc) == (
        "Abc",
        Decimal("1.0"),
        "a@x",
        "happy",
        "'a' 'b'",
    )
    assert isinstance(row.pair, Record) and tuple(row.pair) == (Decimal("1.5"), _DAY)
    assert (row.span, row.rect, row.rects, row.label) == (
        Range(1, 5),
        _BOX,
        [_BOX, None],
        Label("Hi"),
    )


async def test_custom_values_are_the_drivers(cus_db: Database) -> None:
    # custom(): whatever asyncpg reads and writes for the type, with no SQL around it.
    query = cus_db.select().from_(CusRow).order_by(CusRow.id)
    assert "::" not in query.to_sql()[0]
    rows = await query
    _check_first(rows[0])
    assert [r.span for r in rows] == [Range(1, 5), Range(10, 12), Range(2, 4)]
    found = await cus_db.query(CusRow).find_first(where=eq(CusRow.ci, "ABC"), order_by=CusRow.id)
    assert found is not None
    _check_first(found)
    assert [r.span async for r in cus_db.query(CusRow).find_many(order_by=CusRow.id).stream()] == [
        r.span for r in rows
    ]
    assert [r.ci async for r in query.stream()] == ["Abc", "abc", "Z"]
    listed = await cus_db.select().from_(CusRow).where(in_(CusRow.span, [Range(2, 4), Range(7, 8)]))
    assert [r.ci for r in listed] == ["Z"]
    mood = await cus_db.select({"m": CusRow.mood}).from_(CusRow).where(eq(CusRow.mood, "happy"))
    assert mood == [{"m": "happy"}]
    labelled = await cus_db.select().from_(CusRow).where(eq(CusRow.label, Label("HI")))
    assert [r.label for r in labelled] == [Label("Hi")]

    upd = cus_db.update(CusRow).set(span=Range(2, 3, upper_inc=True), label=Label("x"))
    [changed] = await upd.where(eq(CusRow.ci, "Z")).returning()
    assert (changed.span, changed.label) == (Range(2, 4), Label("x"))
    ups = cus_db.insert(CusRow).values(id=changed.id, ci="z", rect=Box((1, 1), (3, 3)))
    ups = ups.on_conflict_do_update(target=CusRow.id, set_={"rect": excluded(CusRow.rect)})
    [upserted] = await ups.returning({"r": CusRow.rect, "c": CusRow.ci})
    assert upserted == {"r": Box((3, 3), (1, 1)), "c": "Z"}


async def test_custom_nested_rows_are_json(cus_db: Database) -> None:
    # A relational query nests a custom() value as to_json of it; decode still applies.
    [owner] = await cus_db.query(CusOwner).find_many(with_={"rows": {"order_by": CusRow.id}})
    row = owner.rows[0]
    assert (row.ci, row.mail, row.mood, row.doc, row.label) == (
        "Abc",
        "a@x",
        "happy",
        "'a' 'b'",
        Label("Hi"),
    )
    assert row.num == 1.0 and row.pair == {"amount": 1.5, "day": "2020-01-02"}
    assert (row.span, row.rect, row.rects) == ("[1,5)", "(2,2),(0,0)", ["(2,2),(0,0)", None])


async def test_custom_sorts_and_compares_as_sql(cus_db: Database, driver: PostgresDriver) -> None:
    # DISTINCT, UNION, INTERSECT, EXCEPT and ORDER BY act on the type, as plain SQL does.
    async def plain(text: str) -> list[object]:
        async with driver.acquire() as conn:
            return [r[0] for r in await conn.fetch(text, [])]

    for name, column in (("ci", CusRow.ci), ("num", CusRow.num)):
        query = cus_db.select({"v": column}).from_(CusRow).distinct()
        assert sorted(map(str, (r["v"] for r in await query))) == sorted(
            map(str, await plain(f"SELECT DISTINCT {name} FROM rel_cus_row"))
        )
        ordered = await query.order_by(desc(column))
        sql_ordered = await plain(f"SELECT DISTINCT {name} FROM rel_cus_row ORDER BY 1 DESC")
        assert [r["v"] for r in ordered] == sql_ordered

    def one(column: Column[object], row: int) -> Select[dict[str, object]]:
        return cus_db.select({"v": column}).from_(CusRow).where(eq(CusRow.id, row))

    top = await one(CusRow.span, 1).union_all(one(CusRow.span, 2)).order_by(desc(CusRow.span))
    assert [r["v"] for r in top] == [Range(10, 12), Range(1, 5)]
    assert await one(CusRow.ci, 1).intersect(one(CusRow.ci, 2)) == [{"v": "Abc"}]
    assert await one(CusRow.num, 1).except_(one(CusRow.num, 2)) == []


async def test_custom_copies_with_from_select(cus_db: Database) -> None:
    source = cus_db.select({"c": CusRow.ci, "s": CusRow.span}).from_(CusRow).order_by(CusRow.id)
    assert await cus_db.insert(CusCopy).from_select([CusCopy.ci, CusCopy.span], source) == 3
    copied = await cus_db.select({"c": CusCopy.ci, "s": CusCopy.span}).from_(CusCopy)
    assert copied == await source
    # A subquery selecting it compares as the type.
    names = cus_db.select({"c": CusRow.ci}).from_(CusRow)
    assert len(await cus_db.select().from_(CusCopy).where(in_(CusCopy.ci, names))) == 3

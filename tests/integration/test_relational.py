import datetime
import enum
import ipaddress
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import pytest

from mizzle import (
    Database,
    QueryError,
    RelationNotLoaded,
    Table,
    any_,
    desc,
    eq,
    gt,
    in_,
    like,
    sql,
)
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


class CusRow(Table, name="rel_cus_row"):
    id = serial().primary_key()
    owner_id = integer().not_null()
    num = custom("rel_c_num")  # a domain over numeric
    pair = custom("rel_c_pair")  # a composite
    pairs = custom("rel_c_pair[]")
    span = custom("int4range")
    rect = custom("box")
    owner = one(
        lambda: CusOwner, fields=lambda: [CusRow.owner_id], references=lambda: [CusOwner.id]
    )


_CUS_DDL = """
CREATE DOMAIN rel_c_num AS numeric(30,3);
CREATE TYPE rel_c_pair AS (amount numeric, day date);
CREATE TABLE rel_cus_owner (id serial PRIMARY KEY);
CREATE TABLE rel_cus_row (id serial PRIMARY KEY, owner_id int NOT NULL REFERENCES rel_cus_owner,
  num rel_c_num, pair rel_c_pair, pairs rel_c_pair[], span int4range, rect box);
"""
_CUS_DROP = (
    "DROP TABLE IF EXISTS rel_cus_row, rel_cus_owner; DROP TYPE IF EXISTS rel_c_pair;"
    " DROP DOMAIN IF EXISTS rel_c_num"
)


async def test_custom_columns_read_and_write_the_types_text(
    driver: PostgresDriver, pg_url: str
) -> None:
    # custom(): any type, sent as text and read as the server prints it, nested or not.
    async with driver.acquire() as conn:
        await conn.execute(_CUS_DROP + ";" + _CUS_DDL, [])
    values = {
        "num": "12345678901234567890.123",
        "pair": "(1.10,2020-01-02)",
        "pairs": '{"(1,2020-01-02)",NULL}',
        "span": "[1,5)",
        "rect": "(2,2),(0,0)",
    }
    try:
        async with await Database.connect(pg_url, schema=[CusOwner, CusRow]) as db:
            [owner] = await db.insert(CusOwner).values({}).returning()
            [row] = await db.insert(CusRow).values(owner_id=owner.id, **values).returning()
            await db.insert(CusRow).values(owner_id=owner.id, span="[2,3]")  # printed [2,4)
            assert {k: getattr(row, k) for k in values} == values
            [found] = await db.select().from_(CusRow).where(eq(CusRow.pair, values["pair"]))
            assert {k: getattr(found, k) for k in values} == values
            spans = await db.select({"s": CusRow.span}).from_(CusRow).order_by(CusRow.id)
            assert [r["s"] for r in spans] == ["[1,5)", "[2,4)"]
            listed = await db.select().from_(CusRow).where(in_(CusRow.span, ["[2,4)", "[7,8)"]))
            assert [r.span for r in listed] == ["[2,4)"]
            anyof = await db.select().from_(CusRow).where(eq(CusRow.rect, any_(["(2,2),(0,0)"])))
            assert [r.rect for r in anyof] == ["(2,2),(0,0)"]
            await db.update(CusRow).set(rect="(1,1),(3,3)").where(eq(CusRow.id, row.id))
            [nested] = await db.query(CusOwner).find_many(with_={"rows": {"order_by": CusRow.id}})
            assert nested.rows[0].rect == "(3,3),(1,1)"
            assert {k: getattr(nested.rows[0], k) for k in values if k != "rect"} == {
                k: v for k, v in values.items() if k != "rect"
            }
            assert nested.rows[1].span == "[2,4)" and nested.rows[1].pair is None
    finally:
        async with driver.acquire() as conn:
            await conn.execute(_CUS_DROP, [])


class CusBox(Table, name="rel_cus_box"):
    id = serial().primary_key()
    span = custom("int4range")
    rects = array(custom("box"))


class CusCopy(Table, name="rel_cus_copy"):
    id = serial().primary_key()
    span = custom("int4range")


@pytest.fixture
async def cus_db(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    """Two rows of CusBox (one with a 2-D array) and an empty CusCopy."""
    drop = "DROP TABLE IF EXISTS rel_cus_box, rel_cus_copy"
    async with driver.acquire() as conn:
        await conn.execute(
            f"{drop}; CREATE TABLE rel_cus_box (id serial PRIMARY KEY, span int4range,"
            " rects box[]); CREATE TABLE rel_cus_copy (id serial PRIMARY KEY, span int4range)",
            [],
        )
    try:
        async with await Database.connect(pg_url, schema=[CusBox, CusCopy]) as db:
            await db.insert(CusBox).values(span="[1,3)", rects=_RECTS)
            await db.insert(CusBox).values(span="[1,3)", rects=[["(1,1),(0,0)"]])
            yield db
    finally:
        async with driver.acquire() as conn:
            await conn.execute(drop, [])


_RECTS = ["(1,1),(0,0)", "(3,3),(2,2)"]


async def test_custom_array_reads_as_text(cus_db: Database) -> None:
    # Q6-2: an array of custom() values read as asyncpg objects, but as text nested.
    [ret] = await cus_db.insert(CusBox).values(rects=_RECTS).returning()
    assert ret.rects == _RECTS
    rows = await cus_db.select().from_(CusBox).order_by(CusBox.id)
    assert [r.rects for r in rows] == [_RECTS, [["(1,1),(0,0)"]], _RECTS]


async def test_query_root_reads_custom_as_text(cus_db: Database) -> None:
    # Q6-1: db.query() root rows read custom() columns as asyncpg objects.
    found = await cus_db.query(CusBox).find_first(order_by=CusBox.id)
    assert found is not None and (found.span, found.rects) == ("[1,3)", _RECTS)
    streamed = [r.span async for r in cus_db.query(CusBox).find_many().stream()]
    assert streamed == ["[1,3)", "[1,3)"]


async def test_distinct_orders_custom_by_its_output(cus_db: Database) -> None:
    # Q6-3: SELECT DISTINCT col::text ... ORDER BY col was refused by PostgreSQL.
    query = cus_db.select({"s": CusBox.span}).from_(CusBox).distinct()
    assert await query.order_by(desc(CusBox.span)) == [{"s": "[1,3)"}]


async def test_select_feeding_a_write_keeps_the_custom_type(
    cus_db: Database, driver: PostgresDriver
) -> None:
    # Q6-4: the SELECT of an INSERT ... SELECT or a MERGE source read the column as text.
    source = cus_db.select({"s": CusBox.span}).from_(CusBox)
    assert await cus_db.insert(CusCopy).from_select([CusCopy.span], source) == 2
    copied = await cus_db.select({"s": CusCopy.span}).from_(CusCopy)
    assert copied == [{"s": "[1,3)"}, {"s": "[1,3)"}]
    # A subquery in an expression compares as the type too.
    found = await cus_db.select().from_(CusCopy).where(in_(CusCopy.span, source))
    assert len(found) == 2
    async with driver.acquire() as conn:
        [version] = await conn.fetch("SHOW server_version_num", [])
    if int(str(version[0])) >= 150000:  # MERGE
        src = cus_db.select({"id": CusBox.id, "s": CusBox.span}).from_(CusBox).subquery("src")
        merged = await (
            cus_db.merge(CusCopy)
            .using(src)
            .on(eq(CusCopy.id, src.c("id", int)))
            .when_matched_then_update({"span": src.c("s", str)})
        )
        assert merged == 2


async def test_distinct_orders_custom_by_its_type(cus_db: Database) -> None:
    # Q6-7: SELECT DISTINCT ordered a custom() column by its text: "[10,12)" < "[2,4)".
    await cus_db.insert(CusBox).values([{"span": "[10,12)"}, {"span": "[2,4)"}])
    query = cus_db.select({"s": CusBox.span}).from_(CusBox).distinct().order_by(CusBox.span)
    assert await query == [{"s": "[1,3)"}, {"s": "[2,4)"}, {"s": "[10,12)"}]
    top = cus_db.select(CusBox.span).from_(CusBox).distinct().order_by(desc(CusBox.span))
    assert await top.limit(2) == [("[10,12)",), ("[2,4)",)]
    # The subquery's order is not kept by the query around it: ordered again outside.
    assert top.to_sql()[0].endswith('AS "_d" ORDER BY "_d"."_0" DESC')


async def test_cte_and_subquery_keep_the_custom_type(cus_db: Database) -> None:
    # Q6-8: a CTE selecting a custom() column gave text, so comparing it outside compared
    # text with the type (an error) or text with text.
    await cus_db.insert(CusBox).values(span="[10,12)")
    spans = cus_db.select({"s": CusBox.span}).from_(CusBox).cte("spans")
    found = (
        await cus_db.with_(spans)
        .select({"s": spans.c("s", str)})
        .from_(spans)
        .where(gt(spans.c("s", str), "[2,4)"))
    )
    assert found == [{"s": "[10,12)"}]
    sub = cus_db.select({"id": CusBox.id, "s": CusBox.span}).from_(CusBox).subquery("sub")
    joined = (
        await cus_db.select(sub.c("s", str))
        .from_(sub)
        .join(CusBox, eq(CusBox.span, sub.c("s", str)))
        .where(eq(CusBox.id, sub.c("id", int)))
        .order_by(sub.c("id", int))
    )
    assert joined == [("[1,3)",), ("[1,3)",), ("[10,12)",)]

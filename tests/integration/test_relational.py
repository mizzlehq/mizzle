import datetime
import enum
import ipaddress
import uuid
from collections.abc import Iterable, Sized
from decimal import Decimal
from typing import Any, cast

import pytest
from asyncpg import Range, Record

from mizzle import Database, QueryError, RelationNotLoaded, Table, desc, eq, gt, like, sql
from mizzle.core.schema.relations import many, one
from mizzle.core.snapshot import empty_snapshot
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import (
    array,
    bytea,
    date,
    inet,
    integer,
    interval,
    jsonb,
    numeric,
    pg_composite,
    pg_domain,
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


class DomOwner(Table, name="rel_dom_owner"):
    id = serial().primary_key()
    rows = many(lambda: DomRow)


class DomRow(Table, name="rel_dom_row"):
    id = serial().primary_key()
    owner_id = integer().not_null()
    num = pg_domain("rel_d_num")
    tstz = pg_domain("rel_d_tstz")
    day = pg_domain("rel_d_day")
    span = pg_domain("rel_d_span")
    nums = pg_domain("rel_d_nums")
    deep = pg_domain("rel_d_deep")
    blob = pg_domain("rel_d_blob")
    money_none = pg_composite("rel_c_money")
    money_tuple = pg_composite("rel_c_money", tuple)
    money_dict = pg_composite("rel_c_money", dict)
    owner = one(
        lambda: DomOwner, fields=lambda: [DomRow.owner_id], references=lambda: [DomOwner.id]
    )


_DOM_DDL = r"""
CREATE DOMAIN rel_d_num AS numeric; CREATE DOMAIN rel_d_tstz AS timestamptz;
CREATE DOMAIN rel_d_day AS date; CREATE DOMAIN rel_d_span AS int4range;
CREATE DOMAIN rel_d_nums AS numeric(30,3)[]; CREATE DOMAIN rel_d_deep AS rel_d_num;
CREATE DOMAIN rel_d_blob AS bytea;
CREATE TYPE rel_c_money AS (amount numeric, day date, at timestamptz, tags text[], d rel_d_num);
CREATE TABLE rel_dom_owner (id serial PRIMARY KEY);
CREATE TABLE rel_dom_row (id serial PRIMARY KEY, owner_id int NOT NULL, num rel_d_num,
  tstz rel_d_tstz, day rel_d_day, span rel_d_span, nums rel_d_nums, deep rel_d_deep,
  blob rel_d_blob, money_none rel_c_money, money_tuple rel_c_money, money_dict rel_c_money);
INSERT INTO rel_dom_owner DEFAULT VALUES;
INSERT INTO rel_dom_row (owner_id, num, tstz, day, span, nums, deep, blob,
  money_none, money_tuple, money_dict)
SELECT 1, 12345678901234567890.123, '2020-01-01 00:00:00.123456+00', '2020-02-29', '[1,5)',
  '{1.500,NULL}', 0.1, '\x00ff', m, m, m
FROM (SELECT ROW(1.10, '2020-01-02', '2020-01-01+00', '{a,NULL}', 2.50)::rel_c_money AS m) s;
INSERT INTO rel_dom_row (owner_id, money_none) VALUES (1, ROW(NULL, NULL, NULL, NULL, NULL));
"""
_DOM_DROP = (
    "DROP TABLE IF EXISTS rel_dom_row, rel_dom_owner; DROP TYPE IF EXISTS rel_c_money; "
    "DROP DOMAIN IF EXISTS rel_d_deep, rel_d_num, rel_d_tstz, rel_d_day, rel_d_span, "
    "rel_d_nums, rel_d_blob"
)


def _assert_same(nested: object, plain: object) -> None:
    """Equal and of the same type, all the way down (Decimal digits too)."""
    if isinstance(plain, Record):
        plain = dict(plain.items())  # a nested composite is a dict by field name
    assert type(nested) is type(plain), (nested, plain)
    if isinstance(plain, Decimal):
        assert str(nested) == str(plain)
    elif isinstance(plain, list | tuple):
        items = list(cast(Iterable[object], nested))
        assert len(items) == len(cast(Sized, plain))
        for a, b in zip(items, cast(Iterable[object], plain), strict=True):
            _assert_same(a, b)
    elif isinstance(plain, dict):
        got = cast(dict[str, object], nested)
        want = cast(dict[str, object], plain)
        assert got.keys() == want.keys()
        for key, value in want.items():
            _assert_same(got[key], value)
    elif isinstance(plain, Range):  # bounds of the same type too, not just equal
        assert nested == plain
        for end in ("lower", "upper"):
            _assert_same(getattr(nested, end), getattr(plain, end))
    else:
        assert nested == plain


@pytest.mark.parametrize(
    "attr", [a for a in DomRow.__table__.columns if a not in ("id", "owner_id")]
)
async def test_nested_domains_and_composites_decode_like_a_plain_select(
    driver: PostgresDriver, pg_url: str, attr: str
) -> None:
    # Q4-6: without a py_type, a nested domain came back as raw JSON (numeric -> float,
    # timestamptz -> str, range -> '[1,5)'), and composite fields too (Decimal -> float).
    async with driver.acquire() as conn:
        await conn.execute(_DOM_DROP + ";" + _DOM_DDL, [])
    try:
        async with await Database.connect(pg_url, schema=[DomOwner, DomRow]) as db:
            plain = await db.select().from_(DomRow).order_by(DomRow.id)
            query = db.query(DomOwner).find_many(with_={"rows": {"order_by": DomRow.id}})
            [owner] = await query.prepare().execute()  # prepared before the first type lookup
            streamed = [o async for o in query.stream()]
            for nested in (owner.rows, streamed[0].rows):
                for got, want in zip(nested, plain, strict=True):
                    _assert_same(getattr(got, attr), getattr(want, attr))
    finally:
        async with driver.acquire() as conn:
            await conn.execute(_DOM_DROP, [])


class TenOwner(Table, name="rel_ten_owner"):
    id = serial().primary_key()
    rows = many(lambda: TenRow)


class TenRow(Table, name="rel_ten_row"):
    id = serial().primary_key()
    owner_id = integer().not_null()
    amt = pg_domain("rel_ten_amt")
    pair = pg_composite("rel_ten_pair")
    owner = one(
        lambda: TenOwner, fields=lambda: [TenRow.owner_id], references=lambda: [TenOwner.id]
    )


async def test_nested_domain_types_are_looked_up_per_database(
    driver: PostgresDriver, pg_url: str
) -> None:
    # Q4-17: the looked-up types were cached per process by name, so a second database
    # where the name is another type (int vs numeric, other composite fields) decoded wrong.
    ddl = (
        "CREATE SCHEMA {s}; CREATE DOMAIN {s}.rel_ten_amt AS {t};"
        " CREATE TYPE {s}.rel_ten_pair AS (a {t}, b {t});"
        " CREATE TABLE {s}.rel_ten_owner (id serial PRIMARY KEY);"
        " CREATE TABLE {s}.rel_ten_row (id serial PRIMARY KEY, owner_id int NOT NULL,"
        "  amt {s}.rel_ten_amt, pair {s}.rel_ten_pair);"
        " INSERT INTO {s}.rel_ten_owner DEFAULT VALUES;"
        " INSERT INTO {s}.rel_ten_row (owner_id, amt, pair) VALUES (1, 2, ROW(3, 4));"
    )
    drop = "DROP SCHEMA IF EXISTS rel_ten_a, rel_ten_b CASCADE"
    async with driver.acquire() as conn:
        await conn.execute(drop, [])
        await conn.execute(ddl.format(s="rel_ten_a", t="int"), [])
        await conn.execute(ddl.format(s="rel_ten_b", t="numeric(10,2)"), [])
    try:
        for schema, want in (("rel_ten_a", 2), ("rel_ten_b", Decimal("2.00"))):
            settings = {"search_path": schema}
            async with await Database.connect(pg_url, server_settings=settings) as db:
                plain = await db.select().from_(TenRow)
                [owner] = await db.query(TenOwner).find_many(with_={"rows": True})
                for got, row in zip(owner.rows, plain, strict=True):
                    _assert_same(got.amt, row.amt)
                    _assert_same(got.pair, row.pair)
                    _assert_same(got.amt, want)
    finally:
        async with driver.acquire() as conn:
            await conn.execute(drop, [])


class RngOwner(Table, name="rel_rng_owner"):
    id = serial().primary_key()
    rows = many(lambda: RngRow)
    kinds = many(lambda: RngKind)


class RngRow(Table, name="rel_rng_row"):
    id = serial().primary_key()
    owner_id = integer().not_null()
    dom = pg_domain("rel_rng_dom")
    plain = pg_domain("rel_rng")  # a custom range column, typed by lookup too
    multi = pg_domain("rel_rng_multi")
    doms = pg_domain("rel_rng_dom[]")
    ranges = pg_domain("rel_rng[]")
    owner = one(
        lambda: RngOwner, fields=lambda: [RngRow.owner_id], references=lambda: [RngOwner.id]
    )


# Q4-20: a custom range over any subtype nests like a plain select reads it.
_SUBTYPES = {
    "f8": ("float8", "'[1.1,0.30000000000000004e1)'"),
    "f4": ("float4", "'[1.1,2.5]'"),
    "txt": ("text", """'[a,"b c")'"""),
    "tm": ("time", "'[10:00:00.5,12:30)'"),
    "iv": ("interval", "'[1 day 02:00:00.000001,1 mon)'"),
    "ip": ("inet", "'(10.0.0.1,10.0.0.9]'"),
    "tz": ("timestamptz", "'[2020-01-01 00:00:00.123+00,)'"),
    "i4": ("int4", "'[1,5]'"),
}


class RngKind(Table, name="rel_rng_kind"):
    id = serial().primary_key()
    owner_id = integer().not_null()
    f8 = pg_domain("rel_rng_f8")
    f8_dom = pg_domain("rel_rng_f8_dom")
    f8_multi = pg_domain("rel_rng_f8_multi")
    f8_arr = pg_domain("rel_rng_f8[]")
    f8_multis = pg_domain("rel_rng_f8_multi[]")
    f4 = pg_domain("rel_rng_f4")
    txt = pg_domain("rel_rng_txt")
    txt_multi = pg_domain("rel_rng_txt_multi")
    tm = pg_domain("rel_rng_tm")
    iv = pg_domain("rel_rng_iv")
    ip = pg_domain("rel_rng_ip")
    tz = pg_domain("rel_rng_tz")
    i4 = pg_domain("rel_rng_i4")
    owner = one(
        lambda: RngOwner, fields=lambda: [RngKind.owner_id], references=lambda: [RngOwner.id]
    )


async def test_nested_custom_range_and_its_domain_decode_like_a_plain_select(
    driver: PostgresDriver, pg_url: str
) -> None:
    # Q4-16: a domain over a user-defined range came back as its text in a nested row.
    drop = (
        "DROP TABLE IF EXISTS rel_rng_row, rel_rng_kind, rel_rng_owner;"
        " DROP DOMAIN IF EXISTS rel_rng_dom, rel_rng_f8_dom; DROP TYPE IF EXISTS rel_rng"
        + "".join(f", rel_rng_{k}" for k in _SUBTYPES)
    )
    kinds = "".join(
        f"CREATE TYPE rel_rng_{k} AS RANGE (subtype = {t},"
        f" multirange_type_name = rel_rng_{k}_multi);"
        for k, (t, _) in _SUBTYPES.items()
    )
    kinds += """
    CREATE DOMAIN rel_rng_f8_dom AS rel_rng_f8;
    CREATE TABLE rel_rng_kind (id serial PRIMARY KEY, owner_id int NOT NULL, f8 rel_rng_f8,
      f8_dom rel_rng_f8_dom, f8_multi rel_rng_f8_multi, f8_arr rel_rng_f8[],
      f8_multis rel_rng_f8_multi[], f4 rel_rng_f4, txt rel_rng_txt,
      txt_multi rel_rng_txt_multi, tm rel_rng_tm, iv rel_rng_iv, ip rel_rng_ip, tz rel_rng_tz,
      i4 rel_rng_i4);
    INSERT INTO rel_rng_kind (owner_id) VALUES (1);
    """
    values = {k: v for k, (_, v) in _SUBTYPES.items()} | {
        "f8_dom": "'(,-2.5e-7]'",
        "f8_multi": "'{[1,2),[3.25,4]}'",
        "f8_arr": """'{"[0.1,0.2)",NULL,empty}'""",
        "f8_multis": """'{"{[1,2)}","{[3,4),[5,6)}"}'""",
        "txt_multi": """'{[a,b),["x y",z]}'""",
    }
    kinds += f"INSERT INTO rel_rng_kind (owner_id, {', '.join(values)})"
    kinds += f" VALUES (1, {', '.join(values.values())});"
    ddl = """
    CREATE TYPE rel_rng AS RANGE (subtype = numeric, multirange_type_name = rel_rng_multi);
    CREATE DOMAIN rel_rng_dom AS rel_rng;
    CREATE TABLE rel_rng_owner (id serial PRIMARY KEY);
    CREATE TABLE rel_rng_row (id serial PRIMARY KEY, owner_id int NOT NULL, dom rel_rng_dom,
      plain rel_rng, multi rel_rng_multi, doms rel_rng_dom[], ranges rel_rng[]);
    INSERT INTO rel_rng_owner DEFAULT VALUES;
    INSERT INTO rel_rng_row (owner_id, dom, plain, multi, doms, ranges) VALUES
      (1, '[1.10,2.25)', '(,3.5]', '{[1,2),[3.5,4]}', '{"[0.1,0.2)",NULL}', '{empty}'),
      (1, NULL, NULL, NULL, NULL, NULL);
    """
    async with driver.acquire() as conn:
        await conn.execute(drop + ";" + ddl + kinds, [])
    try:
        async with await Database.connect(pg_url) as db:
            plain = await db.select().from_(RngRow).order_by(RngRow.id)
            [owner] = await db.query(RngOwner).find_many(with_={"rows": {"order_by": RngRow.id}})
            assert isinstance(owner.rows[0].dom, Range)
            for got, want in zip(owner.rows, plain, strict=True):
                for attr in ("dom", "plain", "multi", "doms", "ranges"):
                    _assert_same(getattr(got, attr), getattr(want, attr))
            plain_kinds = await db.select().from_(RngKind).order_by(RngKind.id)
            [owner] = await db.query(RngOwner).find_many(with_={"kinds": {"order_by": RngKind.id}})
            assert isinstance(owner.kinds[1].f8, Range)
            for got, want in zip(owner.kinds, plain_kinds, strict=True):
                for attr in values:
                    _assert_same(getattr(got, attr), getattr(want, attr))
    finally:
        async with driver.acquire() as conn:
            await conn.execute(drop, [])

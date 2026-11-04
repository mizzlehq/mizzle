"""EX1-EX11 against a live database: quantified comparison, DISTINCT FROM, json operators,
FILTER/WITHIN GROUP, extra string/math/array/date helpers, COLLATE and AT TIME ZONE."""

import datetime
from decimal import Decimal
from typing import cast

import pytest

from mizzle import (
    Database,
    QueryError,
    age,
    all_,
    any_,
    array_agg,
    array_length,
    array_position,
    array_remove,
    at_time_zone,
    bool_and,
    ceil,
    collate,
    contains,
    count,
    current_timestamp,
    eq,
    get_field,
    get_field_text,
    get_path,
    gt,
    has_all_keys,
    has_key,
    is_distinct_from,
    is_not_distinct_from,
    json_agg,
    jsonb_build_object,
    jsonb_set,
    left,
    localtime,
    make_interval,
    mod,
    mode,
    percentile_cont,
    percentile_disc,
    power,
    random,
    regexp_match,
    replace,
    stddev,
    string_agg,
    strpos,
    substring,
    to_char,
    to_date,
    to_timestamp,
    unnest,
)
from tests.models import Posts, Role, Users


async def seed(db: Database) -> None:
    await db.insert(Users).values(
        [
            {"email": "ann@x", "bio": "cats", "role": Role.ADMIN},
            {"email": "bob@y", "bio": None},
            {"email": "cid@x", "bio": "dogs"},
        ]
    )
    await db.insert(Posts).values(
        [
            {"author_id": 1, "title": "cat", "meta": {"a": {"x": 1}, "tags": ["c", "d"]}},
            {"author_id": 1, "title": "dog", "meta": {"a": 1}},
            {"author_id": 3, "title": "bird", "meta": {"b": 2, "tags": ["e"]}},
        ]
    )


async def test_any_all(db: Database) -> None:
    await seed(db)
    some = await db.select(Users.email).from_(Users).where(eq(Users.id, any_([1, 3])))
    assert sorted(some) == [("ann@x",), ("cid@x",)]
    every = await db.select(Users.id).from_(Users).where(gt(Users.id, all_([0])))
    assert sorted(i for (i,) in every) == [1, 2, 3]


async def test_distinct_from(db: Database) -> None:
    await seed(db)
    distinct = await db.select(Users.email).from_(Users).where(is_distinct_from(Users.bio, None))
    assert sorted(distinct) == [("ann@x",), ("cid@x",)]
    not_distinct = (
        await db.select(Users.email).from_(Users).where(is_not_distinct_from(Users.bio, None))
    )
    assert not_distinct == [("bob@y",)]


async def test_json_operators(db: Database) -> None:
    await seed(db)
    contains_a = await db.select(Posts.title).from_(Posts).where(contains(Posts.meta, {"a": 1}))
    assert contains_a == [("dog",)]
    has = await db.select(Posts.title).from_(Posts).where(has_key(Posts.meta, "a"))
    assert sorted(has) == [("cat",), ("dog",)]
    keys = await db.select(Posts.title).from_(Posts).where(has_all_keys(Posts.meta, ["a", "b"]))
    assert keys == []


async def test_json_access(db: Database) -> None:
    await seed(db)
    field = await db.select(get_field(Posts.meta, "a")).from_(Posts).where(eq(Posts.id, 1))
    assert field == [({"x": 1},)]
    text = await db.select(get_field_text(Posts.meta, "a")).from_(Posts).where(eq(Posts.id, 2))
    assert text == [("1",)]
    path = await db.select(get_path(Posts.meta, ["a", "x"])).from_(Posts).where(eq(Posts.id, 1))
    assert path == [(1,)]


async def test_jsonb_set_and_build(db: Database) -> None:
    await seed(db)
    rows = (
        await db.select(jsonb_set(Posts.meta, ["a"], {"y": 2})).from_(Posts).where(eq(Posts.id, 1))
    )
    assert rows == [({"a": {"y": 2}, "tags": ["c", "d"]},)]
    built = await db.select({"o": jsonb_build_object("n", count())}).from_(Users)
    assert built == [{"o": {"n": 3}}]


async def test_filter_and_within_group(db: Database) -> None:
    await seed(db)
    admins = await db.select(count(Users.id).filter(eq(Users.role, Role.ADMIN))).from_(Users)
    assert admins == [(1,)]
    median = await db.select(percentile_cont(0.5, Users.id)).from_(Users)
    assert median == [(2.0,)]
    top = await db.select(mode(Users.role)).from_(Users)
    assert top == [("user",)]
    disc = await db.select(percentile_disc(0.0, Users.id)).from_(Users)
    assert disc == [(1,)]


async def test_more_aggregates(db: Database) -> None:
    await seed(db)
    row = (
        await db.select(
            {
                "emails": array_agg(Users.email),
                "joined": string_agg(Users.email, ","),
                "positive": bool_and(gt(Users.id, 0)),
                "sd": stddev(Users.id),
                "ids": json_agg(Users.id),
            }
        ).from_(Users)
    )[0]
    assert row["emails"] == ["ann@x", "bob@y", "cid@x"]
    assert row["joined"] == "ann@x,bob@y,cid@x"
    assert row["positive"] is True
    assert row["ids"] == [1, 2, 3]
    assert isinstance(row["sd"], (float, Decimal))


async def test_more_strings(db: Database) -> None:
    await seed(db)
    row = (
        await db.select(
            {
                "sub": substring(Users.email, 1, 3),
                "rep": replace(Users.email, "@", "-"),
                "pos": strpos(Users.email, "@"),
                "l": left(Users.email, 3),
                "m": regexp_match(Users.email, "([a-z]+)@"),
            }
        )
        .from_(Users)
        .where(eq(Users.id, 1))
    )[0]
    assert row == {"sub": "ann", "rep": "ann-x", "pos": 4, "l": "ann", "m": ["ann"]}


async def test_more_math(db: Database) -> None:
    await seed(db)
    row = (
        await db.select({"up": ceil(Users.id), "sq": power(Users.id, 2), "m": mod(Users.id, 2)})
        .from_(Users)
        .where(eq(Users.id, 3))
    )[0]
    assert row["up"] == 3
    assert row["sq"] == 9
    assert row["m"] == 1


async def test_random(db: Database) -> None:
    await seed(db)
    row = (await db.select({"r": random()}).from_(Users).limit(1))[0]
    assert 0.0 <= cast(float, row["r"]) < 1.0


async def test_array_functions(db: Database) -> None:
    await seed(db)
    numbers = db.select({"v": array_agg(Users.id)}).from_(Users).subquery("numbers")
    v = numbers.c("v", list[int])
    assert await db.select(array_length(v)).from_(numbers) == [(3,)]
    assert await db.select(array_position(v, 2)).from_(numbers) == [(2,)]
    rows = await db.select(unnest(v)).from_(numbers)
    assert sorted(r for (r,) in rows) == [1, 2, 3]


async def test_array_remove(db: Database) -> None:
    await seed(db)
    numbers = db.select({"v": array_agg(Users.id)}).from_(Users).subquery("numbers")
    value = (await db.select(array_remove(numbers.c("v", list[int]), 2)).from_(numbers))[0][0]
    assert value == [1, 3]


async def test_date_time_helpers(db: Database) -> None:
    await seed(db)
    row = (
        await db.select(
            {
                "between": age(current_timestamp(), Users.created_at),
                "year": to_char(Users.created_at, "YYYY"),
                "date": to_date("2024-01-02", "YYYY-MM-DD"),
                "epoch": to_timestamp(0),
                "week": make_interval(days=7),
                "now": current_timestamp(),
                "clock": localtime(),
            }
        )
        .from_(Users)
        .where(eq(Users.id, 1))
    )[0]
    assert isinstance(row["between"], datetime.timedelta)
    assert row["date"] == datetime.date(2024, 1, 2)
    assert row["epoch"] == datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
    assert row["week"] == datetime.timedelta(days=7)
    assert isinstance(row["now"], datetime.datetime)
    assert isinstance(row["clock"], datetime.time)
    assert isinstance(row["year"], str) and len(row["year"]) == 4


async def test_collate_and_at_time_zone(db: Database) -> None:
    await seed(db)
    emails = await db.select(collate(Users.email, "C")).from_(Users).order_by(Users.id)
    assert emails == [("ann@x",), ("bob@y",), ("cid@x",)]
    converted = (
        await db.select(at_time_zone(Users.created_at, "UTC")).from_(Users).where(eq(Users.id, 1))
    )[0][0]
    assert isinstance(converted, datetime.datetime)


async def test_enum_any_and_arrays(db: Database) -> None:
    # Q9/Q11/Q13: enum lists/arrays are typed as the enum, not text.
    await seed(db)
    some = await db.select(Users.email).from_(Users).where(eq(Users.role, any_([Role.ADMIN])))
    assert sorted(some) == [("ann@x",)]
    roles = db.select({"v": array_agg(Users.role)}).from_(Users).subquery("roles")
    v = roles.c("v", list[Role])
    assert await db.select(contains(v, [Role.ADMIN])).from_(roles) == [(True,)]
    removed = (await db.select(array_remove(v, Role.ADMIN)).from_(roles))[0][0]
    assert sorted(removed) == [Role.USER, Role.USER]
    order = await db.select(v).from_(roles).order_by(array_length(v))
    assert isinstance(order[0][0][0], Role)
    top = (await db.select(mode(Users.role)).from_(Users))[0][0]
    assert isinstance(top, Role)
    disc = (await db.select(percentile_disc(0.0, Users.role)).from_(Users))[0][0]
    assert isinstance(disc, Role)


async def test_jsonb_set_scalar_and_contains_array(db: Database) -> None:
    # Q12: a scalar set value and a jsonb list operand are encoded as jsonb.
    await seed(db)
    rows = await db.select(jsonb_set(Posts.meta, ["a"], 5)).from_(Posts).where(eq(Posts.id, 1))
    assert rows == [({"a": 5, "tags": ["c", "d"]},)]
    text_rows = (
        await db.select(jsonb_set(Posts.meta, ["a"], "hi")).from_(Posts).where(eq(Posts.id, 1))
    )
    assert text_rows == [({"a": "hi", "tags": ["c", "d"]},)]
    tagged = (
        await db.select(Posts.title)
        .from_(Posts)
        .where(contains(get_field(Posts.meta, "tags"), ["c"]))
    )
    assert tagged == [("cat",)]


async def test_string_keys_are_rejected(db: Database) -> None:
    # Q10: a bare string is a sequence of characters, not a JSON path.
    for call in (
        lambda: get_path(Posts.meta, "ab"),  # pyright: ignore[reportArgumentType]
        lambda: has_all_keys(Posts.meta, "ab"),  # pyright: ignore[reportArgumentType]
    ):
        with pytest.raises(QueryError, match="not a single string"):
            call()

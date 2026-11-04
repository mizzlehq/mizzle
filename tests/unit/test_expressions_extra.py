"""EX1-EX11: quantified comparison, DISTINCT FROM, arrays, json, more string/math/aggregate
helpers, FILTER, WITHIN GROUP, date/time, COLLATE and AT TIME ZONE."""

import pytest

from mizzle import (
    age,
    all_,
    any_,
    array_agg,
    array_length,
    array_position,
    at_time_zone,
    bool_and,
    cast,
    ceil,
    collate,
    contains,
    count,
    current_timestamp,
    eq,
    every,
    get_field,
    get_field_text,
    get_path,
    get_path_text,
    gt,
    has_all_keys,
    has_any_key,
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
    overlaps,
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
from mizzle.core.errors import QueryError
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.sql.raw import sql
from mizzle.pg import array, integer
from tests.models import Posts, Role, Users


def test_any_all(qb: QueryBuilder) -> None:
    assert qb.select(Users.id).from_(Users).where(eq(Users.id, any_([1, 2, 3]))).to_sql() == (
        'SELECT "users"."id" FROM "users" WHERE "users"."id" = ANY (CAST($1 AS bigint[]))',
        ([1, 2, 3],),
    )
    assert (
        qb.select(Users.id)
        .from_(Users)
        .where(gt(Users.id, all_([1, 2])))
        .to_sql()[0]
        .endswith('"users"."id" > ALL (CAST($1 AS bigint[]))')
    )
    with pytest.raises(QueryError, match="at least one value"):
        any_([])


def test_distinct_from(qb: QueryBuilder) -> None:
    assert qb.select(Users.id).from_(Users).where(is_distinct_from(Users.bio, None)).to_sql() == (
        'SELECT "users"."id" FROM "users" WHERE "users"."bio" IS DISTINCT FROM $1',
        (None,),
    )
    not_distinct = (
        qb.select(Users.id).from_(Users).where(is_not_distinct_from(Users.bio, "x")).to_sql()[0]
    )
    assert not_distinct.endswith('"users"."bio" IS NOT DISTINCT FROM $1')


def test_container_operators(qb: QueryBuilder) -> None:
    assert qb.select(Users.id).from_(Users).where(contains(Users.email, ["a", "b"])).to_sql() == (
        'SELECT "users"."id" FROM "users" WHERE "users"."email" @> CAST($1 AS text[])',
        (["a", "b"],),
    )
    json = qb.select(Users.id).from_(Users).where(contains(Posts.meta, {"a": 1})).to_sql()
    assert json[0].endswith('"posts"."meta" @> CAST($1 AS jsonb)')
    assert json[1] == ({"a": 1},)
    assert (
        qb.select(Users.id)
        .from_(Users)
        .where(overlaps(Posts.title, Posts.title))
        .to_sql()[0]
        .endswith('"posts"."title" && "posts"."title"')
    )


def test_json_operators(qb: QueryBuilder) -> None:
    q = qb.select(
        get_field(Posts.meta, "a"),
        get_field_text(Posts.meta, "a"),
        get_path(Posts.meta, ["a", "b"]),
        get_path_text(Posts.meta, ["a", "b"]),
    ).from_(Posts)
    assert q.to_sql() == (
        'SELECT "posts"."meta" -> CAST($1 AS text), "posts"."meta" ->> CAST($2 AS text), '
        '"posts"."meta" #> CAST($3 AS text[]), "posts"."meta" #>> CAST($4 AS text[]) FROM "posts"',
        ("a", "a", ["a", "b"], ["a", "b"]),
    )
    has = qb.select(Users.id).from_(Posts).where(has_key(Posts.meta, "a")).to_sql()
    assert has[0].endswith('"posts"."meta" ? CAST($1 AS text)')
    anyk = qb.select(Users.id).from_(Posts).where(has_any_key(Posts.meta, ["a", "b"])).to_sql()[0]
    assert anyk.endswith('"posts"."meta" ?| CAST($1 AS text[])')
    allk = qb.select(Users.id).from_(Posts).where(has_all_keys(Posts.meta, ["a", "b"])).to_sql()[0]
    assert allk.endswith('"posts"."meta" ?& CAST($1 AS text[])')


def test_jsonb_set_and_build(qb: QueryBuilder) -> None:
    q = qb.select(jsonb_set(Posts.meta, ["a"], {"b": 1})).from_(Posts)
    assert q.to_sql()[0].startswith('SELECT jsonb_set("posts"."meta", CAST($1 AS text[])')
    build = qb.select(jsonb_build_object("e", Users.email, "n", count())).from_(Users)
    assert build.to_sql()[0].startswith("SELECT jsonb_build_object(CAST($1 AS text)")
    with pytest.raises(QueryError, match="key/value pairs"):
        jsonb_build_object("k")


def test_more_aggregates(qb: QueryBuilder) -> None:
    q = qb.select(
        array_agg(Users.email),
        string_agg(Users.email, ","),
        bool_and(gt(Users.id, 0)),
        every(gt(Users.id, 0)),
        stddev(Users.id),
        json_agg(Users.id),
    ).from_(Users)
    assert q.to_sql()[0] == (
        'SELECT array_agg("users"."email"), string_agg("users"."email", CAST($1 AS text)), '
        'bool_and("users"."id" > $2), every("users"."id" > $3), stddev_samp("users"."id"), '
        'json_agg("users"."id") FROM "users"'
    )


def test_filter_and_within_group(qb: QueryBuilder) -> None:
    filtered = qb.select(count(Users.id).filter(eq(Users.role, Role.ADMIN))).from_(Users)
    assert filtered.to_sql() == (
        'SELECT count("users"."id") FILTER (WHERE "users"."role" = $1) FROM "users"',
        ("admin",),
    )
    assert (
        qb.select(percentile_cont(0.5, Users.id))
        .from_(Users)
        .to_sql()[0]
        .endswith('percentile_cont(0.5) WITHIN GROUP (ORDER BY "users"."id" ASC) FROM "users"')
    )
    assert (
        qb.select(mode(Users.role))
        .from_(Users)
        .to_sql()[0]
        .endswith('mode() WITHIN GROUP (ORDER BY "users"."role" ASC) FROM "users"')
    )
    assert (
        qb.select(percentile_disc(0.9, Users.id))
        .from_(Users)
        .to_sql()[0]
        .startswith("SELECT percentile_disc(0.9)")
    )
    with pytest.raises(QueryError, match="between 0 and 1"):
        percentile_cont(2.0, Users.id)


def test_more_strings(qb: QueryBuilder) -> None:
    q = qb.select(
        substring(Users.email, 1, 3),
        replace(Users.email, "a", "b"),
        strpos(Users.email, "a"),
        left(Users.email, 2),
        regexp_match(Users.email, "a(b)"),
    ).from_(Users)
    assert q.to_sql()[0] == (
        'SELECT substr("users"."email", 1, 3), replace("users"."email", CAST($1 AS text), '
        'CAST($2 AS text)), strpos("users"."email", CAST($3 AS text)), left("users"."email", 2), '
        'regexp_match("users"."email", CAST($4 AS text)) FROM "users"'
    )


def test_more_math(qb: QueryBuilder) -> None:
    q = qb.select(ceil(Users.id), power(Users.id, 2), mod(Users.id, 2), random()).from_(Users)
    assert q.to_sql() == (
        'SELECT ceil("users"."id"), power("users"."id", $1), mod("users"."id", $2), random() '
        'FROM "users"',
        (2, 2),
    )


def test_array_functions(qb: QueryBuilder) -> None:
    arr = cast(sql("ARRAY[1,2]"), array(integer()))
    q = qb.select(unnest(arr), array_length(arr), array_position(arr, 2)).from_(Users)
    assert q.to_sql() == (
        "SELECT unnest(CAST(ARRAY[1,2] AS integer[])), "
        "array_length(CAST(ARRAY[1,2] AS integer[]), 1), "
        'array_position(CAST(ARRAY[1,2] AS integer[]), CAST($1 AS bigint)) FROM "users"',
        (2,),
    )


def test_date_time_helpers(qb: QueryBuilder) -> None:
    q = qb.select(
        age(Users.created_at),
        age(Users.created_at, Users.created_at),
        to_char(Users.created_at, "YYYY"),
        to_date("2024-01-02", "YYYY-MM-DD"),
        to_timestamp(0),
        make_interval(days=7),
        current_timestamp(),
        localtime(),
    ).from_(Users)
    sql_text = q.to_sql()[0]
    assert sql_text.startswith('SELECT age("users"."created_at"), age("users"."created_at", ')
    for fragment in (
        'to_char("users"."created_at", CAST($1 AS text))',
        "to_date(CAST($2 AS text), CAST($3 AS text))",
        "to_timestamp(CAST($4 AS bigint))",
        "make_interval(days => 7)",
        "CURRENT_TIMESTAMP",
        "LOCALTIME",
    ):
        assert fragment in sql_text


def test_collate_and_at_time_zone(qb: QueryBuilder) -> None:
    assert qb.select(collate(Users.email, "C")).from_(Users).to_sql() == (
        'SELECT "users"."email" COLLATE "C" FROM "users"',
        (),
    )
    assert qb.select(at_time_zone(Users.created_at, "UTC")).from_(Users).to_sql() == (
        'SELECT "users"."created_at" AT TIME ZONE CAST($1 AS text) FROM "users"',
        ("UTC",),
    )

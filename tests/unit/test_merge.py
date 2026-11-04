"""MERGE (QB14): SQL generation, validation, RETURNING and rowcount."""

import pytest

from mizzle.core.errors import QueryError
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.table import Table
from mizzle.core.schema.types import text
from mizzle.core.sql.operators import eq, gt, is_not_null
from mizzle.dialects.postgres.types import serial
from tests.models import Posts, Users
from tests.unit.conftest import FakeRunner


class Readings(Table, name="merge_readings"):
    id = serial().primary_key()
    sensor = text().not_null()
    unit = text().not_null().default_fn(lambda: "celsius")


def merge_q(qb: QueryBuilder):
    return (
        qb.merge(Posts)
        .using(Users)
        .on(eq(Posts.author_id, Users.id))
        .when_matched_then_update({"title": Users.email}, where=is_not_null(Users.bio))
        .when_not_matched_then_insert({"author_id": Users.id, "title": "seed"})
        .when_not_matched_by_source_then_delete()
    )


def test_merge_sql(qb: QueryBuilder) -> None:
    assert merge_q(qb).to_sql() == (
        'MERGE INTO "posts" USING "users" ON "posts"."author_id" = "users"."id" '
        'WHEN MATCHED AND "users"."bio" IS NOT NULL THEN UPDATE SET "title" = "users"."email" '
        'WHEN NOT MATCHED THEN INSERT ("author_id", "title") VALUES ("users"."id", $1) '
        "WHEN NOT MATCHED BY SOURCE THEN DELETE",
        ("seed",),
    )


def test_merge_matched_delete_and_do_nothing(qb: QueryBuilder) -> None:
    q = (
        qb.merge(Posts)
        .using(Users)
        .on(eq(Posts.author_id, Users.id))
        .when_matched_then_delete(where=gt(Users.id, 10))
        .when_matched_then_do_nothing()
    )
    assert q.to_sql()[0].endswith(
        'WHEN MATCHED AND "users"."id" > $1 THEN DELETE WHEN MATCHED THEN DO NOTHING'
    )


def test_merge_subquery_source(qb: QueryBuilder) -> None:
    source = qb.select(Users.id).from_(Users).where(eq(Users.id, 1)).subquery("src")
    q = (
        qb.merge(Posts)
        .using(source)
        .on(eq(Posts.author_id, source.c("id", int)))
        .when_not_matched_then_insert({"author_id": source.c("id", int), "title": "seed"})
    )
    assert q.to_sql() == (
        'MERGE INTO "posts" USING (SELECT "users"."id" FROM "users" WHERE "users"."id" = $1) '
        'AS "src" ON "posts"."author_id" = "src"."id" '
        'WHEN NOT MATCHED THEN INSERT ("author_id", "title") VALUES ("src"."id", $2)',
        (1, "seed"),
    )


async def test_merge_returns_rowcount(qb: QueryBuilder, runner: FakeRunner) -> None:
    runner.rowcount = 3
    assert await merge_q(qb) == 3


async def test_merge_returning(qb: QueryBuilder, runner: FakeRunner) -> None:
    q = merge_q(qb).returning(Posts.id, Posts.title)
    assert q.to_sql()[0].endswith('RETURNING "posts"."id", "posts"."title"')
    runner.rows = [(1, "a"), (2, "b")]
    assert await q == [(1, "a"), (2, "b")]


def test_merge_validation(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match=r"needs \.using"):
        qb.merge(Posts).on(eq(Posts.author_id, Users.id)).when_matched_then_delete().to_sql()
    with pytest.raises(QueryError, match=r"needs \.on"):
        qb.merge(Posts).using(Users).when_matched_then_delete().to_sql()
    with pytest.raises(QueryError, match="at least one when_"):
        qb.merge(Posts).using(Users).on(eq(Posts.author_id, Users.id)).to_sql()
    with pytest.raises(QueryError, match="no column"):
        qb.merge(Posts).using(Users).on(eq(Posts.author_id, Users.id)).when_matched_then_update(
            {"nope": 1}
        )
    with pytest.raises(QueryError, match="non-empty values"):
        qb.merge(Posts).using(Users).on(eq(Posts.author_id, Users.id)).when_not_matched_then_insert(
            {}
        )


def test_merge_insert_applies_python_defaults(qb: QueryBuilder) -> None:
    # Q4: a column with .default_fn() that the source does not supply is filled here,
    # not left to the database (which has no default for it).
    q = (
        qb.merge(Readings)
        .using(Users)
        .on(eq(Readings.sensor, Users.email))
        .when_not_matched_then_insert({"sensor": Users.email})
    )
    assert q.to_sql() == (
        'MERGE INTO "merge_readings" USING "users" ON "merge_readings"."sensor" = "users"."email" '
        'WHEN NOT MATCHED THEN INSERT ("sensor", "unit") VALUES ("users"."email", $1)',
        ("celsius",),
    )


def test_merge_update_validates_and_needs_a_set(qb: QueryBuilder) -> None:
    # Q5: the same checks Update.set() makes, and no empty SET.
    base = qb.merge(Posts).using(Users).on(eq(Posts.author_id, Users.id))
    with pytest.raises(QueryError, match="non-empty"):
        base.when_matched_then_update({})
    with pytest.raises(QueryError, match="no column"):
        base.when_matched_then_update({"nope": 1})
    with pytest.raises(QueryError, match="expects str"):
        base.when_matched_then_update({"title": 123})

import pytest

from mizzle.core.errors import QueryError
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.table import Table
from mizzle.core.schema.types import integer, text
from mizzle.core.sql.operators import eq, excluded, gt
from mizzle.core.sql.raw import sql
from mizzle.dialects.postgres.types import serial
from tests.models import Posts, Role, Users
from tests.unit.conftest import FakeRunner

USER_COLS = '"users"."id", "users"."email", "users"."bio", "users"."role", "users"."created_at"'


class Tokens(Table):
    id = serial().primary_key()
    value = text().not_null().default_fn(lambda: "generated")


# -- insert ---------------------------------------------------------------------------


def test_insert_one(qb: QueryBuilder) -> None:
    q = qb.insert(Users).values(email="a@b.c", role=Role.ADMIN)
    assert q.to_sql() == (
        'INSERT INTO "users" ("email", "role") VALUES ($1, $2)',
        ("a@b.c", "admin"),
    )


def test_insert_sql_cache_matches_full_path(qb: QueryBuilder) -> None:
    def insert(email: str) -> tuple[str, tuple[object, ...]]:
        return qb.insert(Users).values(email=email, role=Role.ADMIN, bio=None).to_sql()

    first, second = insert("a@x"), insert("b@x")  # the second one uses the cached SQL
    assert first[0] == second[0]
    assert first[1] == ("a@x", None, "admin")  # column order; enum encoded
    assert second[1] == ("b@x", None, "admin")
    # A different column set is a different statement.
    assert qb.insert(Users).values(email="c@x").to_sql()[0] != first[0]


def test_insert_bulk_fills_gaps_with_default(qb: QueryBuilder) -> None:
    q = qb.insert(Users).values([{"email": "a"}, {"email": "b", "bio": "hi"}])
    assert q.to_sql() == (
        'INSERT INTO "users" ("email", "bio") VALUES ($1, DEFAULT), ($2, $3)',
        ("a", "b", "hi"),
    )


def test_insert_all_defaults(qb: QueryBuilder) -> None:
    class AllDefault(Table):
        id = serial().primary_key()

    assert qb.insert(AllDefault).values({}).to_sql() == (
        'INSERT INTO "all_default" DEFAULT VALUES',
        (),
    )
    assert qb.insert(AllDefault).values([{}, {}]).to_sql() == (
        'INSERT INTO "all_default" ("id") VALUES (DEFAULT), (DEFAULT)',
        (),
    )


def test_insert_applies_default_fn(qb: QueryBuilder) -> None:
    assert qb.insert(Tokens).values({}).to_sql() == (
        'INSERT INTO "tokens" ("value") VALUES ($1)',
        ("generated",),
    )


def test_insert_validation(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match="has no column"):
        qb.insert(Users).values(emial="x")
    with pytest.raises(QueryError, match=r"Users\.email is required"):
        qb.insert(Users).values(bio="x")
    with pytest.raises(QueryError, match="expects str"):
        qb.insert(Users).values(email=5)
    with pytest.raises(QueryError, match="NOT NULL"):
        qb.insert(Users).values(email=None)
    with pytest.raises(QueryError, match="at least one row"):
        qb.insert(Users).values()
    with pytest.raises(QueryError, match="empty list"):
        qb.insert(Users).values([])
    with pytest.raises(QueryError, match="not both"):
        qb.insert(Users).values({"email": "a"}, bio="b")
    with pytest.raises(QueryError, match="no values"):
        qb.insert(Users).to_sql()


def test_insert_sql_expression_value(qb: QueryBuilder) -> None:
    q = qb.insert(Users).values(email=sql("lower({})", "A@B.C"))
    assert q.to_sql() == ('INSERT INTO "users" ("email") VALUES (lower($1))', ("A@B.C",))


def test_insert_too_many_params_has_clear_error(qb: QueryBuilder) -> None:
    rows = [{"email": f"u{i}", "bio": "x"} for i in range(16384)]  # 32768 params
    with pytest.raises(QueryError, match="Split it into smaller batches"):
        qb.insert(Users).values(rows).to_sql()


def test_upsert(qb: QueryBuilder) -> None:
    q = (
        qb.insert(Users)
        .values(email="a")
        .on_conflict_do_update(
            target=Users.email, set_={"bio": excluded(Users.bio), "role": Role.ADMIN}
        )
    )
    assert q.to_sql() == (
        'INSERT INTO "users" ("email") VALUES ($1) ON CONFLICT ("email") '
        'DO UPDATE SET "bio" = excluded."bio", "role" = $2',
        ("a", "admin"),
    )


def test_on_conflict_do_nothing(qb: QueryBuilder) -> None:
    sql_text, _ = qb.insert(Users).values(email="a").on_conflict_do_nothing().to_sql()
    assert sql_text.endswith("ON CONFLICT DO NOTHING")


def test_on_conflict_validation(qb: QueryBuilder) -> None:
    q = qb.insert(Users).values(email="a")
    with pytest.raises(QueryError, match="not a column of Users"):
        q.on_conflict_do_nothing(Posts.id)
    with pytest.raises(QueryError, match="non-empty set_"):
        q.on_conflict_do_update(target=Users.email, set_={})
    with pytest.raises(QueryError, match="has no column"):
        q.on_conflict_do_update(target=Users.email, set_={"nope": 1})


async def test_insert_returning(qb: QueryBuilder, runner: FakeRunner) -> None:
    q = qb.insert(Users).values(email="a").returning()
    assert q.to_sql()[0] == f'INSERT INTO "users" ("email") VALUES ($1) RETURNING {USER_COLS}'
    runner.rows = [(1, "a", None, "user", None)]
    [user] = await q
    assert user.id == 1 and user.role is Role.USER


async def test_insert_returns_rowcount(qb: QueryBuilder, runner: FakeRunner) -> None:
    runner.rowcount = 2
    assert await qb.insert(Users).values([{"email": "a"}, {"email": "b"}]) == 2


# -- update / delete ------------------------------------------------------------------


def test_update(qb: QueryBuilder) -> None:
    q = qb.update(Users).set(bio="hi").set(role=Role.ADMIN).where(eq(Users.id, 1))
    assert q.to_sql() == (
        'UPDATE "users" SET "bio" = $1, "role" = $2 WHERE "users"."id" = $3',
        ("hi", "admin", 1),
    )


def test_update_with_expression(qb: QueryBuilder) -> None:
    class Counters(Table):
        id = serial().primary_key()
        n = integer().not_null()

    q = qb.update(Counters).set(n=sql("{} + 1", Counters.n)).where(eq(Counters.id, 1))
    assert (
        q.to_sql()[0] == 'UPDATE "counters" SET "n" = "counters"."n" + 1 WHERE "counters"."id" = $1'
    )


def test_update_without_where_is_blocked(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match=r"no \.where\(\)"):
        qb.update(Users).set(bio="x").to_sql()
    assert qb.update(Users).set(bio="x").all_rows().to_sql() == (
        'UPDATE "users" SET "bio" = $1',
        ("x",),
    )


def test_update_validation(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match=r"needs \.set"):
        qb.update(Users).where(eq(Users.id, 1)).to_sql()
    with pytest.raises(QueryError, match="has no column"):
        qb.update(Users).set(nope=1)
    with pytest.raises(QueryError, match="NOT NULL"):
        qb.update(Users).set(email=None)


def test_delete(qb: QueryBuilder) -> None:
    q = qb.delete(Users).where(gt(Users.id, 5)).where(eq(Users.role, Role.USER))
    assert q.to_sql() == (
        'DELETE FROM "users" WHERE "users"."id" > $1 AND "users"."role" = $2',
        (5, "user"),
    )


def test_delete_without_where_is_blocked(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match=r"call \.all_rows"):
        qb.delete(Users).to_sql()
    assert qb.delete(Users).all_rows().to_sql() == ('DELETE FROM "users"', ())


async def test_delete_returning(qb: QueryBuilder, runner: FakeRunner) -> None:
    q = qb.delete(Users).where(eq(Users.id, 1)).returning()
    assert q.to_sql()[0].endswith(f"RETURNING {USER_COLS}")
    runner.rows = [(1, "a", None, "admin", None)]
    assert [u.email for u in await q] == ["a"]

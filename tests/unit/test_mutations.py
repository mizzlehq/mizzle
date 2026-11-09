from typing import cast

import pytest

from mizzle.core.errors import QueryError
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.table import Table
from mizzle.core.schema.types import integer, text
from mizzle.core.sql.nodes import ExprBase
from mizzle.core.sql.operators import count, eq, excluded, gt
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


def test_on_conflict_on_constraint(qb: QueryBuilder) -> None:
    q = (
        qb.insert(Users)
        .values(email="a")
        .on_conflict_do_update(constraint="users_email_key", set_={"bio": "x"})
    )
    assert q.to_sql() == (
        'INSERT INTO "users" ("email") VALUES ($1) '
        'ON CONFLICT ON CONSTRAINT "users_email_key" DO UPDATE SET "bio" = $2',
        ("a", "x"),
    )
    nothing = (
        qb.insert(Users).values(email="a").on_conflict_do_nothing(constraint="users_email_key")
    )
    assert nothing.to_sql()[0].endswith('ON CONFLICT ON CONSTRAINT "users_email_key" DO NOTHING')


def test_on_conflict_target_or_constraint(qb: QueryBuilder) -> None:
    q = qb.insert(Users).values(email="a")
    with pytest.raises(QueryError, match="not both"):
        q.on_conflict_do_nothing(target=Users.email, constraint="users_email_key")
    with pytest.raises(QueryError, match="needs a target or a constraint"):
        q.on_conflict_do_update(set_={"bio": "x"})
    with pytest.raises(QueryError, match="cannot be empty"):
        q.on_conflict_do_nothing(constraint="")


def test_insert_from_select(qb: QueryBuilder) -> None:
    source = qb.select(Users.email, Users.bio).from_(Users).where(eq(Users.role, Role.ADMIN))
    q = qb.insert(Users).from_select([Users.email, Users.bio], source)
    assert q.to_sql() == (
        'INSERT INTO "users" ("email", "bio") '
        'SELECT "users"."email", "users"."bio" FROM "users" WHERE "users"."role" = $1',
        ("admin",),
    )


def test_insert_from_select_validation(qb: QueryBuilder) -> None:
    one = qb.select(Users.email).from_(Users)
    with pytest.raises(QueryError, match="outputs 1"):
        qb.insert(Users).from_select([Users.email, Users.bio], one)
    with pytest.raises(QueryError, match="not a column of Users"):
        qb.insert(Users).from_select(Posts.title, qb.select(Posts.title).from_(Posts))
    with pytest.raises(QueryError, match="at least one target column"):
        qb.insert(Users).from_select([], one)
    with pytest.raises(QueryError, match="already has values"):
        qb.insert(Users).values(email="a").from_select(Users.email, one)
    with pytest.raises(QueryError, match="already has from_select"):
        qb.insert(Users).from_select(Users.email, one).values(email="a")


async def test_insert_returning(qb: QueryBuilder, runner: FakeRunner) -> None:
    q = qb.insert(Users).values(email="a").returning()
    assert q.to_sql()[0] == f'INSERT INTO "users" ("email") VALUES ($1) RETURNING {USER_COLS}'
    runner.rows = [(1, "a", None, "user", None)]
    [user] = await q
    assert user.id == 1 and user.role is Role.USER


async def test_insert_returns_rowcount(qb: QueryBuilder, runner: FakeRunner) -> None:
    runner.rowcount = 2
    assert await qb.insert(Users).values([{"email": "a"}, {"email": "b"}]) == 2


def test_returning_expressions_sql(qb: QueryBuilder) -> None:
    q = qb.insert(Users).values(email="a").returning(Users.id, Users.email)
    assert q.to_sql() == (
        'INSERT INTO "users" ("email") VALUES ($1) RETURNING "users"."id", "users"."email"',
        ("a",),
    )
    mapped = (
        qb.update(Users)
        .set(bio="x")
        .where(eq(Users.id, 1))
        .returning({"id": Users.id, "n": count()})
    )
    assert mapped.to_sql() == (
        'UPDATE "users" SET "bio" = $1 WHERE "users"."id" = $2 '
        'RETURNING "users"."id" AS "id", count(*) AS "n"',
        ("x", 1),
    )


async def test_returning_expressions_decode(qb: QueryBuilder, runner: FakeRunner) -> None:
    q = qb.delete(Users).where(eq(Users.id, 1)).returning(Users.id, Users.email)
    runner.rows = [(1, "a@b.c"), (2, "c@d.e")]
    assert await q == [(1, "a@b.c"), (2, "c@d.e")]


async def test_returning_mapping_decodes(qb: QueryBuilder, runner: FakeRunner) -> None:
    q = qb.update(Users).set(bio="x").where(eq(Users.id, 1)).returning({"id": Users.id})
    runner.rows = [(1,)]
    assert await q == [{"id": 1}]


async def test_returning_projection_decodes_enum(qb: QueryBuilder, runner: FakeRunner) -> None:
    # A projected column is decoded through its own decoder, so an enum comes back as enum.
    q = qb.delete(Users).where(eq(Users.id, 1)).returning(Users.role)
    runner.rows = [("admin",)]
    assert await q == [(Role.ADMIN,)]


def test_returning_validation(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match="takes expressions"):
        qb.delete(Users).where(eq(Users.id, 1)).returning(cast("ExprBase", cast(object, Users)))
    with pytest.raises(QueryError, match="at least one field"):
        qb.delete(Users).where(eq(Users.id, 1)).returning({})


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


def test_update_from(qb: QueryBuilder) -> None:
    q = qb.update(Posts).set(title=Users.bio).from_(Users).where(eq(Posts.author_id, Users.id))
    assert q.to_sql() == (
        'UPDATE "posts" SET "title" = "users"."bio" FROM "users" '
        'WHERE "posts"."author_id" = "users"."id"',
        (),
    )
    with pytest.raises(QueryError, match="at least one table"):
        qb.update(Posts).set(title="x").from_()


def test_update_from_and_delete_using_accept_a_cte(qb: QueryBuilder) -> None:
    # Q14: a CTE/subquery is a valid source for UPDATE ... FROM and DELETE ... USING,
    # not only a table.
    sub = qb.select({"uid": Users.id}).from_(Users).subquery("u")
    uid = sub.c("uid", int)
    upd = qb.update(Posts).set(title="x").from_(sub).where(eq(Posts.author_id, uid))
    assert upd.to_sql() == (
        'UPDATE "posts" SET "title" = $1 '
        'FROM (SELECT "users"."id" AS "uid" FROM "users") AS "u" '
        'WHERE "posts"."author_id" = "u"."uid"',
        ("x",),
    )
    dele = qb.delete(Posts).using(sub).where(eq(Posts.author_id, uid))
    assert dele.to_sql() == (
        'DELETE FROM "posts" USING (SELECT "users"."id" AS "uid" FROM "users") AS "u" '
        'WHERE "posts"."author_id" = "u"."uid"',
        (),
    )


def test_delete_using(qb: QueryBuilder) -> None:
    q = qb.delete(Posts).using(Users).where(eq(Posts.author_id, Users.id))
    assert q.to_sql() == (
        'DELETE FROM "posts" USING "users" WHERE "posts"."author_id" = "users"."id"',
        (),
    )
    with pytest.raises(QueryError, match="at least one table"):
        qb.delete(Posts).using()


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


async def test_prepared_update_runs_on_update_hook_each_time(runner: FakeRunner) -> None:
    from itertools import count

    from mizzle.core.sql.operators import placeholder

    ticks = count(1)

    class Edited(Table, name="edited"):
        id = serial().primary_key()
        body = text()
        version = integer().on_update(lambda: next(ticks))

    qb = QueryBuilder(runner)
    query = qb.update(Edited).set(body="x").where(eq(Edited.id, placeholder("id"))).prepare()
    await query.execute(id=1)
    await query.execute(id=2)
    # prepare() compiled once (tick 1); each execution ran the hook again (2, 3).
    assert [q.params for q in runner.executed] == [("x", 2, 1), ("x", 3, 2)]

import pytest

from mizzle.core.errors import QueryError
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.sql.operators import count, desc, eq, exists, gt, in_, placeholder
from tests.models import Posts, Role, Users
from tests.unit.conftest import FakeRunner

USER_COLS = '"users"."id", "users"."email", "users"."bio", "users"."role", "users"."created_at"'
POST_COLS = '"posts"."id", "posts"."author_id", "posts"."title", "posts"."meta"'


def test_select_all(qb: QueryBuilder) -> None:
    assert qb.select().from_(Users).to_sql() == (f'SELECT {USER_COLS} FROM "users"', ())


def test_clauses_in_order(qb: QueryBuilder) -> None:
    q = (
        qb.select(Users.id)
        .from_(Users)
        .where(eq(Users.role, Role.ADMIN))
        .order_by(desc(Users.id))
        .limit(10)
        .offset(5)
    )
    assert q.to_sql() == (
        'SELECT "users"."id" FROM "users" WHERE "users"."role" = $1 '
        'ORDER BY "users"."id" DESC LIMIT $2 OFFSET $3',
        ("admin", 10, 5),
    )


def test_where_twice_ands(qb: QueryBuilder) -> None:
    sql, params = (
        qb.select(Users.id).from_(Users).where(gt(Users.id, 1)).where(gt(Users.id, 2)).to_sql()
    )
    assert sql.endswith('WHERE "users"."id" > $1 AND "users"."id" > $2')
    assert params == (1, 2)


def test_builders_are_immutable(qb: QueryBuilder) -> None:
    base = qb.select(Users.id).from_(Users)
    base.where(eq(Users.id, 1))
    assert "WHERE" not in base.to_sql()[0]


def test_mapping_projection_with_group_by(qb: QueryBuilder) -> None:
    q = (
        qb.select({"author": Posts.author_id, "n": count()})
        .from_(Posts)
        .group_by(Posts.author_id)
        .having(gt(count(), 2))
    )
    assert q.to_sql() == (
        'SELECT "posts"."author_id" AS "author", count(*) AS "n" FROM "posts" '
        'GROUP BY "posts"."author_id" HAVING count(*) > $1',
        (2,),
    )


def test_join_sql(qb: QueryBuilder) -> None:
    q = qb.select(Users, Posts).from_(Users).left_join(Posts, eq(Posts.author_id, Users.id))
    assert q.to_sql() == (
        f'SELECT {USER_COLS}, {POST_COLS} FROM "users" '
        'LEFT JOIN "posts" ON "posts"."author_id" = "users"."id"',
        (),
    )


def test_right_join_needs_explicit_projection(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match="right join needs an explicit select"):
        qb.select().from_(Users).right_join(Posts, eq(Posts.author_id, Users.id))


def test_distinct_and_lock(qb: QueryBuilder) -> None:
    sql, _ = (
        qb.select(Users.id)
        .from_(Users)
        .distinct()
        .for_update(of=[Users], skip_locked=True)
        .to_sql()
    )
    assert sql == 'SELECT DISTINCT "users"."id" FROM "users" FOR UPDATE OF "users" SKIP LOCKED'
    with pytest.raises(QueryError):
        qb.select().from_(Users).for_update(skip_locked=True, nowait=True)


@pytest.mark.parametrize("bad", [-1, True])
def test_limit_validation(qb: QueryBuilder, bad: int) -> None:
    with pytest.raises(QueryError):
        qb.select().from_(Users).limit(bad)


def test_select_validation(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError):
        qb.select({})
    with pytest.raises(QueryError):
        qb.select("id")  # type: ignore[call-overload]  # pyright: ignore[reportCallIssue, reportArgumentType]


def test_subqueries(qb: QueryBuilder) -> None:
    authors = qb.select(Posts.author_id).from_(Posts).where(eq(Posts.title, "x"))
    sql, params = qb.select(Users.id).from_(Users).where(in_(Users.id, authors)).to_sql()
    assert sql == (
        'SELECT "users"."id" FROM "users" WHERE "users"."id" IN '
        '(SELECT "posts"."author_id" FROM "posts" WHERE "posts"."title" = $1)'
    )
    assert params == ("x",)
    has_posts = qb.select(Posts.id).from_(Posts).where(eq(Posts.author_id, Users.id))
    sql, _ = qb.select(Users.id).from_(Users).where(exists(has_posts)).to_sql()
    assert sql.endswith(
        'WHERE EXISTS (SELECT "posts"."id" FROM "posts" WHERE "posts"."author_id" = "users"."id")'
    )


def test_cte(qb: QueryBuilder) -> None:
    admins = (
        qb.select(Users.id, Users.email)
        .from_(Users)
        .where(eq(Users.role, Role.ADMIN))
        .cte("admins")
    )
    q = (
        qb.with_(admins)
        .select(admins.c("email", str))
        .from_(admins)
        .where(gt(admins.c("id", int), 3))
    )
    assert q.to_sql() == (
        'WITH "admins" AS (SELECT "users"."id", "users"."email" FROM "users" '
        'WHERE "users"."role" = $1) SELECT "admins"."email" FROM "admins" WHERE "admins"."id" > $2',
        ("admin", 3),
    )


async def test_decode_whole_rows(qb: QueryBuilder, runner: FakeRunner) -> None:
    runner.rows = [(1, "a@b.c", None, "admin", None)]
    [user] = await qb.select().from_(Users)
    assert isinstance(user, Users)
    assert user.id == 1
    assert user.role is Role.ADMIN
    assert user.bio is None


async def test_decode_tuples_and_outer_join_none(qb: QueryBuilder, runner: FakeRunner) -> None:
    runner.rows = [(1, "a@b.c", None, "user", None, None, None, None, None)]
    rows = (
        await qb.select(Users, Posts).from_(Users).left_join(Posts, eq(Posts.author_id, Users.id))
    )
    user, post = rows[0]
    assert user.email == "a@b.c"
    assert post is None


async def test_decode_columns_and_mapping(qb: QueryBuilder, runner: FakeRunner) -> None:
    runner.rows = [(7, "user")]
    assert await qb.select(Users.id, Users.role).from_(Users) == [(7, Role.USER)]
    assert await qb.select({"id": Users.id, "role": Users.role}).from_(Users) == [
        {"id": 7, "role": Role.USER}
    ]


async def test_placeholder_requires_prepare(qb: QueryBuilder, runner: FakeRunner) -> None:
    q = (
        qb.select(Users.id)
        .from_(Users)
        .where(eq(Users.role, placeholder("role")))
        .limit(placeholder("n"))
    )
    with pytest.raises(QueryError, match="prepare"):
        await q
    prepared = q.prepare()
    runner.rows = [(1,)]
    assert await prepared.execute(role=Role.ADMIN, n=5) == [(1,)]
    assert runner.executed[-1].params == ("admin", 5)
    await prepared.execute(role=Role.USER, n=1)
    assert runner.executed[-1].params == ("user", 1)

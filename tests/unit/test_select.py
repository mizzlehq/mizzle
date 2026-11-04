from typing import Literal, cast

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


def test_distinct_on(qb: QueryBuilder) -> None:
    q = (
        qb.select(Users.id, Users.email)
        .from_(Users)
        .distinct_on(Users.email)
        .order_by(desc(Users.id))
    )
    assert q.to_sql() == (
        'SELECT DISTINCT ON ("users"."email") "users"."id", "users"."email" '
        'FROM "users" ORDER BY "users"."id" DESC',
        (),
    )


def test_distinct_on_validation(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match="at least one expression"):
        qb.select().from_(Users).distinct_on()
    with pytest.raises(QueryError, match="cannot be combined"):
        qb.select(Users.id).from_(Users).distinct().distinct_on(Users.id).to_sql()


def test_row_lock_strengths(qb: QueryBuilder) -> None:
    base = qb.select(Users.id).from_(Users)
    assert base.for_share().to_sql()[0].endswith("FOR SHARE")
    assert base.for_no_key_update().to_sql()[0].endswith("FOR NO KEY UPDATE")
    assert (
        base.for_key_share(of=[Users], nowait=True)
        .to_sql()[0]
        .endswith('FOR KEY SHARE OF "users" NOWAIT')
    )
    with pytest.raises(QueryError, match="not both"):
        base.for_share(skip_locked=True, nowait=True)


async def test_first_one_scalar(qb: QueryBuilder, runner: FakeRunner) -> None:
    query = qb.select(Users.id).from_(Users)
    runner.rows = [(1,)]
    assert await query.first() == (1,)
    runner.rows = []
    assert await query.first() is None
    assert await query.scalar() is None
    runner.rows = [(7,)]
    assert await query.scalar() == 7
    assert await query.one() == (7,)
    assert await qb.select({"n": Users.id}).from_(Users).scalar() == 7
    runner.rows = [(1,), (2,)]
    with pytest.raises(QueryError, match="more than one row"):
        await query.one()
    runner.rows = []
    with pytest.raises(QueryError, match="no rows"):
        await query.one()


async def test_scalar_shape_validation(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match="exactly one selected column"):
        await qb.select(Users.id, Users.email).from_(Users).scalar()
    with pytest.raises(QueryError, match="explicit select"):
        await qb.select().from_(Users).scalar()


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


def test_join_lateral(qb: QueryBuilder) -> None:
    top = (
        qb.select({"author": Posts.author_id, "n": count()})
        .from_(Posts)
        .group_by(Posts.author_id)
        .subquery("top")
    )
    author, n = top.c("author", int), top.c("n", int)
    q = qb.select(Users.email, n).from_(Users).left_join_lateral(top, eq(author, Users.id))
    assert q.to_sql() == (
        'SELECT "users"."email", "top"."n" FROM "users" LEFT JOIN LATERAL '
        '(SELECT "posts"."author_id" AS "author", count(*) AS "n" FROM "posts" '
        'GROUP BY "posts"."author_id") AS "top" ON "top"."author" = "users"."id"',
        (),
    )
    cross = (
        qb.select(Users.id)
        .from_(Users)
        .cross_join_lateral(qb.select(Posts.id).from_(Posts).subquery("p"))
    )
    assert cross.to_sql()[0].startswith(
        'SELECT "users"."id" FROM "users" CROSS JOIN LATERAL (SELECT "posts"."id"'
    )
    inner = qb.select(Posts.id).from_(Posts).subquery("p")
    assert (
        qb.select(Users.id)
        .from_(Users)
        .join_lateral(inner, eq(Posts.author_id, Users.id))
        .to_sql()[0]
        .startswith('SELECT "users"."id" FROM "users" INNER JOIN LATERAL (')
    )


def test_rollup_cube_grouping_sets(qb: QueryBuilder) -> None:
    base = qb.select(Users.role, count()).from_(Users)
    assert base.rollup(Users.role).to_sql()[0].endswith('GROUP BY ROLLUP ("users"."role")')
    assert base.cube(Users.role).to_sql()[0].endswith('GROUP BY CUBE ("users"."role")')
    assert (
        base.grouping_sets([Users.role], [])
        .to_sql()[0]
        .endswith('GROUP BY GROUPING SETS (("users"."role"), ())')
    )
    with pytest.raises(QueryError, match="at least one expression"):
        base.rollup()
    with pytest.raises(QueryError, match="at least one expression"):
        base.cube()
    with pytest.raises(QueryError, match="at least one set"):
        base.grouping_sets()


def test_tablesample(qb: QueryBuilder) -> None:
    q = qb.select(Users.id).from_(Users).tablesample("SYSTEM", 10, seed=42)
    assert q.to_sql() == (
        'SELECT "users"."id" FROM "users" TABLESAMPLE SYSTEM ($1) REPEATABLE ($2)',
        (10, 42),
    )
    # Q8: the seed and percent keep full precision (bound, not formatted with :g).
    precise = qb.select(Users.id).from_(Users).tablesample("SYSTEM", 12.3456789, seed=123456789)
    assert precise.to_sql() == (
        'SELECT "users"."id" FROM "users" TABLESAMPLE SYSTEM ($1) REPEATABLE ($2)',
        (12.3456789, 123456789),
    )
    plain = qb.select(Users.id).from_(Users).tablesample("BERNOULLI", 2.5)
    assert plain.to_sql() == (
        'SELECT "users"."id" FROM "users" TABLESAMPLE BERNOULLI ($1)',
        (2.5,),
    )
    with pytest.raises(QueryError, match="between 0 and 100"):
        qb.select().from_(Users).tablesample("SYSTEM", 101)
    with pytest.raises(QueryError, match="finite"):
        qb.select().from_(Users).tablesample("SYSTEM", 10, seed=float("nan"))
    with pytest.raises(QueryError, match="SYSTEM or BERNOULLI"):
        bad = cast("Literal['SYSTEM', 'BERNOULLI']", cast(object, "NOPE"))
        qb.select().from_(Users).tablesample(bad, 10)


def test_recursive_cte(qb: QueryBuilder) -> None:
    seed = qb.select({"n": Users.id}).from_(Users).where(eq(Users.id, 3)).cte("walk")
    n = seed.c("n", int)
    step = qb.select({"n": n - 1}).from_(seed).where(gt(n, 1))
    tree = (
        qb.select({"n": Users.id})
        .from_(Users)
        .where(eq(Users.id, 3))
        .union_all(step)
        .cte("walk", recursive=True)
    )
    q = qb.with_(tree).select(tree.c("n", int)).from_(tree).order_by(desc(tree.c("n", int)))
    assert q.to_sql() == (
        'WITH RECURSIVE "walk" AS ((SELECT "users"."id" AS "n" FROM "users" '
        'WHERE "users"."id" = $1) UNION ALL (SELECT ("walk"."n" - $2) AS "n" FROM "walk" '
        'WHERE "walk"."n" > $3)) SELECT "walk"."n" FROM "walk" ORDER BY "walk"."n" DESC',
        (3, 1, 1),
    )


def test_data_modifying_cte(qb: QueryBuilder) -> None:
    moved = qb.delete(Posts).where(eq(Posts.title, "x")).returning().cte("moved")
    q = qb.with_(moved).select(count()).from_(moved)
    assert q.to_sql() == (
        'WITH "moved" AS (DELETE FROM "posts" WHERE "posts"."title" = $1 RETURNING '
        '"posts"."id", "posts"."author_id", "posts"."title", "posts"."meta") '
        'SELECT count(*) FROM "moved"',
        ("x",),
    )


def test_dict_returning_inside_a_cte_is_aliased(qb: QueryBuilder) -> None:
    # Q7: a named RETURNING projection must alias its outputs or the CTE column does
    # not exist (``m.c("c", ...)`` -> column m.c does not exist).
    m = qb.delete(Posts).where(eq(Posts.id, 1)).returning({"c": Posts.id}).cte("m")
    q = qb.with_(m).select(m.c("c", int)).from_(m)
    assert q.to_sql() == (
        'WITH "m" AS (DELETE FROM "posts" WHERE "posts"."id" = $1 '
        'RETURNING "posts"."id" AS "c") SELECT "m"."c" FROM "m"',
        (1,),
    )

"""Static type tests. Checked by pyright and mypy (not run by pytest).

Lines marked "# type: ignore" / "# pyright: ignore" MUST error; both checkers report
unused ignores, so if an expected error disappears, CI fails.
"""

from __future__ import annotations

import datetime
from typing import Any, assert_type

from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.column import Column
from mizzle.core.sql.operators import count, eq, nullable
from tests.models import Posts, Role, Users


async def rows(qb: QueryBuilder, user: Users, post: Posts) -> None:
    assert_type(Users.id, Column[int])
    assert_type(Users.bio, Column[str | None])
    assert_type(user.id, int)
    assert_type(user.bio, str | None)
    assert_type(user.role, Role)
    assert_type(user.created_at, datetime.datetime)
    assert_type(post.meta, dict[str, Any] | None)

    assert_type(await qb.select().from_(Users), list[Users])
    assert_type(await qb.select(Users.id, Users.email).from_(Users), list[tuple[int, str]])
    assert_type(await qb.select(Users).from_(Users), list[tuple[Users]])
    assert_type(
        await qb.select(Users, Posts).from_(Users).left_join(Posts, eq(Posts.author_id, Users.id)),
        list[tuple[Users, Posts | None]],
    )
    assert_type(
        await qb.select(Users, Posts).from_(Users).right_join(Posts, eq(Posts.author_id, Users.id)),
        list[tuple[Users | None, Posts]],
    )
    assert_type(
        await qb.select({"n": count()}).from_(Users).where(eq(Users.id, 1)),
        list[dict[str, object]],
    )
    # select() + join filters by the join but still returns whole rows of the FROM table
    assert_type(
        await qb.select().from_(Users).join(Posts, eq(Posts.author_id, Users.id)), list[Users]
    )


def errors() -> None:
    eq(Users.id, "x")  # type: ignore[misc]  # pyright: ignore[reportArgumentType]


async def relational(qb: QueryBuilder) -> None:
    users = await qb.query(Users).find_many(with_={"posts": {"with_": {"author": True}}})
    assert_type(users, list[Users])
    assert_type(users[0].posts, list[Posts])
    assert_type(users[0].posts[0].author, Users | None)
    assert_type(await qb.query(Users).find_first(), Users | None)


async def left_join_columns(qb: QueryBuilder) -> None:
    rows = await (
        qb.select(Users.id, nullable(Posts.title))
        .from_(Users)
        .left_join(Posts, eq(Posts.author_id, Users.id))
    )
    assert_type(rows, list[tuple[int, str | None]])


async def expressions(qb: QueryBuilder) -> None:
    from decimal import Decimal

    from mizzle import (
        abs_,
        alias,
        case_,
        cast,
        coalesce,
        concat,
        current_date,
        date_trunc,
        desc,
        extract,
        func,
        greatest,
        lag,
        least,
        length,
        lower,
        matches,
        now,
        nullif,
        rank,
        round_,
        row_number,
        sum_,
        to_tsquery,
        to_tsvector,
        ts_rank,
    )
    from mizzle.core.sql.nodes import Expr
    from mizzle.pg import integer, text

    assert_type(Users.id + 1, Expr[int])
    assert_type(1 + Users.id, Expr[int])
    assert_type(Users.id * Users.id - 2, Expr[int])
    assert_type(Users.id + nullable(Posts.id), Expr[int | None])
    assert_type(count() / 2, Expr[int])
    _ = Users.id + 1.5  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]
    # Not numeric (mypy misses this one):
    _ = Users.email + 1  # pyright: ignore[reportOperatorIssue]

    no_else: Expr[str | None] = case_().when(eq(Users.id, 1), "one")
    _ = no_else
    assert_type(case_().when(eq(Users.id, 1), "one").else_("other"), Expr[str])
    assert_type(case_().when(eq(Users.id, 1), Users.bio).else_("other"), Expr[str | None])
    assert_type(case_().when(eq(Users.id, 1), 1).when(eq(Users.id, 2), 2).else_(0), Expr[int])
    assert_type(case_().when(eq(Users.id, 1), 1).else_(None), Expr[int | None])
    case_().when(eq(Users.id, 1), 1).else_("x")  # type: ignore[call-overload]  # pyright: ignore[reportCallIssue, reportArgumentType]

    assert_type(coalesce(Users.bio, "none"), Expr[str])
    assert_type(coalesce(Users.bio, Users.email), Expr[str])
    assert_type(coalesce(Users.bio, nullable(Users.email)), Expr[str | None])
    coalesce(Users.bio, 1)  # type: ignore[misc]  # pyright: ignore[reportCallIssue, reportArgumentType]
    assert_type(nullif(Users.email, ""), Expr[str | None])
    assert_type(greatest(Users.id, 5), Expr[int])
    assert_type(least(nullable(Users.id), 5), Expr[int | None])
    assert_type(cast(Users.id, text()), Expr[str | None])
    assert_type(cast(Users.email, integer().not_null()), Expr[int])

    assert_type(lower(Users.email), Expr[str])
    assert_type(lower(Users.bio), Expr[str | None])
    assert_type(length(Users.bio), Expr[int | None])
    assert_type(concat(Users.email, "-", Users.id), Expr[str])
    assert_type(abs_(Users.id), Expr[int])
    round_(cast(Users.id, text()))  # type: ignore[type-var]  # pyright: ignore[reportArgumentType]
    assert_type(round_(Users.id, 2), Expr[Decimal])
    assert_type(now(), Expr[datetime.datetime])
    assert_type(current_date(), Expr[datetime.date])
    assert_type(date_trunc("month", Users.created_at), Expr[datetime.datetime])
    assert_type(extract("year", Users.created_at), Expr[Decimal])
    date_trunc("fortnight", Users.created_at)  # type: ignore[call-overload]  # pyright: ignore[reportCallIssue, reportArgumentType]
    assert_type(func[float]("random"), func[float])

    assert_type(row_number().over(order_by=Users.id), Expr[int])
    assert_type(rank().over(partition_by=Users.role, order_by=desc(Users.id)), Expr[int])
    assert_type(lag(Users.email).over(order_by=Users.id), Expr[str | None])
    assert_type(lag(Users.email, 1, "").over(order_by=Users.id), Expr[str])
    assert_type(sum_(Users.id).over(partition_by=Users.role), Expr[int | Decimal | None])
    eq(row_number(), 1)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]

    vector = to_tsvector(Posts.title, config="english")
    assert_type(vector, Expr[str])
    assert_type(matches(vector, to_tsquery("cat")), Expr[bool])
    assert_type(ts_rank(vector, to_tsquery("cat")), Expr[float])

    manager = alias(Users, "manager")
    assert_type(manager.id, Column[int])
    rows = await qb.select(Users, manager).from_(Users).left_join(manager, eq(manager.id, Users.id))
    assert_type(rows, list[tuple[Users, Users | None]])

    union = qb.select(Users.id).from_(Users).union(qb.select(Posts.author_id).from_(Posts))
    assert_type(await union.order_by(Users.id).limit(5), list[tuple[int]])
    qb.select(Users.id).from_(Users).union(qb.select(Users.email).from_(Users))  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]

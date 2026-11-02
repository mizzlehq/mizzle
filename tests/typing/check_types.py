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

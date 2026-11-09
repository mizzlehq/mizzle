"""Static type tests. Checked by pyright and mypy (not run by pytest).

Lines marked "# type: ignore" / "# pyright: ignore" MUST error; both checkers report
unused ignores, so if an expected error disappears, CI fails.
"""

from __future__ import annotations

import datetime
from typing import Any, TypedDict, assert_type

from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.column import Column
from mizzle.core.sql.nodes import Aggregate, Expr, Json
from mizzle.core.sql.operators import count, eq, json_agg, lt, nullable
from mizzle.dialects.postgres.types import CustomColumn
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
    # C34: the joined table may sit in any slot; that slot is the optional one.
    on = eq(Users.id, Posts.author_id)
    assert_type(
        await qb.select(Users, Posts).from_(Posts).left_join(Users, on),
        list[tuple[Users | None, Posts]],
    )
    assert_type(await qb.select(Users).from_(Posts).left_join(Users, on), list[tuple[Users | None]])
    assert_type(
        await qb.select(Users, Posts, Users.id).from_(Posts).left_join(Users, on),
        list[tuple[Users | None, Posts, int]],
    )
    assert_type(await qb.select(Users).from_(Posts).full_join(Users, on), list[tuple[Users | None]])
    assert_type(
        await qb.select(Users.id, Users, Posts).from_(Users).right_join(Posts, on),
        list[tuple[int | None, Users | None, Posts]],
    )
    # Q3: right/full join with the joined table first, and 4 slots.
    assert_type(
        await qb.select(Posts, Users).from_(Users).right_join(Posts, on),
        list[tuple[Posts, Users | None]],
    )
    assert_type(
        await qb.select(Posts, Users).from_(Users).full_join(Posts, on),
        list[tuple[Posts | None, Users | None]],
    )
    assert_type(
        await qb.select(Users, Users.id, Users.email, Posts).from_(Users).left_join(Posts, on),
        list[tuple[Users, int, str, Posts | None]],
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
    eq(Users.id, "x")  # type: ignore[arg-type, misc]  # pyright: ignore[reportCallIssue, reportArgumentType]


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
        age,
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
        left,
        length,
        log,
        lower,
        matches,
        md5,
        mod,
        now,
        nullif,
        percentile_cont,
        power,
        rank,
        regexp_match,
        replace,
        round_,
        row_number,
        starts_with,
        strpos,
        sum_,
        to_char,
        to_date,
        to_tsquery,
        to_tsvector,
        ts_rank,
    )
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
    # T2: the newer string/date functions accept nullable columns and stay nullable.
    assert_type(replace(Users.bio, "a", "b"), Expr[str | None])
    assert_type(md5(Users.bio), Expr[str | None])
    assert_type(starts_with(Users.bio, "a"), Expr[bool | None])
    assert_type(strpos(Users.bio, "a"), Expr[int | None])
    assert_type(left(Users.bio, 2), Expr[str | None])
    # B13: a non-null input gives a non-null result; a text column is not a date.
    assert_type(age(Users.created_at), Expr[datetime.timedelta])
    assert_type(age(Users.created_at, Users.created_at), Expr[datetime.timedelta])
    assert_type(age(Users.created_at, nullable(Users.created_at)), Expr[datetime.timedelta | None])
    age(Users.email)  # type: ignore[arg-type]  # pyright: ignore[reportCallIssue, reportArgumentType]
    assert_type(to_char(Users.created_at, "YYYY"), Expr[str])
    assert_type(to_char(Users.id, "999"), Expr[str])
    assert_type(regexp_match(Users.bio, "a(b)"), Expr[list[str] | None])
    assert_type(to_date(Users.email, "YYYY-MM-DD"), Expr[datetime.date])
    assert_type(to_date(Users.bio, "YYYY-MM-DD"), Expr[datetime.date | None])
    assert_type(power(Users.id, 2), Expr[float])
    assert_type(log(Users.id, 10), Expr[Decimal])
    assert_type(mod(Users.id, 2), Expr[int])
    assert_type(percentile_cont(0.5, Users.id), Aggregate[float | None])
    # Nullable inputs stay nullable; non-null inputs do not gain None.
    assert_type(power(nullable(Users.id), 2), Expr[float | None])
    assert_type(mod(nullable(Users.id), 2), Expr[int | None])
    assert_type(to_char(nullable(Users.created_at), "YYYY"), Expr[str | None])
    assert_type(age(nullable(Users.created_at)), Expr[datetime.timedelta | None])
    assert_type(percentile_cont(0.5, nullable(Users.id)), Aggregate[float | None])
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
    eq(row_number(), 1)  # type: ignore[call-overload]  # pyright: ignore[reportCallIssue, reportArgumentType]

    vector = to_tsvector(Posts.title, config="english")
    assert_type(vector, Expr[str])
    assert_type(matches(vector, to_tsquery("cat")), Expr[bool])
    assert_type(ts_rank(vector, to_tsquery("cat")), Expr[float])

    manager = alias(Users, "manager")
    assert_type(manager.id, Column[int])
    # Q3: manager has Users' type, so either Users slot may be the joined one.
    rows = await qb.select(Users, manager).from_(Users).left_join(manager, eq(manager.id, Users.id))
    assert_type(rows, list[tuple[Users | None, Users | None]])
    rows = await qb.select(manager, Users).from_(Users).right_join(manager, eq(manager.id, 1))
    assert_type(rows, list[tuple[Users | None, Users | None]])

    union = qb.select(Users.id).from_(Users).union(qb.select(Posts.author_id).from_(Posts))
    assert_type(await union.order_by(Users.id).limit(5), list[tuple[int]])
    qb.select(Users.id).from_(Users).union(qb.select(Users.email).from_(Users))  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


def date_time_and_text_operators(
    stamp: Expr[datetime.datetime],
    maybe_stamp: Expr[datetime.datetime | None],
    day: Expr[datetime.date],
    maybe_day: Expr[datetime.date | None],
    interval: Expr[datetime.timedelta | None],
    every: Expr[datetime.timedelta],
) -> None:
    week = datetime.timedelta(days=7)
    assert_type(stamp + week, Expr[datetime.datetime])
    assert_type(stamp - interval, Expr[datetime.datetime | None])
    assert_type(maybe_stamp + week, Expr[datetime.datetime | None])
    assert_type(day + week, Expr[datetime.datetime])
    assert_type(stamp - stamp, Expr[datetime.timedelta])
    assert_type(stamp - maybe_stamp, Expr[datetime.timedelta | None])
    assert_type(stamp - datetime.datetime.now(), Expr[datetime.timedelta])
    assert_type(day - day, Expr[int])
    assert_type(maybe_day - day, Expr[int | None])
    assert_type(every + week, Expr[datetime.timedelta])
    assert_type(every - every, Expr[datetime.timedelta])
    assert_type(every + interval, Expr[datetime.timedelta | None])
    assert_type(interval - week, Expr[datetime.timedelta | None])
    assert_type(week + stamp, Expr[datetime.datetime])
    assert_type(week + day, Expr[datetime.datetime])
    assert_type(week + maybe_stamp, Expr[datetime.datetime | None])
    assert_type(week + maybe_day, Expr[datetime.datetime | None])
    assert_type(week + every, Expr[datetime.timedelta])
    assert_type(week + interval, Expr[datetime.timedelta | None])
    assert_type(Users.email + "!" + Users.email, Expr[str])
    assert_type(Users.email + Users.bio, Expr[str | None])
    assert_type("<" + Users.bio, Expr[str | None])
    assert_type(Users.id + 1, Expr[int])
    _ = stamp + 1  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]


def nullable_comparisons(user: Users) -> None:
    # A nullable column meets a non-null value or column, either way round.
    assert_type(eq(Users.bio, user.email), Expr[bool])
    assert_type(eq(Users.bio, Users.email), Expr[bool])
    assert_type(eq(Users.email, Users.bio), Expr[bool])
    assert_type(lt(Users.bio, "m"), Expr[bool])
    eq(Users.bio, 1)  # type: ignore[misc]  # pyright: ignore[reportCallIssue, reportArgumentType]


async def returning_and_scalar(qb: QueryBuilder) -> None:
    # T1: a projection is typed from the expressions, not Unknown.
    assert_type(await qb.insert(Users).values({}).returning(Users.id), list[tuple[int]])
    assert_type(
        await qb.insert(Users).values({}).returning(Users.id, Users.email), list[tuple[int, str]]
    )
    assert_type(
        await qb.update(Users).set({"bio": "x"}).returning(Users.bio),
        list[tuple[str | None]],
    )
    # T3: scalar() follows the selected column.
    assert_type(await qb.select(Users.id).from_(Users).scalar(), int | None)
    assert_type(await qb.select(Users.bio).from_(Users).scalar(), str | None)
    assert_type(await qb.select({"n": count()}).from_(Users).scalar(), object)


class UsersRow(TypedDict):
    id: int
    email: str


async def typed_rows(qb: QueryBuilder) -> None:
    # T8: the generated <Table>Row TypedDict names the shape a select returns.
    rows = await qb.select({"id": Users.id, "email": Users.email}).from_(Users).as_rows(UsersRow)
    assert_type(rows, list[UsersRow])


def json_aggregates_are_json() -> None:
    # C32: json_agg(created_at) said list[datetime] but returned ISO strings.
    assert_type(json_agg(Users.created_at), Aggregate[list[Json] | None])


def json_fields_are_json() -> None:
    from mizzle import get_field, get_path

    # Q15: a field of a dict[str, Any] column was typed as the whole dict.
    assert_type(get_field(Posts.meta, "a"), Expr[Json])
    assert_type(get_path(Posts.meta, ["a", "b"]), Expr[Json])


def string_agg_takes_text() -> None:
    from mizzle import string_agg

    # C46: string_agg(integer, text) does not exist in PostgreSQL.
    string_agg(Users.id, ",")  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    string_agg(Users.bio, ",")


def custom_columns_take_their_python_type() -> None:
    from decimal import Decimal

    from mizzle.pg import custom

    def money(value: Decimal) -> int:
        return int(value * 100)

    def cents(value: int) -> Decimal:
        return Decimal(value) / 100

    assert_type(custom("citext"), CustomColumn[object])
    assert_type(custom("citext", py_type=str), CustomColumn[str | None])
    assert_type(custom("cash", decode=money, encode=cents), CustomColumn[int | None])
    assert_type(custom("cash", py_type=int, decode=money).not_null(), Column[int])

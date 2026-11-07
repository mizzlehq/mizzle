"""as_rows() type checks, per-run Python defaults, MERGE and the version guard."""

import datetime
import enum
import itertools
from typing import Any, Protocol, TypedDict

import pytest

from mizzle import jsonb_build_array, jsonb_build_object, make_interval
from mizzle.core.errors import QueryError
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.table import Table
from mizzle.core.schema.types import (
    boolean,
    date,
    double_precision,
    integer,
    numeric,
    text,
    timestamp,
)
from mizzle.core.sql.operators import eq
from mizzle.dialects.postgres.driver import _needs_newer_server
from mizzle.dialects.postgres.types import int4range, pg_enum, serial
from tests.models import Users


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


_ticket = itertools.count(1)


class Kinds(Table, name="kinds"):
    id = serial().primary_key()
    n = integer().not_null()
    flag = boolean().not_null()
    ratio = double_precision().not_null()
    amount = numeric(10, 2).not_null()
    day = date().not_null()
    at = timestamp().not_null()
    span = int4range()
    level = pg_enum(Level)
    tag = text().not_null().default_fn(lambda: f"t{next(_ticket)}")


def test_as_rows_takes_typegen_any_fields(qb: QueryBuilder) -> None:
    # B42: typing.Any is a class since Python 3.11, so it was checked as a concrete type and
    # typegen's own `span: Any | None` was refused.
    class Row(TypedDict):
        span: Any | None
        n: object

    qb.select({"span": Kinds.span, "n": Kinds.n}).from_(Kinds).as_rows(Row)


def test_as_rows_skips_hints_issubclass_refuses(qb: QueryBuilder) -> None:
    # B45: a TypedDict or non-runtime Protocol hint made issubclass raise a raw TypeError.
    class Inner(TypedDict):
        a: int

    class P(Protocol):
        def f(self) -> None: ...

    class Row(TypedDict):
        n: Inner
        flag: P

    qb.select({"n": Kinds.n, "flag": Kinds.flag}).from_(Kinds).as_rows(Row)


def test_as_rows_compares_with_the_decoded_type(qb: QueryBuilder) -> None:
    # B46: the check ran both ways over the write-side types, so it refused float for an
    # integer column and let through date-as-datetime, int-as-bool, double-as-int and
    # numeric-as-int.
    class IntAsFloat(TypedDict):
        n: float

    qb.select({"n": Kinds.n}).from_(Kinds).as_rows(IntAsFloat)

    class DateAsDatetime(TypedDict):
        day: datetime.datetime

    class IntAsBool(TypedDict):
        n: bool

    class DoubleAsInt(TypedDict):
        ratio: int

    class NumericAsInt(TypedDict):
        amount: int

    for shape, column in (
        (DateAsDatetime, Kinds.day),
        (IntAsBool, Kinds.n),
        (DoubleAsInt, Kinds.ratio),
        (NumericAsInt, Kinds.amount),
    ):
        name = next(iter(shape.__annotations__))
        with pytest.raises(QueryError, match="but the TypedDict says"):
            qb.select({name: column}).from_(Kinds).as_rows(shape)


def test_default_fn_runs_on_every_execution(qb: QueryBuilder) -> None:
    # B43: values() ran default_fn once, so a prepared or re-awaited insert reused the value.
    query = qb.insert(Kinds).values(
        {
            "n": 1,
            "flag": True,
            "ratio": 1.0,
            "amount": 1,
            "day": datetime.date(2026, 1, 1),
            "at": datetime.datetime(2026, 1, 1),
        }
    )
    first, second = query.to_sql()[1][-1], query.to_sql()[1][-1]
    assert first != second
    assert query.prepare().compiled.dynamic


def test_merge_insert_needs_the_required_columns(qb: QueryBuilder) -> None:
    # B48: a missing NOT NULL column without a default reached the server.
    with pytest.raises(QueryError, match=r"Users\.email is required"):
        qb.merge(Users).using(Users).on(eq(Users.id, Users.id)).when_not_matched_then_insert(
            {"bio": "x"}
        )


def test_an_int_enum_is_sent_as_its_label(qb: QueryBuilder) -> None:
    # B49: the encoder sent the int 1 to a text enum.
    [param] = qb.select(Kinds.id).from_(Kinds).where(eq(Kinds.level, Level.LOW)).to_sql()[1]
    assert param == "1"
    assert Kinds.level.decode("2") is Level.HIGH


def test_json_builders_take_none(qb: QueryBuilder) -> None:
    # B50: a bare NULL parameter cannot be typed in a variadic "any" function.
    sql, params = qb.select(jsonb_build_object("k", None)).from_(Users).to_sql()
    assert "CAST($2 AS text)" in sql
    assert params[1] is None
    sql, _ = qb.select(jsonb_build_array(None)).from_(Users).to_sql()
    assert "CAST($1 AS text)" in sql


def test_out_of_range_arguments_are_query_errors(qb: QueryBuilder) -> None:
    with pytest.raises(QueryError, match="32 bits"):
        make_interval(days=10**30)
    with pytest.raises(QueryError, match="not bool"):
        qb.select(Users.id).from_(Users).tablesample("SYSTEM", True)


@pytest.mark.parametrize(
    ("message", "sql"),
    [
        ('syntax error at or near "merge"', "select (1 merge)"),
        (
            'syntax error at or near "returning"',
            "update merge_requests set x = 1 where returning id",
        ),
        (
            'syntax error at or near "nulls"',
            "create unique index on t (a) nulls not distinct nulls",
        ),
    ],
)
def test_the_version_guard_leaves_real_syntax_errors_alone(message: str, sql: str) -> None:
    # B44: upper-casing made lowercase SQL match, and the server version was never checked.
    assert _needs_newer_server(message, sql, 18) is None
    if "merge" in message:
        assert _needs_newer_server(message, sql) is None  # not a MERGE statement at all


def test_the_version_guard_still_names_the_version_on_an_old_server() -> None:
    sql = 'MERGE INTO "t" USING "s" ON true WHEN NOT MATCHED THEN DO NOTHING RETURNING 1'
    assert _needs_newer_server('syntax error at or near "RETURNING"', sql, 16) == (
        "MERGE ... RETURNING needs PostgreSQL 17 or later"
    )


class Tok(Table, name="tok"):
    id = serial().primary_key()
    token = text().not_null().default_fn(lambda: "generated-token")
    name = text().not_null()


def test_a_cached_insert_binds_values_in_column_order(qb: QueryBuilder) -> None:
    # C1: the second insert hit the SQL cache and bound in the dict's order, which
    # with_defaults() changes (default_fn last), so token and name were swapped.
    for name in ("first", "second", "third"):
        sql, params = qb.insert(Tok).values(name=name).to_sql()
        assert sql == 'INSERT INTO "tok" ("token", "name") VALUES ($1, $2)'
        assert params == ("generated-token", name)


def test_two_any_lists_never_share_a_parameter(qb: QueryBuilder) -> None:
    # C19: markers were cached by id() of a temporary Param; a freed one's id could be
    # reused, so both lists bound to $1 and a DELETE removed the wrong rows.
    from mizzle import any_, or_

    for _ in range(200):
        sql, params = (
            qb.delete(Kinds).where(or_(eq(Kinds.id, any_([1])), eq(Kinds.n, any_([99])))).to_sql()
        )
        assert params == ([1], [99]), sql


def test_a_placeholder_value_is_encoded_by_its_column(qb: QueryBuilder) -> None:
    # C33: in insert().values() and update().set() a placeholder skipped the column's
    # encoder, so an IntEnum was sent as 2 to a text enum.
    from mizzle import placeholder

    insert = qb.insert(Kinds).values(
        {
            "n": 1,
            "flag": True,
            "ratio": 1.0,
            "amount": 1,
            "day": datetime.date(2026, 1, 1),
            "at": datetime.datetime(2026, 1, 1),
            "level": placeholder("p"),
        }
    )
    assert "2" in insert.prepare().compiled.bind({"p": Level.HIGH}).params
    update = qb.update(Kinds).set({"level": placeholder("p")}).where(eq(Kinds.id, 1))
    assert "2" in update.prepare().compiled.bind({"p": Level.HIGH}).params

from typing import Any

import pytest

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.sql.nodes import Node
from mizzle.core.sql.operators import (
    and_,
    between,
    count,
    desc,
    eq,
    excluded,
    gt,
    ilike,
    in_,
    is_null,
    lt,
    not_,
    not_in,
    or_,
    placeholder,
)
from mizzle.core.sql.raw import sql
from mizzle.dialects.postgres.compiler import PostgresCompiler
from tests.models import Posts, Role, Users


def compile_(node: Node) -> tuple[str, tuple[Any, ...]]:
    c = PostgresCompiler().compile(node)
    return c.sql, c.params


def test_eq_binds_param() -> None:
    assert compile_(eq(Users.id, 1)) == ('"users"."id" = $1', (1,))


def test_column_vs_column() -> None:
    assert compile_(eq(Posts.author_id, Users.id)) == ('"posts"."author_id" = "users"."id"', ())


def test_eq_none_is_rejected() -> None:
    with pytest.raises(QueryError, match="use is_null"):
        eq(Users.bio, None)


def test_enum_param_is_encoded() -> None:
    assert compile_(eq(Users.role, Role.ADMIN)) == ('"users"."role" = $1', ("admin",))


def test_nested_bool_groups_get_parens() -> None:
    cond = and_(eq(Users.id, 1), or_(gt(Users.id, 5), lt(Users.id, 2)), None)
    assert compile_(cond) == (
        '"users"."id" = $1 AND ("users"."id" > $2 OR "users"."id" < $3)',
        (1, 5, 2),
    )


def test_and_with_single_condition_is_that_condition() -> None:
    cond = eq(Users.id, 1)
    assert and_(cond, None) is cond


def test_and_needs_a_condition() -> None:
    with pytest.raises(QueryError):
        and_(None)


def test_not() -> None:
    assert compile_(not_(eq(Users.id, 1))) == ('NOT ("users"."id" = $1)', (1,))


def test_in_list() -> None:
    assert compile_(in_(Users.id, [1, 2])) == ('"users"."id" IN ($1, $2)', (1, 2))


def test_empty_in_matches_nothing() -> None:
    assert compile_(in_(Users.id, [])) == ("false", ())
    assert compile_(not_in(Users.id, [])) == ("true", ())


def test_is_null_between_ilike() -> None:
    assert compile_(is_null(Users.bio)) == ('"users"."bio" IS NULL', ())
    assert compile_(between(Users.id, 1, 9)) == ('"users"."id" BETWEEN $1 AND $2', (1, 9))
    assert compile_(ilike(Users.email, "%@x.com")) == ('"users"."email" ILIKE $1', ("%@x.com",))


def test_functions_and_order() -> None:
    assert compile_(count()) == ("count(*)", ())
    assert compile_(count(Users.id, distinct=True)) == ('count(DISTINCT "users"."id")', ())
    assert compile_(desc(Users.id, nulls="last")) == ('"users"."id" DESC NULLS LAST', ())


def test_excluded() -> None:
    assert compile_(excluded(Users.email)) == ('excluded."email"', ())


def test_raw_sql_with_args_and_braces() -> None:
    raw = sql("select '{{}}'::jsonb, {} from x where id = {}", Users.email, 5)
    assert compile_(raw) == ('select \'{}\'::jsonb, "users"."email" from x where id = $1', (5,))


def test_raw_identifier_is_quoted() -> None:
    assert compile_(sql("select * from {}", sql.identifier('we"ird'))) == (
        'select * from "we""ird"',
        (),
    )


@pytest.mark.parametrize(
    ("template", "args"),
    [("{} {}", (1,)), ("{}", (1, 2)), ("{name}", (1,)), ("{!r}", (1,))],
)
def test_raw_slot_errors(template: str, args: tuple[Any, ...]) -> None:
    with pytest.raises(QueryError):
        sql(template, *args)


def test_placeholders_bind_by_name() -> None:
    compiled = PostgresCompiler().compile(
        and_(eq(Users.role, placeholder("role")), gt(Users.id, 3))
    )
    assert compiled.has_placeholders
    bound = compiled.bind({"role": Role.ADMIN})
    assert bound.params == ("admin", 3)
    assert not bound.has_placeholders


def test_bind_rejects_missing_and_unknown() -> None:
    compiled = PostgresCompiler().compile(eq(Users.id, placeholder("id")))
    with pytest.raises(QueryError, match="missing values for placeholders: id"):
        compiled.bind({})
    with pytest.raises(QueryError, match="unknown placeholders: x"):
        compiled.bind({"id": 1, "x": 2})


def test_too_many_params() -> None:
    with pytest.raises(QueryError, match="at most 32767"):
        PostgresCompiler().compile(in_(Users.id, range(32768)))


def test_compiled_query_is_plain_data() -> None:
    assert CompiledQuery("x", (), ()) == CompiledQuery("x", (), ())

"""Filter and helper functions: ``eq``, ``and_``, ``in_``, ``count`` ...

Functions, not overloaded ``==``: overloading ``__eq__`` on columns breaks hashing and ``in``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any, Literal, cast

from mizzle.core.errors import QueryError
from mizzle.core.schema.column import Column
from mizzle.core.sql.nodes import (
    Between,
    BinaryOp,
    BoolOp,
    Excluded,
    Exists,
    Expr,
    Func,
    InList,
    Node,
    Not,
    OrderBy,
    Param,
    Placeholder,
    Postfix,
    Star,
)


def _wrap(value: object, like: Node) -> Node:
    """Turn a Python value into a Param; the column on the other side encodes it."""
    column = cast("Column[Any]", like) if isinstance(like, Column) else None
    if isinstance(value, Placeholder) and column is not None and value.column is None:
        return replace(value, column=column)
    if isinstance(value, Node):
        return value
    return Param(value, column)


def _compare[T](left: Expr[T], op: str, right: T | Expr[T]) -> Expr[bool]:
    if right is None:
        raise QueryError(f"comparing with None using {op!r} is never true in SQL; use is_null()")
    return BinaryOp(left, op, _wrap(right, left))


def eq[T](left: Expr[T], right: T | Expr[T]) -> Expr[bool]:
    return _compare(left, "=", right)


def ne[T](left: Expr[T], right: T | Expr[T]) -> Expr[bool]:
    return _compare(left, "<>", right)


def gt[T](left: Expr[T], right: T | Expr[T]) -> Expr[bool]:
    return _compare(left, ">", right)


def gte[T](left: Expr[T], right: T | Expr[T]) -> Expr[bool]:
    return _compare(left, ">=", right)


def lt[T](left: Expr[T], right: T | Expr[T]) -> Expr[bool]:
    return _compare(left, "<", right)


def lte[T](left: Expr[T], right: T | Expr[T]) -> Expr[bool]:
    return _compare(left, "<=", right)


def _combine(op: Literal["AND", "OR"], conditions: tuple[Expr[bool] | None, ...]) -> Expr[bool]:
    items = tuple(c for c in conditions if c is not None)
    if not items:
        raise QueryError(f"{op.lower()}_() needs at least one condition")
    return items[0] if len(items) == 1 else BoolOp(op, items)


def and_(*conditions: Expr[bool] | None) -> Expr[bool]:
    """AND of the conditions. ``None`` entries are skipped, which helps optional filters."""
    return _combine("AND", conditions)


def or_(*conditions: Expr[bool] | None) -> Expr[bool]:
    """OR of the conditions. ``None`` entries are skipped."""
    return _combine("OR", conditions)


def not_(condition: Expr[bool]) -> Expr[bool]:
    return Not(condition)


def _in(left: Expr[Any], values: Iterable[Any] | Node, negate: bool) -> Expr[bool]:
    if isinstance(values, Node):  # subquery
        return InList(left, values, negate)
    return InList(left, tuple(_wrap(v, left) for v in values), negate)


def in_[T](left: Expr[T], values: Iterable[T] | Node) -> Expr[bool]:
    """``left IN (...)``. ``values`` may be a select query. An empty list matches nothing."""
    return _in(left, values, negate=False)


def not_in[T](left: Expr[T], values: Iterable[T] | Node) -> Expr[bool]:
    return _in(left, values, negate=True)


def like(left: Expr[Any], pattern: str | Expr[Any]) -> Expr[bool]:
    return BinaryOp(left, "LIKE", _wrap(pattern, left))


def ilike(left: Expr[Any], pattern: str | Expr[Any]) -> Expr[bool]:
    return BinaryOp(left, "ILIKE", _wrap(pattern, left))


def is_null(expr: Expr[Any]) -> Expr[bool]:
    return Postfix(expr, "IS NULL")


def is_not_null(expr: Expr[Any]) -> Expr[bool]:
    return Postfix(expr, "IS NOT NULL")


def between[T](expr: Expr[T], low: T | Expr[T], high: T | Expr[T]) -> Expr[bool]:
    return Between(expr, _wrap(low, expr), _wrap(high, expr))


def exists(query: Node) -> Expr[bool]:
    return Exists(query)


def not_exists(query: Node) -> Expr[bool]:
    return Exists(query, negate=True)


def asc(expr: Expr[Any], *, nulls: Literal["first", "last"] | None = None) -> OrderBy:
    return OrderBy(expr, descending=False, nulls=nulls)


def desc(expr: Expr[Any], *, nulls: Literal["first", "last"] | None = None) -> OrderBy:
    return OrderBy(expr, descending=True, nulls=nulls)


def count(expr: Expr[Any] | None = None, *, distinct: bool = False) -> Expr[int]:
    return Func("count", (expr if expr is not None else Star(),), distinct)


def sum_(expr: Expr[Any]) -> Expr[Any]:
    """``sum()``. Typed ``Any``: PostgreSQL widens int to bigint and bigint to numeric."""
    return Func("sum", (expr,))


def avg(expr: Expr[Any]) -> Expr[Any]:
    return Func("avg", (expr,))


def min_[T](expr: Expr[T]) -> Expr[T | None]:
    return Func("min", (expr,))


def max_[T](expr: Expr[T]) -> Expr[T | None]:
    return Func("max", (expr,))


def placeholder(name: str) -> Any:
    """A value supplied later: ``q.prepare().execute(name=value)``."""
    return Placeholder(name)


def excluded[T](column: Column[T]) -> Expr[T]:
    """The row proposed for insertion, inside ``on_conflict_do_update(set_=...)``."""
    return Excluded(column)

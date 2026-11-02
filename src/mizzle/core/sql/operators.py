"""Filter and helper functions: ``eq``, ``and_``, ``in_``, ``count`` ...

Functions, not overloaded ``==``: overloading ``__eq__`` on columns breaks hashing and ``in``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from decimal import Decimal
from typing import Literal, overload

from mizzle.core.errors import QueryError
from mizzle.core.schema.column import ColumnBase
from mizzle.core.sql.nodes import (
    Between,
    BinaryOp,
    BoolOp,
    Condition,
    Excluded,
    Exists,
    Expr,
    ExprBase,
    InList,
    Node,
    Not,
    OrderBy,
    Param,
    Placeholder,
    Postfix,
    Raw,
    Star,
    TypedFunc,
    TypedPlaceholder,
)

# A comparison's right side: a value, a typed expression, or raw SQL.
type Operand[T] = T | Expr[T] | Raw


def _wrap(value: object, like: ExprBase) -> Node:
    """Turn a Python value into a Param; the column on the other side encodes it."""
    column = like if isinstance(like, ColumnBase) else None
    if isinstance(value, Placeholder) and column is not None and value.column is None:
        return replace(value, column=column)
    if isinstance(value, Node):
        return value
    return Param(value, column)


def _compare[T](left: Expr[T], op: str, right: Operand[T]) -> Expr[bool]:
    if right is None:
        raise QueryError(f"comparing with None using {op!r} is never true in SQL; use is_null()")
    return BinaryOp(left, op, _wrap(right, left))


def eq[T](left: Expr[T], right: Operand[T]) -> Expr[bool]:
    return _compare(left, "=", right)


def ne[T](left: Expr[T], right: Operand[T]) -> Expr[bool]:
    return _compare(left, "<>", right)


def gt[T](left: Expr[T], right: Operand[T]) -> Expr[bool]:
    return _compare(left, ">", right)


def gte[T](left: Expr[T], right: Operand[T]) -> Expr[bool]:
    return _compare(left, ">=", right)


def lt[T](left: Expr[T], right: Operand[T]) -> Expr[bool]:
    return _compare(left, "<", right)


def lte[T](left: Expr[T], right: Operand[T]) -> Expr[bool]:
    return _compare(left, "<=", right)


def _combine(op: Literal["AND", "OR"], conditions: tuple[Condition | None, ...]) -> Expr[bool]:
    items = tuple(c for c in conditions if c is not None)
    if not items:
        raise QueryError(f"{op.lower()}_() needs at least one condition")
    if len(items) == 1 and isinstance(items[0], Expr):
        return items[0]
    return BoolOp(op, items)


def and_(*conditions: Condition | None) -> Expr[bool]:
    """AND of the conditions. ``None`` entries are skipped, which helps optional filters."""
    return _combine("AND", conditions)


def or_(*conditions: Condition | None) -> Expr[bool]:
    """OR of the conditions. ``None`` entries are skipped."""
    return _combine("OR", conditions)


def not_(condition: Condition) -> Expr[bool]:
    return Not(condition)


def _in(left: ExprBase, values: Iterable[object] | Node, negate: bool) -> Expr[bool]:
    if isinstance(values, Node):  # subquery
        return InList(left, values, negate)
    return InList(left, tuple(_wrap(v, left) for v in values), negate)


def in_[T](left: Expr[T], values: Iterable[T] | Node) -> Expr[bool]:
    """``left IN (...)``. ``values`` may be a select query. An empty list matches nothing."""
    return _in(left, values, negate=False)


def not_in[T](left: Expr[T], values: Iterable[T] | Node) -> Expr[bool]:
    return _in(left, values, negate=True)


type Text = Expr[str] | Expr[str | None]


def like(left: Text, pattern: str | Text) -> Expr[bool]:
    return BinaryOp(left, "LIKE", _wrap(pattern, left))


def ilike(left: Text, pattern: str | Text) -> Expr[bool]:
    return BinaryOp(left, "ILIKE", _wrap(pattern, left))


def is_null(expr: ExprBase) -> Expr[bool]:
    return Postfix(expr, "IS NULL")


def is_not_null(expr: ExprBase) -> Expr[bool]:
    return Postfix(expr, "IS NOT NULL")


def between[T](expr: Expr[T], low: Operand[T], high: Operand[T]) -> Expr[bool]:
    return Between(expr, _wrap(low, expr), _wrap(high, expr))


def exists(query: Node) -> Expr[bool]:
    return Exists(query)


def not_exists(query: Node) -> Expr[bool]:
    return Exists(query, negate=True)


def asc(expr: ExprBase, *, nulls: Literal["first", "last"] | None = None) -> OrderBy:
    return OrderBy(expr, descending=False, nulls=nulls)


def desc(expr: ExprBase, *, nulls: Literal["first", "last"] | None = None) -> OrderBy:
    return OrderBy(expr, descending=True, nulls=nulls)


def count(expr: ExprBase | None = None, *, distinct: bool = False) -> Expr[int]:
    return TypedFunc[int]("count", (expr if expr is not None else Star(),), distinct)


type _Int = Expr[int] | Expr[int | None]
type _Decimal = Expr[Decimal] | Expr[Decimal | None]
type _Float = Expr[float] | Expr[float | None]


# The overloads "overlap" only for impossible bottom types; both checkers flag that.
@overload
def sum_(expr: _Int) -> Expr[int | Decimal | None]: ...  # type: ignore[overload-overlap]  # pyright: ignore[reportOverlappingOverload]
@overload
def sum_(expr: _Decimal) -> Expr[Decimal | None]: ...  # type: ignore[overload-overlap]
@overload
def sum_(expr: _Float) -> Expr[float | None]: ...
def sum_(expr: _Int | _Decimal | _Float) -> ExprBase:
    """``sum()``. Integers give ``int | Decimal``: PostgreSQL sums integer to bigint, but
    bigint to numeric, and both are ``int`` in Python. NULL when there are no rows."""
    return TypedFunc[object]("sum", (expr,))


@overload
def avg(expr: _Int | _Decimal) -> Expr[Decimal | None]: ...  # type: ignore[overload-overlap]  # pyright: ignore[reportOverlappingOverload]
@overload
def avg(expr: _Float) -> Expr[float | None]: ...
def avg(expr: _Int | _Decimal | _Float) -> ExprBase:
    """``avg()``: numeric (``Decimal``) for integers and decimals, float for floats."""
    return TypedFunc[object]("avg", (expr,))


def min_[T](expr: Expr[T]) -> Expr[T | None]:
    return TypedFunc[T | None]("min", (expr,))


def max_[T](expr: Expr[T]) -> Expr[T | None]:
    return TypedFunc[T | None]("max", (expr,))


def placeholder[T](name: str) -> TypedPlaceholder[T]:
    """A value supplied later: ``q.prepare().execute(name=value)``. Its type comes from
    where it is used: ``eq(Users.id, placeholder("id"))`` makes it ``int``."""
    return TypedPlaceholder[T](name)


def excluded(column: ColumnBase) -> Excluded:
    """The row proposed for insertion, inside ``on_conflict_do_update(set_=...)``."""
    return Excluded(column)

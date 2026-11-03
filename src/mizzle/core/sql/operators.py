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
    Aggregate,
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
    TypedNullable,
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


def _compare(left: ExprBase, op: str, right: object) -> Expr[bool]:
    if right is None:
        raise QueryError(f"comparing with None using {op!r} is never true in SQL; use is_null()")
    return BinaryOp(left, op, _wrap(right, left))


# The first overload lets a nullable column meet a non-null value or column
# (eq(Users.manager_id, boss.id)); Expr is invariant, so Expr[int] is not an Expr[int | None].
@overload
def eq[T](left: Expr[T | None], right: Operand[T]) -> Expr[bool]: ...
@overload
def eq[T](left: Expr[T], right: Operand[T] | Expr[T | None]) -> Expr[bool]: ...
def eq(left: ExprBase, right: object) -> Expr[bool]:
    return _compare(left, "=", right)


@overload
def ne[T](left: Expr[T | None], right: Operand[T]) -> Expr[bool]: ...
@overload
def ne[T](left: Expr[T], right: Operand[T] | Expr[T | None]) -> Expr[bool]: ...
def ne(left: ExprBase, right: object) -> Expr[bool]:
    return _compare(left, "<>", right)


@overload
def gt[T](left: Expr[T | None], right: Operand[T]) -> Expr[bool]: ...
@overload
def gt[T](left: Expr[T], right: Operand[T] | Expr[T | None]) -> Expr[bool]: ...
def gt(left: ExprBase, right: object) -> Expr[bool]:
    return _compare(left, ">", right)


@overload
def gte[T](left: Expr[T | None], right: Operand[T]) -> Expr[bool]: ...
@overload
def gte[T](left: Expr[T], right: Operand[T] | Expr[T | None]) -> Expr[bool]: ...
def gte(left: ExprBase, right: object) -> Expr[bool]:
    return _compare(left, ">=", right)


@overload
def lt[T](left: Expr[T | None], right: Operand[T]) -> Expr[bool]: ...
@overload
def lt[T](left: Expr[T], right: Operand[T] | Expr[T | None]) -> Expr[bool]: ...
def lt(left: ExprBase, right: object) -> Expr[bool]:
    return _compare(left, "<", right)


@overload
def lte[T](left: Expr[T | None], right: Operand[T]) -> Expr[bool]: ...
@overload
def lte[T](left: Expr[T], right: Operand[T] | Expr[T | None]) -> Expr[bool]: ...
def lte(left: ExprBase, right: object) -> Expr[bool]:
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


def not_like(left: Text, pattern: str | Text) -> Expr[bool]:
    return BinaryOp(left, "NOT LIKE", _wrap(pattern, left))


def not_ilike(left: Text, pattern: str | Text) -> Expr[bool]:
    return BinaryOp(left, "NOT ILIKE", _wrap(pattern, left))


def regex(left: Text, pattern: str | Text, *, ignore_case: bool = False) -> Expr[bool]:
    """POSIX regular expression match (``~``, or ``~*`` with ``ignore_case``); the pattern
    may match anywhere unless anchored with ``^``/``$``."""
    return BinaryOp(left, "~*" if ignore_case else "~", _wrap(pattern, left))


def not_regex(left: Text, pattern: str | Text, *, ignore_case: bool = False) -> Expr[bool]:
    return BinaryOp(left, "!~*" if ignore_case else "!~", _wrap(pattern, left))


def similar_to(left: Text, pattern: str | Text) -> Expr[bool]:
    """SQL regular expression (``SIMILAR TO``): matches the whole string; ``%`` and ``_``
    work as in LIKE, plus ``|``, ``*``, ``+``, ``?``, ``()`` and ``[]`` as in regexes."""
    return BinaryOp(left, "SIMILAR TO", _wrap(pattern, left))


def not_similar_to(left: Text, pattern: str | Text) -> Expr[bool]:
    return BinaryOp(left, "NOT SIMILAR TO", _wrap(pattern, left))


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


def count(expr: ExprBase | None = None, *, distinct: bool = False) -> Aggregate[int]:
    return Aggregate[int]("count", (expr if expr is not None else Star(),), distinct)


type _Int = Expr[int] | Expr[int | None]
type _Decimal = Expr[Decimal] | Expr[Decimal | None]
type _Float = Expr[float] | Expr[float | None]


# The overloads "overlap" only for impossible bottom types; both checkers flag that.
@overload
def sum_(expr: _Int) -> Aggregate[int | Decimal | None]: ...  # type: ignore[overload-overlap]  # pyright: ignore[reportOverlappingOverload]
@overload
def sum_(expr: _Decimal) -> Aggregate[Decimal | None]: ...  # type: ignore[overload-overlap]
@overload
def sum_(expr: _Float) -> Aggregate[float | None]: ...
def sum_(expr: _Int | _Decimal | _Float) -> ExprBase:
    """``sum()``. Integers give ``int | Decimal``: PostgreSQL sums integer to bigint, but
    bigint to numeric, and both are ``int`` in Python. NULL when there are no rows."""
    return Aggregate[object]("sum", (expr,))


@overload
def avg(expr: _Int | _Decimal) -> Aggregate[Decimal | None]: ...  # type: ignore[overload-overlap]  # pyright: ignore[reportOverlappingOverload]
@overload
def avg(expr: _Float) -> Aggregate[float | None]: ...
def avg(expr: _Int | _Decimal | _Float) -> ExprBase:
    """``avg()``: numeric (``Decimal``) for integers and decimals, float for floats."""
    return Aggregate[object]("avg", (expr,))


def min_[T](expr: Expr[T]) -> Aggregate[T | None]:
    return Aggregate[T | None]("min", (expr,), keeps_type=True)


def max_[T](expr: Expr[T]) -> Aggregate[T | None]:
    return Aggregate[T | None]("max", (expr,), keeps_type=True)


def placeholder[T](name: str) -> TypedPlaceholder[T]:
    """A value supplied later: ``q.prepare().execute(name=value)``. Its type comes from
    where it is used: ``eq(Users.id, placeholder("id"))`` makes it ``int``."""
    return TypedPlaceholder[T](name)


def nullable[T](expr: Expr[T]) -> Expr[T | None]:
    """Type a selected expression as possibly ``None``, e.g. a column of an outer-joined
    table: ``select(Users.id, nullable(Posts.title)).from_(Users).left_join(Posts, ...)``.
    (Whole tables in ``select(A, B)`` are made optional by the join automatically.)"""
    return TypedNullable[T | None](expr)


def excluded(column: ColumnBase) -> Excluded:
    """The row proposed for insertion, inside ``on_conflict_do_update(set_=...)``."""
    return Excluded(column)

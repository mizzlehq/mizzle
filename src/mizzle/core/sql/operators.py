"""Filter and helper functions: ``eq``, ``and_``, ``in_``, ``count`` ...

Functions, not overloaded ``==``: overloading ``__eq__`` on columns breaks hashing and ``in``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from decimal import Decimal
from typing import Literal, cast, overload

from mizzle.core.errors import QueryError
from mizzle.core.schema.column import ColumnBase
from mizzle.core.sql.functions import bind, column_of, path_keys, sql_type_of
from mizzle.core.sql.nodes import (
    Aggregate,
    AnyAll,
    Between,
    BinaryOp,
    BoolOp,
    Cast,
    Condition,
    Excluded,
    Exists,
    Expr,
    ExprBase,
    Fragment,
    InList,
    Json,
    Node,
    Not,
    OrderBy,
    Param,
    Placeholder,
    Postfix,
    PostfixOp,
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
    if isinstance(values, Placeholder):
        # IN ($1) would compare with the whole list as one value: bind it as an array,
        # typed by the left side like any_() (Q5).
        quantified = AnyAll[object]("ALL" if negate else "ANY", values)
        return BinaryOp(left, "<>" if negate else "=", quantified)
    if isinstance(values, Node):  # subquery
        return InList(left, values, negate)
    if isinstance(values, (str, bytes)):  # a str is an Iterable[str]: "abc" is not a list (C36)
        raise QueryError(
            "in_()/not_in() take a sequence of values, not a string; wrap it in a list"
        )
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
    if distinct and expr is None:  # count(DISTINCT *) is not SQL (Q17)
        raise QueryError("count(distinct=True) needs an expression")
    return Aggregate[int]("count", (expr if expr is not None else Star(),), distinct)


type _Int = Expr[int] | Expr[int | None]
type _Decimal = Expr[Decimal] | Expr[Decimal | None]
type _Float = Expr[float] | Expr[float | None]
type _Number = _Int | _Decimal | _Float


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


# -- null-safe comparison (EX2) -------------------------------------------------------


def is_distinct_from[T](left: Expr[T], right: Operand[T] | None) -> Expr[bool]:
    """``left IS DISTINCT FROM right``: like ``<>`` but NULL is an ordinary value, so the
    result is never NULL. Use it to compare nullable columns reliably."""
    return BinaryOp(left, "IS DISTINCT FROM", _wrap(right, left))


def is_not_distinct_from[T](left: Expr[T], right: Operand[T] | None) -> Expr[bool]:
    """``left IS NOT DISTINCT FROM right``: like ``=`` but NULL-safe."""
    return BinaryOp(left, "IS NOT DISTINCT FROM", _wrap(right, left))


# -- quantified comparison (EX1) ------------------------------------------------------


def _quantified(quantifier: Literal["ANY", "ALL"], values: object) -> AnyAll[object]:
    if isinstance(values, (str, bytes)):
        raise QueryError("any_()/all_() take a sequence of values, not a string; wrap it in a list")
    if isinstance(values, Node) or not isinstance(values, Iterable):
        return AnyAll[object](quantifier, bind(values))
    items = tuple(values)
    if not items:
        raise QueryError("any_()/all_() need at least one value")
    # Keep the raw values: the compiler casts the array to the type of the value it is
    # compared against (an enum array, not text[]).
    return AnyAll[object](quantifier, values=items)


@overload
def any_[T](values: Expr[list[T]]) -> Expr[T]: ...
@overload
def any_[T](values: list[T] | tuple[T, ...]) -> Expr[T]: ...
@overload
def any_[T](values: Node) -> Expr[T]: ...
def any_[T](values: object) -> Expr[T]:
    """``= ANY (values)``: true when the compared value equals any of them. Use it with
    ``eq(col, any_([...]))``, or against an array column or a subquery."""
    return cast("AnyAll[T]", _quantified("ANY", values))


@overload
def all_[T](values: Expr[list[T]]) -> Expr[T]: ...
@overload
def all_[T](values: list[T] | tuple[T, ...]) -> Expr[T]: ...
@overload
def all_[T](values: Node) -> Expr[T]: ...
def all_[T](values: object) -> Expr[T]:
    """``= ALL (values)``: true when the value equals every one of them."""
    return cast("AnyAll[T]", _quantified("ALL", values))


# -- container operators (EX3, EX4) ---------------------------------------------------


def _container(value: object, like: object = None) -> Node:
    column = column_of(like) if like is not None else None
    on_json = column is not None and column.sql_type in ("jsonb", "json")
    # A multirange is a list of ranges too, not a text[] (Q4-19).
    on_array = column is not None and (
        column.item is not None or column.sql_type.endswith("multirange")
    )
    if isinstance(value, Placeholder) and value.column is None and column is not None:
        # Typed like a literal value (Q5); on a range column it is a range, as PostgreSQL
        # infers it; a scalar bound needs cast(placeholder(...), integer()).
        return Cast(value, "jsonb") if on_json else replace(value, column=column)
    if isinstance(value, Node):
        return value
    if isinstance(value, dict):
        return Cast(Param(cast("dict[str, object]", value)), "jsonb")
    if isinstance(value, (list, tuple, set, frozenset)):
        items: list[object] = list(cast("Iterable[object]", value))
        if not items:
            raise QueryError("an empty container is never a useful operand")
        if on_json:
            return Cast(Param(items), "jsonb")
        if on_array:
            # No cast: the server types the parameter from the array column, and an
            # enum array's type name may need quotes or a schema (C38).
            return Param(items, column)
        kind = next((t for v in items if (t := sql_type_of(v)) is not None), "text")
        return Cast(Param(items), kind + "[]")
    element = _RANGE_ELEMENTS.get(column.sql_type) if column is not None else None
    if element is not None and not hasattr(value, "lower_inc"):  # a scalar, not a range (C39)
        return Cast(Param(value), element)
    return bind(value)


_RANGE_ELEMENTS = {
    f"{name}{kind}": element
    for name, element in (
        ("int4", "integer"),
        ("int8", "bigint"),
        ("num", "numeric"),
        ("ts", "timestamp"),
        ("tstz", "timestamptz"),
        ("date", "date"),
    )
    for kind in ("range", "multirange")
}


def contains(left: ExprBase, right: object) -> Expr[bool]:
    """``left @> right``: left contains right (arrays, ranges, jsonb)."""
    return BinaryOp(left, "@>", _container(right, left))


def contained_by(left: ExprBase, right: object) -> Expr[bool]:
    """``left <@ right``: left is contained by right."""
    return BinaryOp(left, "<@", _container(right, left))


def overlaps(left: ExprBase, right: object) -> Expr[bool]:
    """``left && right``: the two share any element (arrays, ranges)."""
    return BinaryOp(left, "&&", _container(right, left))


def _json_key(key: str | int) -> Node:
    if isinstance(key, int) and not isinstance(key, bool):
        return Cast(Param(key), "integer")
    return Cast(Param(key), "text")


def get_field(value: ExprBase, key: str | int) -> Expr[Json]:
    """``value -> key``: a jsonb field (as jsonb). ``key`` is a string or array index."""
    return PostfixOp[Json](value, "->", _json_key(key))  # a field, not the column type (Q15)


def get_field_text(value: ExprBase, key: str | int) -> Expr[str | None]:
    """``value ->> key``: a jsonb field as text."""
    return PostfixOp[str | None](value, "->>", _json_key(key))


def get_path(value: ExprBase, path: list[str] | tuple[str, ...]) -> Expr[Json]:
    """``value #> path``: a jsonb value at a text-array path."""
    return PostfixOp[Json](value, "#>", Cast(Param(path_keys(path, "get_path")), "text[]"))


def get_path_text(value: ExprBase, path: list[str] | tuple[str, ...]) -> Expr[str | None]:
    """``value #>> path``: a jsonb value at a path, as text."""
    return PostfixOp[str | None](
        value, "#>>", Cast(Param(path_keys(path, "get_path_text")), "text[]")
    )


def has_key(value: ExprBase, key: str) -> Expr[bool]:
    """``value ? key``: the jsonb object has this top-level key (or the array this string)."""
    return BinaryOp(value, "?", Cast(Param(key), "text"))


def has_any_key(value: ExprBase, keys: list[str] | tuple[str, ...]) -> Expr[bool]:
    """``value ?| keys``: the jsonb object has any of these top-level keys."""
    return BinaryOp(value, "?|", Cast(Param(path_keys(keys, "has_any_key")), "text[]"))


def has_all_keys(value: ExprBase, keys: list[str] | tuple[str, ...]) -> Expr[bool]:
    """``value ?& keys``: the jsonb object has all of these top-level keys."""
    return BinaryOp(value, "?&", Cast(Param(path_keys(keys, "has_all_keys")), "text[]"))


# -- more aggregates (EX7) ------------------------------------------------------------


def array_agg[T](expr: Expr[T]) -> Aggregate[list[T] | None]:
    """Collects the values into an array (NULL when there are no rows)."""
    return Aggregate[list[T] | None]("array_agg", (expr,), keeps_type=True, returns_array=True)


def string_agg(
    expr: Expr[str] | Expr[str | None], delimiter: str | Expr[str]
) -> Aggregate[str | None]:
    """Joins the values with ``delimiter``: ``string_agg(Users.email, ", ")``."""
    return Aggregate[str | None]("string_agg", (expr, bind(delimiter)))


def bool_and(expr: Expr[bool]) -> Aggregate[bool | None]:
    """True when every row is true."""
    return Aggregate[bool | None]("bool_and", (expr,))


def every(expr: Expr[bool]) -> Aggregate[bool | None]:
    """``every()`` is SQL's other name for ``bool_and``."""
    return Aggregate[bool | None]("every", (expr,))


def bool_or(expr: Expr[bool]) -> Aggregate[bool | None]:
    """True when any row is true."""
    return Aggregate[bool | None]("bool_or", (expr,))


def stddev(expr: _Number) -> Aggregate[float | Decimal | None]:
    """Sample standard deviation (``stddev_samp``); NULL with fewer than two rows."""
    return Aggregate[float | Decimal | None]("stddev_samp", (expr,))


def stddev_pop(expr: _Number) -> Aggregate[float | Decimal | None]:
    """Population standard deviation."""
    return Aggregate[float | Decimal | None]("stddev_pop", (expr,))


def variance(expr: _Number) -> Aggregate[float | Decimal | None]:
    """Sample variance (``var_samp``)."""
    return Aggregate[float | Decimal | None]("var_samp", (expr,))


def var_pop(expr: _Number) -> Aggregate[float | Decimal | None]:
    """Population variance."""
    return Aggregate[float | Decimal | None]("var_pop", (expr,))


# The values become JSON: a date, Decimal or UUID comes back as JSON text or a float, not as
# the column's type, so the result is typed Json (C32).


def json_agg(expr: ExprBase) -> Aggregate[list[Json] | None]:
    """Collects the values into a ``json`` array."""
    return Aggregate[list[Json] | None]("json_agg", (expr,))


def jsonb_agg(expr: ExprBase) -> Aggregate[list[Json] | None]:
    """Collects the values into a ``jsonb`` array."""
    return Aggregate[list[Json] | None]("jsonb_agg", (expr,))


def json_object_agg(key: ExprBase, value: ExprBase) -> Aggregate[dict[str, Json] | None]:
    """Collects key/value pairs into a ``json`` object."""
    return Aggregate[dict[str, Json] | None]("json_object_agg", (key, value))


# -- ordered-set aggregates (EX9) -----------------------------------------------------


def _fraction(fraction: float) -> Node:
    number = float(fraction)
    if not 0 <= number <= 1:
        raise QueryError("the fraction must be between 0 and 1")
    return Fragment((repr(number),))


def mode[T](expr: Expr[T]) -> Aggregate[T | None]:
    """The most frequent value: ``mode(Users.role)``."""
    return Aggregate[T | None](
        "mode",
        (),
        keeps_type=True,
        within_group_=(OrderBy(expr, descending=False, nulls=None),),
    )


def percentile_cont(fraction: float, expr: _Number) -> Aggregate[float | None]:
    """Continuous percentile: interpolates between values. PostgreSQL always returns
    ``double precision`` for a numeric input (never ``numeric``); NULL when the group is
    empty."""
    return Aggregate[float | None](
        "percentile_cont", (_fraction(fraction),), within_group_=(OrderBy(expr, False, None),)
    )


def percentile_disc[T](fraction: float, expr: Expr[T]) -> Aggregate[T | None]:
    """Discrete percentile: the first value whose cumulative fraction reaches ``fraction``."""
    return Aggregate[T | None](
        "percentile_disc",
        (_fraction(fraction),),
        keeps_type=True,
        within_group_=(OrderBy(expr, False, None),),
    )

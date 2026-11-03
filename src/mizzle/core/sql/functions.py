"""SQL expressions and functions: ``case_``, ``coalesce``, ``cast``, ``lower``, window
functions (``row_number().over(...)``), full-text search (``matches``)...

Python values become bind parameters. Next to a column they are encoded and typed like it;
otherwise they are cast to the SQL type of the Python value, so functions that take any type
(``coalesce``, ``concat``) know what they get.
"""

from __future__ import annotations

import datetime
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Literal, overload

from mizzle.core.errors import QueryError
from mizzle.core.schema.column import Column, ColumnBase
from mizzle.core.sql.nodes import (
    BinaryOp,
    Cast,
    Condition,
    Expr,
    ExprBase,
    Fragment,
    Frame,
    Func,
    Node,
    Nullable,
    OrderBy,
    Over,
    Param,
    Placeholder,
    TypedCast,
    TypedFragment,
    TypedFunc,
    over,
)

# -- binding values -------------------------------------------------------------------

_SQL_TYPES: tuple[tuple[type[object], str], ...] = (
    (bool, "boolean"),  # before int: bool is an int
    (int, "bigint"),
    (float, "double precision"),
    (Decimal, "numeric"),
    (str, "text"),
    (datetime.date, "date"),
    (datetime.time, "time"),
    (datetime.timedelta, "interval"),
    (uuid.UUID, "uuid"),
    (bytes, "bytea"),
)


def _sql_type(value: object) -> str | None:
    if isinstance(value, datetime.datetime):
        return "timestamp" if value.tzinfo is None else "timestamptz"
    return next((name for kind, name in _SQL_TYPES if isinstance(value, kind)), None)


def column_of(node: object) -> ColumnBase | None:
    """The column whose type ``node`` has, if known. It encodes values used next to
    ``node`` and decodes ``node``'s value when selected (e.g. back into an enum)."""
    match node:
        case ColumnBase():
            return node
        case Nullable():
            return column_of(node.expr)
        case Cast():
            return node.column
        case Func(keeps_type=True):
            return _first_column(node.args)
        case Over():
            return column_of(node.func)
        case CaseBase():
            return _first_column((*(v for _, v in node.whens), node.default))
        case _:
            return None


def _first_column(nodes: Iterable[object]) -> ColumnBase | None:
    for node in nodes:
        if (column := column_of(node)) is not None:
            return column
    return None


def bind(value: object, like: Iterable[object] = ()) -> Node:
    """A value used next to the nodes in ``like``: nodes stay as they are; Python values
    become parameters encoded by a column among ``like``, or else cast to their SQL type."""
    if isinstance(value, Placeholder):
        column = None if value.column is not None else _first_column(like)
        return value if column is None else replace(value, column=column)
    if isinstance(value, Node):
        return value
    column = _first_column(like)
    if column is not None:
        return Param(value, column)
    sql_type = _sql_type(value)
    return Param(value) if sql_type is None else Cast(Param(value), sql_type)


def _func(name: str, *args: object, keeps_type: bool = False) -> TypedFunc[object]:
    return TypedFunc[object](name, tuple(bind(a, args) for a in args), keeps_type=keeps_type)


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?")


def _identifier(kind: str, name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise QueryError(f"invalid {kind} name {name!r}")
    return name


def _whole(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise QueryError(f"{name} must be an int; got {value!r}")
    return value


# -- CASE -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseBase(ExprBase):
    """``CASE WHEN ... THEN ... ELSE ... END``; build it with ``case_()``."""

    whens: tuple[tuple[Node, Node], ...]
    default: Node | None
    # The values as given (THEN values, then ELSE): all are bound again when one is added,
    # so a column in any branch types the plain values of the others.
    raw: tuple[object, ...] = field(default=(), repr=False, compare=False)


def _case[C: CaseBase](
    kind: type[C], conditions: tuple[Node, ...], raw: tuple[object, ...], default: bool
) -> C:
    nodes = tuple(bind(v, raw) for v in raw)
    whens = tuple(zip(conditions, nodes, strict=False))
    return kind(whens, nodes[-1] if default else None, raw)


def _conditions(case: CaseBase) -> tuple[Node, ...]:
    return tuple(c for c, _ in case.whens)


@dataclass(frozen=True)
class Case[T](CaseBase, Expr[T | None]):
    """A CASE without ELSE (NULL when no branch matches). Add branches with ``.when()``,
    finish with ``.else_()``."""

    @overload
    def when(self, condition: Condition, value: T | Expr[T], /) -> Case[T]: ...
    @overload
    def when(self, condition: Condition, value: Expr[T | None] | None, /) -> Case[T | None]: ...
    def when(self, condition: Condition, value: object, /) -> CaseBase:
        return _case(Case[object], (*_conditions(self), condition), (*self.raw, value), False)

    @overload
    def else_(self, value: T | Expr[T], /) -> Expr[T]: ...
    @overload
    def else_(self, value: Expr[T | None] | None, /) -> Expr[T | None]: ...
    def else_(self, value: object, /) -> ExprBase:
        return _case(CaseElse[object], _conditions(self), (*self.raw, value), True)


@dataclass(frozen=True)
class CaseElse[T](CaseBase, Expr[T]):
    pass


class CaseStart:
    @overload
    def when[T](self, condition: Condition, value: Expr[T], /) -> Case[T]: ...
    @overload
    def when[T](self, condition: Condition, value: T, /) -> Case[T]: ...
    def when(self, condition: Condition, value: object, /) -> CaseBase:
        return _case(Case[object], (condition,), (value,), False)


def case_() -> CaseStart:
    """``case_().when(gt(Users.age, 17), "adult").else_("minor")``: the first matching
    branch's value. Without ``.else_()`` the result is NULL when nothing matches."""
    return CaseStart()


# -- NULL handling and comparison -----------------------------------------------------


@overload
def coalesce[T](first: Expr[T | None], second: Expr[T], /) -> Expr[T]: ...  # pyright: ignore[reportOverlappingOverload]
@overload
def coalesce[T](first: Expr[T | None], second: Expr[T | None], /) -> Expr[T | None]: ...
@overload
def coalesce[T](first: Expr[T | None], second: T, /) -> Expr[T]: ...
@overload
def coalesce[T](
    first: Expr[T | None], /, *rest: T | Expr[T] | Expr[T | None]
) -> Expr[T | None]: ...
@overload
def coalesce[T](first: Expr[T], /, *rest: T | Expr[T] | Expr[T | None]) -> Expr[T]: ...
def coalesce(*args: object) -> ExprBase:
    """The first non-NULL argument: ``coalesce(Users.nickname, Users.name, "anonymous")``."""
    if not args:
        raise QueryError("coalesce() needs at least one argument")
    return _func("coalesce", *args, keeps_type=True)


@overload
def nullif[T](value: Expr[T], other: Expr[T], /) -> Expr[T | None]: ...
@overload
def nullif[T](value: Expr[T], other: T, /) -> Expr[T | None]: ...
def nullif(value: ExprBase, other: object, /) -> ExprBase:
    """NULL when ``value`` equals ``other``, else ``value``: ``nullif(Users.bio, "")``."""
    return _func("nullif", value, other, keeps_type=True)


@overload
def greatest[T](first: Expr[T], /, *rest: T | Expr[T] | Expr[T | None]) -> Expr[T]: ...
@overload
def greatest[T](
    first: Expr[T | None], /, *rest: T | Expr[T] | Expr[T | None]
) -> Expr[T | None]: ...
def greatest(first: ExprBase, /, *rest: object) -> ExprBase:
    """The largest argument; NULLs are ignored (NULL only when all are NULL)."""
    return _func("greatest", first, *rest, keeps_type=True)


@overload
def least[T](first: Expr[T], /, *rest: T | Expr[T] | Expr[T | None]) -> Expr[T]: ...
@overload
def least[T](first: Expr[T | None], /, *rest: T | Expr[T] | Expr[T | None]) -> Expr[T | None]: ...
def least(first: ExprBase, /, *rest: object) -> ExprBase:
    """The smallest argument; NULLs are ignored (NULL only when all are NULL)."""
    return _func("least", first, *rest, keeps_type=True)


_SERIALS = {"serial": "integer", "bigserial": "bigint", "smallserial": "smallint"}


def cast[T](expr: ExprBase, to: Column[T], /) -> Expr[T]:
    """``CAST(expr AS type)``, the type given as a column: ``cast(Users.id, text())``.
    The result is typed (and decoded) like that column; ``text().not_null()`` makes it
    non-nullable."""
    sql_type = _SERIALS.get(to.sql_type, to.sql_type)
    if to.enum is not None:  # a user-defined type name: quote it
        base = sql_type.removesuffix("[]")
        sql_type = '"' + base.replace('"', '""') + '"' + sql_type[len(base) :]
    return TypedCast[T](expr, sql_type, to)


# -- strings --------------------------------------------------------------------------


def lower[S: (str, str | None)](expr: Expr[S], /) -> Expr[S]:
    return TypedFunc[S]("lower", (expr,))


def upper[S: (str, str | None)](expr: Expr[S], /) -> Expr[S]:
    return TypedFunc[S]("upper", (expr,))


def trim[S: (str, str | None)](expr: Expr[S], /) -> Expr[S]:
    """Removes spaces from both ends."""
    return TypedFunc[S]("btrim", (expr,))


@overload
def length(expr: Expr[str], /) -> Expr[int]: ...
@overload
def length(expr: Expr[str | None], /) -> Expr[int | None]: ...
def length(expr: Expr[str] | Expr[str | None], /) -> ExprBase:
    """Number of characters."""
    return _func("length", expr)


def concat(*parts: ExprBase | str | int | float | Decimal) -> Expr[str]:
    """Joins the parts as text; NULL parts count as empty: ``concat(Users.first, " ", ...)``."""
    if not parts:
        raise QueryError("concat() needs at least one argument")
    return TypedFunc[str]("concat", tuple(bind(p) for p in parts))


# -- numbers --------------------------------------------------------------------------


def abs_[N: (int, int | None, float, float | None, Decimal, Decimal | None)](
    expr: Expr[N], /
) -> Expr[N]:
    return TypedFunc[N]("abs", (expr,))


@overload
def round_[N: (float, float | None, Decimal, Decimal | None)](expr: Expr[N], /) -> Expr[N]: ...
@overload
def round_[N: (int, float, Decimal)](expr: Expr[N], /, digits: int) -> Expr[Decimal]: ...
@overload
def round_[N: (int, float, Decimal)](
    expr: Expr[N | None], /, digits: int
) -> Expr[Decimal | None]: ...
def round_(expr: ExprBase, /, digits: int | None = None) -> ExprBase:
    """Round to the nearest integer, or to ``digits`` decimal places (as numeric)."""
    if digits is None:
        return _func("round", expr)
    places = Fragment((str(_whole("digits", digits)),))
    return TypedFunc[Decimal]("round", (Cast(expr, "numeric"), places))


# -- dates and times ------------------------------------------------------------------

type DateUnit = Literal[
    "microseconds",
    "milliseconds",
    "second",
    "minute",
    "hour",
    "day",
    "week",
    "month",
    "quarter",
    "year",
    "decade",
    "century",
    "millennium",
]
type DateField = Literal[
    "century",
    "day",
    "decade",
    "dow",
    "doy",
    "epoch",
    "hour",
    "isodow",
    "isoyear",
    "julian",
    "microseconds",
    "millennium",
    "milliseconds",
    "minute",
    "month",
    "quarter",
    "second",
    "timezone",
    "timezone_hour",
    "timezone_minute",
    "week",
    "year",
]
_WORD = re.compile(r"[a-z_]+")  # units and fields are written into the SQL text


def now() -> Expr[datetime.datetime]:
    """The start time of the current transaction (``timestamptz``)."""
    return TypedFunc[datetime.datetime]("now", ())


def current_date() -> Expr[datetime.date]:
    return TypedFragment[datetime.date](("CURRENT_DATE",))


@overload
def date_trunc(unit: DateUnit, expr: Expr[datetime.date], /) -> Expr[datetime.datetime]: ...
@overload
def date_trunc(  # type: ignore[overload-overlap]
    unit: DateUnit, expr: Expr[datetime.date | None], /
) -> Expr[datetime.datetime | None]: ...
@overload
def date_trunc[
    D: (
        datetime.datetime,
        datetime.datetime | None,
        datetime.timedelta,
        datetime.timedelta | None,
    )
](unit: DateUnit, expr: Expr[D], /) -> Expr[D]: ...
def date_trunc(unit: DateUnit, expr: ExprBase, /) -> ExprBase:
    """Truncate to the ``unit``: ``date_trunc("month", Orders.created_at)``. The unit is
    written into the SQL, so the same expression can be selected and grouped by."""
    if not _WORD.fullmatch(unit):
        raise QueryError(f"unknown date_trunc unit {unit!r}")
    return _func("date_trunc", Fragment((f"'{unit}'",)), expr, keeps_type=True)


@overload
def extract(
    field: DateField,
    expr: Expr[datetime.datetime] | Expr[datetime.date] | Expr[datetime.timedelta],
    /,
) -> Expr[Decimal]: ...
@overload
def extract(
    field: DateField,
    expr: Expr[datetime.datetime | None]
    | Expr[datetime.date | None]
    | Expr[datetime.timedelta | None],
    /,
) -> Expr[Decimal | None]: ...
def extract(field: DateField, expr: ExprBase, /) -> ExprBase:
    """``extract(year FROM expr)`` as numeric: ``extract("year", Users.created_at)``."""
    if not _WORD.fullmatch(field):
        raise QueryError(f"unknown extract field {field!r}")
    return TypedFragment[Decimal](("extract(", field, " FROM ", expr, ")"))


# -- any function ---------------------------------------------------------------------


class func[T](TypedFunc[T]):
    """Any SQL function, typed by you: ``func[float]("similarity", Users.name, "jon")``.

    The name must be a plain (optionally schema-qualified) identifier. Python values are
    always bind parameters; PostgreSQL infers their types from the function, so for
    functions taking any type use ``cast()``."""

    def __init__(self, name: str, /, *args: object) -> None:
        nodes = tuple(a if isinstance(a, Node) else Param(a) for a in args)
        super().__init__(_identifier("function", name), nodes)


# -- window functions -----------------------------------------------------------------


class WindowFunctionBase:
    """A function that needs a window: call ``.over(...)`` to use it."""

    _func: Func

    def __init__(self, func: Func) -> None:
        self._func = func


class WindowFunction[T](WindowFunctionBase):
    def over(
        self,
        *,
        partition_by: ExprBase | Sequence[ExprBase] = (),
        order_by: ExprBase | OrderBy | Sequence[ExprBase | OrderBy] = (),
        frame: Frame | None = None,
    ) -> Expr[T]:
        """``row_number().over(partition_by=Posts.author_id, order_by=desc(Posts.id))``."""
        return over(self._func, partition_by, order_by, frame)


def _window[T](name: str, *args: object, keeps_type: bool = False) -> WindowFunction[T]:
    return WindowFunction[T](Func(name, tuple(bind(a, args) for a in args), False, keeps_type))


def rows_between(start: int | None = None, end: int | None = 0) -> Frame:
    """``ROWS BETWEEN ...``: offsets count rows; negative is PRECEDING, 0 is CURRENT ROW,
    positive is FOLLOWING, None is UNBOUNDED. The default is a running total's frame."""
    return _frame("ROWS", start, end)


def range_between(start: int | None = None, end: int | None = 0) -> Frame:
    """``RANGE BETWEEN ...``: like ``rows_between``, but rows with equal ORDER BY values
    are peers, and offsets are in ORDER BY values."""
    return _frame("RANGE", start, end)


def _frame(kind: Literal["ROWS", "RANGE"], start: int | None, end: int | None) -> Frame:
    return Frame(
        kind,
        None if start is None else _whole("frame start", start),
        None if end is None else _whole("frame end", end),
    )


def row_number() -> WindowFunction[int]:
    return _window("row_number")


def rank() -> WindowFunction[int]:
    return _window("rank")


def dense_rank() -> WindowFunction[int]:
    return _window("dense_rank")


def percent_rank() -> WindowFunction[float]:
    return _window("percent_rank")


def cume_dist() -> WindowFunction[float]:
    return _window("cume_dist")


def ntile(buckets: int) -> WindowFunction[int]:
    return WindowFunction[int](Func("ntile", (Fragment((str(_whole("buckets", buckets)),)),)))


@overload
def lag[T](expr: Expr[T], offset: int = 1) -> WindowFunction[T | None]: ...
@overload
def lag[T](expr: Expr[T], offset: int, default: T | Expr[T]) -> WindowFunction[T]: ...
def lag(expr: ExprBase, offset: int = 1, default: object = None) -> WindowFunctionBase:
    """The value ``offset`` rows before this one; ``default`` (or NULL) when there is none."""
    return _shift("lag", expr, offset, default)


@overload
def lead[T](expr: Expr[T], offset: int = 1) -> WindowFunction[T | None]: ...
@overload
def lead[T](expr: Expr[T], offset: int, default: T | Expr[T]) -> WindowFunction[T]: ...
def lead(expr: ExprBase, offset: int = 1, default: object = None) -> WindowFunctionBase:
    """The value ``offset`` rows after this one; ``default`` (or NULL) when there is none."""
    return _shift("lead", expr, offset, default)


def _shift(name: str, expr: ExprBase, offset: int, default: object) -> WindowFunction[object]:
    args: tuple[Node, ...] = (expr, Fragment((str(_whole("offset", offset)),)))
    if default is not None:
        args += (bind(default, (expr,)),)
    return WindowFunction[object](Func(name, args, keeps_type=True))


def first_value[T](expr: Expr[T]) -> WindowFunction[T]:
    return _window("first_value", expr, keeps_type=True)


def last_value[T](expr: Expr[T]) -> WindowFunction[T]:
    """Mind the default frame, which ends at the current row: pass ``frame=rows_between(
    None, None)`` to see the whole partition."""
    return _window("last_value", expr, keeps_type=True)


def nth_value[T](expr: Expr[T], n: int) -> WindowFunction[T | None]:
    nth = Fragment((str(_whole("n", n)),))
    return WindowFunction[T | None](Func("nth_value", (expr, nth), keeps_type=True))


# -- full-text search -----------------------------------------------------------------
# tsvector and tsquery values are text in Python (asyncpg returns them as str).

type _Text = Expr[str] | Expr[str | None]


def _config(config: str | None) -> tuple[Node, ...]:
    if config is None:
        return ()
    return (Fragment((f"'{_identifier('text search config', config)}'::regconfig",)),)


@overload
def to_tsvector[S: (str, str | None)](
    document: Expr[S], /, *, config: str | None = None
) -> Expr[S]: ...
@overload
def to_tsvector(document: str, /, *, config: str | None = None) -> Expr[str]: ...
def to_tsvector(document: ExprBase | str, /, *, config: str | None = None) -> ExprBase:
    """``to_tsvector('english', Posts.body)``. Without ``config`` the server's
    ``default_text_search_config`` is used."""
    return TypedFunc[str]("to_tsvector", (*_config(config), bind(document)))


def _query(name: str, query: Expr[str] | str, config: str | None) -> Expr[str]:
    return TypedFunc[str](name, (*_config(config), bind(query)))


def to_tsquery(query: Expr[str] | str, /, *, config: str | None = None) -> Expr[str]:
    """A query in tsquery syntax: ``to_tsquery("cat & !dog")``."""
    return _query("to_tsquery", query, config)


def plainto_tsquery(query: Expr[str] | str, /, *, config: str | None = None) -> Expr[str]:
    """All words of plain text must match."""
    return _query("plainto_tsquery", query, config)


def phraseto_tsquery(query: Expr[str] | str, /, *, config: str | None = None) -> Expr[str]:
    """The words of plain text must match in this order."""
    return _query("phraseto_tsquery", query, config)


def websearch_to_tsquery(query: Expr[str] | str, /, *, config: str | None = None) -> Expr[str]:
    """Web search syntax: ``"quoted phrase" or -excluded``. Never fails on bad input."""
    return _query("websearch_to_tsquery", query, config)


def matches(vector: _Text, query: Expr[str], /) -> Expr[bool]:
    """``vector @@ query``: ``matches(to_tsvector(Posts.body), websearch_to_tsquery(q))``."""
    return BinaryOp(vector, "@@", query)


@overload
def ts_rank(vector: Expr[str], query: Expr[str], /, *, normalization: int = 0) -> Expr[float]: ...
@overload
def ts_rank(
    vector: Expr[str | None], query: Expr[str], /, *, normalization: int = 0
) -> Expr[float | None]: ...
def ts_rank(vector: _Text, query: Expr[str], /, *, normalization: int = 0) -> ExprBase:
    """How well ``vector`` matches ``query`` (higher is better). ``normalization`` is
    PostgreSQL's bit mask for document length (0: ignore length)."""
    args: tuple[Node, ...] = (vector, query)
    if normalization:
        args += (Fragment((str(_whole("normalization", normalization)),)),)
    return TypedFunc[float]("ts_rank", args)

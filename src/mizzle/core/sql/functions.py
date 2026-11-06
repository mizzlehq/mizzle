"""SQL expressions and functions: ``case_``, ``coalesce``, ``cast``, ``lower``, window
functions (``row_number().over(...)``), full-text search (``matches``)...

Python values become bind parameters. Next to a column they are encoded and typed like it;
otherwise they are cast to the SQL type of the Python value, so functions that take any type
(``coalesce``, ``concat``) know what they get.
"""

from __future__ import annotations

import datetime
import math
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from decimal import Decimal
from typing import Literal, overload

from mizzle.core.errors import QueryError
from mizzle.core.schema.column import Column, ColumnBase
from mizzle.core.sql.nodes import (
    Aggregate,
    BinaryOp,
    Cast,
    Condition,
    CteColumn,
    Expr,
    ExprBase,
    Fragment,
    Frame,
    Func,
    Node,
    Nullable,
    Operation,
    OrderBy,
    Over,
    Param,
    Placeholder,
    PostfixOp,
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


def sql_type_of(value: object) -> str | None:
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
        case Aggregate(keeps_type=True, returns_array=True):
            column = _first_column(node.args)
            return None if column is None else column.array_column()
        case Aggregate(keeps_type=True):
            grouped: list[object] = [
                item.expr if isinstance(item, OrderBy) else item for item in node.within_group_
            ]
            return _first_column((*node.args, *grouped))
        case Func(keeps_type=True):
            return _first_column(node.args)
        case Over():
            return column_of(node.func)
        case CteColumn():
            return node.cte.source_column(node.name)
        case PostfixOp(op="->" | "#>"):
            # A jsonb extraction returns jsonb: the source column carries the type.
            return column_of(node.item)
        case CaseBase():
            return _first_column((*(v for _, v in node.whens), node.default))
        case _:
            return None


_TEXT_TYPES = frozenset({"text", "varchar", "character varying", "char", "character", "citext"})
_TEXT_FUNCS = frozenset({"lower", "upper", "btrim", "concat"})


def is_text(node: object) -> bool:
    """Whether ``node`` is known to be text, so ``+`` on it means ``||``."""
    match node:
        case str():
            return True
        case Operation():
            return node.op == "||"
        case Cast():
            return node.type.split("(", 1)[0] in _TEXT_TYPES
        case Func(keeps_type=False):
            return node.name in _TEXT_FUNCS
        case Func():
            return any(is_text(a) for a in node.args)
        case CaseBase():
            return any(is_text(v) for v in (*(v for _, v in node.whens), node.default))
        case Nullable():
            return is_text(node.expr)
        case Over():
            return is_text(node.func)
        case CteColumn() if node.type_ is str:
            return True
        case _:
            column = column_of(node)
            return column is not None and column.sql_type.split("(", 1)[0] in _TEXT_TYPES


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
        return value if column is None else _replace(value, column=column)
    if isinstance(value, Node):
        return value
    column = _first_column(like)
    if column is not None:
        return Param(value, column)
    sql_type = sql_type_of(value)
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
    return int(value)  # a subclass (IntEnum) must not write its own repr


def _number(name: str, value: int | float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise QueryError(f"{name} must be a number; got {value!r}")
    number = float(value) if isinstance(value, float) else int(value)
    if isinstance(number, float) and not math.isfinite(number):
        raise QueryError(f"{name} must be a finite number; got {value!r}")
    return number


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
        if to.enum_schema is not None:
            sql_type = '"' + to.enum_schema.replace('"', '""') + '".' + sql_type
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
        super().__init__(_identifier("function", name), nodes, opaque=True)


# -- window functions -----------------------------------------------------------------


class WindowFunctionBase:
    """A function that needs a window: call ``.over(...)`` to use it."""

    _func: Func

    def __init__(self, func: Func) -> None:
        self._func = func


class WindowFunction[T](WindowFunctionBase):
    def over(
        self,
        window: str | None = None,
        /,
        *,
        partition_by: ExprBase | Sequence[ExprBase] = (),
        order_by: ExprBase | OrderBy | Sequence[ExprBase | OrderBy] = (),
        frame: Frame | None = None,
    ) -> Expr[T]:
        """``row_number().over(partition_by=Posts.author_id, order_by=desc(Posts.id))``, or
        ``.over("w")`` for a window named with ``query.window("w", ...)``."""
        return over(self._func, window, partition_by, order_by, frame)


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
def ts_rank(
    vector: Expr[str],
    query: Expr[str],
    /,
    *,
    weights: Sequence[float] | None = None,
    normalization: int = 0,
) -> Expr[float]: ...
@overload
def ts_rank(
    vector: Expr[str | None],
    query: Expr[str],
    /,
    *,
    weights: Sequence[float] | None = None,
    normalization: int = 0,
) -> Expr[float | None]: ...
def ts_rank(
    vector: _Text,
    query: Expr[str],
    /,
    *,
    weights: Sequence[float] | None = None,
    normalization: int = 0,
) -> ExprBase:
    """How well ``vector`` matches ``query`` (higher is better). ``weights`` are the
    weights of the D, C, B and A labels (default ``[0.1, 0.2, 0.4, 1.0]``).
    ``normalization`` is PostgreSQL's bit mask for document length (0: ignore length)."""
    return _rank("ts_rank", vector, query, weights, normalization)


@overload
def ts_rank_cd(
    vector: Expr[str],
    query: Expr[str],
    /,
    *,
    weights: Sequence[float] | None = None,
    normalization: int = 0,
) -> Expr[float]: ...
@overload
def ts_rank_cd(
    vector: Expr[str | None],
    query: Expr[str],
    /,
    *,
    weights: Sequence[float] | None = None,
    normalization: int = 0,
) -> Expr[float | None]: ...
def ts_rank_cd(
    vector: _Text,
    query: Expr[str],
    /,
    *,
    weights: Sequence[float] | None = None,
    normalization: int = 0,
) -> ExprBase:
    """Like ``ts_rank``, but rewards matching words that are close together ("cover
    density"). The vector needs positions (a ``to_tsvector`` result has them)."""
    return _rank("ts_rank_cd", vector, query, weights, normalization)


def _rank(
    name: str, vector: _Text, query: Expr[str], weights: Sequence[float] | None, norm: int
) -> ExprBase:
    args: tuple[Node, ...] = (vector, query)
    if weights is not None:
        values = [float(w) for w in weights]
        if len(values) != 4:
            raise QueryError(f"{name} weights must be 4 numbers (D, C, B, A); got {weights!r}")
        args = (Cast(Param(values), "real[]"), *args)
    if norm:
        args += (Fragment((str(_whole("normalization", norm)),)),)
    return TypedFunc[float](name, args)


# -- more strings (EX5) ---------------------------------------------------------------


def substring[S: (str, str | None)](
    expr: Expr[S], start: int, length: int | None = None
) -> Expr[S]:
    """``substr(expr, start, length)``: ``start`` is 1-based; without ``length`` it runs to
    the end of the string."""
    args: tuple[Node, ...] = (expr, Fragment((str(_whole("start", start)),)))
    if length is not None:
        args += (Fragment((str(_whole("length", length)),)),)
    return TypedFunc[S]("substr", args)


def replace[S: (str, str | None)](expr: Expr[S], old: str, new: str) -> Expr[S]:
    """Replace every occurrence of ``old`` with ``new``."""
    return TypedFunc[S]("replace", (expr, bind(old), bind(new)))


@overload
def strpos(expr: Expr[str], needle: str | Expr[str]) -> Expr[int]: ...
@overload
def strpos(expr: Expr[str | None], needle: str | Expr[str]) -> Expr[int | None]: ...
def strpos(expr: Expr[str] | Expr[str | None], needle: str | Expr[str]) -> ExprBase:
    """1-based position of ``needle`` in ``expr``, or 0 when absent."""
    return TypedFunc[object]("strpos", (expr, bind(needle)))


def left[S: (str, str | None)](expr: Expr[S], n: int) -> Expr[S]:
    """The first ``n`` characters."""
    return TypedFunc[S]("left", (expr, Fragment((str(_whole("n", n)),))))


def right[S: (str, str | None)](expr: Expr[S], n: int) -> Expr[S]:
    """The last ``n`` characters."""
    return TypedFunc[S]("right", (expr, Fragment((str(_whole("n", n)),))))


def lpad[S: (str, str | None)](expr: Expr[S], length: int, fill: str = " ") -> Expr[S]:
    """Pad on the left with ``fill`` until ``length`` characters."""
    return TypedFunc[S]("lpad", (expr, Fragment((str(_whole("length", length)),)), bind(fill)))


def rpad[S: (str, str | None)](expr: Expr[S], length: int, fill: str = " ") -> Expr[S]:
    """Pad on the right with ``fill`` until ``length`` characters."""
    return TypedFunc[S]("rpad", (expr, Fragment((str(_whole("length", length)),)), bind(fill)))


def repeat[S: (str, str | None)](expr: Expr[S], n: int) -> Expr[S]:
    """``expr`` repeated ``n`` times."""
    return TypedFunc[S]("repeat", (expr, Fragment((str(_whole("n", n)),))))


def reverse[S: (str, str | None)](expr: Expr[S]) -> Expr[S]:
    """The string with its characters in reverse order."""
    return TypedFunc[S]("reverse", (expr,))


def split_part[S: (str, str | None)](expr: Expr[S], delimiter: str | Expr[str], n: int) -> Expr[S]:
    """The ``n``-th field of ``expr`` split on ``delimiter`` (1-based)."""
    return TypedFunc[S]("split_part", (expr, bind(delimiter), Fragment((str(_whole("n", n)),))))


@overload
def starts_with(expr: Expr[str], prefix: str | Expr[str]) -> Expr[bool]: ...
@overload
def starts_with(expr: Expr[str | None], prefix: str | Expr[str]) -> Expr[bool | None]: ...
def starts_with(expr: Expr[str] | Expr[str | None], prefix: str | Expr[str]) -> ExprBase:
    """Whether ``expr`` begins with ``prefix``."""
    return TypedFunc[object]("starts_with", (expr, bind(prefix)))


def regexp_replace[S: (str, str | None)](
    expr: Expr[S], pattern: str | Expr[str], replacement: str | Expr[str], *, flags: str = ""
) -> Expr[S]:
    """Replace the first match of ``pattern`` (all with the ``g`` flag). ``flags`` combines
    ``g``, ``i``, ``m``, ``n``, ``p``, ``s``, ``x`` and ``w``."""
    args: tuple[Node, ...] = (expr, bind(pattern), bind(replacement))
    if flags:
        args += (bind(flags),)
    return TypedFunc[S]("regexp_replace", args)


def regexp_match(
    expr: Expr[str] | Expr[str | None], pattern: str | Expr[str], *, flags: str = ""
) -> Expr[list[str] | None]:
    """The captured groups of the first match, or NULL when there is none."""
    args: tuple[Node, ...] = (expr, bind(pattern))
    if flags:
        args += (bind(flags),)
    return TypedFunc[list[str] | None]("regexp_match", args)


def md5[S: (str, str | None)](expr: Expr[S]) -> Expr[S]:
    """The MD5 hash of the string, as 32 hex digits."""
    return TypedFunc[S]("md5", (expr,))


# -- more numbers (EX6) ---------------------------------------------------------------


def ceil[N: (float, float | None, Decimal, Decimal | None)](expr: Expr[N]) -> Expr[N]:
    """Round up to the nearest integer."""
    return TypedFunc[N]("ceil", (expr,))


def floor[N: (float, float | None, Decimal, Decimal | None)](expr: Expr[N]) -> Expr[N]:
    """Round down to the nearest integer."""
    return TypedFunc[N]("floor", (expr,))


def sqrt[N: (float, float | None, Decimal, Decimal | None)](expr: Expr[N]) -> Expr[N]:
    """Square root (NaN for negative numbers)."""
    return TypedFunc[N]("sqrt", (expr,))


@overload
def power(base: Expr[int], exponent: int | Expr[int]) -> Expr[float]: ...
@overload
def power(base: Expr[int | None], exponent: int | Expr[int]) -> Expr[float | None]: ...
@overload
def power[N: (float, float | None, Decimal, Decimal | None)](
    base: Expr[N], exponent: N | Expr[N]
) -> Expr[N]: ...
def power(base: ExprBase, exponent: object) -> ExprBase:
    """``base`` raised to ``exponent``. Integers and floats give ``double precision``;
    ``numeric`` stays ``numeric``."""
    return TypedFunc[object]("power", (base, bind(exponent, (base,))))


def ln[N: (float, float | None, Decimal, Decimal | None)](expr: Expr[N]) -> Expr[N]:
    """Natural logarithm."""
    return TypedFunc[N]("ln", (expr,))


def exp[N: (float, float | None, Decimal, Decimal | None)](expr: Expr[N]) -> Expr[N]:
    """``e`` raised to ``expr``."""
    return TypedFunc[N]("exp", (expr,))


@overload
def log(base: Expr[int], value: int | Expr[int]) -> Expr[Decimal]: ...
@overload
def log(base: Expr[int | None], value: int | Expr[int]) -> Expr[Decimal | None]: ...
@overload
def log[N: (float, float | None, Decimal, Decimal | None)](
    base: Expr[N], value: N | Expr[N]
) -> Expr[N]: ...
def log(base: ExprBase, value: object) -> ExprBase:
    """Logarithm of ``value`` to ``base``. Two-argument ``log`` always returns ``numeric``,
    whatever the arguments' type."""
    return TypedFunc[object]("log", (base, bind(value, (base,))))


def mod[N: (int, int | None, Decimal, Decimal | None)](
    value: Expr[N], divisor: N | Expr[N]
) -> Expr[N]:
    """``value`` modulo ``divisor`` (integers and ``numeric``; no float overload)."""
    return TypedFunc[N]("mod", (value, bind(divisor, (value,))))


def sign[N: (float, float | None, Decimal, Decimal | None)](expr: Expr[N]) -> Expr[N]:
    """-1, 0 or 1 according to the sign of ``expr``."""
    return TypedFunc[N]("sign", (expr,))


def random() -> Expr[float]:
    """A random value between 0.0 and 1.0."""
    return TypedFunc[float]("random", ())


# -- arrays (EX3) ---------------------------------------------------------------------


# An array expression, nullable or not: array columns and ``array_agg`` are nullable.
type Array[T] = Expr[list[T]] | Expr[list[T] | None]


def unnest[T](array: Array[T]) -> Expr[T]:
    """Expands an array to one row per element (a set-returning function)."""
    return TypedFunc[T]("unnest", (array,))


def array_length[T](array: Array[T], dimension: int = 1) -> Expr[int | None]:
    """The length of ``array`` along ``dimension`` (NULL when that dimension is empty)."""
    return TypedFunc[int | None](
        "array_length", (array, Fragment((str(_whole("dimension", dimension)),)))
    )


def _array_element(value: object, array: object) -> Node:
    """Bind ``value`` as an element of ``array``: the element column types and encodes it
    (``array_remove(colors, Role.ADMIN)`` -> ``array_remove(role[], role)``)."""
    column = column_of(array)
    item = column.item if column is not None else None
    return bind(value, () if item is None else (item,))


def array_position[T](array: Array[T], value: T | Expr[T]) -> Expr[int | None]:
    """1-based position of ``value`` in ``array``, or NULL when absent."""
    return TypedFunc[int | None]("array_position", (array, _array_element(value, array)))


def array_append[T](array: Array[T], value: T | Expr[T]) -> Expr[list[T]]:
    """``array`` with ``value`` appended at the end."""
    return TypedFunc[list[T]]("array_append", (array, _array_element(value, array)))


def array_remove[T](array: Array[T], value: T | Expr[T]) -> Expr[list[T]]:
    """``array`` with every element equal to ``value`` removed."""
    return TypedFunc[list[T]]("array_remove", (array, _array_element(value, array)))


def array_cat[T](left: Array[T], right: Array[T]) -> Expr[list[T]]:
    """The two arrays concatenated."""
    return TypedFunc[list[T]]("array_cat", (left, right))


# -- json (EX4) -----------------------------------------------------------------------


def _jsonb(value: object) -> Node:
    # Any Python value is JSON: a dict/list encode as before, a scalar (int, str, bool)
    # encodes as a jsonb scalar too (``jsonb_set(meta, ["a"], 5)``), not as bigint/text.
    # ``None`` is the JSON literal ``null``, not SQL NULL: ``jsonb_set`` with a NULL value
    # returns NULL and would wipe the whole column (B10).
    if isinstance(value, Node):
        return value
    if value is None:
        # A literal SQL NULL keeps jsonb_set's "return NULL" behaviour, so send the JSON
        # text `null` and cast it. Binding the Python string "null" would instead store the
        # JSON *string* "null".
        return Fragment(("CAST('null' AS jsonb)",))
    return Cast(Param(value), "jsonb")


def path_keys(path: Iterable[str], name: str) -> list[str]:
    """A JSON path or key list. Reject a bare string: iterating it would give characters."""
    if isinstance(path, (str, bytes)):
        raise QueryError(f"{name}() takes a list of keys, not a single string")
    return list(path)


def jsonb_set(
    target: ExprBase, path: Sequence[str], value: object, *, create: bool = True
) -> ExprBase:
    """``jsonb_set(target, path, value, create)``: set the value at a text-array path."""
    return _func(
        "jsonb_set",
        target,
        Cast(Param(path_keys(path, "jsonb_set")), "text[]"),
        _jsonb(value),
        Fragment(("true" if create else "false",)),
    )


def jsonb_build_object(*pairs: object) -> Expr[dict[str, object]]:
    """Build a jsonb object from alternating keys and values:
    ``jsonb_build_object("name", Users.email, "n", count())``."""
    if len(pairs) % 2:
        raise QueryError("jsonb_build_object() takes key/value pairs")
    return TypedFunc[dict[str, object]]("jsonb_build_object", tuple(bind(p) for p in pairs))


def jsonb_build_array(*values: object) -> Expr[list[object]]:
    """Build a jsonb array from the values."""
    return TypedFunc[list[object]]("jsonb_build_array", tuple(bind(v) for v in values))


# -- dates and times, extras (EX10) ---------------------------------------------------


# A date or timestamp expression, non-null or nullable (B13: age/to_char keep nullability).
type _Moment = Expr[datetime.datetime] | Expr[datetime.date]
type _MaybeMoment = Expr[datetime.datetime | None] | Expr[datetime.date | None]


@overload
def age(value: _Moment, start: _Moment | None = None) -> Expr[datetime.timedelta]: ...
@overload
def age(
    value: _MaybeMoment, start: _Moment | _MaybeMoment | None = None
) -> Expr[datetime.timedelta | None]: ...
@overload
def age(value: _Moment, start: _MaybeMoment) -> Expr[datetime.timedelta | None]: ...
def age(value: ExprBase, start: ExprBase | None = None) -> ExprBase:
    """The interval between two timestamps, or from ``value`` to today (at midnight).
    NULL when either timestamp is NULL."""
    args: tuple[Node, ...] = (value,) if start is None else (value, start)
    return TypedFunc[datetime.timedelta | None]("age", args)


@overload
def to_char(
    value: _Moment | Expr[datetime.timedelta] | Expr[int] | Expr[float] | Expr[Decimal],
    template: str,
) -> Expr[str]: ...
@overload
def to_char(
    value: _MaybeMoment
    | Expr[datetime.timedelta | None]
    | Expr[int | None]
    | Expr[float | None]
    | Expr[Decimal | None],
    template: str,
) -> Expr[str | None]: ...
def to_char(value: ExprBase, template: str) -> ExprBase:
    """Format a value as text with a template: ``to_char(Users.created_at, "YYYY-MM")``.
    NULL when the value is NULL."""
    return TypedFunc[str | None]("to_char", (value, bind(template)))


@overload
def to_date(text: str | Expr[str], format: str) -> Expr[datetime.date]: ...
@overload
def to_date(text: Expr[str | None], format: str) -> Expr[datetime.date | None]: ...
def to_date(text: str | ExprBase, format: str) -> ExprBase:
    """Parse ``text`` as a date with a template: ``to_date("2024-01-02", "YYYY-MM-DD")``.
    NULL when the text is NULL."""
    return TypedFunc[datetime.date | None]("to_date", (bind(text), bind(format)))


def to_timestamp(value: float | ExprBase) -> Expr[datetime.datetime]:
    """A Unix epoch (seconds since 1970) as ``timestamptz``."""
    return TypedFunc[datetime.datetime]("to_timestamp", (bind(value),))


def make_interval(
    *,
    years: int = 0,
    months: int = 0,
    weeks: int = 0,
    days: int = 0,
    hours: int = 0,
    mins: int = 0,
    secs: float = 0.0,
) -> Expr[datetime.timedelta]:
    """Build an interval from named parts: ``make_interval(days=7)``."""
    parts: tuple[tuple[str, int | float], ...] = (
        ("years", _whole("years", years)),
        ("months", _whole("months", months)),
        ("weeks", _whole("weeks", weeks)),
        ("days", _whole("days", days)),
        ("hours", _whole("hours", hours)),
        ("mins", _whole("mins", mins)),
        ("secs", _number("secs", secs)),
    )
    # ``!r`` on an int subclass (an IntEnum, or one with a crafted __repr__) is not SQL:
    # format the plain number instead (B21).
    args = [Fragment((f"{name} => {value}",)) for name, value in parts if value]
    return TypedFunc[datetime.timedelta]("make_interval", tuple(args))


def current_timestamp() -> Expr[datetime.datetime]:
    """The start time of the transaction as ``timestamptz`` (SQL standard spelling)."""
    return TypedFragment[datetime.datetime](("CURRENT_TIMESTAMP",))


def localtime() -> Expr[datetime.time]:
    """The current time of day without a time zone."""
    return TypedFragment[datetime.time](("LOCALTIME",))


# -- collation and time zone (EX11) ---------------------------------------------------


def collate[T](expr: Expr[T], name: str) -> Expr[T]:
    """``expr COLLATE name``: sort/compare ``expr`` with the named collation."""
    quoted = '"' + name.replace('"', '""') + '"'
    return PostfixOp[T](expr, "COLLATE", Fragment((quoted,)))


def at_time_zone[
    T: (
        datetime.datetime,
        datetime.datetime | None,
        datetime.time,
        datetime.time | None,
    )
](expr: Expr[T], zone: str | Expr[str]) -> Expr[T]:
    """``expr AT TIME ZONE zone``: convert between ``timestamp`` and ``timestamptz``."""
    return PostfixOp[T](expr, "AT TIME ZONE", bind(zone))

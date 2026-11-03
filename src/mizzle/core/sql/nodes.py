"""SQL expression tree. Nodes are immutable data; dialect compilers turn them into SQL text.

Typing pattern: a node that internal code handles generically is a plain class (e.g.
``Placeholder``, ``Func``); when user code needs its value type, a typed subclass adds it
(``TypedPlaceholder[T]``, ``TypedFunc[T]``). Internal code never needs ``Any``.
"""

# The operator overloads of Expr "overlap" only through bottom types (Expr is invariant, so
# Expr[str] and Expr[datetime] share no value); both checkers would flag every pair.
# mypy: disable-error-code="overload-overlap"
# pyright: reportOverlappingOverload=false

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, overload

if TYPE_CHECKING:
    from mizzle.core.query.select import Cte
    from mizzle.core.schema.column import ColumnBase


class Node:
    """Base class of every SQL tree node."""


class ExprBase(Node):
    """Any SQL expression, whatever its value type."""


class Expr[T](ExprBase):
    """An SQL expression whose Python value type is ``T``."""

    # Never set per instance. A public, mutable attribute mentioning T makes T invariant,
    # so eq(Users.id, "x") is a type error (underscore names are ignored for variance).
    py_type: type[T] | None = None

    # Text: ``Users.first + " " + Users.last`` is SQL ``||`` (NULL if either side is NULL).
    @overload
    def __add__(self: Expr[str], other: str | Expr[str], /) -> Expr[str]: ...
    @overload
    def __add__(self: Expr[str], other: Expr[str | None], /) -> Expr[str | None]: ...
    @overload
    def __add__(self: Expr[str | None], other: _Str, /) -> Expr[str | None]: ...
    # Dates and times: ``timestamp + interval`` and ``date + interval`` give a timestamp;
    # ``interval + interval`` an interval.
    @overload
    def __add__(
        self: Expr[datetime.timedelta], other: _Interval, /
    ) -> Expr[datetime.timedelta]: ...
    @overload
    def __add__(
        self: Expr[datetime.timedelta], other: Expr[datetime.timedelta | None], /
    ) -> Expr[datetime.timedelta | None]: ...
    @overload
    def __add__(
        self: Expr[datetime.timedelta | None], other: _AnyInterval, /
    ) -> Expr[datetime.timedelta | None]: ...
    @overload
    def __add__(self: Expr[datetime.datetime], other: _Interval, /) -> Expr[datetime.datetime]: ...
    @overload
    def __add__(self: Expr[datetime.date], other: _Interval, /) -> Expr[datetime.datetime]: ...
    @overload
    def __add__(
        self: Expr[datetime.datetime], other: Expr[datetime.timedelta | None], /
    ) -> Expr[datetime.datetime | None]: ...
    @overload
    def __add__(
        self: Expr[datetime.date], other: Expr[datetime.timedelta | None], /
    ) -> Expr[datetime.datetime | None]: ...
    @overload
    def __add__(
        self: Expr[datetime.datetime | None], other: _AnyInterval, /
    ) -> Expr[datetime.datetime | None]: ...
    @overload
    def __add__(
        self: Expr[datetime.date | None], other: _AnyInterval, /
    ) -> Expr[datetime.datetime | None]: ...
    # Arithmetic: ``Users.age + 1``. Both sides have the same numeric type (an int literal
    # also fits float and Decimal); NULL on either side makes the result NULL. As in SQL,
    # ``/`` on integers is integer division.
    @overload
    def __add__[N: (int, float, Decimal)](
        self: Expr[N], other: N | int | Expr[N], /
    ) -> Expr[N]: ...
    @overload
    def __add__[N: (int, float, Decimal)](
        self: Expr[N], other: Expr[N | None], /
    ) -> Expr[N | None]: ...
    @overload
    def __add__[N: (int, float, Decimal)](
        self: Expr[N | None], other: _Num[N], /
    ) -> Expr[N | None]: ...
    def __add__(self: ExprBase, other: object, /) -> ExprBase:
        return _arith(self, "+", other)

    # ``timestamp - interval`` is a timestamp, ``timestamp - timestamp`` an interval and
    # ``date - date`` a number of days.
    @overload
    def __sub__(
        self: Expr[datetime.timedelta], other: _Interval, /
    ) -> Expr[datetime.timedelta]: ...
    @overload
    def __sub__(
        self: Expr[datetime.timedelta], other: Expr[datetime.timedelta | None], /
    ) -> Expr[datetime.timedelta | None]: ...
    @overload
    def __sub__(
        self: Expr[datetime.timedelta | None], other: _AnyInterval, /
    ) -> Expr[datetime.timedelta | None]: ...
    @overload
    def __sub__(self: Expr[datetime.datetime], other: _Interval, /) -> Expr[datetime.datetime]: ...
    @overload
    def __sub__(self: Expr[datetime.date], other: _Interval, /) -> Expr[datetime.datetime]: ...
    @overload
    def __sub__(
        self: Expr[datetime.datetime], other: Expr[datetime.timedelta | None], /
    ) -> Expr[datetime.datetime | None]: ...
    @overload
    def __sub__(
        self: Expr[datetime.date], other: Expr[datetime.timedelta | None], /
    ) -> Expr[datetime.datetime | None]: ...
    @overload
    def __sub__(
        self: Expr[datetime.datetime | None], other: _AnyInterval, /
    ) -> Expr[datetime.datetime | None]: ...
    @overload
    def __sub__(
        self: Expr[datetime.date | None], other: _AnyInterval, /
    ) -> Expr[datetime.datetime | None]: ...
    @overload
    def __sub__(
        self: Expr[datetime.datetime], other: datetime.datetime | Expr[datetime.datetime], /
    ) -> Expr[datetime.timedelta]: ...
    @overload
    def __sub__(
        self: Expr[datetime.datetime], other: Expr[datetime.datetime | None], /
    ) -> Expr[datetime.timedelta | None]: ...
    @overload
    def __sub__(
        self: Expr[datetime.datetime | None], other: _Timestamp, /
    ) -> Expr[datetime.timedelta | None]: ...
    @overload
    def __sub__(
        self: Expr[datetime.date], other: datetime.date | Expr[datetime.date], /
    ) -> Expr[int]: ...
    @overload
    def __sub__(
        self: Expr[datetime.date], other: Expr[datetime.date | None], /
    ) -> Expr[int | None]: ...
    @overload
    def __sub__(self: Expr[datetime.date | None], other: _Date, /) -> Expr[int | None]: ...
    @overload
    def __sub__[N: (int, float, Decimal)](
        self: Expr[N], other: N | int | Expr[N], /
    ) -> Expr[N]: ...
    @overload
    def __sub__[N: (int, float, Decimal)](
        self: Expr[N], other: Expr[N | None], /
    ) -> Expr[N | None]: ...
    @overload
    def __sub__[N: (int, float, Decimal)](
        self: Expr[N | None], other: _Num[N], /
    ) -> Expr[N | None]: ...
    def __sub__(self: ExprBase, other: object, /) -> ExprBase:
        return _arith(self, "-", other)

    @overload
    def __mul__[N: (int, float, Decimal)](
        self: Expr[N], other: N | int | Expr[N], /
    ) -> Expr[N]: ...
    @overload
    def __mul__[N: (int, float, Decimal)](
        self: Expr[N], other: Expr[N | None], /
    ) -> Expr[N | None]: ...
    @overload
    def __mul__[N: (int, float, Decimal)](
        self: Expr[N | None], other: _Num[N], /
    ) -> Expr[N | None]: ...
    def __mul__(self: ExprBase, other: object, /) -> ExprBase:
        return _arith(self, "*", other)

    @overload
    def __truediv__[N: (int, float, Decimal)](
        self: Expr[N], other: N | int | Expr[N], /
    ) -> Expr[N]: ...
    @overload
    def __truediv__[N: (int, float, Decimal)](
        self: Expr[N], other: Expr[N | None], /
    ) -> Expr[N | None]: ...
    @overload
    def __truediv__[N: (int, float, Decimal)](
        self: Expr[N | None], other: _Num[N], /
    ) -> Expr[N | None]: ...
    def __truediv__(self: ExprBase, other: object, /) -> ExprBase:
        return _arith(self, "/", other)

    # ``timedelta(days=1) + Users.created_at``: a timestamp (or an interval).
    @overload
    def __radd__(
        self: Expr[datetime.datetime], other: datetime.timedelta, /
    ) -> Expr[datetime.datetime]: ...
    @overload
    def __radd__(
        self: Expr[datetime.date], other: datetime.timedelta, /
    ) -> Expr[datetime.datetime]: ...
    @overload
    def __radd__(
        self: Expr[datetime.datetime | None], other: datetime.timedelta, /
    ) -> Expr[datetime.datetime | None]: ...
    @overload
    def __radd__(
        self: Expr[datetime.date | None], other: datetime.timedelta, /
    ) -> Expr[datetime.datetime | None]: ...
    @overload
    def __radd__(
        self: Expr[datetime.timedelta], other: datetime.timedelta, /
    ) -> Expr[datetime.timedelta]: ...
    @overload
    def __radd__(
        self: Expr[datetime.timedelta | None], other: datetime.timedelta, /
    ) -> Expr[datetime.timedelta | None]: ...
    @overload
    def __radd__[N: (int, float, Decimal)](self: Expr[N], other: N | int, /) -> Expr[N]: ...
    @overload
    def __radd__[N: (int, float, Decimal)](
        self: Expr[N | None], other: N | int, /
    ) -> Expr[N | None]: ...
    @overload
    def __radd__(self: Expr[str], other: str, /) -> Expr[str]: ...
    @overload
    def __radd__(self: Expr[str | None], other: str, /) -> Expr[str | None]: ...
    def __radd__(self: ExprBase, other: object, /) -> ExprBase:
        return _arith(other, "+", self)

    @overload
    def __rsub__[N: (int, float, Decimal)](self: Expr[N], other: N | int, /) -> Expr[N]: ...
    @overload
    def __rsub__[N: (int, float, Decimal)](
        self: Expr[N | None], other: N | int, /
    ) -> Expr[N | None]: ...
    def __rsub__(self: ExprBase, other: object, /) -> ExprBase:
        return _arith(other, "-", self)

    @overload
    def __rmul__[N: (int, float, Decimal)](self: Expr[N], other: N | int, /) -> Expr[N]: ...
    @overload
    def __rmul__[N: (int, float, Decimal)](
        self: Expr[N | None], other: N | int, /
    ) -> Expr[N | None]: ...
    def __rmul__(self: ExprBase, other: object, /) -> ExprBase:
        return _arith(other, "*", self)

    @overload
    def __rtruediv__[N: (int, float, Decimal)](self: Expr[N], other: N | int, /) -> Expr[N]: ...
    @overload
    def __rtruediv__[N: (int, float, Decimal)](
        self: Expr[N | None], other: N | int, /
    ) -> Expr[N | None]: ...
    def __rtruediv__(self: ExprBase, other: object, /) -> ExprBase:
        return _arith(other, "/", self)


# The right side of arithmetic on a nullable left side.
type _Num[N] = N | int | Expr[N] | Expr[N | None]
type _Str = str | Expr[str] | Expr[str | None]
type _Interval = datetime.timedelta | Expr[datetime.timedelta]
type _AnyInterval = _Interval | Expr[datetime.timedelta | None]
type _Timestamp = datetime.datetime | Expr[datetime.datetime] | Expr[datetime.datetime | None]
type _Date = datetime.date | Expr[datetime.date] | Expr[datetime.date | None]


def _arith(left: object, op: str, right: object) -> ExprBase:
    from mizzle.core.sql.functions import bind, is_text  # functions imports this module

    if op == "+" and (is_text(left) or is_text(right)):
        # Plain strings are cast to text: a column type would not tell || what they are.
        return TypedOperation[object](bind(left), "||", bind(right))
    # A Python interval next to a timestamp is an interval, not a value of that column.
    return TypedOperation[object](
        bind(left, () if isinstance(left, datetime.timedelta) else (right,)),
        op,
        bind(right, () if isinstance(right, datetime.timedelta) else (left,)),
    )


@dataclass(frozen=True)
class Raw(ExprBase):
    """Raw SQL from ``sql(...)``. String parts are SQL text; node parts are compiled (values
    become bind parameters). Its value type is unknown, so APIs accept it explicitly."""

    parts: tuple[str | Node, ...]


# Where a boolean condition goes: a typed expression or raw SQL.
type Condition = Expr[bool] | Raw


@dataclass(frozen=True)
class Param(ExprBase):
    """A value sent as a bind parameter. ``column`` (if known) encodes it."""

    value: object
    column: ColumnBase | None = None


@dataclass(frozen=True)
class Placeholder(ExprBase):
    """A named value supplied later to a prepared query (see ``placeholder()``)."""

    name: str
    column: ColumnBase | None = None


@dataclass(frozen=True)
class TypedPlaceholder[T](Placeholder, Expr[T]):
    pass


@dataclass(frozen=True)
class BinaryOp(Expr[bool]):
    left: Node
    op: str
    right: Node


@dataclass(frozen=True)
class BoolOp(Expr[bool]):
    op: Literal["AND", "OR"]
    items: tuple[Node, ...]


@dataclass(frozen=True)
class Not(Expr[bool]):
    item: Node


@dataclass(frozen=True)
class Postfix(Expr[bool]):
    item: Node
    op: str


@dataclass(frozen=True)
class InList(Expr[bool]):
    """``item IN (...)``. ``values`` is a tuple of nodes or a subquery node."""

    item: Node
    values: tuple[Node, ...] | Node
    negate: bool = False


@dataclass(frozen=True)
class Between(Expr[bool]):
    item: Node
    low: Node
    high: Node


@dataclass(frozen=True)
class Exists(Expr[bool]):
    query: Node
    negate: bool = False


@dataclass(frozen=True)
class Star(Node):
    pass


@dataclass(frozen=True)
class Func(ExprBase):
    name: str
    args: tuple[Node, ...]
    distinct: bool = False
    # The result has the type of its arguments (min, coalesce, lag...): a column among them
    # decodes it (e.g. back into an enum).
    keeps_type: bool = False


@dataclass(frozen=True)
class TypedFunc[T](Func, Expr[T]):
    pass


@dataclass(frozen=True)
class Aggregate[T](TypedFunc[T]):
    """An aggregate (``count``, ``sum_``...); ``.over(...)`` makes it a window function."""

    def over(
        self,
        window: str | None = None,
        /,
        *,
        partition_by: ExprBase | Sequence[ExprBase] = (),
        order_by: ExprBase | OrderBy | Sequence[ExprBase | OrderBy] = (),
        frame: Frame | None = None,
    ) -> Expr[T]:
        """``.over(partition_by=..., order_by=...)``, or ``.over("w")`` for a window named
        with ``query.window("w", ...)`` (other arguments then add to it)."""
        return over(self, window, partition_by, order_by, frame)


@dataclass(frozen=True)
class Operation(ExprBase):
    """Arithmetic: ``left op right``."""

    left: Node
    op: str
    right: Node


@dataclass(frozen=True)
class TypedOperation[T](Operation, Expr[T]):
    pass


@dataclass(frozen=True)
class Frame:
    """A window frame; see ``rows_between()``. Offsets: negative = PRECEDING, 0 = CURRENT ROW,
    positive = FOLLOWING, None = UNBOUNDED."""

    kind: Literal["ROWS", "RANGE", "GROUPS"]
    start: int | None
    end: int | None


@dataclass(frozen=True)
class Window:
    """A window: ``[base] PARTITION BY ... ORDER BY ... frame``; ``base`` names a window
    of the query's ``WINDOW`` clause."""

    partition_by: tuple[Node, ...]
    order_by: tuple[Node, ...]
    frame: Frame | None
    base: str | None = None


@dataclass(frozen=True)
class Over(ExprBase):
    """``func OVER (window)``."""

    func: Func
    window: Window


@dataclass(frozen=True)
class TypedOver[T](Over, Expr[T]):
    pass


def window(
    base: str | None,
    partition_by: ExprBase | Sequence[ExprBase],
    order_by: ExprBase | OrderBy | Sequence[ExprBase | OrderBy],
    frame: Frame | None,
) -> Window:
    partition = (partition_by,) if isinstance(partition_by, ExprBase) else tuple(partition_by)
    order = (order_by,) if isinstance(order_by, ExprBase | OrderBy) else tuple(order_by)
    return Window(partition, order, frame, base)


def over[T](
    func: Func,
    base: str | None,
    partition_by: ExprBase | Sequence[ExprBase],
    order_by: ExprBase | OrderBy | Sequence[ExprBase | OrderBy],
    frame: Frame | None,
) -> Expr[T]:
    return TypedOver[T](func, window(base, partition_by, order_by, frame))


@dataclass(frozen=True)
class Fragment(ExprBase):
    """SQL built by mizzle itself (``CURRENT_DATE``, ``extract(year FROM ...)``). String parts
    are fixed or validated text, never user input; node parts are compiled. Unlike ``Raw``
    it cannot change session state."""

    parts: tuple[str | Node, ...]


@dataclass(frozen=True)
class TypedFragment[T](Fragment, Expr[T]):
    pass


@dataclass(frozen=True)
class Cast(ExprBase):
    """``CAST(expr AS type)``. ``column`` (if any) decodes the result."""

    expr: Node
    type: str
    column: ColumnBase | None = None


@dataclass(frozen=True)
class TypedCast[T](Cast, Expr[T]):
    pass


@dataclass(frozen=True)
class Nullable(ExprBase):
    """Marks an expression as possibly NULL for type checkers (see ``nullable()``).
    Compiles and decodes exactly like the wrapped expression."""

    expr: ExprBase


@dataclass(frozen=True)
class TypedNullable[T](Nullable, Expr[T]):
    pass


@dataclass(frozen=True)
class OrderBy(Node):
    expr: Node
    descending: bool = False
    nulls: Literal["first", "last"] | None = None


@dataclass(frozen=True)
class Identifier(Node):
    name: str


@dataclass(frozen=True)
class Excluded(ExprBase):
    """``excluded.<column>`` inside ON CONFLICT DO UPDATE."""

    column: ColumnBase


@dataclass(frozen=True, eq=False)
class CteColumn(ExprBase):
    """A column of a CTE or subquery, by name; see ``Cte.c()``."""

    cte: Cte
    name: str
    type_: type[object] | None = None  # as declared by the user, unchecked


@dataclass(frozen=True, eq=False)
class TypedCteColumn[T](CteColumn, Expr[T]):
    pass

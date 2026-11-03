"""SQL expression tree. Nodes are immutable data; dialect compilers turn them into SQL text.

Typing pattern: a node that internal code handles generically is a plain class (e.g.
``Placeholder``, ``Func``); when user code needs its value type, a typed subclass adds it
(``TypedPlaceholder[T]``, ``TypedFunc[T]``). Internal code never needs ``Any``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, overload

if TYPE_CHECKING:
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

    @overload
    def __radd__[N: (int, float, Decimal)](self: Expr[N], other: N | int, /) -> Expr[N]: ...
    @overload
    def __radd__[N: (int, float, Decimal)](
        self: Expr[N | None], other: N | int, /
    ) -> Expr[N | None]: ...
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


def _arith(left: object, op: str, right: object) -> ExprBase:
    from mizzle.core.sql.functions import bind  # functions imports this module

    return TypedOperation[object](bind(left, (right,)), op, bind(right, (left,)))


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
        *,
        partition_by: ExprBase | Sequence[ExprBase] = (),
        order_by: ExprBase | OrderBy | Sequence[ExprBase | OrderBy] = (),
        frame: Frame | None = None,
    ) -> Expr[T]:
        return over(self, partition_by, order_by, frame)


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
class Over(ExprBase):
    """``func OVER (PARTITION BY ... ORDER BY ... frame)``."""

    func: Func
    partition_by: tuple[Node, ...]
    order_by: tuple[Node, ...]
    frame: Frame | None


@dataclass(frozen=True)
class TypedOver[T](Over, Expr[T]):
    pass


def over[T](
    func: Func,
    partition_by: ExprBase | Sequence[ExprBase],
    order_by: ExprBase | OrderBy | Sequence[ExprBase | OrderBy],
    frame: Frame | None,
) -> Expr[T]:
    partition = (partition_by,) if isinstance(partition_by, ExprBase) else tuple(partition_by)
    order = (order_by,) if isinstance(order_by, ExprBase | OrderBy) else tuple(order_by)
    return TypedOver[T](func, partition, order, frame)


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

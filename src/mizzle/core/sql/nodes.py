"""SQL expression tree. Nodes are immutable data; dialect compilers turn them into SQL text.

Typing pattern: a node that internal code handles generically is a plain class (e.g.
``Placeholder``, ``Func``); when user code needs its value type, a typed subclass adds it
(``TypedPlaceholder[T]``, ``TypedFunc[T]``). Internal code never needs ``Any``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

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


@dataclass(frozen=True)
class TypedFunc[T](Func, Expr[T]):
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

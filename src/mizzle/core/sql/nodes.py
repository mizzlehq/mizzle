"""SQL expression tree. Nodes are immutable data; dialect compilers turn them into SQL text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from mizzle.core.schema.column import Column


class Node:
    """Base class of every SQL tree node."""


class Expr[T](Node):
    """An SQL expression whose Python value type is ``T``."""

    # Never assigned. A public attribute that mentions T makes T invariant, so
    # eq(Users.id, "x") is a type error. Underscore names are ignored by variance inference.
    py_type: type[T] | None


@dataclass(frozen=True)
class Param[T](Expr[T]):
    """A value sent as a bind parameter. ``column`` (if known) encodes it."""

    value: T
    column: Column[Any] | None = None


@dataclass(frozen=True)
class Placeholder(Expr[Any]):
    """A named value supplied later to a prepared query."""

    name: str
    column: Column[Any] | None = None


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
class Func[T](Expr[T]):
    name: str
    args: tuple[Node, ...]
    distinct: bool = False


@dataclass(frozen=True)
class OrderBy(Node):
    expr: Node
    descending: bool = False
    nulls: Literal["first", "last"] | None = None


@dataclass(frozen=True)
class Raw(Expr[Any]):
    """Raw SQL. String parts are SQL text; node parts are compiled (values become params)."""

    parts: tuple[str | Node, ...]


@dataclass(frozen=True)
class Identifier(Node):
    name: str


@dataclass(frozen=True)
class Excluded[T](Expr[T]):
    """``excluded.<column>`` inside ON CONFLICT DO UPDATE."""

    column: Column[T]

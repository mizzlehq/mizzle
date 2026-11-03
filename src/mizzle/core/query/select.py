"""SELECT builder."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Self, cast, overload, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import Executable, Row, Runner, decode_table
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import (
    Condition,
    Expr,
    ExprBase,
    Node,
    Nullable,
    OrderBy,
    Placeholder,
)
from mizzle.core.sql.operators import and_

type JoinKind = Literal["INNER", "LEFT", "RIGHT", "FULL"]
type Source = type[Table] | Cte
type Slot = ExprBase | type[Table]
# None: whole rows of the source table. Tuple: one result slot per item. Dict: named fields.
type Projection = tuple[Slot, ...] | dict[str, ExprBase] | None


@dataclass(frozen=True)
class Join:
    kind: JoinKind
    source: Source
    on: Condition


@dataclass(frozen=True)
class Lock:
    of: tuple[type[Table], ...] = ()
    skip_locked: bool = False
    nowait: bool = False


@dataclass(frozen=True, eq=False)
class Cte(Node):
    """A named subquery for ``WITH``. Reference its columns with ``cte.c("name")``."""

    name: str
    query: SelectBase

    def c[T](self, column: str, type_: type[T]) -> TypedCteColumn[T]:
        """A column of this CTE, e.g. ``cte.c("id", int)``. The type is not checked."""
        _ = type_  # only tells type checkers what the column holds
        return TypedCteColumn[T](self, column)


@dataclass(frozen=True, eq=False)
class CteColumn(ExprBase):
    cte: Cte
    name: str


@dataclass(frozen=True, eq=False)
class TypedCteColumn[T](CteColumn, Expr[T]):
    pass


def _count(name: str, value: int | Expr[int]) -> int | Placeholder:
    if isinstance(value, Placeholder):
        return value
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise QueryError(f"{name}() needs a non-negative int or placeholder(); got {value!r}")
    return value


@dataclass(frozen=True, eq=False)
class SelectBase(Node):
    """A SELECT and its clauses, without the result type (see ``Select[R]``)."""

    runner: Runner = field(repr=False)
    projection: Projection
    source: Source
    ctes: tuple[Cte, ...] = ()
    joins: tuple[Join, ...] = ()
    where_: Condition | None = None
    group_by_: tuple[ExprBase, ...] = ()
    having_: Condition | None = None
    order_by_: tuple[ExprBase | OrderBy, ...] = ()
    limit_: int | Placeholder | None = None
    offset_: int | Placeholder | None = None
    distinct_: bool = False
    lock: Lock | None = None

    # -- clauses ----------------------------------------------------------------------

    def where(self, condition: Condition) -> Self:
        """Add a filter. Calling ``where`` again ANDs the conditions."""
        combined = condition if self.where_ is None else and_(self.where_, condition)
        return replace(self, where_=combined)

    def _join(self, kind: JoinKind, source: Source, on: Condition) -> Self:
        if kind in ("RIGHT", "FULL") and self.projection is None:
            raise QueryError(
                f"{kind.lower()} join needs an explicit select(A, B) so missing rows are typed"
            )
        return replace(self, joins=(*self.joins, Join(kind, source, on)))

    def join(self, source: Source, on: Condition) -> Self:
        return self._join("INNER", source, on)

    def group_by(self, *exprs: ExprBase) -> Self:
        return replace(self, group_by_=(*self.group_by_, *exprs))

    def having(self, condition: Condition) -> Self:
        combined = condition if self.having_ is None else and_(self.having_, condition)
        return replace(self, having_=combined)

    def order_by(self, *items: ExprBase | OrderBy) -> Self:
        return replace(self, order_by_=(*self.order_by_, *items))

    def limit(self, n: int | Expr[int]) -> Self:
        return replace(self, limit_=_count("limit", n))

    def offset(self, n: int | Expr[int]) -> Self:
        return replace(self, offset_=_count("offset", n))

    def distinct(self) -> Self:
        return replace(self, distinct_=True)

    def for_update(
        self, *, of: Sequence[type[Table]] = (), skip_locked: bool = False, nowait: bool = False
    ) -> Self:
        if skip_locked and nowait:
            raise QueryError("for_update() takes skip_locked or nowait, not both")
        return replace(self, lock=Lock(tuple(of), skip_locked, nowait))

    def cte(self, name: str) -> Cte:
        """Use this query as a named CTE: ``db.with_(cte).select(...).from_(cte)``."""
        return Cte(name, self)

    # -- results ----------------------------------------------------------------------

    def slots(self) -> tuple[Slot, ...]:
        if self.projection is None:
            assert not isinstance(self.source, Cte)  # select() only accepts tables
            return (self.source,)
        if isinstance(self.projection, dict):
            return tuple(self.projection.values())
        return self.projection

    def decode(self, row: Row) -> object:
        values: list[object] = []
        index = 0
        for slot in self.slots():
            if isinstance(slot, type):
                values.append(decode_table(slot, row, index))
                index += len(slot.__table__.columns)
            else:
                value = row[index]
                inner = slot.expr if isinstance(slot, Nullable) else slot
                values.append(inner.decode(value) if isinstance(inner, ColumnBase) else value)
                index += 1
        if self.projection is None:
            return values[0]
        if isinstance(self.projection, dict):
            return dict(zip(self.projection, values, strict=True))
        return tuple(values)


@dataclass(frozen=True, eq=False)
class Select[R](SelectBase, Executable[list[R]]):
    """A SELECT whose rows are ``R`` (a table row, a tuple, or a dict)."""

    @overload
    def left_join[A, J: Table](
        self: Select[tuple[A, J]], source: type[J], on: Condition
    ) -> Select[tuple[A, J | None]]: ...
    @overload
    def left_join[A, B, J: Table](  # type: ignore[overload-overlap]
        self: Select[tuple[A, B, J]], source: type[J], on: Condition
    ) -> Select[tuple[A, B, J | None]]: ...
    @overload
    def left_join[A, B, J: Table](
        self: Select[tuple[A, J, B]], source: type[J], on: Condition
    ) -> Select[tuple[A, J | None, B]]: ...
    @overload
    def left_join(self, source: Source, on: Condition) -> Self: ...
    def left_join(self, source: Source, on: Condition) -> SelectBase:
        return self._join("LEFT", source, on)

    @overload
    def right_join[A, J: Table](
        self: Select[tuple[A, J]], source: type[J], on: Condition
    ) -> Select[tuple[A | None, J]]: ...
    @overload
    def right_join(self, source: Source, on: Condition) -> Self: ...
    def right_join(self, source: Source, on: Condition) -> SelectBase:
        return self._join("RIGHT", source, on)

    @overload
    def full_join[A, J: Table](
        self: Select[tuple[A, J]], source: type[J], on: Condition
    ) -> Select[tuple[A | None, J | None]]: ...
    @overload
    def full_join(self, source: Source, on: Condition) -> Self: ...
    def full_join(self, source: Source, on: Condition) -> SelectBase:
        return self._join("FULL", source, on)

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> list[R]:
        # decode() builds exactly the shape R describes (rows, tuples or dicts).
        # FOR UPDATE takes row locks, so only a lock-free SELECT may go to a replica.
        rows = await self.runner.fetch(compiled, read_only=self.lock is None)
        return [cast(R, self.decode(row)) for row in rows]

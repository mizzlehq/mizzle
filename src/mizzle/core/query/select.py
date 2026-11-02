"""SELECT builder."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Self, overload

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import Executable, Row, Runner, decode_table
from mizzle.core.schema.column import Column
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Expr, Node, OrderBy, Placeholder
from mizzle.core.sql.operators import and_

type JoinKind = Literal["INNER", "LEFT", "RIGHT", "FULL"]
type Source = type[Table] | Cte
type Slot = Expr[Any] | type[Table]
# None: whole rows of the source table. Tuple: one result slot per item. Dict: named fields.
type Projection = tuple[Slot, ...] | dict[str, Expr[Any]] | None


@dataclass(frozen=True)
class Join:
    kind: JoinKind
    source: Source
    on: Expr[bool]


@dataclass(frozen=True)
class Lock:
    of: tuple[type[Table], ...] = ()
    skip_locked: bool = False
    nowait: bool = False


@dataclass(frozen=True, eq=False)
class Cte(Node):
    """A named subquery for ``WITH``. Reference its columns with ``cte.c("name")``."""

    name: str
    query: Select[Any]

    def c(self, column: str) -> CteColumn:
        return CteColumn(self, column)


@dataclass(frozen=True, eq=False)
class CteColumn(Expr[Any]):
    cte: Cte
    name: str


def _check_count(name: str, value: int | Placeholder) -> None:
    if isinstance(value, Placeholder):
        return
    if isinstance(value, bool) or value < 0:
        raise QueryError(f"{name}() needs a non-negative int; got {value!r}")


@dataclass(frozen=True, eq=False)
class Select[R](Executable[list[R]]):
    runner: Runner = field(repr=False)
    projection: Projection
    source: Source
    ctes: tuple[Cte, ...] = ()
    joins: tuple[Join, ...] = ()
    where_: Expr[bool] | None = None
    group_by_: tuple[Expr[Any], ...] = ()
    having_: Expr[bool] | None = None
    order_by_: tuple[Expr[Any] | OrderBy, ...] = ()
    limit_: int | Placeholder | None = None
    offset_: int | Placeholder | None = None
    distinct_: bool = False
    lock: Lock | None = None

    # -- clauses ----------------------------------------------------------------------

    def where(self, condition: Expr[bool]) -> Self:
        """Add a filter. Calling ``where`` again ANDs the conditions."""
        combined = condition if self.where_ is None else and_(self.where_, condition)
        return replace(self, where_=combined)

    def _join(self, kind: JoinKind, source: Source, on: Expr[bool]) -> Self:
        if kind in ("RIGHT", "FULL") and self.projection is None:
            raise QueryError(
                f"{kind.lower()} join needs an explicit select(A, B) so missing rows are typed"
            )
        return replace(self, joins=(*self.joins, Join(kind, source, on)))

    def join(self, source: Source, on: Expr[bool]) -> Self:
        return self._join("INNER", source, on)

    @overload
    def left_join[A, J: Table](
        self: Select[tuple[A, J]], source: type[J], on: Expr[bool]
    ) -> Select[tuple[A, J | None]]: ...
    @overload
    def left_join[A, B, J: Table](  # type: ignore[overload-overlap]
        self: Select[tuple[A, B, J]], source: type[J], on: Expr[bool]
    ) -> Select[tuple[A, B, J | None]]: ...
    @overload
    def left_join[A, B, J: Table](
        self: Select[tuple[A, J, B]], source: type[J], on: Expr[bool]
    ) -> Select[tuple[A, J | None, B]]: ...
    @overload
    def left_join(self, source: Source, on: Expr[bool]) -> Self: ...
    def left_join(self, source: Source, on: Expr[bool]) -> Any:
        return self._join("LEFT", source, on)

    @overload
    def right_join[A, J: Table](
        self: Select[tuple[A, J]], source: type[J], on: Expr[bool]
    ) -> Select[tuple[A | None, J]]: ...
    @overload
    def right_join(self, source: Source, on: Expr[bool]) -> Self: ...
    def right_join(self, source: Source, on: Expr[bool]) -> Any:
        return self._join("RIGHT", source, on)

    @overload
    def full_join[A, J: Table](
        self: Select[tuple[A, J]], source: type[J], on: Expr[bool]
    ) -> Select[tuple[A | None, J | None]]: ...
    @overload
    def full_join(self, source: Source, on: Expr[bool]) -> Self: ...
    def full_join(self, source: Source, on: Expr[bool]) -> Any:
        return self._join("FULL", source, on)

    def group_by(self, *exprs: Expr[Any]) -> Self:
        return replace(self, group_by_=(*self.group_by_, *exprs))

    def having(self, condition: Expr[bool]) -> Self:
        combined = condition if self.having_ is None else and_(self.having_, condition)
        return replace(self, having_=combined)

    def order_by(self, *items: Expr[Any] | OrderBy) -> Self:
        return replace(self, order_by_=(*self.order_by_, *items))

    def limit(self, n: int | Placeholder) -> Self:
        _check_count("limit", n)
        return replace(self, limit_=n)

    def offset(self, n: int | Placeholder) -> Self:
        _check_count("offset", n)
        return replace(self, offset_=n)

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

    def decode(self, row: Row) -> Any:
        values: list[Any] = []
        index = 0
        for slot in self.slots():
            if isinstance(slot, type):
                values.append(decode_table(slot, row, index))
                index += len(slot.__table__.columns)
            else:
                value = row[index]
                values.append(slot.decode(value) if isinstance(slot, Column) else value)
                index += 1
        if self.projection is None:
            return values[0]
        if isinstance(self.projection, dict):
            return dict(zip(self.projection, values, strict=True))
        return tuple(values)

    async def run_compiled(self, compiled: CompiledQuery) -> list[R]:
        return [self.decode(row) for row in await self.runner.fetch(compiled)]

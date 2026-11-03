"""SELECT builder."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Self, cast, overload, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import (
    Executable,
    Row,
    Runner,
    evolve,
    row_decoder,
    table_columns,
    table_decoder,
)
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
        return evolve(self, where_=combined)

    def _join(self, kind: JoinKind, source: Source, on: Condition) -> Self:
        if kind in ("RIGHT", "FULL") and self.projection is None:
            raise QueryError(
                f"{kind.lower()} join needs an explicit select(A, B) so missing rows are typed"
            )
        return evolve(self, joins=(*self.joins, Join(kind, source, on)))

    def join(self, source: Source, on: Condition) -> Self:
        return self._join("INNER", source, on)

    def group_by(self, *exprs: ExprBase) -> Self:
        return evolve(self, group_by_=(*self.group_by_, *exprs))

    def having(self, condition: Condition) -> Self:
        combined = condition if self.having_ is None else and_(self.having_, condition)
        return evolve(self, having_=combined)

    def order_by(self, *items: ExprBase | OrderBy) -> Self:
        return evolve(self, order_by_=(*self.order_by_, *items))

    def limit(self, n: int | Expr[int]) -> Self:
        return evolve(self, limit_=_count("limit", n))

    def offset(self, n: int | Expr[int]) -> Self:
        return evolve(self, offset_=_count("offset", n))

    def distinct(self) -> Self:
        return evolve(self, distinct_=True)

    def for_update(
        self, *, of: Sequence[type[Table]] = (), skip_locked: bool = False, nowait: bool = False
    ) -> Self:
        if skip_locked and nowait:
            raise QueryError("for_update() takes skip_locked or nowait, not both")
        return evolve(self, lock=Lock(tuple(of), skip_locked, nowait))

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

    def decoder(self) -> Callable[[Row], object]:
        """A function that decodes one result row. Built once per query, used per row."""
        if self.projection is None and not self.joins and isinstance(self.source, type):
            return table_decoder(self.source)
        outer = self._outer_sources()
        parts: list[Callable[[Row], object]] = []
        index = 0
        for slot in self.slots():
            if isinstance(slot, type):
                columns = table_columns(slot)
                decode = row_decoder(slot, columns, index)
                parts.append(_or_none(decode, index, len(columns)) if slot in outer else decode)
                index += len(columns)
            else:
                inner = slot.expr if isinstance(slot, Nullable) else slot
                convert = inner.decoder if isinstance(inner, ColumnBase) else None
                parts.append(_value(index, convert))
                index += 1
        if self.projection is None:
            return parts[0]
        if isinstance(self.projection, dict):
            keys = tuple(self.projection)
            return lambda row: dict(zip(keys, [part(row) for part in parts], strict=True))
        return lambda row: tuple([part(row) for part in parts])

    def _outer_sources(self) -> set[Source]:
        """Sources an outer join can leave empty (their columns come back all NULL)."""
        nullable: set[Source] = set()
        seen: list[Source] = [self.source]
        for join in self.joins:
            if join.kind in ("LEFT", "FULL"):
                nullable.add(join.source)
            if join.kind in ("RIGHT", "FULL"):
                nullable.update(seen)
            seen.append(join.source)
        return nullable


def _or_none(
    decode: Callable[[Row], Table], start: int, width: int
) -> Callable[[Row], Table | None]:
    # ponytail: a real row whose columns are all NULL (only possible in a table with no
    # NOT NULL column) is indistinguishable from "no match" and decodes to None.
    stop = start + width

    def decode_or_none(row: Row) -> Table | None:
        return None if all(v is None for v in row[start:stop]) else decode(row)

    return decode_or_none


def _value(index: int, convert: Callable[[object], object] | None) -> Callable[[Row], object]:
    if convert is None:
        return lambda row: row[index]

    def decode(row: Row) -> object:
        value = row[index]
        return None if value is None else convert(value)

    return decode


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
        rows = await self.runner.fetch(compiled, read_only=True)
        decode = self.decoder()
        return cast(list[R], [decode(row) for row in rows])

"""SELECT builder."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field, replace
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
from mizzle.core.sql.alias import alias_table, real_table
from mizzle.core.sql.functions import column_of
from mizzle.core.sql.nodes import (
    Condition,
    CteColumn,
    Expr,
    ExprBase,
    Frame,
    Node,
    OrderBy,
    Placeholder,
    TypedCteColumn,
    Window,
    window,
)
from mizzle.core.sql.operators import and_

type JoinKind = Literal["INNER", "LEFT", "RIGHT", "FULL", "CROSS"]
type Source = type[Table] | Cte
type Slot = ExprBase | type[Table]
# None: whole rows of the source table. Tuple: one result slot per item. Dict: named fields.
type Projection = tuple[Slot, ...] | dict[str, ExprBase] | None


@dataclass(frozen=True)
class Join:
    kind: JoinKind
    source: Source
    on: Condition | None  # None: CROSS JOIN


@dataclass(frozen=True)
class Lock:
    of: tuple[type[Table], ...] = ()
    skip_locked: bool = False
    nowait: bool = False


@dataclass(frozen=True, eq=False)
class Cte(Node):
    """A named subquery: a ``WITH`` query (``query.cte(name)``), or with ``inline`` a
    subquery in FROM (``query.subquery(name)``). Reference its columns with ``c()``."""

    name: str
    query: SelectBase | CompoundBase
    alias: str | None = None  # see alias(cte, name)
    inline: bool = False

    def c[T](self, column: str, type_: type[T]) -> TypedCteColumn[T]:
        """A column of this query, e.g. ``cte.c("id", int)``. The type is not checked;
        values decode like the column of the inner query they come from."""
        return TypedCteColumn[T](self, column, type_)

    def source_column(self, name: str) -> ColumnBase | None:
        """The column whose values the output column ``name`` holds, if known."""
        query = self.query
        while isinstance(query, CompoundBase):
            query = query.left  # output columns are named by the first query
        if isinstance(query.projection, dict):
            node = query.projection.get(name)
            return None if node is None else column_of(node)
        for slot in query.slots():
            if isinstance(slot, type):
                columns: tuple[ExprBase, ...] = tuple(slot.__table__.columns.values())
            else:
                columns = (slot,)
            for item in columns:
                if isinstance(item, ColumnBase) and name in (item.attr, item.db_name()):
                    return item
                if isinstance(item, CteColumn) and item.name == name:
                    return column_of(item)
        return None


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
    windows_: tuple[tuple[str, Window], ...] = ()

    # -- clauses ----------------------------------------------------------------------

    def where(self, condition: Condition) -> Self:
        """Add a filter. Calling ``where`` again ANDs the conditions."""
        combined = condition if self.where_ is None else and_(self.where_, condition)
        return evolve(self, where_=combined)

    def _join(self, kind: JoinKind, source: Source, on: Condition | None) -> Self:
        if kind in ("RIGHT", "FULL") and self.projection is None:
            raise QueryError(
                f"{kind.lower()} join needs an explicit select(A, B) so missing rows are typed"
            )
        return evolve(self, joins=(*self.joins, Join(kind, source, on)))

    def join(self, source: Source, on: Condition) -> Self:
        return self._join("INNER", source, on)

    def cross_join(self, source: Source) -> Self:
        """Every row paired with every row of ``source`` (``CROSS JOIN``, no condition)."""
        return self._join("CROSS", source, None)

    def group_by(self, *exprs: ExprBase) -> Self:
        return evolve(self, group_by_=(*self.group_by_, *exprs))

    def having(self, condition: Condition) -> Self:
        combined = condition if self.having_ is None else and_(self.having_, condition)
        return evolve(self, having_=combined)

    def window(
        self,
        name: str,
        /,
        *,
        partition_by: ExprBase | Sequence[ExprBase] = (),
        order_by: ExprBase | OrderBy | Sequence[ExprBase | OrderBy] = (),
        frame: Frame | None = None,
    ) -> Self:
        """A named window (``WINDOW name AS (...)``) for ``.over(name)`` in this query."""
        spec = window(None, partition_by, order_by, frame)
        return evolve(self, windows_=(*self.windows_, (name, spec)))

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

    def subquery(self, name: str) -> Cte:
        """Use this query in FROM or a join: ``db.select(sub.c("n", int)).from_(sub)``."""
        return Cte(name, self, inline=True)

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
            return table_decoder(real_table(self.source))
        outer = self._outer_sources()
        parts: list[Callable[[Row], object]] = []
        index = 0
        for slot in self.slots():
            if isinstance(slot, type):
                columns = table_columns(slot)
                decode = row_decoder(real_table(slot), columns, index)
                parts.append(_or_none(decode, index, len(columns)) if slot in outer else decode)
                index += len(columns)
            else:
                column = column_of(slot)
                parts.append(_value(index, column.decoder if column is not None else None))
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


class SetOps[R]:
    """``union``, ``intersect`` and ``except_`` between queries with the same row type."""

    def union(self, other: Select[R] | Compound[R]) -> Compound[R]:
        """Rows of both queries, without duplicates."""
        return self._set_op("UNION", other)

    def union_all(self, other: Select[R] | Compound[R]) -> Compound[R]:
        """Rows of both queries, duplicates kept."""
        return self._set_op("UNION ALL", other)

    def intersect(self, other: Select[R] | Compound[R]) -> Compound[R]:
        """Rows in both queries, without duplicates."""
        return self._set_op("INTERSECT", other)

    def intersect_all(self, other: Select[R] | Compound[R]) -> Compound[R]:
        return self._set_op("INTERSECT ALL", other)

    def except_(self, other: Select[R] | Compound[R]) -> Compound[R]:
        """Rows of this query that are not in ``other``, without duplicates."""
        return self._set_op("EXCEPT", other)

    def except_all(self, other: Select[R] | Compound[R]) -> Compound[R]:
        return self._set_op("EXCEPT ALL", other)

    def _set_op(self, op: str, other: Select[R] | Compound[R]) -> Compound[R]:
        assert isinstance(self, SelectBase | CompoundBase)
        return Compound[R](self.runner, self, op, other)


@dataclass(frozen=True, eq=False)
class CompoundBase(Node):
    """Queries combined with UNION / INTERSECT / EXCEPT (see ``Compound[R]``)."""

    runner: Runner = field(repr=False)
    left: SelectBase | CompoundBase
    op: str
    right: SelectBase | CompoundBase
    order_by_: tuple[ExprBase | OrderBy, ...] = ()
    limit_: int | Placeholder | None = None
    offset_: int | Placeholder | None = None

    def order_by(self, *items: ExprBase | OrderBy) -> Self:
        """Order the combined rows. Columns are matched by name to the result columns;
        expressions may use them too (``abs_(Items.a - 3)``)."""
        return evolve(self, order_by_=(*self.order_by_, *items))

    def limit(self, n: int | Expr[int]) -> Self:
        return evolve(self, limit_=_count("limit", n))

    def offset(self, n: int | Expr[int]) -> Self:
        return evolve(self, offset_=_count("offset", n))

    def cte(self, name: str) -> Cte:
        return Cte(name, self)

    def subquery(self, name: str) -> Cte:
        return Cte(name, self, inline=True)

    def decoder(self) -> Callable[[Row], object]:
        return self.left.decoder()  # rows have the shape of the first query


@dataclass(frozen=True, eq=False)
class Compound[R](CompoundBase, Executable[list[R]], SetOps[R]):
    @override
    async def run_compiled(self, compiled: CompiledQuery) -> list[R]:
        rows = await self.runner.fetch(compiled, read_only=True)
        decode = self.decoder()
        return cast(list[R], [decode(row) for row in rows])


@overload
def alias[T: Table](source: type[T], name: str, /) -> type[T]: ...
@overload
def alias(source: Cte, name: str, /) -> Cte: ...
def alias(source: type[Table] | Cte, name: str, /) -> type[Table] | Cte:
    """The same table (or CTE) under another name, to use it twice in one query:
    ``manager = alias(Employees, "manager")``, then ``manager.id`` and ``.join(manager, ...)``.
    Rows of an aliased table decode into the table's own class."""
    if isinstance(source, Cte):
        return replace(source, alias=name)
    return alias_table(source, name)


@dataclass(frozen=True, eq=False)
class Select[R](SelectBase, Executable[list[R]], SetOps[R]):
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

    async def stream(self, batch_size: int = 1000) -> AsyncGenerator[R]:
        """Rows one by one, read ``batch_size`` at a time from a server-side cursor, so
        large results never sit in memory at once. Holds a connection (and a transaction)
        until the loop ends; wrap in ``contextlib.aclosing()`` to release it right away
        when breaking out early."""
        decode = self.decoder()
        compiled = self._compile_for_run()
        async with aclosing(self.runner.stream(compiled, batch_size, read_only=True)) as batches:
            async for batch in batches:
                for row in batch:
                    yield cast(R, decode(row))

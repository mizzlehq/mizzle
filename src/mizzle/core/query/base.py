"""Shared machinery for executable statements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast, overload

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Node


class Row(Protocol):
    """A result row (asyncpg Record, tuple...): positional access, slices and iteration."""

    @overload
    def __getitem__(self, index: int, /) -> object: ...
    @overload
    def __getitem__(self, index: slice, /) -> Sequence[object]: ...
    def __iter__(self) -> Iterator[object]: ...
    def __len__(self) -> int: ...


def evolve[N: Node](node: N, **changes: object) -> N:
    """``dataclasses.replace`` for builder nodes, without re-running ``__init__``.

    Builders copy themselves on every chained call, so this is on the hot path. The nodes
    are frozen dataclasses without ``__post_init__``; field names are still checked.
    """
    fields = node.__dict__
    if not changes.keys() <= fields.keys():
        raise TypeError(
            f"{type(node).__name__} has no field {sorted(changes.keys() - fields.keys())}"
        )
    new = object.__new__(type(node))
    new.__dict__.update(fields)
    new.__dict__.update(changes)
    return new


class Runner(Protocol):
    """What a statement needs to run: a compiler and a connection. ``Session`` implements it."""

    def compile(self, node: Node) -> CompiledQuery: ...
    def fetch(self, query: CompiledQuery, *, read_only: bool = False) -> Awaitable[list[Row]]:
        """``read_only=True`` marks a read that may go to a read replica, unless the
        query takes row locks (``query.locking``)."""
        ...

    def run(self, query: CompiledQuery) -> Awaitable[int]: ...
    def stream(
        self, query: CompiledQuery, batch_size: int, *, read_only: bool = False
    ) -> AsyncGenerator[list[Row]]:
        """Rows in batches of ``batch_size`` from a server-side cursor."""
        ...


@dataclass(frozen=True, eq=False)
class Executable[R](Node, ABC):
    """A statement that can be awaited, compiled with ``to_sql()``, or prepared."""

    runner: Runner = field(repr=False)

    @abstractmethod
    async def run_compiled(self, compiled: CompiledQuery) -> R: ...

    def to_sql(self) -> tuple[str, tuple[object, ...]]:
        compiled = self.runner.compile(self)
        return compiled.sql, compiled.params

    def prepare(self) -> Prepared[R]:
        """Compile once; run many times with ``await prepared.execute(name=value)``."""
        return Prepared(self, self.runner.compile(self))

    def _compile_for_run(self) -> CompiledQuery:
        compiled = self.runner.compile(self)
        if compiled.has_placeholders:
            raise QueryError(
                "query uses placeholder(); call .prepare() and pass values to execute()"
            )
        return compiled

    async def execute(self) -> R:
        return await self.run_compiled(self._compile_for_run())

    def __await__(self) -> Generator[object, None, R]:
        # Straight to run_compiled: every coroutine layer is re-entered on each I/O wait.
        return self.run_compiled(self._compile_for_run()).__await__()


@dataclass(frozen=True)
class Prepared[R]:
    query: Executable[R]
    compiled: CompiledQuery

    async def execute(self, **values: object) -> R:
        compiled = self.compiled
        if compiled.dynamic:  # an .on_update(fn) hook must run on every execution
            compiled = self.query.runner.compile(self.query)
        return await self.query.run_compiled(compiled.bind(values))


type Converter = Callable[[object], object]


def column_decoder(column: ColumnBase) -> Converter | None:
    return column.decoder


def row_decoder[T: Table](
    table: type[T],
    columns: Sequence[tuple[str, ColumnBase]],
    start: int = 0,
    converter: Callable[[ColumnBase], Converter | None] = column_decoder,
) -> Callable[[Row], T]:
    """Build a function that turns ``row[start:start + len(columns)]`` into a ``table``
    row. Work that is the same for every row (names, converters) happens once, here.

    ``converter(column)`` gives the function applied to the column's non-NULL values, or
    None to keep them as they are."""
    names = tuple(name for name, _ in columns)
    converters = [
        (name, convert) for name, col in columns if (convert := converter(col)) is not None
    ]
    stop = start + len(names)
    whole = start == 0
    new = object.__new__

    def decode(values: Row) -> T:
        # zip() stops at the shorter side, so a full row needs no slice copy.
        data = dict(zip(names, values if whole else values[start:stop], strict=False))
        for name, convert in converters:
            value = data[name]
            if value is not None:
                data[name] = convert(value)
        obj = new(table)
        obj.__dict__ = data
        return obj

    return decode


def table_columns(table: type[Table]) -> tuple[tuple[str, ColumnBase], ...]:
    return tuple(table.__table__.columns.items())


# ponytail: never evicted; one small entry per table class, fine unless classes are
# generated without bound.
_TABLE_DECODERS: dict[type[Table], Callable[[Row], Table]] = {}


def table_decoder[T: Table](table: type[T]) -> Callable[[Row], T]:
    """The decoder for rows holding exactly ``table``'s columns, built once per table."""
    decode = _TABLE_DECODERS.get(table)
    if decode is None:
        decode = _TABLE_DECODERS[table] = row_decoder(table, table_columns(table))
    return cast(Callable[[Row], T], decode)

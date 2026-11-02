"""Shared machinery for executable statements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Protocol

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Node


class Row(Protocol):
    """A result row. Only positional access is needed (asyncpg Record, tuple...)."""

    def __getitem__(self, index: int, /) -> object: ...
    def __len__(self) -> int: ...


class Runner(Protocol):
    """What a statement needs to run: a compiler and a connection. ``Session`` implements it."""

    def compile(self, node: Node) -> CompiledQuery: ...
    async def fetch(self, query: CompiledQuery) -> list[Row]: ...
    async def run(self, query: CompiledQuery) -> int: ...


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

    async def execute(self) -> R:
        compiled = self.runner.compile(self)
        if compiled.has_placeholders:
            raise QueryError(
                "query uses placeholder(); call .prepare() and pass values to execute()"
            )
        return await self.run_compiled(compiled)

    def __await__(self) -> Generator[object, None, R]:
        return self.execute().__await__()


@dataclass(frozen=True)
class Prepared[R]:
    query: Executable[R]
    compiled: CompiledQuery

    async def execute(self, **values: object) -> R:
        return await self.query.run_compiled(self.compiled.bind(values))


def decode_table[T: Table](table: type[T], row: Row, start: int) -> T | None:
    """Decode ``table``'s columns from ``row`` starting at ``start``.

    Returns None when every column is NULL, which is how an outer join reports "no match".
    """
    # ponytail: a real row whose columns are all NULL (only possible in a table with no
    # NOT NULL column) is indistinguishable from "no match" and also decodes to None.
    columns = table.__table__.columns
    raw = [row[start + i] for i in range(len(columns))]
    if all(v is None for v in raw):
        return None
    return table.from_db(
        {attr: col.decode(v) for (attr, col), v in zip(columns.items(), raw, strict=True)}
    )

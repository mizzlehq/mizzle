from __future__ import annotations

from dataclasses import dataclass, field

from mizzle.core.compiler import CompiledQuery
from mizzle.core.query.base import Executable, Runner, decode_table
from mizzle.core.schema.table import Table


@dataclass(frozen=True, eq=False)
class Returning[T: Table](Executable[list[T]]):
    """An INSERT/UPDATE/DELETE that returns the affected rows."""

    runner: Runner = field(repr=False)
    query: Executable[int]
    table: type[T]

    async def run_compiled(self, compiled: CompiledQuery) -> list[T]:
        rows = await self.runner.fetch(compiled)
        decoded = [decode_table(self.table, row, 0) for row in rows]
        return [row for row in decoded if row is not None]

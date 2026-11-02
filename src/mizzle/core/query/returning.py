from __future__ import annotations

from dataclasses import dataclass
from typing import override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.query.base import Executable, decode_table
from mizzle.core.schema.table import Table


@dataclass(frozen=True, eq=False)
class Returning[T: Table](Executable[list[T]]):
    """An INSERT/UPDATE/DELETE that returns the affected rows."""

    query: Executable[int]
    table: type[T]

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> list[T]:
        rows = await self.runner.fetch(compiled)
        decoded = [decode_table(self.table, row, 0) for row in rows]
        return [row for row in decoded if row is not None]

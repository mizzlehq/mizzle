from __future__ import annotations

from dataclasses import dataclass
from typing import override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.query.base import Executable, table_decoder
from mizzle.core.schema.table import Table


@dataclass(frozen=True, eq=False)
class Returning[T: Table](Executable[list[T]]):
    """An INSERT/UPDATE/DELETE that returns the affected rows."""

    query: Executable[int]
    table: type[T]

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> list[T]:
        rows = await self.runner.fetch(compiled)
        decode = table_decoder(self.table)
        return [decode(row) for row in rows]

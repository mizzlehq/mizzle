"""UPDATE and DELETE builders.

Both refuse to run without ``.where()`` unless ``.all_rows()`` is called, so a forgotten
filter cannot rewrite or wipe a whole table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import Executable, evolve
from mizzle.core.query.insert import RowLike, as_mapping, check_values
from mizzle.core.query.returning import Returning
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Condition
from mizzle.core.sql.operators import and_


@dataclass(frozen=True, eq=False)
class Update[T: Table](Executable[int]):
    """Awaiting returns the number of updated rows; ``.returning()`` returns the rows."""

    table: type[T]
    values_: dict[str, object] = field(default_factory=dict[str, object])
    where_: Condition | None = None
    all_rows_: bool = False

    def set(self, values: RowLike | None = None, /, **fields: object) -> Self:
        """``set(bio="x")``, ``set({"bio": "x"})`` or ``set(pydantic_model)``."""
        if values is not None and fields:
            raise QueryError("pass a mapping or keyword arguments to set(), not both")
        changes = dict(as_mapping(values)) if values is not None else fields
        check_values(self.table, changes)
        return evolve(self, values_={**self.values_, **changes})

    def where(self, condition: Condition) -> Self:
        """Add a filter. Calling ``where`` again ANDs the conditions."""
        combined = condition if self.where_ is None else and_(self.where_, condition)
        return evolve(self, where_=combined)

    def all_rows(self) -> Self:
        """Allow running without ``.where()``."""
        return evolve(self, all_rows_=True)

    def returning(self) -> Returning[T]:
        return Returning(self.runner, self, self.table)

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> int:
        return await self.runner.run(compiled)


@dataclass(frozen=True, eq=False)
class Delete[T: Table](Executable[int]):
    """Awaiting returns the number of deleted rows; ``.returning()`` returns the rows."""

    table: type[T]
    where_: Condition | None = None
    all_rows_: bool = False

    def where(self, condition: Condition) -> Self:
        """Add a filter. Calling ``where`` again ANDs the conditions."""
        combined = condition if self.where_ is None else and_(self.where_, condition)
        return evolve(self, where_=combined)

    def all_rows(self) -> Self:
        """Allow running without ``.where()``."""
        return evolve(self, all_rows_=True)

    def returning(self) -> Returning[T]:
        return Returning(self.runner, self, self.table)

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> int:
        return await self.runner.run(compiled)

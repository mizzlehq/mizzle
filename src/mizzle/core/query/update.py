"""UPDATE and DELETE builders.

Both refuse to run without ``.where()`` unless ``.all_rows()`` is called, so a forgotten
filter cannot rewrite or wipe a whole table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import evolve
from mizzle.core.query.insert import RowLike, as_mapping, check_values
from mizzle.core.query.returning import ReturningMixin
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Condition
from mizzle.core.sql.operators import and_

if TYPE_CHECKING:
    from mizzle.core.query.select import Cte


@dataclass(frozen=True, eq=False)
class Update[T: Table](ReturningMixin[T]):
    """Awaiting returns the number of updated rows; ``.returning()`` returns the rows."""

    values_: dict[str, object] = field(default_factory=dict[str, object])
    where_: Condition | None = None
    all_rows_: bool = False
    # UPDATE ... FROM: other tables to join into the update.
    from_sources: tuple[type[Table], ...] = ()
    # A WITH clause passed with db.with_(...): emitted before the UPDATE.
    ctes: tuple[Cte, ...] = ()

    def set(self, values: RowLike | None = None, /, **fields: object) -> Self:
        """``set(bio="x")``, ``set({"bio": "x"})`` or ``set(pydantic_model)``."""
        if values is not None and fields:
            raise QueryError("pass a mapping or keyword arguments to set(), not both")
        changes = dict(as_mapping(values)) if values is not None else fields
        check_values(self.table, changes)
        return evolve(self, values_={**self.values_, **changes})

    def from_(self, *sources: type[Table]) -> Self:
        """``UPDATE ... FROM other``: join another table into the update; reference its
        columns in ``set_`` and ``where``."""
        if not sources:
            raise QueryError("from_() needs at least one table")
        return evolve(self, from_sources=(*self.from_sources, *sources))

    def where(self, condition: Condition) -> Self:
        """Add a filter. Calling ``where`` again ANDs the conditions."""
        combined = condition if self.where_ is None else and_(self.where_, condition)
        return evolve(self, where_=combined)

    def all_rows(self) -> Self:
        """Allow running without ``.where()``."""
        return evolve(self, all_rows_=True)

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> int:
        return await self.runner.run(compiled)


@dataclass(frozen=True, eq=False)
class Delete[T: Table](ReturningMixin[T]):
    """Awaiting returns the number of deleted rows; ``.returning()`` returns the rows."""

    where_: Condition | None = None
    all_rows_: bool = False
    # DELETE ... USING: other tables to join into the delete.
    using_: tuple[type[Table], ...] = ()
    # A WITH clause passed with db.with_(...): emitted before the DELETE.
    ctes: tuple[Cte, ...] = ()

    def where(self, condition: Condition) -> Self:
        """Add a filter. Calling ``where`` again ANDs the conditions."""
        combined = condition if self.where_ is None else and_(self.where_, condition)
        return evolve(self, where_=combined)

    def using(self, *sources: type[Table]) -> Self:
        """``DELETE ... USING other``: match rows against another table; reference its
        columns in ``where``."""
        if not sources:
            raise QueryError("using() needs at least one table")
        return evolve(self, using_=(*self.using_, *sources))

    def all_rows(self) -> Self:
        """Allow running without ``.where()``."""
        return evolve(self, all_rows_=True)

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> int:
        return await self.runner.run(compiled)

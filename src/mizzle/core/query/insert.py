"""INSERT builder."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, Self, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import Executable, evolve
from mizzle.core.query.returning import Returning
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Expr


class ModelLike(Protocol):
    """A Pydantic model (or anything with the same ``model_dump``)."""

    def model_dump(self, *, exclude_unset: bool = ...) -> dict[str, object]: ...


type RowLike = Mapping[str, object] | ModelLike


def as_mapping(value: RowLike) -> Mapping[str, object]:
    """A Pydantic model becomes the dict of fields the client actually set."""
    if isinstance(value, Mapping):
        return value
    return value.model_dump(exclude_unset=True)


def check_values(table: type[Table], values: Mapping[str, object]) -> None:
    """Reject unknown columns and values the columns cannot hold."""
    columns = table.__table__.columns
    if unknown := [k for k in values if k not in columns]:
        raise QueryError(f"{table.__name__} has no column(s): {', '.join(unknown)}")
    for key, value in values.items():
        columns[key].validate(value)


def prepare_row(table: type[Table], row: Mapping[str, object]) -> dict[str, object]:
    """Validate one insert row and apply Python-side defaults."""
    columns = table.__table__.columns
    if not row.keys() <= columns.keys():
        check_values(table, row)  # raises with the unknown names
    out: dict[str, object] = {}
    for attr, column in columns.items():
        if attr in row:
            value = row[attr]
            column.validate(value)
            out[attr] = value
        elif column.default_factory is not None:
            value = column.default_factory()
            column.validate(value)
            out[attr] = value
        elif column.required_on_insert:
            raise QueryError(f"{column.qualified_name} is required")
    return out


@dataclass(frozen=True)
class OnConflict:
    target: tuple[ColumnBase, ...]
    action: Literal["nothing", "update"]
    set_: dict[str, object] = field(default_factory=dict[str, object])
    where: Expr[bool] | None = None


def _targets(
    table: type[Table], target: ColumnBase | Sequence[ColumnBase] | None
) -> tuple[ColumnBase, ...]:
    if target is None:
        return ()
    columns = (target,) if isinstance(target, ColumnBase) else tuple(target)
    for column in columns:
        if column.table is not table:
            raise QueryError(
                f"conflict target {column.qualified_name} is not a column of {table.__name__}"
            )
    return columns


@dataclass(frozen=True, eq=False)
class Insert[T: Table](Executable[int]):
    """Awaiting returns the number of inserted rows; ``.returning()`` returns the rows."""

    table: type[T]
    rows: tuple[dict[str, object], ...] = ()
    conflict: OnConflict | None = None

    def values(self, rows: RowLike | Sequence[RowLike] | None = None, /, **fields: object) -> Self:
        """``values(email="a")``, ``values({"email": "a"})`` or ``values([{...}, {...}])``."""
        if rows is not None and fields:
            raise QueryError("pass rows or keyword arguments to values(), not both")
        if rows is None:
            if not fields:
                raise QueryError("values() needs at least one row; use values({}) for all defaults")
            batch: list[Mapping[str, object]] = [fields]
        elif isinstance(rows, Sequence):
            batch = [as_mapping(r) for r in rows]
            if not batch:
                raise QueryError("values() got an empty list")
        else:
            batch = [as_mapping(rows)]
        return evolve(self, rows=tuple(prepare_row(self.table, row) for row in batch))

    def on_conflict_do_nothing(
        self, target: ColumnBase | Sequence[ColumnBase] | None = None
    ) -> Self:
        return evolve(self, conflict=OnConflict(_targets(self.table, target), "nothing"))

    def on_conflict_do_update(
        self,
        *,
        target: ColumnBase | Sequence[ColumnBase],
        set_: Mapping[str, object],
        where: Expr[bool] | None = None,
    ) -> Self:
        """Upsert. Use ``excluded(Users.col)`` in ``set_`` to refer to the proposed row."""
        targets = _targets(self.table, target)
        if not targets:
            raise QueryError("on_conflict_do_update() needs a target")
        if not set_:
            raise QueryError("on_conflict_do_update() needs a non-empty set_")
        check_values(self.table, set_)
        return evolve(self, conflict=OnConflict(targets, "update", dict(set_), where))

    def returning(self) -> Returning[T]:
        return Returning(self.runner, self, self.table)

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> int:
        return await self.runner.run(compiled)

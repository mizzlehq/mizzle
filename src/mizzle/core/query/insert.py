"""INSERT builder."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, Self, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import evolve
from mizzle.core.query.returning import ReturningMixin
from mizzle.core.query.select import CompoundBase, Cte, SelectBase
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
        column = columns[key]
        if column.read_only:
            column.check_writable()
        column.validate(value)


def prepare_row(
    table: type[Table], row: Mapping[str, object], *, allow_identity: bool = False
) -> dict[str, object]:
    """Validate one insert row and apply Python-side defaults. ``allow_identity`` lets it
    set ``GENERATED ALWAYS`` identity columns (for ``overriding_system_value()``)."""
    columns = table.__table__.columns
    if not row.keys() <= columns.keys():
        check_values(table, row)  # raises with the unknown names
    out: dict[str, object] = {}
    for attr, column in columns.items():
        if attr in row:
            value = row[attr]
            if column.read_only and not (allow_identity and column.computed is None):
                if column.computed is None:  # GENERATED ALWAYS identity (bulk paths)
                    raise QueryError(
                        f"{column.qualified_name} is GENERATED ALWAYS AS IDENTITY; the "
                        "database assigns it. insert_many() and copy_into() cannot set it: "
                        f"use db.insert({table.__name__}).values(...).overriding_system_value(), "
                        "or make the column generated_by_default_as_identity()"
                    )
                column.check_writable()
            column.validate(value)
            out[attr] = value
        elif column.default_factory is not None:
            value = column.default_factory()
            column.validate(value)
            out[attr] = value
        elif column.required_on_insert:
            raise QueryError(f"{column.qualified_name} is required")
    return out


def output_width(query: SelectBase | CompoundBase) -> int:
    """How many columns a query's SELECT list produces (a whole-table slot expands)."""
    while isinstance(query, CompoundBase):
        query = query.left  # output columns are named by the first query
    projection = query.projection
    if projection is None:
        source = query.source
        return len(source.__table__.columns) if isinstance(source, type) else 0
    if isinstance(projection, dict):
        return len(projection)
    return sum(len(slot.__table__.columns) if isinstance(slot, type) else 1 for slot in projection)


@dataclass(frozen=True)
class OnConflict:
    target: tuple[ColumnBase, ...]
    action: Literal["nothing", "update"]
    set_: dict[str, object] = field(default_factory=dict[str, object])
    where: Expr[bool] | None = None
    # A named unique/exclusion constraint instead of a column list.
    constraint: str | None = None


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


def _conflict(
    table: type[Table], target: ColumnBase | Sequence[ColumnBase] | None, constraint: str | None
) -> tuple[tuple[ColumnBase, ...], str | None]:
    """Resolve the conflict target, which is either columns or a named constraint."""
    targets = _targets(table, target)
    if constraint is not None:
        if targets:
            raise QueryError("on_conflict takes a target or a constraint, not both")
        if not constraint:
            raise QueryError("on_conflict constraint name cannot be empty")
    return targets, constraint


@dataclass(frozen=True, eq=False)
class Insert[T: Table](ReturningMixin[T]):
    """Awaiting returns the number of inserted rows; ``.returning()`` returns the rows."""

    rows: tuple[dict[str, object], ...] = ()
    conflict: OnConflict | None = None
    overriding: Literal["SYSTEM", "USER"] | None = None
    # A GENERATED ALWAYS identity column the rows set: compiling fails without overriding.
    identity: ColumnBase | None = None
    # INSERT ... SELECT (see from_select): a query as the source of rows, and its target columns.
    source: SelectBase | CompoundBase | None = None
    source_columns: tuple[ColumnBase, ...] = ()
    # A WITH clause passed with db.with_(...): emitted before the INSERT.
    ctes: tuple[Cte, ...] = ()

    def values(self, rows: RowLike | Sequence[RowLike] | None = None, /, **fields: object) -> Self:
        """``values(email="a")``, ``values({"email": "a"})`` or ``values([{...}, {...}])``."""
        if self.source is not None:
            raise QueryError("this insert already has from_select(); it cannot also take values()")
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
        rows = tuple(prepare_row(self.table, row, allow_identity=True) for row in batch)
        identity = next(
            (
                column
                for attr, column in self.table.__table__.columns.items()
                if column.identity is not None
                and column.identity.always
                and any(attr in row for row in rows)
            ),
            None,
        )
        return evolve(self, rows=rows, identity=identity)

    def overriding_system_value(self) -> Self:
        """``OVERRIDING SYSTEM VALUE``: store the given values of ``GENERATED ALWAYS``
        identity columns. Without it, inserting them fails."""
        return evolve(self, overriding="SYSTEM")

    def overriding_user_value(self) -> Self:
        """``OVERRIDING USER VALUE``: ignore the given values of identity columns; the
        database assigns them."""
        return evolve(self, overriding="USER")

    def from_select(
        self,
        columns: ColumnBase | Sequence[ColumnBase],
        query: SelectBase | CompoundBase,
    ) -> Self:
        """``INSERT ... SELECT``: insert the rows a query produces. ``columns`` are the target
        columns, in the same order the query outputs them."""
        if self.rows:
            raise QueryError("this insert already has values(); it cannot also use from_select()")
        target = (columns,) if isinstance(columns, ColumnBase) else tuple(columns)
        if not target:
            raise QueryError("from_select() needs at least one target column")
        for column in target:
            if column.table is not self.table:
                raise QueryError(
                    f"target column {column.qualified_name} is not a column of "
                    f"{self.table.__name__}"
                )
        width = output_width(query)
        if width != len(target):
            raise QueryError(
                f"from_select() got {len(target)} target column(s) but the query outputs {width}"
            )
        identity = next((c for c in target if c.identity is not None and c.identity.always), None)
        return evolve(self, source=query, source_columns=target, identity=identity)

    def on_conflict_do_nothing(
        self,
        target: ColumnBase | Sequence[ColumnBase] | None = None,
        *,
        constraint: str | None = None,
    ) -> Self:
        targets, constraint = _conflict(self.table, target, constraint)
        return evolve(self, conflict=OnConflict(targets, "nothing", constraint=constraint))

    def on_conflict_do_update(
        self,
        *,
        target: ColumnBase | Sequence[ColumnBase] | None = None,
        constraint: str | None = None,
        set_: Mapping[str, object],
        where: Expr[bool] | None = None,
    ) -> Self:
        """Upsert. Use ``excluded(Users.col)`` in ``set_`` to refer to the proposed row.
        Pass ``target`` (columns) or ``constraint`` (a named unique constraint)."""
        targets, constraint = _conflict(self.table, target, constraint)
        if not targets and constraint is None:
            raise QueryError("on_conflict_do_update() needs a target or a constraint")
        if not set_:
            raise QueryError("on_conflict_do_update() needs a non-empty set_")
        check_values(self.table, set_)
        return evolve(self, conflict=OnConflict(targets, "update", dict(set_), where, constraint))

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> int:
        return await self.runner.run(compiled)

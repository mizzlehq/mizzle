"""Table-level constraints and indexes, returned from ``Table.config()``."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from mizzle.core.schema.column import ColumnBase, ReferentialAction
from mizzle.core.sql.nodes import Condition, ExprBase


@dataclass(frozen=True)
class Index:
    name: str
    columns: tuple[ExprBase, ...] = ()  # columns, or expressions such as sql("lower(email)")
    unique: bool = False
    predicate: Condition | None = None

    def on(self, *keys: ExprBase) -> Index:
        return replace(self, columns=keys)

    def where(self, condition: Condition) -> Index:
        """Partial index."""
        return replace(self, predicate=condition)


@dataclass(frozen=True)
class UniqueConstraint:
    columns: tuple[ColumnBase, ...]
    name: str | None = None


@dataclass(frozen=True)
class PrimaryKey:
    columns: tuple[ColumnBase, ...]
    name: str | None = None


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[ColumnBase, ...]
    references: Callable[[], Sequence[ColumnBase]]
    name: str | None = None
    on_delete: ReferentialAction | None = None
    on_update: ReferentialAction | None = None


@dataclass(frozen=True)
class Check:
    name: str
    condition: Condition


type Constraint = Index | UniqueConstraint | PrimaryKey | ForeignKey | Check


def index(name: str) -> Index:
    return Index(name)


def unique_index(name: str) -> Index:
    return Index(name, unique=True)


def unique(*columns: ColumnBase, name: str | None = None) -> UniqueConstraint:
    return UniqueConstraint(columns, name)


def primary_key(*columns: ColumnBase, name: str | None = None) -> PrimaryKey:
    return PrimaryKey(columns, name)


def foreign_key(
    columns: Sequence[ColumnBase],
    references: Callable[[], Sequence[ColumnBase]],
    *,
    name: str | None = None,
    on_delete: ReferentialAction | None = None,
    on_update: ReferentialAction | None = None,
) -> ForeignKey:
    return ForeignKey(tuple(columns), references, name, on_delete, on_update)


def check(name: str, condition: Condition) -> Check:
    return Check(name, condition)

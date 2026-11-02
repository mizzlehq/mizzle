"""Table-level constraints and indexes, returned from ``Table.config()``."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from mizzle.core.schema.column import Column, ReferentialAction
from mizzle.core.sql.nodes import Expr


@dataclass(frozen=True)
class Index:
    name: str
    columns: tuple[Column[Any], ...] = ()
    unique: bool = False
    predicate: Expr[bool] | None = None

    def on(self, *columns: Column[Any]) -> Index:
        return replace(self, columns=columns)

    def where(self, condition: Expr[bool]) -> Index:
        """Partial index."""
        return replace(self, predicate=condition)


@dataclass(frozen=True)
class UniqueConstraint:
    columns: tuple[Column[Any], ...]
    name: str | None = None


@dataclass(frozen=True)
class PrimaryKey:
    columns: tuple[Column[Any], ...]
    name: str | None = None


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[Column[Any], ...]
    references: Callable[[], Sequence[Column[Any]]]
    name: str | None = None
    on_delete: ReferentialAction | None = None
    on_update: ReferentialAction | None = None


@dataclass(frozen=True)
class Check:
    name: str
    condition: Expr[bool]


type Constraint = Index | UniqueConstraint | PrimaryKey | ForeignKey | Check


def index(name: str) -> Index:
    return Index(name)


def unique_index(name: str) -> Index:
    return Index(name, unique=True)


def unique(*columns: Column[Any], name: str | None = None) -> UniqueConstraint:
    return UniqueConstraint(columns, name)


def primary_key(*columns: Column[Any], name: str | None = None) -> PrimaryKey:
    return PrimaryKey(columns, name)


def foreign_key(
    columns: Sequence[Column[Any]],
    references: Callable[[], Sequence[Column[Any]]],
    *,
    name: str | None = None,
    on_delete: ReferentialAction | None = None,
    on_update: ReferentialAction | None = None,
) -> ForeignKey:
    return ForeignKey(tuple(columns), references, name, on_delete, on_update)


def check(name: str, condition: Expr[bool]) -> Check:
    return Check(name, condition)

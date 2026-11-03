"""Table-level constraints and indexes, returned from ``Table.config()``."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from mizzle.core.errors import SchemaError
from mizzle.core.schema.column import ColumnBase, ReferentialAction
from mizzle.core.sql.nodes import Condition, ExprBase

_SQL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")


def _sql_name(what: str, name: str) -> str:
    if not _SQL_NAME.fullmatch(name):
        raise SchemaError(f"invalid index {what} {name!r}")
    return name


@dataclass(frozen=True)
class Index:
    name: str
    columns: tuple[ExprBase, ...] = ()  # columns, or expressions such as sql("lower(email)")
    unique: bool = False
    predicate: Condition | None = None
    method: str | None = None  # access method; None is btree
    opclass: str | None = None  # operator class of every key
    options: tuple[tuple[str, str], ...] = ()  # storage parameters, as text

    def on(self, *keys: ExprBase) -> Index:
        return replace(self, columns=keys)

    def where(self, condition: Condition) -> Index:
        """Partial index."""
        return replace(self, predicate=condition)

    def using(self, method: str, opclass: str | None = None) -> Index:
        """An access method (``gin``, ``gist``, ``hnsw``, ``ivfflat``...) and optionally a
        non-default operator class for every key: ``.using("hnsw", "vector_cosine_ops")``."""
        if opclass is not None:
            _sql_name("operator class", opclass)
        return replace(self, method=_sql_name("method", method).lower(), opclass=opclass)

    def with_(self, **options: int | float | str | bool) -> Index:
        """Storage parameters: ``.with_(m=16, ef_construction=64)``."""

        def text(value: int | float | str | bool) -> str:
            return ("true" if value else "false") if isinstance(value, bool) else str(value)

        items = tuple((_sql_name("option", k), text(v)) for k, v in options.items())
        return replace(self, options=items)


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


type PolicyCommand = Literal["all", "select", "insert", "update", "delete"]


@dataclass(frozen=True)
class Policy:
    """A row-level security policy (``CREATE POLICY``); applies once ``rls()`` is on."""

    name: str
    command: PolicyCommand = "all"
    roles: tuple[str, ...] = ("public",)
    using: Condition | None = None
    with_check: Condition | None = None
    permissive: bool = True


@dataclass(frozen=True)
class RowLevelSecurity:
    enabled: bool = True
    force: bool = False  # also for the table owner


type Constraint = (
    Index | UniqueConstraint | PrimaryKey | ForeignKey | Check | Policy | RowLevelSecurity
)


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


def rls(*, enabled: bool = True, force: bool = False) -> RowLevelSecurity:
    """Row-level security on the table; ``force=True`` applies it to the owner too."""
    return RowLevelSecurity(enabled, force)


def policy(
    name: str,
    *,
    for_: PolicyCommand = "all",
    to: Sequence[str] = ("public",),
    using: Condition | None = None,
    with_check: Condition | None = None,
    as_: Literal["permissive", "restrictive"] = "permissive",
) -> Policy:
    """``policy("own_rows", for_="select", to=["authenticated"], using=sql("owner = ..."))``.
    The roles must exist; ``"public"`` means every role."""
    if not to:
        raise SchemaError(f"policy {name!r} needs at least one role")
    # PostgreSQL ignores the other roles when PUBLIC is one of them.
    roles = ("public",) if "public" in to else tuple(sorted(set(to)))
    return Policy(name, for_, roles, using, with_check, as_ == "permissive")

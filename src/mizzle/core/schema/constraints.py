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
    # Operator class of every key, or one per key (None: the default).
    opclass: str | tuple[str | None, ...] | None = None
    options: tuple[tuple[str, str], ...] = ()  # storage parameters, as text
    nulls_not_distinct: bool = False  # UNIQUE index: NULLs are equal (PostgreSQL 15+)

    def on(self, *keys: ExprBase) -> Index:
        return replace(self, columns=keys)

    def where(self, condition: Condition) -> Index:
        """Partial index."""
        return replace(self, predicate=condition)

    def using(self, method: str, opclass: str | Sequence[str | None] | None = None) -> Index:
        """An access method (``gin``, ``gist``, ``hnsw``, ``ivfflat``...) and optionally an
        operator class for every key: ``.using("hnsw", "vector_cosine_ops")``, or one per
        key: ``.using("btree", [None, "text_pattern_ops"])`` (None: the default)."""
        if isinstance(opclass, str):
            _sql_name("operator class", opclass)
        elif opclass is not None:
            opclass = tuple(o if o is None else _sql_name("operator class", o) for o in opclass)
        return replace(self, method=_sql_name("method", method).lower(), opclass=opclass)

    def opclasses(self, keys: int) -> list[str | None]:
        """The operator class of each of ``keys`` keys."""
        if isinstance(self.opclass, tuple):
            if len(self.opclass) != keys:
                raise SchemaError(
                    f"index {self.name!r}: {keys} keys but {len(self.opclass)} opclasses"
                )
            return list(self.opclass)
        return [self.opclass] * keys

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
    nulls_not_distinct: bool = False  # treat NULLs as equal (PostgreSQL 15+)
    deferrable: bool = False
    initially_deferred: bool = False


@dataclass(frozen=True)
class PrimaryKey:
    columns: tuple[ColumnBase, ...]
    name: str | None = None
    deferrable: bool = False
    initially_deferred: bool = False


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[ColumnBase, ...]
    references: Callable[[], Sequence[ColumnBase]]
    name: str | None = None
    on_delete: ReferentialAction | None = None
    on_update: ReferentialAction | None = None
    deferrable: bool = False
    initially_deferred: bool = False
    not_valid: bool = False  # ADD ... NOT VALID: skip checking existing rows


@dataclass(frozen=True)
class Check:
    name: str
    condition: Condition
    not_valid: bool = False  # ADD ... NOT VALID: skip checking existing rows


@dataclass(frozen=True)
class Exclusion:
    """``EXCLUDE USING method (key WITH operator, ...)``: a generalized unique constraint
    (ranges that must not overlap: ``(during, "&&")``)."""

    elements: tuple[tuple[ColumnBase | ExprBase, str], ...]
    name: str | None = None
    method: str = "gist"
    predicate: Condition | None = None  # WHERE (...): only rows that match are checked
    deferrable: bool = False
    initially_deferred: bool = False


type PolicyCommand = Literal["all", "select", "insert", "update", "delete"]


@dataclass(frozen=True)
class Role:
    """A database role. Migrations create it (if missing) and keep its options in sync;
    ``existing=True`` only references a role that something else manages (Supabase's
    ``anon``, ``authenticated``...). Roles belong to the whole server, so mizzle never
    drops one it does not manage."""

    name: str
    login: bool = False
    create_db: bool = False
    create_role: bool = False
    inherit: bool = True
    existing: bool = False


def role(
    name: str,
    *,
    login: bool = False,
    create_db: bool = False,
    create_role: bool = False,
    inherit: bool = True,
    existing: bool = False,
) -> Role:
    """``role("admin", create_role=True)``. Passwords are not managed: set them outside
    the schema (they are secrets)."""
    return Role(name, login, create_db, create_role, inherit, existing)


@dataclass(frozen=True)
class Policy:
    """A row-level security policy (``CREATE POLICY``); applies once ``rls()`` is on."""

    name: str
    command: PolicyCommand = "all"
    roles: tuple[str, ...] = ("public",)
    using: Condition | None = None
    with_check: Condition | None = None
    permissive: bool = True
    declared_roles: tuple[Role, ...] = ()  # Role objects in ``to``, created by migrations


_PRIVILEGES = (
    "select",
    "insert",
    "update",
    "delete",
    "truncate",
    "references",
    "trigger",
    "maintain",
    "all",
)
type Privilege = Literal[
    "select", "insert", "update", "delete", "truncate", "references", "trigger", "maintain", "all"
]


@dataclass(frozen=True)
class Grant:
    """``GRANT privileges ON table TO roles``, kept in sync by migrations."""

    privileges: tuple[str, ...]  # sorted; ("all",) for ALL
    roles: tuple[str, ...]  # sorted; "public" is everyone
    declared_roles: tuple[Role, ...] = ()  # Role objects in ``to``, created by migrations


@dataclass(frozen=True)
class RowLevelSecurity:
    enabled: bool = True
    force: bool = False  # also for the table owner


@dataclass(frozen=True)
class Partition:
    """One partition of a partitioned table, declared in ``config()``:
    ``CREATE TABLE name PARTITION OF parent for_values``. ``for_values`` is the bound as
    PostgreSQL prints it: ``"FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"``,
    ``"FOR VALUES IN ('a', 'b')"``, ``"FOR VALUES WITH (modulus 4, remainder 0)"`` or
    ``"DEFAULT"``."""

    name: str
    for_values: str


type Constraint = (
    Index
    | UniqueConstraint
    | PrimaryKey
    | ForeignKey
    | Check
    | Exclusion
    | Policy
    | RowLevelSecurity
    | Grant
    | Partition
)


def index(name: str, *, nulls_not_distinct: bool = False) -> Index:
    return Index(name, nulls_not_distinct=nulls_not_distinct)


def unique_index(name: str, *, nulls_not_distinct: bool = False) -> Index:
    return Index(name, unique=True, nulls_not_distinct=nulls_not_distinct)


def unique(
    *columns: ColumnBase,
    name: str | None = None,
    nulls_not_distinct: bool = False,
    deferrable: bool = False,
    initially_deferred: bool = False,
) -> UniqueConstraint:
    return UniqueConstraint(columns, name, nulls_not_distinct, deferrable, initially_deferred)


def primary_key(
    *columns: ColumnBase,
    name: str | None = None,
    deferrable: bool = False,
    initially_deferred: bool = False,
) -> PrimaryKey:
    return PrimaryKey(columns, name, deferrable, initially_deferred)


def foreign_key(
    columns: Sequence[ColumnBase],
    references: Callable[[], Sequence[ColumnBase]],
    *,
    name: str | None = None,
    on_delete: ReferentialAction | None = None,
    on_update: ReferentialAction | None = None,
    deferrable: bool = False,
    initially_deferred: bool = False,
    not_valid: bool = False,
) -> ForeignKey:
    return ForeignKey(
        tuple(columns),
        references,
        name,
        on_delete,
        on_update,
        deferrable,
        initially_deferred,
        not_valid,
    )


def check(name: str, condition: Condition, *, not_valid: bool = False) -> Check:
    return Check(name, condition, not_valid)


def exclusion(
    *elements: tuple[ColumnBase | ExprBase, str],
    name: str | None = None,
    using: str = "gist",
    where: Condition | None = None,
    deferrable: bool = False,
    initially_deferred: bool = False,
) -> Exclusion:
    """``exclusion((Bookings.during, "&&"), using="gist")``: no two rows may have
    overlapping ``during`` ranges. Each key is a column or SQL expression and the operator
    it is compared with (``=``, ``&&``, ``<@``...)."""
    if not elements:
        raise SchemaError("exclusion() needs at least one (key, operator) element")
    return Exclusion(
        tuple(elements),
        name,
        _sql_name("method", using).lower(),
        where,
        deferrable,
        initially_deferred,
    )


def rls(*, enabled: bool = True, force: bool = False) -> RowLevelSecurity:
    """Row-level security on the table; ``force=True`` applies it to the owner too."""
    return RowLevelSecurity(enabled, force)


def policy(
    name: str,
    *,
    for_: PolicyCommand = "all",
    to: Sequence[str | Role] = ("public",),
    using: Condition | None = None,
    with_check: Condition | None = None,
    as_: Literal["permissive", "restrictive"] = "permissive",
) -> Policy:
    """``policy("own_rows", for_="select", to=["authenticated"], using=sql("owner = ..."))``.
    The roles must exist; ``"public"`` means every role."""
    if not to:
        raise SchemaError(f"policy {name!r} needs at least one role")
    names = [r.name if isinstance(r, Role) else r for r in to]
    managed = tuple(r for r in to if isinstance(r, Role) and not r.existing)
    # PostgreSQL ignores the other roles when PUBLIC is one of them.
    roles = ("public",) if "public" in names else tuple(sorted(set(names)))
    return Policy(name, for_, roles, using, with_check, as_ == "permissive", managed)


def grant(privileges: Privilege | Sequence[Privilege], *, to: Sequence[str | Role]) -> Grant:
    """``grant(["select", "insert"], to=[reader])`` in a table's or view's ``config()``.
    Migrations grant what is declared and revoke what was declared before and no longer
    is; privileges the schema never declared for a role are left alone."""
    wanted = [privileges] if isinstance(privileges, str) else list(privileges)
    if not wanted or not to:
        raise SchemaError("grant() needs at least one privilege and one role")
    if bad := [p for p in wanted if p not in _PRIVILEGES]:
        raise SchemaError(f"grant(): unknown privileges {bad}")
    names = sorted({r.name if isinstance(r, Role) else r for r in to})
    managed = tuple(r for r in to if isinstance(r, Role) and not r.existing)
    privs = ("all",) if "all" in wanted else tuple(sorted(set(wanted)))
    return Grant(privs, tuple(names), managed)


def partition(name: str, *, for_values: str) -> Partition:
    """Declare a partition of the table, which must set ``partition_by=...``. ``for_values``
    is the bound in PostgreSQL's spelling (see ``Partition``); the partition is created in
    the table's schema."""
    if not for_values.strip():
        raise SchemaError(f"partition {name!r} needs for_values")
    return Partition(name, for_values.strip())

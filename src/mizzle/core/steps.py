"""Database-neutral migration steps, produced by the diff and rendered to SQL by a dialect."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, override

from mizzle.core.snapshot import (
    CheckSnap,
    ColumnSnap,
    CompositeSnap,
    DomainSnap,
    EnumSnap,
    ExclusionSnap,
    ExtensionSnap,
    ForeignKeySnap,
    FunctionSnap,
    GeneratedSnap,
    IdentitySnap,
    IndexSnap,
    PolicySnap,
    PrimaryKeySnap,
    RoleSnap,
    TableSnap,
    TriggerSnap,
    UniqueSnap,
    ViewSnap,
)

type ConstraintKind = Literal["primary_key", "unique", "check", "foreign_key", "exclusion"]

_TEXT = ("text", "character varying")
_INTS = ("smallint", "integer", "bigint")
_SIZED = re.compile(r"(character varying|numeric)(?:\((\d+)(?:,(\d+))?\))?")


def widens(old: str, new: str) -> bool:
    """Whether every value of type ``old`` fits ``new`` unchanged (types as normalized in
    snapshots: ``character varying(40)``, ``numeric(10,2)``)."""
    if old.endswith("[]") and new.endswith("[]"):
        return widens(old[:-2], new[:-2])
    if new == "text":  # every value as its exact text
        return old in (*_TEXT, *_INTS, "boolean", "uuid") or old.startswith(
            ("character varying(", "numeric")
        )
    if old in _INTS and new in _INTS:
        return _INTS.index(old) <= _INTS.index(new)
    if (old in _INTS and new == "numeric") or (old, new) == ("real", "double precision"):
        return True
    a, b = _SIZED.fullmatch(old), _SIZED.fullmatch(new)
    if a is None or b is None or a.group(1) != b.group(1):
        return False
    if b.group(2) is None:  # unbounded
        return True
    if a.group(2) is None:
        return False
    scale_a, scale_b = int(a.group(3) or 0), int(b.group(3) or 0)
    # numeric(p, s): as many digits before the point, and the same scale (no rounding)
    return scale_a == scale_b and int(b.group(2)) >= int(a.group(2))


@dataclass(frozen=True)
class Step:
    @property
    def data_loss(self) -> bool:
        """Whether the step can lose data (the CLI asks for --allow-data-loss)."""
        return False

    def describe(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class CreateSchema(Step):
    name: str

    @override
    def describe(self) -> str:
        return f"create schema {self.name}"


@dataclass(frozen=True)
class CreateEnum(Step):
    enum: EnumSnap

    @override
    def describe(self) -> str:
        return f"create enum {self.enum['name']}"


@dataclass(frozen=True)
class AddEnumValue(Step):
    schema: str
    name: str
    value: str
    after: str | None = None
    before: str | None = None

    @override
    def describe(self) -> str:
        return f"add value {self.value!r} to enum {self.name}"


@dataclass(frozen=True)
class DropEnum(Step):
    schema: str
    name: str

    @override
    def describe(self) -> str:
        return f"drop enum {self.name}"


@dataclass(frozen=True)
class CreateDomain(Step):
    domain: DomainSnap

    @override
    def describe(self) -> str:
        return f"create domain {self.domain['name']}"


@dataclass(frozen=True)
class DropDomain(Step):
    schema: str
    name: str

    @override
    def describe(self) -> str:
        return f"drop domain {self.name}"


@dataclass(frozen=True)
class CreateComposite(Step):
    composite: CompositeSnap

    @override
    def describe(self) -> str:
        return f"create type {self.composite['name']}"


@dataclass(frozen=True)
class DropComposite(Step):
    schema: str
    name: str

    @override
    def describe(self) -> str:
        return f"drop type {self.name}"


@dataclass(frozen=True)
class CreateTable(Step):
    """Columns, primary key, uniques and checks. Indexes and foreign keys are separate steps."""

    table: TableSnap

    @override
    def describe(self) -> str:
        return f"create table {self.table['name']}"


@dataclass(frozen=True)
class DropTable(Step):
    schema: str
    name: str

    @property
    @override
    def data_loss(self) -> bool:
        return True

    @override
    def describe(self) -> str:
        return f"drop table {self.name}"


@dataclass(frozen=True)
class CreatePartition(Step):
    """``CREATE TABLE name PARTITION OF parent bound``."""

    schema: str
    parent: str
    name: str
    bound: str  # as PostgreSQL prints it: "FOR VALUES FROM (...) TO (...)" or "DEFAULT"

    @override
    def describe(self) -> str:
        return f"create partition {self.name} of {self.parent}"


@dataclass(frozen=True)
class DropPartition(Step):
    schema: str
    name: str

    @property
    @override
    def data_loss(self) -> bool:
        return True

    @override
    def describe(self) -> str:
        return f"drop partition {self.name}"


@dataclass(frozen=True)
class RenameTable(Step):
    schema: str
    old: str
    new: str

    @override
    def describe(self) -> str:
        return f"rename table {self.old} to {self.new}"


@dataclass(frozen=True)
class SetTableSchema(Step):
    """Move a table to another schema (its indexes, constraints and sequences go along)."""

    schema: str
    name: str
    new_schema: str

    @override
    def describe(self) -> str:
        return f"move table {self.name} from schema {self.schema} to {self.new_schema}"


@dataclass(frozen=True)
class AddColumn(Step):
    schema: str
    table: str
    column: ColumnSnap

    @override
    def describe(self) -> str:
        return f"add column {self.table}.{self.column['name']}"


@dataclass(frozen=True)
class DropColumn(Step):
    schema: str
    table: str
    name: str

    @property
    @override
    def data_loss(self) -> bool:
        return True

    @override
    def describe(self) -> str:
        return f"drop column {self.table}.{self.name}"


@dataclass(frozen=True)
class RenameColumn(Step):
    schema: str
    table: str
    old: str
    new: str

    @override
    def describe(self) -> str:
        return f"rename column {self.table}.{self.old} to {self.new}"


@dataclass(frozen=True)
class AlterColumnType(Step):
    schema: str
    table: str
    column: str
    old_type: str
    new_type: str
    generated: bool = False  # a generated column: recomputed, so no USING cast

    @property
    @override
    def data_loss(self) -> bool:
        """Casts can fail, round or truncate; widening (varchar(40) -> text, integer ->
        bigint...) keeps every value."""
        return not widens(self.old_type, self.new_type)

    @override
    def describe(self) -> str:
        return f"change type of {self.table}.{self.column} from {self.old_type} to {self.new_type}"


@dataclass(frozen=True)
class AlterColumnCollation(Step):
    """Change (or drop) a column's ``COLLATE``. ``sql_type`` is part of the ALTER: PostgreSQL
    spells a collation change as ``ALTER COLUMN c TYPE <type> COLLATE <name>``."""

    schema: str
    table: str
    column: str
    sql_type: str
    collation: str | None  # None: the collation the type uses by default

    @override
    def describe(self) -> str:
        return f"change collation of {self.table}.{self.column} to {self.collation or 'default'}"


@dataclass(frozen=True)
class SetTableComment(Step):
    """``COMMENT ON TABLE``; ``None`` removes the comment."""

    schema: str
    table: str
    comment: str | None

    @override
    def describe(self) -> str:
        what = "comment" if self.comment is not None else "remove comment"
        return f"{what} on table {self.table}"


@dataclass(frozen=True)
class SetColumnComment(Step):
    """``COMMENT ON COLUMN``; ``None`` removes the comment."""

    schema: str
    table: str
    column: str
    comment: str | None

    @override
    def describe(self) -> str:
        what = "comment" if self.comment is not None else "remove comment"
        return f"{what} on {self.table}.{self.column}"


@dataclass(frozen=True)
class SetTableLogged(Step):
    """``ALTER TABLE ... SET LOGGED`` / ``SET UNLOGGED``."""

    schema: str
    table: str
    unlogged: bool

    @override
    def describe(self) -> str:
        return f"set {self.table} {'unlogged' if self.unlogged else 'logged'}"


@dataclass(frozen=True)
class AlterTableStorage(Step):
    """Change a table's storage parameters: ``SET (...)`` for the new or changed ones and
    ``RESET (...)`` for the removed ones."""

    schema: str
    table: str
    set_options: dict[str, str]
    reset_options: tuple[str, ...] = ()

    @override
    def describe(self) -> str:
        parts: list[str] = []
        if self.set_options:
            parts.append("set " + ", ".join(f"{k}={v}" for k, v in self.set_options.items()))
        if self.reset_options:
            parts.append("reset " + ", ".join(self.reset_options))
        return f"storage of {self.table}: {'; '.join(parts)}"


@dataclass(frozen=True)
class AlterColumnNullability(Step):
    schema: str
    table: str
    column: str
    not_null: bool

    @override
    def describe(self) -> str:
        return f"{'set' if self.not_null else 'drop'} not null on {self.table}.{self.column}"


@dataclass(frozen=True)
class AlterColumnDefault(Step):
    schema: str
    table: str
    column: str
    default: str | None

    @override
    def describe(self) -> str:
        return f"change default of {self.table}.{self.column}"


@dataclass(frozen=True)
class AlterColumnIdentity(Step):
    """Add, drop or change ``GENERATED ... AS IDENTITY`` (``None``: not an identity)."""

    schema: str
    table: str
    column: str
    old: IdentitySnap | None
    new: IdentitySnap | None
    sql_type: str = "integer"  # the column's (for the sequence's default bounds)

    @override
    def describe(self) -> str:
        what = "add" if self.old is None else "drop" if self.new is None else "change"
        return f"{what} identity on {self.table}.{self.column}"


@dataclass(frozen=True)
class AlterColumnGenerated(Step):
    """Change or drop the expression of a generated column (``None``: a plain column)."""

    schema: str
    table: str
    column: str
    old: GeneratedSnap | None
    new: GeneratedSnap | None
    # The whole new column: servers without SET EXPRESSION (before PostgreSQL 17) add it again.
    definition: ColumnSnap | None = None

    @override
    def describe(self) -> str:
        what = "add" if self.old is None else "drop" if self.new is None else "change"
        return f"{what} generated expression on {self.table}.{self.column}"


@dataclass(frozen=True)
class AddPrimaryKey(Step):
    schema: str
    table: str
    primary_key: PrimaryKeySnap

    @override
    def describe(self) -> str:
        return f"add primary key on {self.table}"


@dataclass(frozen=True)
class AddUnique(Step):
    schema: str
    table: str
    unique: UniqueSnap

    @override
    def describe(self) -> str:
        return f"add unique {self.unique['name']}"


@dataclass(frozen=True)
class AddCheck(Step):
    schema: str
    table: str
    check: CheckSnap

    @override
    def describe(self) -> str:
        return f"add check {self.check['name']}"


@dataclass(frozen=True)
class AddExclusion(Step):
    schema: str
    table: str
    exclusion: ExclusionSnap

    @override
    def describe(self) -> str:
        return f"add exclusion {self.exclusion['name']}"


@dataclass(frozen=True)
class AddForeignKey(Step):
    schema: str
    table: str
    foreign_key: ForeignKeySnap

    @override
    def describe(self) -> str:
        return f"add foreign key {self.foreign_key['name']}"


@dataclass(frozen=True)
class DropConstraint(Step):
    schema: str
    table: str
    name: str
    kind: ConstraintKind

    @override
    def describe(self) -> str:
        return f"drop {self.kind.replace('_', ' ')} {self.name}"


@dataclass(frozen=True)
class RenameConstraint(Step):
    schema: str
    table: str
    old: str
    new: str

    @override
    def describe(self) -> str:
        return f"rename constraint {self.old} to {self.new}"


@dataclass(frozen=True)
class CreateIndex(Step):
    schema: str
    table: str
    index: IndexSnap

    @override
    def describe(self) -> str:
        return f"create index {self.index['name']}"


@dataclass(frozen=True)
class DropIndex(Step):
    schema: str
    name: str

    @override
    def describe(self) -> str:
        return f"drop index {self.name}"


@dataclass(frozen=True)
class RenameIndex(Step):
    schema: str
    old: str
    new: str

    @override
    def describe(self) -> str:
        return f"rename index {self.old} to {self.new}"


@dataclass(frozen=True)
class CreateRole(Step):
    role: RoleSnap

    @override
    def describe(self) -> str:
        return f"create role {self.role['name']}"


@dataclass(frozen=True)
class AlterRole(Step):
    role: RoleSnap

    @override
    def describe(self) -> str:
        return f"change options of role {self.role['name']}"


@dataclass(frozen=True)
class DropRole(Step):
    name: str

    @override
    def describe(self) -> str:
        return f"drop role {self.name}"


@dataclass(frozen=True)
class CreateExtension(Step):
    extension: ExtensionSnap

    @override
    def describe(self) -> str:
        return f"create extension {self.extension['name']}"


@dataclass(frozen=True)
class AlterExtension(Step):
    name: str
    schema: str | None = None  # move to this schema
    version: str | None = None  # update to this version

    @override
    def describe(self) -> str:
        what = f"schema {self.schema}" if self.schema is not None else f"version {self.version}"
        return f"change extension {self.name} {what}"


@dataclass(frozen=True)
class DropExtension(Step):
    name: str

    @property
    @override
    def data_loss(self) -> bool:
        return True

    @override
    def describe(self) -> str:
        return f"drop extension {self.name}"


@dataclass(frozen=True)
class CreateFunction(Step):
    """``CREATE OR REPLACE FUNCTION``."""

    function: FunctionSnap

    @override
    def describe(self) -> str:
        return f"create function {self.function['name']}"


@dataclass(frozen=True)
class DropFunction(Step):
    schema: str
    name: str
    args: str

    @override
    def describe(self) -> str:
        return f"drop function {self.name}"


@dataclass(frozen=True)
class CreateView(Step):
    view: ViewSnap

    @override
    def describe(self) -> str:
        kind = "materialized view" if self.view["materialized"] else "view"
        return f"create {kind} {self.view['name']}"


@dataclass(frozen=True)
class DropView(Step):
    schema: str
    name: str
    materialized: bool

    @override
    def describe(self) -> str:
        return f"drop {'materialized view' if self.materialized else 'view'} {self.name}"


@dataclass(frozen=True)
class SaveGrants(Step):
    """Remember the privileges, owner and comment of a view about to be dropped and
    created again (they go with the view), for ``RestoreGrants``."""

    schema: str
    name: str

    @override
    def describe(self) -> str:
        return f"keep privileges of {self.name}"


@dataclass(frozen=True)
class RestoreGrants(Step):
    schema: str
    name: str
    materialized: bool

    @override
    def describe(self) -> str:
        return f"restore privileges of {self.name}"


@dataclass(frozen=True)
class GrantPrivileges(Step):
    schema: str
    table: str  # a table or a view
    role: str  # "public": everyone
    privileges: tuple[str, ...]
    revoke: bool = False

    @override
    def describe(self) -> str:
        what, to = ("revoke", "from") if self.revoke else ("grant", "to")
        return f"{what} {', '.join(self.privileges)} on {self.table} {to} {self.role}"


@dataclass(frozen=True)
class SetRowLevelSecurity(Step):
    schema: str
    table: str
    enabled: bool
    force: bool

    @override
    def describe(self) -> str:
        return f"{'enable' if self.enabled else 'disable'} row level security on {self.table}"


@dataclass(frozen=True)
class CreatePolicy(Step):
    schema: str
    table: str
    policy: PolicySnap

    @override
    def describe(self) -> str:
        return f"create policy {self.policy['name']} on {self.table}"


@dataclass(frozen=True)
class AlterPolicy(Step):
    """Change the roles and expressions of a policy (its command and kind stay)."""

    schema: str
    table: str
    policy: PolicySnap

    @override
    def describe(self) -> str:
        return f"change policy {self.policy['name']} on {self.table}"


@dataclass(frozen=True)
class DropPolicy(Step):
    schema: str
    table: str
    name: str

    @override
    def describe(self) -> str:
        return f"drop policy {self.name} on {self.table}"


@dataclass(frozen=True)
class CreateTrigger(Step):
    schema: str
    table: str
    trigger: TriggerSnap
    replace: bool = False  # CREATE OR REPLACE (PostgreSQL 14+)

    @override
    def describe(self) -> str:
        what = "replace" if self.replace else "create"
        return f"{what} trigger {self.trigger['name']} on {self.table}"


@dataclass(frozen=True)
class DropTrigger(Step):
    schema: str
    table: str
    name: str

    @override
    def describe(self) -> str:
        return f"drop trigger {self.name} on {self.table}"


@dataclass(frozen=True)
class Statement:
    """One SQL statement of a migration. ``transactional=False`` statements must run alone
    (outside a transaction block), e.g. ``ALTER TYPE ... ADD VALUE``."""

    sql: str
    transactional: bool = True

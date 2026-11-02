"""Database-neutral migration steps, produced by the diff and rendered to SQL by a dialect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal, override

from mizzle.core.snapshot import (
    CheckSnap,
    ColumnSnap,
    EnumSnap,
    ForeignKeySnap,
    IndexSnap,
    PrimaryKeySnap,
    TableSnap,
    UniqueSnap,
)

type ConstraintKind = Literal["primary_key", "unique", "check", "foreign_key"]


@dataclass(frozen=True)
class Step:
    data_loss: ClassVar[bool] = False

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
class CreateTable(Step):
    """Columns, primary key, uniques and checks. Indexes and foreign keys are separate steps."""

    table: TableSnap

    @override
    def describe(self) -> str:
        return f"create table {self.table['name']}"


@dataclass(frozen=True)
class DropTable(Step):
    data_loss: ClassVar[bool] = True
    schema: str
    name: str

    @override
    def describe(self) -> str:
        return f"drop table {self.name}"


@dataclass(frozen=True)
class RenameTable(Step):
    schema: str
    old: str
    new: str

    @override
    def describe(self) -> str:
        return f"rename table {self.old} to {self.new}"


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
    data_loss: ClassVar[bool] = True
    schema: str
    table: str
    name: str

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
    data_loss: ClassVar[bool] = True  # casts can fail, round or truncate
    schema: str
    table: str
    column: str
    old_type: str
    new_type: str

    @override
    def describe(self) -> str:
        return f"change type of {self.table}.{self.column} from {self.old_type} to {self.new_type}"


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
class Statement:
    """One SQL statement of a migration. ``transactional=False`` statements must run alone
    (outside a transaction block), e.g. ``ALTER TYPE ... ADD VALUE``."""

    sql: str
    transactional: bool = True

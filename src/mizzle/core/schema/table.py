"""Table base class. Subclass it to declare a table; instances of the subclass are rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Self, override

from mizzle.core.errors import SchemaError
from mizzle.core.naming import Casing, snake_case
from mizzle.core.schema.column import MISSING, ColumnBase
from mizzle.core.schema.constraints import (
    Check,
    Constraint,
    Exclusion,
    ForeignKey,
    Grant,
    Index,
    Partition,
    Policy,
    PrimaryKey,
    RowLevelSecurity,
    UniqueConstraint,
)
from mizzle.core.schema.relations import Relation


@dataclass(frozen=True)
class TableInfo:
    name: str
    schema: str | None
    columns: dict[str, ColumnBase]  # attribute name -> column, in declaration order
    relations: dict[str, Relation] = field(default_factory=dict[str, Relation])
    comment: str | None = None
    unlogged: bool = False
    storage: dict[str, str] | None = None  # WITH (fillfactor=70, ...)
    partition_by: str | None = None  # PARTITION BY key, as PostgreSQL prints it


def _storage_text(value: str | int | bool) -> str:
    """A storage option value as PostgreSQL prints it back: booleans lower-case, the rest
    verbatim (PostgreSQL keeps ``off`` as ``off``, not ``false``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Table:
    __table__: ClassVar[TableInfo]

    def __init_subclass__(
        cls,
        *,
        name: str | None = None,
        schema: str | None = None,
        comment: str | None = None,
        unlogged: bool = False,
        storage: Mapping[str, str | int | bool] | None = None,
        partition_by: str | None = None,
        abstract: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if abstract:  # a base such as View: not a table itself
            return
        if parents := [
            b.__name__ for b in cls.__bases__ if issubclass(b, Table) and "__table__" in vars(b)
        ]:
            raise SchemaError(
                f"table class {cls.__name__} inherits from table {parents[0]}; tables cannot "
                "inherit from other tables (mark them @final)"
            )
        members: dict[str, object] = dict(vars(cls))
        columns = {attr: v for attr, v in members.items() if isinstance(v, ColumnBase)}
        if not columns:
            raise SchemaError(f"table class {cls.__name__} declares no columns")
        relations = {attr: v for attr, v in members.items() if isinstance(v, Relation)}
        options = (
            {k.lower(): _storage_text(v) for k, v in storage.items()}
            if storage is not None
            else None
        )
        cls.__table__ = TableInfo(
            name or snake_case(cls.__name__),
            schema,
            columns,
            relations,
            comment,
            unlogged,
            options,
            partition_by,
        )

    @classmethod
    def config(cls) -> Sequence[Constraint]:
        """Override to return table-level indexes and constraints."""
        return ()

    @classmethod
    def from_db(cls, values: dict[str, object]) -> Self:
        """Build a row from decoded column values (attribute name -> value)."""
        row = cls.__new__(cls)
        row.__dict__.update(values)
        return row

    @override
    def __repr__(self) -> str:
        values: dict[str, object] = self.__dict__
        fields = ", ".join(f"{k}={v!r}" for k, v in values.items())
        return f"{type(self).__name__}({fields})"

    @override
    def __eq__(self, other: object) -> bool:  # also makes rows unhashable (__hash__ = None)
        return type(self) is type(other) and self.__dict__ == other.__dict__


def _constraint_columns(constraint: Constraint) -> tuple[ColumnBase, ...]:
    match constraint:
        case Index():  # keys may also be expressions; only the columns are checked
            return tuple(k for k in constraint.columns if isinstance(k, ColumnBase))
        case UniqueConstraint() | PrimaryKey() | ForeignKey():
            return constraint.columns
        case Exclusion():
            return tuple(k for k, _ in constraint.elements if isinstance(k, ColumnBase))
        case Check() | Policy() | RowLevelSecurity() | Grant() | Partition():
            return ()


def _check_generated(column: ColumnBase) -> None:
    if column.identity is None and column.computed is None:
        return
    what = "an identity column" if column.identity is not None else "a generated column"
    if column.identity is not None and column.computed is not None:
        raise SchemaError(f"{column.qualified_name} cannot be both identity and generated")
    if column.identity is not None and column.sql_type not in ("integer", "bigint", "smallint"):
        raise SchemaError(f"{column.qualified_name}: identity needs integer, bigint or smallint")
    if column.default_value is not MISSING or column.default_factory is not None:
        raise SchemaError(f"{column.qualified_name} is {what}; it cannot have a default")
    if column.read_only and column.on_update_value is not None:
        raise SchemaError(f"{column.qualified_name} is {what}; it cannot have on_update")


def validate_tables(tables: Sequence[type[Table]], casing: Casing | None = None) -> None:
    """Check a schema for mistakes that would otherwise surface as confusing SQL errors."""
    registered = set(tables)
    seen: dict[tuple[str | None, str], type[Table]] = {}
    for table in tables:
        info = table.__table__
        key = (info.schema, info.name)
        if key in seen:
            raise SchemaError(
                f"{seen[key].__name__} and {table.__name__} both use table name {info.name!r}"
            )
        seen[key] = table

        names: dict[str, str] = {}
        for attr, column in info.columns.items():
            db_name = column.db_name(casing)
            if db_name in names:
                raise SchemaError(
                    f"{table.__name__}: columns {names[db_name]!r} and {attr!r} "
                    f"both map to {db_name!r}"
                )
            names[db_name] = attr

        constraints = table.config()
        pk_columns = [c for c in info.columns.values() if c.is_primary_key]
        has_table_pk = any(isinstance(c, PrimaryKey) for c in constraints)
        if len(pk_columns) > 1:
            raise SchemaError(
                f"{table.__name__}: several columns call .primary_key(); "
                "use primary_key(...) in config() for a composite key"
            )
        if pk_columns and has_table_pk:
            raise SchemaError(f"{table.__name__}: primary key declared twice")

        for column in info.columns.values():
            _check_generated(column)
            if column.reference is None:
                continue
            target = column.reference.target()
            if target.table is None:
                raise SchemaError(f"{column.qualified_name} must reference a table column")
            if target.table not in registered:
                raise SchemaError(
                    f"{column.qualified_name} references {target.qualified_name}, "
                    f"but {target.table.__name__} is not in the schema"
                )

        for relation in info.relations.values():
            link = relation.link()
            if link.target not in registered:
                raise SchemaError(
                    f"{relation.qualified_name} points to {link.target.__name__}, "
                    "which is not in the schema"
                )

        for constraint in constraints:
            for column in _constraint_columns(constraint):
                if column.table is not table:
                    raise SchemaError(
                        f"{table.__name__}.config() uses {column.qualified_name} from another table"
                    )

        if info.partition_by is None and any(isinstance(c, Partition) for c in constraints):
            raise SchemaError(
                f"{table.__name__}: partition(...) needs partition_by=... on the table"
            )
        if info.partition_by is not None and not info.partition_by.strip():
            raise SchemaError(f"{table.__name__}: partition_by cannot be empty")
        if info.partition_by is not None and info.unlogged:
            raise SchemaError(f"{table.__name__}: a partitioned table cannot be unlogged")

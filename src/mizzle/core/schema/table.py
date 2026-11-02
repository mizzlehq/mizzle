"""Table base class. Subclass it to declare a table; instances of the subclass are rows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from mizzle.core.errors import SchemaError
from mizzle.core.naming import Casing, snake_case
from mizzle.core.schema.column import Column
from mizzle.core.schema.constraints import (
    Check,
    Constraint,
    ForeignKey,
    Index,
    PrimaryKey,
    UniqueConstraint,
)


@dataclass(frozen=True)
class TableInfo:
    name: str
    schema: str | None
    columns: dict[str, Column[Any]]  # attribute name -> column, in declaration order


class Table:
    __table__: ClassVar[TableInfo]

    def __init_subclass__(
        cls, *, name: str | None = None, schema: str | None = None, **kwargs: Any
    ) -> None:
        super().__init_subclass__(**kwargs)
        columns: dict[str, Column[Any]] = {
            attr: value for attr, value in vars(cls).items() if isinstance(value, Column)
        }
        if not columns:
            raise SchemaError(f"table class {cls.__name__} declares no columns")
        cls.__table__ = TableInfo(name or snake_case(cls.__name__), schema, columns)

    @classmethod
    def config(cls) -> Sequence[Constraint]:
        """Override to return table-level indexes and constraints."""
        return ()

    @classmethod
    def from_db(cls, values: dict[str, Any]) -> Self:
        """Build a row from decoded column values (attribute name -> value)."""
        row = cls.__new__(cls)
        row.__dict__.update(values)
        return row

    def __repr__(self) -> str:
        fields = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"{type(self).__name__}({fields})"

    def __eq__(self, other: object) -> bool:  # also makes rows unhashable (__hash__ = None)
        return type(self) is type(other) and self.__dict__ == other.__dict__


def _constraint_columns(constraint: Constraint) -> tuple[Column[Any], ...]:
    match constraint:
        case Index() | UniqueConstraint() | PrimaryKey() | ForeignKey():
            return constraint.columns
        case Check():
            return ()


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

        for constraint in constraints:
            for column in _constraint_columns(constraint):
                if column.table is not table:
                    raise SchemaError(
                        f"{table.__name__}.config() uses {column.qualified_name} from another table"
                    )

"""Build a Snapshot from Python table classes."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from mizzle.core.errors import SchemaError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import Column
from mizzle.core.schema.constraints import Check, ForeignKey, Index, PrimaryKey, UniqueConstraint
from mizzle.core.schema.table import Table, validate_tables
from mizzle.core.snapshot import (
    CheckSnap,
    ColumnSnap,
    EnumSnap,
    ForeignKeySnap,
    IndexSnap,
    PrimaryKeySnap,
    Snapshot,
    TableSnap,
    UniqueSnap,
    empty_snapshot,
    key,
)
from mizzle.dialects.base import Dialect


def build_snapshot(
    tables: Sequence[type[Table]],
    dialect: Dialect,
    casing: Casing | None = None,
    *,
    prev_id: str | None = None,
) -> Snapshot:
    validate_tables(tables, casing)
    snapshot = empty_snapshot(dialect.name, str(uuid.uuid4()))
    snapshot["prev_id"] = prev_id
    default_schema = dialect.default_schema

    def schema_of(table: type[Table]) -> str:
        return table.__table__.schema or default_schema

    def names(columns: Sequence[Column[Any]]) -> list[str]:
        return [c.db_name(casing) for c in columns]

    for table in tables:
        info = table.__table__
        schema, tname = schema_of(table), info.name
        trunc = dialect.truncate_name
        snap = TableSnap(
            schema=schema,
            name=tname,
            columns={},
            primary_key=None,
            uniques={},
            checks={},
            foreign_keys={},
            indexes={},
        )
        for column in info.columns.values():
            cname = column.db_name(casing)
            if column.enum is not None:
                _add_enum(snapshot, default_schema, column)
            snap["columns"][cname] = ColumnSnap(
                name=cname,
                type=dialect.column_type(column),
                not_null=not column.nullable,
                default=dialect.column_default(column, casing),
            )
            if column.is_primary_key:
                snap["primary_key"] = PrimaryKeySnap(name=trunc(f"{tname}_pkey"), columns=[cname])
            if column.is_unique:
                name = column.unique_name or trunc(f"{tname}_{cname}_key")
                snap["uniques"][name] = UniqueSnap(name=name, columns=[cname])
            for i, condition in enumerate(column.checks):
                name = trunc(f"{tname}_{cname}_check{i or ''}")
                expression = dialect.expression(condition, casing)
                snap["checks"][name] = CheckSnap(name=name, expression=expression)
            if column.reference is not None:
                target = column.reference.target()
                assert target.table is not None  # checked by validate_tables
                name = trunc(f"{tname}_{cname}_fkey")
                snap["foreign_keys"][name] = ForeignKeySnap(
                    name=name,
                    columns=[cname],
                    ref_schema=schema_of(target.table),
                    ref_table=target.table.__table__.name,
                    ref_columns=[target.db_name(casing)],
                    on_delete=column.reference.on_delete,
                    on_update=column.reference.on_update,
                )

        for constraint in table.config():
            match constraint:
                case Index():
                    where = constraint.predicate
                    snap["indexes"][constraint.name] = IndexSnap(
                        name=constraint.name,
                        columns=names(constraint.columns),
                        unique=constraint.unique,
                        where=dialect.expression(where, casing) if where is not None else None,
                    )
                case UniqueConstraint():
                    cols = names(constraint.columns)
                    name = constraint.name or trunc(f"{tname}_{'_'.join(cols)}_key")
                    snap["uniques"][name] = UniqueSnap(name=name, columns=cols)
                case PrimaryKey():
                    name = constraint.name or trunc(f"{tname}_pkey")
                    snap["primary_key"] = PrimaryKeySnap(
                        name=name, columns=names(constraint.columns)
                    )
                case ForeignKey():
                    cols = names(constraint.columns)
                    refs = list(constraint.references())
                    ref_table = refs[0].table
                    if ref_table is None or any(r.table is not ref_table for r in refs):
                        raise SchemaError(
                            f"{table.__name__}: a foreign key must reference one table"
                        )
                    name = constraint.name or trunc(f"{tname}_{'_'.join(cols)}_fkey")
                    snap["foreign_keys"][name] = ForeignKeySnap(
                        name=name,
                        columns=cols,
                        ref_schema=schema_of(ref_table),
                        ref_table=ref_table.__table__.name,
                        ref_columns=names(refs),
                        on_delete=constraint.on_delete,
                        on_update=constraint.on_update,
                    )
                case Check():
                    expression = dialect.expression(constraint.condition, casing)
                    snap["checks"][constraint.name] = CheckSnap(
                        name=constraint.name, expression=expression
                    )

        if schema != default_schema and schema not in snapshot["schemas"]:
            snapshot["schemas"].append(schema)
        snapshot["tables"][key(schema, tname)] = snap
    return snapshot


def _add_enum(snapshot: Snapshot, schema: str, column: Column[Any]) -> None:
    assert column.enum is not None
    name = column.sql_type.removesuffix("[]")
    values = [str(member.value) for member in column.enum]
    existing = snapshot["enums"].get(key(schema, name))
    if existing is not None and existing["values"] != values:
        raise SchemaError(f"enum type {name!r} is declared twice with different values")
    snapshot["enums"][key(schema, name)] = EnumSnap(schema=schema, name=name, values=values)

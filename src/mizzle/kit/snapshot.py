"""Build a Snapshot from Python table classes."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import cast

from mizzle.core.errors import SchemaError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import ColumnBase, ReferentialAction
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

    def names(columns: Sequence[ColumnBase]) -> list[str]:
        return [c.db_name(casing) for c in columns]

    for table in tables:
        info = table.__table__
        schema, tname = schema_of(table), info.name

        def trunc(name: str) -> str:
            # Shorten the middle, keeping the "_key" / "_fkey" / ... suffix: cutting it off
            # can give a unique and a foreign key on the same columns the same name.
            over = len(name) - len(dialect.truncate_name(name))
            if over == 0:
                return name
            base, _, suffix = name.rpartition("_")
            return dialect.truncate_name(f"{base[:-over]}_{suffix}")

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
                    on_delete=_action(column.reference.on_delete),
                    on_update=_action(column.reference.on_update),
                )

        for constraint in table.config():
            match constraint:
                case Index():
                    where = constraint.predicate
                    keys = constraint.columns
                    plain = [k for k in keys if isinstance(k, ColumnBase)]
                    index = IndexSnap(
                        name=constraint.name,
                        columns=names(plain) if len(plain) == len(keys) else [],
                        unique=constraint.unique,
                        where=dialect.expression(where, casing) if where is not None else None,
                    )
                    if len(plain) != len(keys):  # an expression index
                        index["expression"] = ", ".join(dialect.expression(k, casing) for k in keys)
                    snap["indexes"][constraint.name] = index
                case UniqueConstraint():
                    cols = names(constraint.columns)
                    name = constraint.name or trunc(f"{tname}_{'_'.join(cols)}_key")
                    snap["uniques"][name] = UniqueSnap(name=name, columns=cols)
                case PrimaryKey():
                    name = constraint.name or trunc(f"{tname}_pkey")
                    snap["primary_key"] = PrimaryKeySnap(
                        name=name, columns=names(constraint.columns)
                    )
                    for cname in names(constraint.columns):  # the database enforces this
                        snap["columns"][cname]["not_null"] = True
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
                        on_delete=_action(constraint.on_delete),
                        on_update=_action(constraint.on_update),
                    )
                case Check():
                    expression = dialect.expression(constraint.condition, casing)
                    snap["checks"][constraint.name] = CheckSnap(
                        name=constraint.name, expression=expression
                    )

        if pk := snap["primary_key"]:  # CREATE TABLE drops a UNIQUE that repeats the key
            snap["uniques"] = {
                n: u for n, u in snap["uniques"].items() if u["columns"] != pk["columns"]
            }
        if schema != default_schema and schema not in snapshot["schemas"]:
            snapshot["schemas"].append(schema)
        snapshot["tables"][key(schema, tname)] = snap
    return snapshot


def _action(action: ReferentialAction | None) -> ReferentialAction | None:
    # "no action" is the database default; introspection reports it as None.
    return None if action == "no action" else action


def _add_enum(snapshot: Snapshot, schema: str, column: ColumnBase) -> None:
    assert column.enum is not None
    name = column.sql_type.removesuffix("[]")
    values = [str(cast(object, member.value)) for member in column.enum]
    existing = snapshot["enums"].get(key(schema, name))
    if existing is not None and existing["values"] != values:
        raise SchemaError(f"enum type {name!r} is declared twice with different values")
    snapshot["enums"][key(schema, name)] = EnumSnap(schema=schema, name=name, values=values)

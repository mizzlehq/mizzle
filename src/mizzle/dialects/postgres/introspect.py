"""Read a live PostgreSQL schema into a Snapshot (used by push, pull and studio)."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

from mizzle.core.snapshot import (
    ColumnSnap,
    EnumSnap,
    Snapshot,
    TableSnap,
    empty_snapshot,
    key,
)
from mizzle.dialects.base import Connection
from mizzle.dialects.postgres.ddl import DEFAULT_SCHEMA, SERIAL_TYPES, normalize_default

log = logging.getLogger("mizzle")

MIGRATIONS_TABLE = "__mizzle_migrations"

_ACTIONS = {"a": None, "r": "restrict", "c": "cascade", "n": "set null", "d": "set default"}
_NEXTVAL = re.compile(r"nextval\('.*'::regclass\)")

_TABLES = """
SELECT c.oid, n.nspname AS schema, c.relname AS name
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY($1::text[]) AND c.relname <> $2
  AND NOT c.relispartition
ORDER BY n.nspname, c.relname
"""

_COLUMNS = """
SELECT a.attrelid AS table_oid, a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS default
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = ANY($1::oid[]) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""

_CONSTRAINTS = """
SELECT con.conrelid AS table_oid, con.conname AS name, con.contype::text AS kind,
       ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, ord)
             JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n
             ORDER BY k.ord) AS columns,
       fn.nspname AS ref_schema, fc.relname AS ref_table,
       ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, ord)
             JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n
             ORDER BY k.ord) AS ref_columns,
       con.confdeltype::text AS on_delete, con.confupdtype::text AS on_update,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
LEFT JOIN pg_class fc ON fc.oid = con.confrelid
LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
WHERE con.conrelid = ANY($1::oid[]) AND con.contype IN ('p', 'u', 'f', 'c')
ORDER BY con.conrelid, con.conname
"""

_INDEXES = """
SELECT i.indrelid AS table_oid, ic.relname AS name, i.indisunique AS unique,
       ARRAY(SELECT a.attname FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(n, ord)
             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n
             ORDER BY k.ord) AS columns,
       pg_get_expr(i.indpred, i.indrelid) AS predicate,
       i.indexprs IS NOT NULL AS has_expressions
FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
WHERE i.indrelid = ANY($1::oid[])
  AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = i.indexrelid
                  AND con.contype IN ('p', 'u', 'x'))
ORDER BY i.indrelid, ic.relname
"""

_ENUMS = """
SELECT n.nspname AS schema, t.typname AS name,
       array_agg(e.enumlabel ORDER BY e.enumsortorder) AS values
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = ANY($1::text[])
GROUP BY n.nspname, t.typname
ORDER BY n.nspname, t.typname
"""


def _column(row: dict[str, Any]) -> ColumnSnap:
    sql_type: str = row["type"]
    default = row["default"]
    serial = next((s for s, base in SERIAL_TYPES.items() if base == sql_type), None)
    if serial is not None and default is not None and _NEXTVAL.fullmatch(default):
        return ColumnSnap(name=row["name"], type=serial, not_null=row["not_null"], default=None)
    return ColumnSnap(
        name=row["name"],
        type=sql_type,
        not_null=row["not_null"],
        default=normalize_default(default),
    )


def _check_expression(definition: str) -> str:
    # pg_get_constraintdef: "CHECK ((title <> ''::text))" -> "(title <> ''::text)"
    expr = definition.removeprefix("CHECK ").removesuffix(" NOT VALID")
    return expr[1:-1] if expr.startswith("(") and expr.endswith(")") else expr


async def introspect(conn: Connection, schemas: Sequence[str] = (DEFAULT_SCHEMA,)) -> Snapshot:
    snapshot = empty_snapshot("postgresql")
    schema_list = list(schemas)
    tables = await conn.fetch_dicts(_TABLES, [schema_list, MIGRATIONS_TABLE])
    by_oid: dict[int, TableSnap] = {}
    for t in tables:
        snap = TableSnap(
            schema=t["schema"],
            name=t["name"],
            columns={},
            primary_key=None,
            uniques={},
            checks={},
            foreign_keys={},
            indexes={},
        )
        by_oid[t["oid"]] = snap
        snapshot["tables"][key(t["schema"], t["name"])] = snap
        if t["schema"] != DEFAULT_SCHEMA and t["schema"] not in snapshot["schemas"]:
            snapshot["schemas"].append(t["schema"])

    oids = list(by_oid)
    for row in await conn.fetch_dicts(_COLUMNS, [oids]):
        column = _column(row)
        by_oid[row["table_oid"]]["columns"][column["name"]] = column

    for row in await conn.fetch_dicts(_CONSTRAINTS, [oids]):
        table = by_oid[row["table_oid"]]
        name, columns = row["name"], list(row["columns"])
        match row["kind"]:
            case "p":
                table["primary_key"] = {"name": name, "columns": columns}
            case "u":
                table["uniques"][name] = {"name": name, "columns": columns}
            case "c":
                expression = _check_expression(row["definition"])
                table["checks"][name] = {"name": name, "expression": expression}
            case "f":
                table["foreign_keys"][name] = {
                    "name": name,
                    "columns": columns,
                    "ref_schema": row["ref_schema"],
                    "ref_table": row["ref_table"],
                    "ref_columns": list(row["ref_columns"]),
                    "on_delete": _ACTIONS[row["on_delete"]],
                    "on_update": _ACTIONS[row["on_update"]],
                }
            case _:
                pass

    for row in await conn.fetch_dicts(_INDEXES, [oids]):
        table = by_oid[row["table_oid"]]
        if row["has_expressions"]:
            log.warning("skipping expression index %s (not supported yet)", row["name"])
            continue
        table["indexes"][row["name"]] = {
            "name": row["name"],
            "columns": list(row["columns"]),
            "unique": row["unique"],
            "where": row["predicate"],
        }

    for row in await conn.fetch_dicts(_ENUMS, [schema_list]):
        enum = EnumSnap(schema=row["schema"], name=row["name"], values=list(row["values"]))
        snapshot["enums"][key(row["schema"], row["name"])] = enum
    return snapshot

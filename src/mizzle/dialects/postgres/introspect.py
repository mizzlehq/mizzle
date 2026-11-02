"""Read a live PostgreSQL schema into a Snapshot (used by push, pull and studio)."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import cast

from mizzle.core.snapshot import (
    ColumnSnap,
    EnumSnap,
    IndexSnap,
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
       i.indexprs IS NOT NULL AS has_expressions,
       pg_get_indexdef(i.indexrelid) AS definition,
       am.amname AS method
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_am am ON am.oid = ic.relam
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


type _Row = dict[str, object]


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError(f"expected int from the catalog, got {type(value).__name__}")
    return value


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _strs(value: object) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"expected an array from the catalog, got {type(value).__name__}")
    return [str(v) for v in cast(list[object], value)]


def _column(row: _Row) -> ColumnSnap:
    name, sql_type, not_null = str(row["name"]), str(row["type"]), bool(row["not_null"])
    default = _opt_str(row["default"])
    serial = next((s for s, base in SERIAL_TYPES.items() if base == sql_type), None)
    if serial is not None and default is not None and _NEXTVAL.fullmatch(default):
        return ColumnSnap(name=name, type=serial, not_null=not_null, default=None)
    return ColumnSnap(
        name=name,
        type=sql_type,
        not_null=not_null,
        default=normalize_default(default),
    )


def _check_expression(definition: str) -> str:
    # pg_get_constraintdef: "CHECK ((title <> ''::text))" -> "(title <> ''::text)"
    expr = definition.removeprefix("CHECK ").removesuffix(" NOT VALID")
    return expr[1:-1] if expr.startswith("(") and expr.endswith(")") else expr


def _index_keys(definition: str) -> str:
    """The key list of ``CREATE INDEX ... USING btree (<keys>) [WHERE ...]``."""
    start = definition.index(" USING btree (") + len(" USING btree (")
    depth, quoted = 1, False
    for i, ch in enumerate(definition[start:], start):
        if ch == "'":
            quoted = not quoted  # '' inside a literal toggles twice: still correct
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
            if depth == 0:
                return definition[start:i]
    raise ValueError(f"cannot read the keys of index definition {definition!r}")


async def introspect(conn: Connection, schemas: Sequence[str] = (DEFAULT_SCHEMA,)) -> Snapshot:
    snapshot = empty_snapshot("postgresql")
    schema_list = list(schemas)
    tables = await conn.fetch_dicts(_TABLES, [schema_list, MIGRATIONS_TABLE])
    by_oid: dict[int, TableSnap] = {}
    for t in tables:
        schema, name = str(t["schema"]), str(t["name"])
        snap = TableSnap(
            schema=schema,
            name=name,
            columns={},
            primary_key=None,
            uniques={},
            checks={},
            foreign_keys={},
            indexes={},
        )
        by_oid[_int(t["oid"])] = snap
        snapshot["tables"][key(schema, name)] = snap
        if schema != DEFAULT_SCHEMA and schema not in snapshot["schemas"]:
            snapshot["schemas"].append(schema)

    oids = list(by_oid)
    for row in await conn.fetch_dicts(_COLUMNS, [oids]):
        column = _column(row)
        by_oid[_int(row["table_oid"])]["columns"][column["name"]] = column

    for row in await conn.fetch_dicts(_CONSTRAINTS, [oids]):
        table = by_oid[_int(row["table_oid"])]
        name, columns = str(row["name"]), _strs(row["columns"])
        match row["kind"]:
            case "p":
                table["primary_key"] = {"name": name, "columns": columns}
            case "u":
                table["uniques"][name] = {"name": name, "columns": columns}
            case "c":
                expression = _check_expression(str(row["definition"]))
                table["checks"][name] = {"name": name, "expression": expression}
            case "f":
                table["foreign_keys"][name] = {
                    "name": name,
                    "columns": columns,
                    "ref_schema": str(row["ref_schema"]),
                    "ref_table": str(row["ref_table"]),
                    "ref_columns": _strs(row["ref_columns"]),
                    "on_delete": _ACTIONS[str(row["on_delete"])],
                    "on_update": _ACTIONS[str(row["on_update"])],
                }
            case _:
                pass

    for row in await conn.fetch_dicts(_INDEXES, [oids]):
        table = by_oid[_int(row["table_oid"])]
        name = str(row["name"])
        method = str(row["method"])
        if method != "btree":
            log.warning("skipping %s index %s (only btree indexes are supported)", method, name)
            continue
        index = IndexSnap(
            name=name,
            columns=_strs(row["columns"]),
            unique=bool(row["unique"]),
            where=_opt_str(row["predicate"]),
        )
        if row["has_expressions"]:
            index["columns"] = []
            index["expression"] = _index_keys(str(row["definition"]))
        table["indexes"][name] = index

    for row in await conn.fetch_dicts(_ENUMS, [schema_list]):
        schema, name = str(row["schema"]), str(row["name"])
        enum = EnumSnap(schema=schema, name=name, values=_strs(row["values"]))
        snapshot["enums"][key(schema, name)] = enum
    return snapshot

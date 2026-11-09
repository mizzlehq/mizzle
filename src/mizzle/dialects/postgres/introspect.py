"""Read a live PostgreSQL schema into a Snapshot (used by push, pull and studio)."""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Sequence
from typing import Protocol, cast

from mizzle.core.errors import DatabaseError, SchemaError
from mizzle.core.snapshot import (
    CheckSnap,
    ColumnSnap,
    EnumSnap,
    ForeignKeySnap,
    GeneratedSnap,
    IdentitySnap,
    IndexSnap,
    PolicySnap,
    PrimaryKeySnap,
    RlsSnap,
    RoleSnap,
    Snapshot,
    TableSnap,
    UniqueSnap,
    ViewSnap,
    check_schema_name,
    empty_snapshot,
    key,
    sequence_defaults,
)
from mizzle.dialects.base import Connection
from mizzle.dialects.postgres.ddl import (
    DEFAULT_SCHEMA,
    SERIAL_TYPES,
    create_index,
    default_sql,
    literal,
    normalize_default,
    qualified,
    quote,
    requote_casts,
    requote_type,
)

MIGRATIONS_TABLE = "__mizzle_migrations"

_ACTIONS = {"a": None, "r": "restrict", "c": "cascade", "n": "set null", "d": "set default"}
_NEXTVAL = re.compile(r"nextval\('.*'::regclass\)")

# Objects that belong to an extension (pg_trgm's functions...) are not the schema's own.
_NOT_EXTENSION = """NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = {catalog}::regclass
                  AND d.objid = {oid} AND d.deptype = 'e')"""


# A partition, an inheritance child or an extension's table: not managed (K6-3, K6-6). A
# foreign key that references one is not managed either (K7-2).
def _unmanaged_relation(oid: str) -> str:
    return (
        f"(EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = {oid})\n  OR "
        + _NOT_EXTENSION.format(catalog="'pg_class'", oid=oid).removeprefix("NOT ")
        + ")"
    )


_TABLES = f"""
SELECT c.oid, n.nspname AS schema, c.relname AS name,
       c.relrowsecurity AS rls, c.relforcerowsecurity AS rls_force,
       obj_description(c.oid, 'pg_class') AS comment,
       c.relpersistence::text AS persistence, coalesce(c.reloptions, '{{}}') AS options,
       coalesce(tc.reloptions, '{{}}') AS toast_options
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class tc ON tc.oid = c.reltoastrelid
-- A partitioned table reads as a plain one; its partitions and inheritance children are
-- not read (not managed, K6-6), nor are an extension's tables (K6-3).
WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY($1::text[]) AND c.relname <> $2
  AND NOT {_unmanaged_relation("c.oid")}
ORDER BY n.nspname, c.relname
"""

log = logging.getLogger("mizzle")

_COLUMNS = """
SELECT a.attrelid AS table_oid, a.attname AS name,
       pg_get_serial_sequence(a.attrelid::regclass::text, a.attname) AS owned_sequence,
       format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS default,
       a.attidentity::text AS identity, a.attgenerated::text AS generated,
       col.collname AS collation, coln.nspname AS collation_schema,
       col_description(a.attrelid, a.attnum) AS comment,
       s.seqstart AS identity_start, s.seqincrement AS identity_increment,
       s.seqmin AS identity_min, s.seqmax AS identity_max, s.seqcache AS identity_cache,
       s.seqcycle AS identity_cycle
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
LEFT JOIN pg_collation col ON col.oid = a.attcollation
LEFT JOIN pg_namespace coln ON coln.oid = col.collnamespace
-- An identity column's sequence depends on it internally (deptype 'i').
LEFT JOIN pg_depend dep ON a.attidentity <> '' AND dep.refclassid = 'pg_class'::regclass
  AND dep.refobjid = a.attrelid AND dep.refobjsubid = a.attnum AND dep.deptype = 'i'
  AND dep.classid = 'pg_class'::regclass
LEFT JOIN pg_sequence s ON s.seqrelid = dep.objid
WHERE a.attrelid = ANY($1::oid[]) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""

_CONSTRAINTS = f"""
SELECT con.conrelid AS table_oid, con.conname AS name, con.contype::text AS kind,
       ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, ord)
             JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n
             ORDER BY k.ord) AS columns,
       fn.nspname AS ref_schema, fc.relname AS ref_table,
       ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, ord)
             JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n
             ORDER BY k.ord) AS ref_columns,
       con.confdeltype::text AS on_delete, con.confupdtype::text AS on_update,
       pg_get_constraintdef(con.oid) AS definition,
       con.condeferrable AS deferrable, con.condeferred AS initially_deferred,
       con.convalidated AS validated,
       EXISTS (SELECT 1 FROM pg_index i WHERE i.indexrelid = con.conindid
               AND i.indnullsnotdistinct) AS nulls_not_distinct
FROM pg_constraint con
LEFT JOIN pg_class fc ON fc.oid = con.confrelid
LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
-- Exclusion constraints (contype 'x') are not managed: not read. Nor are the per-partition
-- copies of an FK that references a partitioned table (K6-2), nor an FK to a table that
-- is not managed (K7-2).
WHERE con.conrelid = ANY($1::oid[]) AND con.contype IN ('p', 'u', 'f', 'c')
  AND con.conparentid = 0
  AND NOT (con.contype = 'f' AND {_unmanaged_relation("con.confrelid")})
ORDER BY con.conrelid, con.conname
"""

_INDEXES = """
SELECT i.indrelid AS table_oid, ic.relname AS name, i.indisunique AS unique,
       -- Key columns only: INCLUDE columns follow them in indkey (C25).
       ARRAY(SELECT a.attname FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(n, ord)
             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n
             WHERE k.ord <= i.indnkeyatts ORDER BY k.ord) AS columns,
       i.indnatts > i.indnkeyatts AS has_include,
       -- Per key: DESC (bit 1) and NULLS FIRST (bit 2), and a collation other than the
       -- column's own.
       ARRAY(SELECT k.o FROM unnest(i.indoption::int2[]) WITH ORDINALITY k(o, ord)
             ORDER BY k.ord) AS key_options,
       ARRAY(SELECT CASE WHEN c.oid IS NULL OR c.collname = 'default'
                          OR c.oid = a.attcollation THEN NULL ELSE c.collname::text END
             FROM unnest(i.indcollation::oid[]) WITH ORDINALITY k(o, ord)
             LEFT JOIN pg_collation c ON c.oid = k.o
             LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid
                                     AND a.attnum = (i.indkey::int2[])[k.ord - 1]
             ORDER BY k.ord) AS collations,
       pg_get_expr(i.indpred, i.indrelid) AS predicate,
       i.indexprs IS NOT NULL AS has_expressions,
       -- Each key on its own, straight from the catalog (no parsing of definition text).
       ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
             FROM generate_series(1, i.indnkeyatts) AS k ORDER BY k) AS keys,
       am.amname AS method,
       ARRAY(SELECT CASE WHEN oc.opcdefault THEN NULL ELSE oc.opcname END
             FROM unnest(i.indclass::oid[]) WITH ORDINALITY k(o, ord)
             JOIN pg_opclass oc ON oc.oid = k.o ORDER BY k.ord) AS opclasses,
       coalesce(ic.reloptions, '{}') AS options,
       i.indnullsnotdistinct AS nulls_not_distinct
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_am am ON am.oid = ic.relam
WHERE i.indrelid = ANY($1::oid[])
  -- The index of a key or of an exclusion constraint (not managed) is not an index of its own.
  AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = i.indexrelid
                  AND con.contype IN ('p', 'u', 'x'))
ORDER BY i.indrelid, ic.relname
"""

_ENUMS = f"""
SELECT n.nspname AS schema, t.typname AS name,
       array_agg(e.enumlabel ORDER BY e.enumsortorder) AS values
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = ANY($1::text[])
  AND {_NOT_EXTENSION.format(catalog="'pg_type'", oid="t.oid")}
GROUP BY n.nspname, t.typname
ORDER BY n.nspname, t.typname
"""

_VIEWS = f"""
SELECT c.oid, n.nspname AS schema, c.relname AS name, c.relkind = 'm' AS materialized,
       pg_get_viewdef(c.oid) AS definition, coalesce(c.reloptions, '{{}}') AS options
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v', 'm') AND n.nspname = ANY($1::text[])
  AND {_NOT_EXTENSION.format(catalog="'pg_class'", oid="c.oid")}
ORDER BY n.nspname, c.relname
"""

# What mizzle does not manage, for pull to say it leaves out (K8): generate, push and pull
# neither read nor drop these.
_SKIPPED = f"""
SELECT CASE p.prokind WHEN 'p' THEN 'procedure ' ELSE 'function ' END
       || p.oid::regprocedure::text AS what
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind IN ('f', 'p') AND n.nspname = ANY($1::text[])
  AND {_NOT_EXTENSION.format(catalog="'pg_proc'", oid="p.oid")}
UNION ALL
SELECT 'trigger ' || quote_ident(t.tgname) || ' on ' || c.oid::regclass::text
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal AND n.nspname = ANY($1::text[])
UNION ALL
SELECT CASE t.typtype WHEN 'd' THEN 'domain ' ELSE 'composite type ' END
       || t.oid::regtype::text
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
LEFT JOIN pg_class c ON c.oid = t.typrelid
WHERE (t.typtype = 'd' OR (t.typtype = 'c' AND c.relkind = 'c'))
  AND n.nspname = ANY($1::text[])
  AND {_NOT_EXTENSION.format(catalog="'pg_type'", oid="t.oid")}
UNION ALL
SELECT 'extension ' || quote_ident(e.extname)
FROM pg_extension e WHERE e.extname <> 'plpgsql'
UNION ALL
SELECT 'exclusion constraint ' || quote_ident(con.conname) || ' on '
       || con.conrelid::regclass::text
FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE con.contype = 'x' AND n.nspname = ANY($1::text[])
UNION ALL
SELECT 'foreign key ' || quote_ident(con.conname) || ' on ' || con.conrelid::regclass::text
       || ' (it references ' || con.confrelid::regclass::text || ')'
FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE con.contype = 'f' AND con.conparentid = 0 AND n.nspname = ANY($1::text[])
  AND {_unmanaged_relation("con.confrelid")}
UNION ALL
SELECT 'sequence ' || c.oid::regclass::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S' AND n.nspname = ANY($1::text[])
  AND {_NOT_EXTENSION.format(catalog="'pg_class'", oid="c.oid")}
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                  AND d.objid = c.oid AND d.refobjsubid > 0 AND d.deptype IN ('a', 'i'))
UNION ALL
SELECT CASE WHEN c.relispartition THEN 'partition ' ELSE 'table inheritance ' END
       || c.oid::regclass::text || ' of ' || i.inhparent::regclass::text
FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY($1::text[]) AND c.relkind IN ('r', 'p', 'f')  -- not indexes (K4-10)
UNION ALL
SELECT 'grants on ' || c.oid::regclass::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm') AND n.nspname = ANY($1::text[]) AND NOT c.relispartition
  AND c.relname <> $2 AND {_NOT_EXTENSION.format(catalog="'pg_class'", oid="c.oid")}
  AND (EXISTS (SELECT 1 FROM aclexplode(c.relacl) g WHERE g.grantee <> c.relowner)
       OR EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attacl IS NOT NULL))
ORDER BY 1
"""


async def skipped_objects(conn: Connection, schemas: Sequence[str]) -> list[str]:
    """Objects mizzle does not manage, so introspection does not read them: functions,
    procedures, triggers, domains, composite types, extensions, exclusion constraints,
    partitions, table inheritance, standalone sequences and grants (K8)."""
    rows = await conn.fetch_dicts(_SKIPPED, [list(schemas), MIGRATIONS_TABLE])
    return [str(row["what"]) for row in rows]


_POLICIES = """
SELECT p.polrelid AS table_oid, p.polname AS name, p.polcmd::text AS command,
       p.polpermissive AS permissive,
       ARRAY(SELECT CASE WHEN r = 0 THEN 'public' ELSE pg_get_userbyid(r) END
             FROM unnest(p.polroles) AS r ORDER BY 1) AS roles,
       pg_get_expr(p.polqual, p.polrelid) AS using,
       pg_get_expr(p.polwithcheck, p.polrelid) AS with_check
FROM pg_policy p
WHERE p.polrelid = ANY($1::oid[])
ORDER BY p.polrelid, p.polname
"""

# Privileges granted on tables and views, except the owner's own.
_ROLES = """
SELECT rolname AS name, rolcanlogin AS login, rolcreatedb AS create_db,
       rolcreaterole AS create_role, rolinherit AS inherit
FROM pg_roles WHERE rolname = any($1::text[])
"""
_COMMANDS = {"*": "all", "r": "select", "a": "insert", "w": "update", "d": "delete"}


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


def _collation(row: _Row) -> str | None:
    """The ``COLLATE`` name, or None for the type's default (PostgreSQL stores that as the
    ``default`` collation). Type and collation mutate independently, so this is separate."""
    name = row.get("collation")
    if name is None or str(name) == "default":
        return None
    return str(name)


def _column(row: _Row) -> ColumnSnap:
    name, sql_type, not_null = str(row["name"]), str(row["type"]), bool(row["not_null"])
    default = _opt_str(row["default"])
    serial = next((s for s, base in SERIAL_TYPES.items() if base == sql_type), None)
    # Only a sequence the column owns (what serial creates; its name survives a rename): a
    # shared or hand-made sequence pulled as serial() changed the column on the next
    # generate (C26).
    if (
        serial is not None
        and row["owned_sequence"] is not None
        and default is not None
        and _NEXTVAL.fullmatch(default)
    ):
        column = ColumnSnap(name=name, type=serial, not_null=not_null, default=None)
        if (comment := _opt_str(row["comment"])) is not None:
            column["comment"] = comment
        return column
    if row["generated"] in ("s", "v"):  # pg_attrdef holds the expression, not a default
        assert default is not None
        column = ColumnSnap(name=name, type=sql_type, not_null=not_null, default=None)
        column["generated"] = GeneratedSnap(expression=default, stored=row["generated"] == "s")
        if (collation := _collation(row)) is not None:
            column["collation"] = collation
        if (comment := _opt_str(row["comment"])) is not None:
            column["comment"] = comment
        return column
    column = ColumnSnap(
        name=name,
        type=sql_type,
        not_null=not_null,
        default=normalize_default(default),
    )
    if (collation := _collation(row)) is not None:
        column["collation"] = collation
    if (comment := _opt_str(row["comment"])) is not None:
        column["comment"] = comment
    if row["identity"] in ("a", "d"):
        increment = _int(row["identity_increment"])
        identity = column["identity"] = IdentitySnap(
            always=row["identity"] == "a", start=_int(row["identity_start"]), increment=increment
        )
        low, high = sequence_defaults(sql_type, increment)
        if (low_value := _int(row["identity_min"])) != low:
            identity["min_value"] = low_value
        if (high_value := _int(row["identity_max"])) != high:
            identity["max_value"] = high_value
        if (cache := _int(row["identity_cache"])) != 1:
            identity["cache"] = cache
        if row["identity_cycle"]:
            identity["cycle"] = True
    return column


def _view_definition(text: str) -> str:
    # pg_get_viewdef: " SELECT id,\n    name\n   FROM t;"
    return text.strip().removesuffix(";")


def _view_options(view: ViewSnap, options: list[str]) -> None:
    """pg_class.reloptions of a view: "security_invoker=true", "check_option=local" (K6)."""
    for option in options:
        name, _, value = option.partition("=")
        on = value.lower() in ("true", "on", "yes", "1", "t", "y")
        if name == "security_invoker" and on:
            view["security_invoker"] = True
        elif name == "security_barrier" and on:
            view["security_barrier"] = True
        elif name == "check_option" and value in ("local", "cascaded"):
            view["check_option"] = "local" if value == "local" else "cascaded"


def _index(row: _Row) -> IndexSnap:
    method = str(row["method"])
    index = IndexSnap(
        name=str(row["name"]),
        columns=_strs(row["columns"]),
        unique=bool(row["unique"]),
        where=_opt_str(row["predicate"]),
    )
    opclasses = cast(list[str | None], row["opclasses"])
    orders = [_key_order(int(cast(int, o))) for o in cast("list[object]", row["key_options"])]
    collations = cast(list[str | None], row["collations"])
    if row["has_include"]:
        _not_modelled(f"index {index['name']}", "INCLUDE")
    if row["has_expressions"] or any(orders) or any(collations):
        # The key list as SQL: the only form that carries DESC, NULLS and COLLATE (C25).
        index["columns"] = []
        keys = _strs(row["keys"])
        index["expression"] = ", ".join(
            k + (f" COLLATE {quote(coll)}" if coll else "") + (f" {op}" if op else "") + order
            for k, op, coll, order in zip(keys, opclasses, collations, orders, strict=True)
        )
    elif any(opclasses):
        index["opclasses"] = opclasses
    if method != "btree":
        index["method"] = method
    if bool(row["nulls_not_distinct"]):
        index["nulls_not_distinct"] = True
    if options := _strs(row["options"]):
        index["options"] = dict(o.split("=", 1) for o in options)
    return index


def _key_order(option: int) -> str:
    """An index key's sort order from ``pg_index.indoption`` (DESC: 1, NULLS FIRST: 2),
    written only where it is not the default."""
    descending, nulls_first = bool(option & 1), bool(option & 2)
    if descending:
        return " DESC" if nulls_first else " DESC NULLS LAST"
    return " NULLS FIRST" if nulls_first else ""


def _not_modelled(what: str, option: str) -> None:
    log.warning(
        "mizzle: %s uses %s, which mizzle does not model; a schema built from this one "
        "creates it without it",
        what,
        option,
    )


def _check_expression(definition: str) -> str:
    # pg_get_constraintdef: "CHECK ((title <> ''::text))" -> "(title <> ''::text)"
    # NO INHERIT is not modelled: kept in the text it made the next migrate fail (C26).
    expr = definition.removeprefix("CHECK ").removesuffix(" NOT VALID").removesuffix(" NO INHERIT")
    return expr[1:-1] if expr.startswith("(") and expr.endswith(")") else expr


async def _server_version_num(conn: Connection) -> int:
    [row] = await conn.fetch_dicts("SHOW server_version_num", [])
    return int(str(row["server_version_num"]))


# pg_index.indnullsnotdistinct exists from PostgreSQL 15; on 14 every index reads as
# distinct, so the column is replaced with a literal false.
def _constraints_query(version: int) -> str:
    if version >= 150000:
        return _CONSTRAINTS
    return _CONSTRAINTS.replace("AND i.indnullsnotdistinct", "AND false")


def _indexes_query(version: int) -> str:
    if version >= 150000:
        return _INDEXES
    return _INDEXES.replace(
        "i.indnullsnotdistinct AS nulls_not_distinct", "false AS nulls_not_distinct"
    )


async def introspect(
    conn: Connection, schemas: Sequence[str] = (DEFAULT_SCHEMA,), roles: Sequence[str] = ()
) -> Snapshot:
    """The database's schema, read in UTC: the server prints a timestamptz constant (a
    default, a check) in the session TimeZone, and a schema writes it in UTC (Q4-15)."""
    for schema in schemas:
        check_schema_name(schema)
    try:
        async with conn.transaction():  # rolled back, and the SET LOCAL with it
            await conn.execute("SET LOCAL TimeZone = 'UTC'", [])
            # Printed '...' strings keep their backslashes as written (K8-3).
            await conn.execute("SET LOCAL standard_conforming_strings = on", [])
            snapshot = await _introspect(conn, schemas, roles)
            rows = await conn.fetch_dicts(_SERVER_KEYWORDS, [])
            _requote_types(snapshot, frozenset(str(r["word"]) for r in rows))
            raise _Undo(snapshot)
    except _Undo as undo:
        return cast(Snapshot, undo.args[0])


_SERVER_KEYWORDS = "SELECT word FROM pg_get_keywords() WHERE catcode <> 'U'"


def _requote_types(snapshot: Snapshot, server_keywords: frozenset[str]) -> None:
    """Type names quoted as the schema quotes them, whatever the server's version (K4-22)."""
    relations = [*snapshot["tables"].values(), *snapshot.get("views", {}).values()]
    for column in (c for r in relations for c in r["columns"].values()):
        column["type"] = requote_type(column["type"], server_keywords)
        if (default := column["default"]) is not None:  # K4-29
            column["default"] = requote_casts(default, server_keywords)


async def _introspect(conn: Connection, schemas: Sequence[str], roles: Sequence[str]) -> Snapshot:
    snapshot = empty_snapshot("postgresql")
    version = await _server_version_num(conn)
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
        if t["rls"] or t["rls_force"]:
            snap["rls"] = RlsSnap(enabled=bool(t["rls"]), force=bool(t["rls_force"]))
        if (comment := _opt_str(t["comment"])) is not None:
            snap["comment"] = comment
        if str(t["persistence"]) == "u":
            snap["unlogged"] = True
        if options := [
            *_strs(t["options"]),
            *(f"toast.{o}" for o in _strs(t["toast_options"])),
        ]:
            snap["options"] = dict(o.split("=", 1) for o in options)
        by_oid[_int(t["oid"])] = snap
        snapshot["tables"][key(schema, name)] = snap
        _add_schema(snapshot, schema)

    oids = list(by_oid)
    for row in await conn.fetch_dicts(_COLUMNS, [oids]):
        column = _column(row)
        by_oid[_int(row["table_oid"])]["columns"][column["name"]] = column

    for row in await conn.fetch_dicts(_constraints_query(version), [oids]):
        table = by_oid[_int(row["table_oid"])]
        name, columns = str(row["name"]), _strs(row["columns"])
        match row["kind"]:
            case "p":
                pk = PrimaryKeySnap(name=name, columns=columns)
                if row["deferrable"]:
                    pk["deferrable"] = True
                if row["initially_deferred"]:
                    pk["initially_deferred"] = True
                table["primary_key"] = pk
            case "u":
                unique = UniqueSnap(name=name, columns=columns)
                if row["nulls_not_distinct"]:
                    unique["nulls_not_distinct"] = True
                if row["deferrable"]:
                    unique["deferrable"] = True
                if row["initially_deferred"]:
                    unique["initially_deferred"] = True
                table["uniques"][name] = unique
            case "c":
                definition = str(row["definition"])
                if " NO INHERIT" in definition:
                    _not_modelled(f"check {name}", "NO INHERIT")
                expression = _check_expression(definition)
                check = CheckSnap(name=name, expression=expression)
                if not row["validated"]:
                    check["not_valid"] = True
                table["checks"][name] = check
            case "f":
                fk = ForeignKeySnap(
                    name=name,
                    columns=columns,
                    ref_schema=str(row["ref_schema"]),
                    ref_table=str(row["ref_table"]),
                    ref_columns=_strs(row["ref_columns"]),
                    on_delete=_ACTIONS[str(row["on_delete"])],
                    on_update=_ACTIONS[str(row["on_update"])],
                )
                if row["deferrable"]:
                    fk["deferrable"] = True
                if row["initially_deferred"]:
                    fk["initially_deferred"] = True
                if not row["validated"]:
                    fk["not_valid"] = True
                if " MATCH FULL" in str(row["definition"]):
                    _not_modelled(f"foreign key {name}", "MATCH FULL")
                table["foreign_keys"][name] = fk
            case _:
                pass

    for row in await conn.fetch_dicts(_indexes_query(version), [oids]):
        index = _index(row)
        by_oid[_int(row["table_oid"])]["indexes"][index["name"]] = index

    for row in await conn.fetch_dicts(_POLICIES, [oids]):
        table = by_oid[_int(row["table_oid"])]
        policy = PolicySnap(
            name=str(row["name"]),
            command=_COMMANDS[str(row["command"])],
            permissive=bool(row["permissive"]),
            roles=_strs(row["roles"]),
            using=_opt_str(row["using"]),
            with_check=_opt_str(row["with_check"]),
        )
        table.setdefault("policies", {})[policy["name"]] = policy

    await _introspect_objects(conn, schema_list, snapshot, roles, version)

    for row in await conn.fetch_dicts(_ENUMS, [schema_list]):
        schema, name = str(row["schema"]), str(row["name"])
        enum = EnumSnap(schema=schema, name=name, values=_strs(row["values"]))
        snapshot["enums"][key(schema, name)] = enum
        _add_schema(snapshot, schema)

    return snapshot


def _add_schema(snapshot: Snapshot, schema: str) -> None:
    if schema != DEFAULT_SCHEMA and schema not in snapshot["schemas"]:
        snapshot["schemas"].append(schema)


async def _introspect_objects(
    conn: Connection, schemas: list[str], snapshot: Snapshot, roles: Sequence[str], version: int
) -> None:
    """Views and roles."""
    views: dict[int, ViewSnap] = {}
    for row in await conn.fetch_dicts(_VIEWS, [schemas]):
        schema, name = str(row["schema"]), str(row["name"])
        view = ViewSnap(
            schema=schema,
            name=name,
            definition=_view_definition(str(row["definition"])),
            materialized=bool(row["materialized"]),
            columns={},
        )
        _view_options(view, _strs(row["options"]))
        views[_int(row["oid"])] = view
        snapshot.setdefault("views", {})[key(schema, name)] = view
        _add_schema(snapshot, schema)
    if views:
        for row in await conn.fetch_dicts(_COLUMNS, [list(views)]):
            column = _column(row)
            views[_int(row["table_oid"])]["columns"][column["name"]] = column
        for row in await conn.fetch_dicts(_indexes_query(version), [list(views)]):
            index = _index(row)
            views[_int(row["table_oid"])].setdefault("indexes", {})[index["name"]] = index

    # Only the named roles: the others belong to the server (and other databases), and a
    # pulled baseline must never contain them, or the next generate would drop them.
    for row in await conn.fetch_dicts(_ROLES, [list(roles)]) if roles else []:
        name = str(row["name"])
        snapshot.setdefault("roles", {})[name] = RoleSnap(
            name=name,
            login=bool(row["login"]),
            create_db=bool(row["create_db"]),
            create_role=bool(row["create_role"]),
            inherit=bool(row["inherit"]),
        )


async def _normalize_indexes(snapshot: Snapshot, read_back: _ReadBack, version: int) -> None:
    """Operator classes (naming the default one is the same as naming none) and expression
    keys as the database prints them, from the index made on a temporary table."""
    rel = f"to_regclass('pg_temp.{quote(_NORM)}')"
    query = _indexes_query(version).replace("ANY($1::oid[])", f"ANY(ARRAY[{rel}::oid])")
    for table in snapshot["tables"].values():
        columns = ", ".join(
            f"{quote(c['name'])} {SERIAL_TYPES.get(c['type'], c['type'])}"
            for c in table["columns"].values()
        )
        for index in table["indexes"].values():
            if "opclasses" not in index and "expression" not in index and index["where"] is None:
                continue
            temp_index = copy.copy(index)
            temp_index["name"] = f"{_NORM}_index"  # not the table's name
            row = await read_back(
                [
                    f"CREATE TEMP TABLE {quote(_NORM)} ({columns})",
                    create_index("pg_temp", _NORM, temp_index),
                ],
                query,
            )
            if row is None:
                continue
            printed = _index(row)
            index.pop("opclasses", None)
            if "opclasses" in printed:
                index["opclasses"] = printed["opclasses"]
            if "expression" in printed:
                index["expression"] = printed["expression"]
            index["where"] = printed["where"]  # as the database prints it (C24)


class _ReadBack(Protocol):
    async def __call__(self, statements: list[str], query: str, /) -> _Row | None: ...


class _Undo(Exception):
    """Rolls back a savepoint whose work was only needed to read something."""


_NORM = "__mizzle_norm"


def _mentions(expression: str, column: str) -> bool:
    """Whether an expression names ``column`` (quoted, or as a plain identifier)."""
    if f'"{column}"' in expression:
        return True
    plain = re.escape(column)
    return re.search(rf"(?<![\w$\"]){plain}(?![\w$\"])", expression, re.IGNORECASE) is not None


async def _normalize_types(snapshot: Snapshot, read_back: _ReadBack, rel: str) -> None:
    """Column types as format_type() prints them, from a temporary table with a column of
    each declared type: ``decimal(10,2)``, ``float(24)``, ``int[3]`` or ``public.int4`` (a
    domain) all have spellings no rule here guessed right (K7-6, K7-7). A type the database
    does not have yet (an enum this migration makes) keeps its offline spelling."""
    printed: dict[str, str | None] = {}
    for table in snapshot["tables"].values():
        for column in table["columns"].values():
            written = column["type"]
            if written in SERIAL_TYPES:
                continue
            if written not in printed:
                row = await read_back(
                    [f"CREATE TEMP TABLE {quote(_NORM)} (c {written})"],
                    "SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute "
                    f"WHERE attrelid = {rel} AND attname = 'c'",
                )
                printed[written] = None if row is None else _opt_str(row["t"])
            column["type"] = printed[written] or written


async def _normalize_column_expressions(snapshot: Snapshot, read_back: _ReadBack, rel: str) -> None:
    """Defaults and generated expressions as the database prints them, from a temporary
    table holding that column. Compared as written, ``1e-7`` never matched ``0.0000001``
    nor ``'1 day 5 seconds'::interval`` its printed form, so push repeated them forever and
    doctor reported drift right after migrate (C24)."""
    for table in snapshot["tables"].values():
        plain = {
            name: f"{quote(name)} {SERIAL_TYPES.get(c['type'], c['type'])}"
            for name, c in table["columns"].items()
        }
        for name, column in table["columns"].items():
            generated, default = column.get("generated"), column["default"]
            if generated is not None:
                extra = f" GENERATED ALWAYS AS ({generated['expression']}) STORED"
            elif default is not None:
                extra = f" DEFAULT {default_sql(default)}"
            else:
                continue
            # Only the columns it can use (K4): one of a type made later (a new enum) failed
            # the whole table, so every other expression kept its text and showed as drift.
            columns = ", ".join(
                sql + (extra if n == name else "")
                for n, sql in plain.items()
                if n == name or (generated is not None and _mentions(generated["expression"], n))
            )
            row = await read_back(
                [f"CREATE TEMP TABLE {quote(_NORM)} ({columns})"],
                "SELECT pg_get_expr(d.adbin, d.adrelid) AS e FROM pg_attrdef d "
                "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
                f"WHERE d.adrelid = {rel} AND a.attname = {literal(name)}",
            )
            if row is None or row["e"] is None:
                continue  # cannot be created here (a type or sequence made later): as is
            if generated is not None:
                generated["expression"] = str(row["e"])
            else:
                column["default"] = normalize_default(str(row["e"]))  # as introspect does


async def normalize_snapshot(conn: Connection, snapshot: Snapshot) -> Snapshot:
    """Let the server print view queries, policy expressions, checks and defaults, by
    creating each as a temporary object and reading it back.

    Everything runs in a transaction that is rolled back. An object that cannot be created
    against the current database (on a new table, using a new column...) keeps its text:
    it differs from the database anyway, so it is created or replaced."""
    out = copy.deepcopy(snapshot)
    version = await _server_version_num(conn)

    async def read_back(statements: list[str], query: str) -> _Row | None:
        try:
            async with conn.transaction():  # a savepoint inside the outer transaction
                for statement in statements:
                    await conn.execute(statement, [])
                [row] = await conn.fetch_dicts(query, [])
                raise _Undo(row)
        except _Undo as undo:
            return cast(_Row, undo.args[0])
        except DatabaseError:
            return None

    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL TimeZone = 'UTC'", [])  # as introspect reads
            await conn.execute("SET LOCAL standard_conforming_strings = on", [])
            # LIKE takes ACCESS SHARE on the real table: never wait long behind a migration
            # holding it (what times out keeps its text, K8-1).
            await conn.execute("SET LOCAL lock_timeout = '2s'", [])
            await _normalize(out, read_back, version)
            rows = await conn.fetch_dicts(_SERVER_KEYWORDS, [])  # as introspect prints them
            _requote_types(out, frozenset(str(r["word"]) for r in rows))
            raise _Undo
    except _Undo:
        pass
    return out


async def _normalize_constraint_expressions(
    table: TableSnap, like: str, norm: str, rel: str, read_back: _ReadBack
) -> None:
    """CHECK expressions as PostgreSQL prints them.

    A CHECK written by hand (``status <> ''``) reads back as ``(status <> ''::text)``.
    Without this ``generate`` after ``pull`` (or a hand-written constraint) drops and
    re-adds it forever. All the table's checks are created in one savepoint, then read
    back together.
    """
    checks = {n: c for n, c in table.get("checks", {}).items() if "expression" in c}
    if not checks:
        return
    statements = [like]
    for name, check in checks.items():
        add = f"ALTER TABLE {norm} ADD CONSTRAINT {quote(name)} CHECK ({check['expression']})"
        statements.append(add + (" NOT VALID" if check.get("not_valid") else ""))
    row = await read_back(
        statements,
        "SELECT "
        "ARRAY(SELECT c.conname FROM pg_constraint c "
        f"WHERE c.conrelid = {rel} AND c.contype = 'c' ORDER BY c.conname) AS check_names, "
        "ARRAY(SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        f"WHERE c.conrelid = {rel} AND c.contype = 'c' ORDER BY c.conname) AS check_defs",
    )
    if row is None:
        return
    for name, definition in zip(_strs(row["check_names"]), _strs(row["check_defs"]), strict=True):
        if name in checks:
            checks[name]["expression"] = _check_expression(definition)


async def _normalize(snapshot: Snapshot, read_back: _ReadBack, version: int) -> None:
    norm = f"pg_temp.{quote(_NORM)}"
    # Looked up when each query runs (a missing object reads as NULL, never as text).
    rel = f"to_regclass('{norm}')"
    for view in snapshot.get("views", {}).values():
        create = f"CREATE TEMP VIEW {quote(_NORM)} AS {view['definition']}"
        row = await read_back(
            [create],
            f"SELECT pg_get_viewdef({rel}) AS d, ARRAY(SELECT attname "
            f"FROM pg_attribute WHERE attrelid = {rel} AND attnum > 0 "
            "ORDER BY attnum) AS columns",
        )
        if row is None or row["d"] is None:
            continue
        view["definition"] = _view_definition(str(row["d"]))
        if missing := [c for c in view["columns"] if c not in _strs(row["columns"])]:
            raise SchemaError(
                f"view {view['name']!r} declares columns {missing} that its query does not "
                f"return (it returns {_strs(row['columns'])})"
            )

    await _normalize_types(snapshot, read_back, rel)
    await _normalize_indexes(snapshot, read_back, version)
    await _normalize_column_expressions(snapshot, read_back, f"to_regclass('{norm}')")

    for table in snapshot["tables"].values():
        like = (
            f"CREATE TEMP TABLE {quote(_NORM)} (LIKE {qualified(table['schema'], table['name'])})"
        )
        real = qualified(table["schema"], table["name"])
        for policy in table.get("policies", {}).values():
            body = f" FOR {policy['command'].upper()}"
            if policy["using"] is not None:
                body += f" USING ({policy['using']})"
            if policy["with_check"] is not None:
                body += f" WITH CHECK ({policy['with_check']})"
            query = (
                "SELECT pg_get_expr(polqual, polrelid) AS using, "
                "pg_get_expr(polwithcheck, polrelid) AS with_check FROM pg_policy "
            )
            row = await read_back(
                [like, f"CREATE POLICY p ON {norm}{body}"], f"{query}WHERE polrelid = {rel}"
            )
            if row is None:  # it names its own table (refs.t, K7-3): on a temporary copy
                # under the same name (pg_temp is searched first), never on the table itself:
                # CREATE POLICY there queued an ACCESS EXCLUSIVE lock behind readers (K8-1).
                shadow = f"pg_temp.{quote(table['name'])}"
                row = await read_back(
                    [
                        f"CREATE TEMP TABLE {quote(table['name'])} (LIKE {real})",
                        f"CREATE POLICY p ON {shadow}{body}",
                    ],
                    f"{query}WHERE polrelid = to_regclass({literal(shadow)})",
                )
            if row is not None:
                policy["using"], policy["with_check"] = (
                    _opt_str(row["using"]),
                    _opt_str(row["with_check"]),
                )
        await _normalize_constraint_expressions(table, like, norm, rel, read_back)

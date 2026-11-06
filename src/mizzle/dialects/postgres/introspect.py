"""Read a live PostgreSQL schema into a Snapshot (used by push, pull and studio)."""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from typing import Protocol, cast

from mizzle.core.errors import DatabaseError, SchemaError
from mizzle.core.snapshot import (
    CheckSnap,
    ColumnSnap,
    CompositeFieldSnap,
    CompositeSnap,
    DomainSnap,
    EnumSnap,
    ExclusionElementSnap,
    ExclusionSnap,
    ExtensionSnap,
    ForeignKeySnap,
    FunctionSnap,
    GeneratedSnap,
    IdentitySnap,
    IndexSnap,
    PolicySnap,
    PrimaryKeySnap,
    RlsSnap,
    RoleSnap,
    Snapshot,
    TableSnap,
    TriggerSnap,
    UniqueSnap,
    ViewSnap,
    default_cost,
    empty_snapshot,
    function_key,
    key,
    sequence_defaults,
)
from mizzle.dialects.base import Connection
from mizzle.dialects.postgres.ddl import (
    DEFAULT_SCHEMA,
    SERIAL_TYPES,
    create_index,
    function_options,
    normalize_default,
    qualified,
    quote,
    sql_standard_body,
    type_ident,
)

MIGRATIONS_TABLE = "__mizzle_migrations"

_ACTIONS = {"a": None, "r": "restrict", "c": "cascade", "n": "set null", "d": "set default"}
_NEXTVAL = re.compile(r"nextval\('.*'::regclass\)")

_TABLES = """
SELECT c.oid, n.nspname AS schema, c.relname AS name,
       c.relrowsecurity AS rls, c.relforcerowsecurity AS rls_force,
       obj_description(c.oid, 'pg_class') AS comment,
       c.relpersistence::text AS persistence, coalesce(c.reloptions, '{}') AS options,
       coalesce(tc.reloptions, '{}') AS toast_options,
       pg_get_partkeydef(c.oid) AS partition_by
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class tc ON tc.oid = c.reltoastrelid
WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY($1::text[]) AND c.relname <> $2
  AND NOT c.relispartition
ORDER BY n.nspname, c.relname
"""

_PARTITIONS = """
SELECT c.relname AS name, pn.nspname AS parent_schema, pc.relname AS parent_name,
       pg_get_expr(c.relpartbound, c.oid) AS bound
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_inherits i ON i.inhrelid = c.oid
JOIN pg_class pc ON pc.oid = i.inhparent
JOIN pg_namespace pn ON pn.oid = pc.relnamespace
WHERE c.relispartition AND n.nspname = ANY($1::text[])
ORDER BY pn.nspname, pc.relname, c.relname
"""

_COLUMNS = """
SELECT a.attrelid AS table_oid, a.attname AS name,
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
       pg_get_constraintdef(con.oid) AS definition,
       con.condeferrable AS deferrable, con.condeferred AS initially_deferred,
       con.convalidated AS validated,
       EXISTS (SELECT 1 FROM pg_index i WHERE i.indexrelid = con.conindid
               AND i.indnullsnotdistinct) AS nulls_not_distinct,
       -- Exclusion constraints (contype 'x'): the backing index gives the keys and method.
       am.amname AS method,
       ARRAY(SELECT op.oprname FROM unnest(con.conexclop) WITH ORDINALITY k(o, ord)
             JOIN pg_operator op ON op.oid = k.o ORDER BY k.ord) AS operators,
       ARRAY(SELECT CASE WHEN k.n = 0 THEN NULL ELSE a.attname END
             FROM unnest(ix.indkey::int2[]) WITH ORDINALITY k(n, ord)
             LEFT JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n
             ORDER BY k.ord) AS excl_keys,
       ARRAY(SELECT pg_get_indexdef(con.conindid, k, true)
             FROM generate_series(1, ix.indnkeyatts) AS k ORDER BY k) AS excl_defs,
       pg_get_expr(ix.indpred, con.conrelid) AS excl_predicate
FROM pg_constraint con
LEFT JOIN pg_class fc ON fc.oid = con.confrelid
LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
LEFT JOIN pg_index ix ON ix.indexrelid = con.conindid
LEFT JOIN pg_class ic ON ic.oid = con.conindid
LEFT JOIN pg_am am ON am.oid = ic.relam
WHERE con.conrelid = ANY($1::oid[]) AND con.contype IN ('p', 'u', 'f', 'c', 'x')
ORDER BY con.conrelid, con.conname
"""

_INDEXES = """
SELECT i.indrelid AS table_oid, ic.relname AS name, i.indisunique AS unique,
       ARRAY(SELECT a.attname FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(n, ord)
             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n
             ORDER BY k.ord) AS columns,
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

_DOMAINS = """
SELECT t.oid, n.nspname AS schema, t.typname AS name,
       format_type(t.typbasetype, t.typtypmod) AS base_type,
       t.typnotnull AS not_null,
       pg_get_expr(t.typdefaultbin, 0) AS default,
       (SELECT c.collname FROM pg_collation c WHERE c.oid = t.typcollation) AS collation
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'd' AND n.nspname = ANY($1::text[])
ORDER BY n.nspname, t.typname
"""

# S8: a pulled table can use a domain from a schema outside schema_filter.
_DOMAIN_OIDS = """
SELECT DISTINCT d.oid
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
JOIN pg_type d ON d.oid = CASE WHEN t.typtype = 'd' THEN t.oid
                               WHEN t.typelem <> 0 THEN t.typelem ELSE 0 END
WHERE a.attrelid = ANY($1::oid[]) AND a.attnum > 0 AND NOT a.attisdropped
  AND d.typtype = 'd'
"""

_DOMAINS_BY_OID = _DOMAINS.replace(
    "t.typtype = 'd' AND n.nspname = ANY($1::text[])", "t.oid = ANY($1::oid[])"
)

_DOMAIN_CHECKS = """
SELECT n.nspname AS schema, t.typname AS domain, pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint c
JOIN pg_type t ON t.oid = c.contypid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE c.contype = 'c' AND n.nspname = ANY($1::text[])
ORDER BY n.nspname, t.typname
"""

_DOMAIN_CHECKS_BY_OID = _DOMAIN_CHECKS.replace(
    "n.nspname = ANY($1::text[])", "t.oid = ANY($1::oid[])"
)

# Standalone composite types; a table's row type is a composite too, so keep only the
# pg_class rows of relkind 'c' (the ones CREATE TYPE ... AS makes).
_COMPOSITES = """
SELECT n.nspname AS schema, t.typname AS name,
       a.attname AS field, format_type(a.atttypid, a.atttypmod) AS type
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
JOIN pg_class c ON c.oid = t.typrelid
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE t.typtype = 'c' AND c.relkind = 'c' AND n.nspname = ANY($1::text[])
ORDER BY n.nspname, t.typname, a.attnum
"""


# Objects that belong to an extension (pg_trgm's functions...) are not the schema's own.
_NOT_EXTENSION = """NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = {catalog}::regclass
                  AND d.objid = {oid} AND d.deptype = 'e')"""

_VIEWS = f"""
SELECT c.oid, n.nspname AS schema, c.relname AS name, c.relkind = 'm' AS materialized,
       pg_get_viewdef(c.oid) AS definition
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v', 'm') AND n.nspname = ANY($1::text[])
  AND {_NOT_EXTENSION.format(catalog="'pg_class'", oid="c.oid")}
ORDER BY n.nspname, c.relname
"""

_FUNCTION_COLUMNS = """
pg_get_function_arguments(p.oid) AS args, pg_get_function_result(p.oid) AS returns,
l.lanname AS language, p.prosrc AS body, p.provolatile::text AS volatility,
p.prosecdef AS security_definer, p.proisstrict AS strict, p.proleakproof AS leakproof,
p.proparallel::text AS parallel, p.procost AS cost, coalesce(p.proconfig, '{}') AS config,
CASE WHEN p.prosqlbody IS NOT NULL THEN pg_get_functiondef(p.oid) END AS definition
"""

_FUNCTIONS = f"""
SELECT n.nspname AS schema, p.proname AS name, {_FUNCTION_COLUMNS}
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname = ANY($1::text[]) AND p.prokind = 'f'
  AND {_NOT_EXTENSION.format(catalog="'pg_proc'", oid="p.oid")}
ORDER BY n.nspname, p.proname, p.oid
"""

_TRIGGERS = """
SELECT t.tgrelid AS table_oid, t.tgname AS name, t.tgtype AS type,
       fn.nspname AS function_schema, f.proname AS function_name,
       pg_get_triggerdef(t.oid) AS definition,
       ARRAY(SELECT a.attname FROM unnest(t.tgattr::int2[]) AS k(n)
             JOIN pg_attribute a ON a.attrelid = t.tgrelid AND a.attnum = k.n
             ORDER BY 1) AS columns,
       t.tgoldtable AS old_table, t.tgnewtable AS new_table
FROM pg_trigger t
JOIN pg_proc f ON f.oid = t.tgfoid
JOIN pg_namespace fn ON fn.oid = f.pronamespace
WHERE t.tgrelid = ANY($1::oid[]) AND NOT t.tgisinternal
ORDER BY t.tgrelid, t.tgname
"""

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
_GRANTS = """
SELECT c.oid, CASE WHEN a.grantee = 0 THEN 'public' ELSE pg_get_userbyid(a.grantee) END AS role,
       array_agg(DISTINCT lower(a.privilege_type)) AS privileges
FROM pg_class c, aclexplode(c.relacl) AS a
WHERE c.oid = ANY($1::oid[]) AND a.grantee <> c.relowner
GROUP BY c.oid, a.grantee
ORDER BY c.oid, 2
"""

_ROLES = """
SELECT rolname AS name, rolcanlogin AS login, rolcreatedb AS create_db,
       rolcreaterole AS create_role, rolinherit AS inherit
FROM pg_roles WHERE rolname = any($1::text[])
"""
_EXTENSIONS = """
SELECT e.extname AS name, n.nspname AS schema, e.extversion AS version
FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname <> 'plpgsql'
ORDER BY e.extname
"""

_COMMANDS = {"*": "all", "r": "select", "a": "insert", "w": "update", "d": "delete"}
_VOLATILITY = {"v": "volatile", "s": "stable", "i": "immutable"}
_PARALLEL = {"s": "safe", "r": "restricted", "u": "unsafe"}
# Settings whose value is a list of names (printed as SQL as is); others are quoted.
_LIST_SETTINGS = {"search_path", "temp_tablespaces", "session_preload_libraries"}
# pg_trigger.tgtype bits
_ROW, _BEFORE, _INSTEAD = 1, 2, 64
_EVENTS = (("insert", 4), ("delete", 8), ("update", 16), ("truncate", 32))


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


def _opt_strs(value: object) -> list[str | None]:
    if not isinstance(value, list):
        raise TypeError(f"expected an array from the catalog, got {type(value).__name__}")
    return [None if v is None else str(v) for v in cast(list[object], value)]


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
    if serial is not None and default is not None and _NEXTVAL.fullmatch(default):
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


def _trigger_when(definition: str) -> str | None:
    """The WHEN condition of pg_get_triggerdef's text, without its outer parentheses."""
    start = definition.find(" WHEN (", definition.find(" FOR EACH "))
    if start < 0:
        return None
    end = definition.rfind(" EXECUTE FUNCTION ")
    return definition[start + len(" WHEN (") : end - 1]


def _trigger(row: _Row) -> TriggerSnap:
    tgtype = _int(row["type"])
    timing = "instead of" if tgtype & _INSTEAD else "before" if tgtype & _BEFORE else "after"
    trigger = TriggerSnap(
        name=str(row["name"]),
        timing=timing,
        events=[name for name, bit in _EVENTS if tgtype & bit],
        for_each="row" if tgtype & _ROW else "statement",
        function=key(str(row["function_schema"]), str(row["function_name"])),
        when=_trigger_when(str(row["definition"])),
    )
    if columns := _strs(row["columns"]):
        trigger["columns"] = columns
    if row["old_table"] is not None:
        trigger["old_table"] = str(row["old_table"])
    if row["new_table"] is not None:
        trigger["new_table"] = str(row["new_table"])
    return trigger


def _function(schema: str, name: str, row: _Row) -> FunctionSnap:
    language = str(row["language"])
    definition = _opt_str(row["definition"])
    fn = FunctionSnap(
        schema=schema,
        name=name,
        args=str(row["args"]),
        returns=str(row["returns"]),
        language=language,
        body=str(row["body"]) if definition is None else _sql_body(definition),
        volatility=_VOLATILITY[str(row["volatility"])],
        security_definer=bool(row["security_definer"]),
    )
    if row["strict"]:
        fn["strict"] = True
    if row["leakproof"]:
        fn["leakproof"] = True
    if (parallel := _PARALLEL[str(row["parallel"])]) != "unsafe":
        fn["parallel"] = parallel
    cost = row["cost"]
    if isinstance(cost, int | float) and float(cost) != default_cost(language):
        fn["cost"] = float(cost)
    if config := _strs(row["config"]):
        fn["settings"] = dict(_setting(c) for c in config)
    return fn


def _setting(config: str) -> tuple[str, str]:
    """An entry of pg_proc.proconfig ("work_mem=64MB") as name and SQL value."""
    name, _, value = config.partition("=")
    if name in _LIST_SETTINGS:
        return name, "''" if value == '""' else value
    return name, "'" + value.replace("'", "''") + "'"


def _sql_body(definition: str) -> str:
    """A SQL-standard body (RETURN ... / BEGIN ATOMIC ... END) from pg_get_functiondef: the
    lines after the header, whose option lines start with a space."""
    lines = definition.rstrip().split("\n")
    start = next((i for i, line in enumerate(lines) if i and not line.startswith(" ")), len(lines))
    return "\n".join(lines[start:])


def _index(row: _Row) -> IndexSnap:
    method = str(row["method"])
    index = IndexSnap(
        name=str(row["name"]),
        columns=_strs(row["columns"]),
        unique=bool(row["unique"]),
        where=_opt_str(row["predicate"]),
    )
    opclasses = cast(list[str | None], row["opclasses"])
    if row["has_expressions"]:
        index["columns"] = []
        keys = _strs(row["keys"])
        index["expression"] = ", ".join(
            f"{k} {op}" if op else k for k, op in zip(keys, opclasses, strict=True)
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


def _check_expression(definition: str) -> str:
    # pg_get_constraintdef: "CHECK ((title <> ''::text))" -> "(title <> ''::text)"
    expr = definition.removeprefix("CHECK ").removesuffix(" NOT VALID")
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
        if (partkey := _opt_str(t["partition_by"])) is not None:
            snap["partition_by"] = partkey
        by_oid[_int(t["oid"])] = snap
        snapshot["tables"][key(schema, name)] = snap
        _add_schema(snapshot, schema)

    for row in await conn.fetch_dicts(_PARTITIONS, [schema_list]):
        parent = snapshot["tables"].get(key(str(row["parent_schema"]), str(row["parent_name"])))
        if parent is not None:  # a partition whose parent is outside the schemas read
            parent.setdefault("partitions", {})[str(row["name"])] = str(row["bound"])

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
                expression = _check_expression(str(row["definition"]))
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
                table["foreign_keys"][name] = fk
            case "x":
                elements: list[ExclusionElementSnap] = []
                keys = _opt_strs(row["excl_keys"])
                defs = _strs(row["excl_defs"])
                for i, operator in enumerate(_strs(row["operators"])):
                    keyname = keys[i] if i < len(keys) else None
                    if keyname is None:
                        elements.append(ExclusionElementSnap(operator=operator, expression=defs[i]))
                    else:
                        elements.append(ExclusionElementSnap(operator=operator, column=keyname))
                excl = ExclusionSnap(name=name, method=str(row["method"]), elements=elements)
                if (where := _opt_str(row["excl_predicate"])) is not None:
                    excl["where"] = where
                if row["deferrable"]:
                    excl["deferrable"] = True
                if row["initially_deferred"]:
                    excl["initially_deferred"] = True
                table.setdefault("exclusions", {})[name] = excl
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

    for row in await conn.fetch_dicts(_TRIGGERS, [oids]):
        trigger = _trigger(row)
        by_oid[_int(row["table_oid"])].setdefault("triggers", {})[trigger["name"]] = trigger

    for row in await conn.fetch_dicts(_GRANTS, [oids]):
        by_oid[_int(row["oid"])].setdefault("grants", {})[str(row["role"])] = sorted(
            _strs(row["privileges"])
        )

    await _introspect_objects(conn, schema_list, snapshot, roles, version)

    for row in await conn.fetch_dicts(_ENUMS, [schema_list]):
        schema, name = str(row["schema"]), str(row["name"])
        enum = EnumSnap(schema=schema, name=name, values=_strs(row["values"]))
        snapshot["enums"][key(schema, name)] = enum
        _add_schema(snapshot, schema)

    domain_rows = await conn.fetch_dicts(_DOMAINS, [schema_list])
    # S8: domains used by the pulled columns but declared in another schema (outside
    # schema_filter) are part of the schema too, or the column reads as an opaque type and
    # the next generate drops the domain.
    referenced = {_int(row["oid"]) for row in await conn.fetch_dicts(_DOMAIN_OIDS, [oids])}
    referenced -= {_int(row["oid"]) for row in domain_rows}
    domain_rows += await conn.fetch_dicts(_DOMAINS_BY_OID, [list(referenced)]) if referenced else []
    domain_checks: dict[str, list[str]] = {}
    for row in await conn.fetch_dicts(_DOMAIN_CHECKS, [schema_list]):
        domain_checks.setdefault(key(str(row["schema"]), str(row["domain"])), []).append(
            _check_expression(str(row["definition"]))
        )
    if referenced:
        for row in await conn.fetch_dicts(_DOMAIN_CHECKS_BY_OID, [list(referenced)]):
            domain_checks.setdefault(key(str(row["schema"]), str(row["domain"])), []).append(
                _check_expression(str(row["definition"]))
            )
    for row in domain_rows:
        schema, name = str(row["schema"]), str(row["name"])
        domain = DomainSnap(schema=schema, name=name, base_type=str(row["base_type"]))
        if row["not_null"]:
            domain["not_null"] = True
        if (default := normalize_default(_opt_str(row["default"]))) is not None:
            domain["default"] = default
        if (collation := _opt_str(row["collation"])) not in (None, "default"):
            domain["collation"] = collation
        if checks := domain_checks.get(key(schema, name)):
            domain["checks"] = sorted(checks)
        snapshot.setdefault("domains", {})[key(schema, name)] = domain
        _add_schema(snapshot, schema)
    _drop_domain_collations(snapshot)

    for row in await conn.fetch_dicts(_COMPOSITES, [schema_list]):
        schema, name = str(row["schema"]), str(row["name"])
        composites = snapshot.setdefault("composites", {})
        composite = composites.setdefault(
            key(schema, name), CompositeSnap(schema=schema, name=name, fields=[])
        )
        composite["fields"].append(
            CompositeFieldSnap(name=str(row["field"]), type=str(row["type"]))
        )
        _add_schema(snapshot, schema)
    return snapshot


def _add_schema(snapshot: Snapshot, schema: str) -> None:
    if schema != DEFAULT_SCHEMA and schema not in snapshot["schemas"]:
        snapshot["schemas"].append(schema)


def _drop_domain_collations(snapshot: Snapshot) -> None:
    """A domain's collation is inherited by every column of that domain, but the column does
    not name it: read it only when it differs, so it does not show as drift forever."""
    domains = snapshot.get("domains", {})
    if not domains:
        return
    relations = [*snapshot["tables"].values(), *snapshot.get("views", {}).values()]
    for relation in relations:
        for column in relation["columns"].values():
            if column.get("collation") is None:
                continue
            domain = _domain_of(column["type"], domains)
            if domain is not None and domain.get("collation") == column.get("collation"):
                del column["collation"]


def _domain_of(sql_type: str, domains: dict[str, DomainSnap]) -> DomainSnap | None:
    if sql_type in domains:
        return domains[sql_type]
    matches = [d for d in domains.values() if d["name"] == sql_type]
    return matches[0] if len(matches) == 1 else None


async def _introspect_objects(
    conn: Connection, schemas: list[str], snapshot: Snapshot, roles: Sequence[str], version: int
) -> None:
    """Views, functions and extensions."""
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
        for row in await conn.fetch_dicts(_TRIGGERS, [list(views)]):
            trigger = _trigger(row)
            views[_int(row["table_oid"])].setdefault("triggers", {})[trigger["name"]] = trigger
        for row in await conn.fetch_dicts(_GRANTS, [list(views)]):
            views[_int(row["oid"])].setdefault("grants", {})[str(row["role"])] = sorted(
                _strs(row["privileges"])
            )

    for row in await conn.fetch_dicts(_FUNCTIONS, [schemas]):
        schema, name = str(row["schema"]), str(row["name"])
        fn = _function(schema, name, row)
        snapshot.setdefault("functions", {})[function_key(fn)] = fn
        _add_schema(snapshot, schema)

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

    for row in await conn.fetch_dicts(_EXTENSIONS, []):
        ext = ExtensionSnap(name=str(row["name"]), schema=str(row["schema"]))
        ext["version"] = str(row["version"])
        snapshot.setdefault("extensions", {})[ext["name"]] = ext


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
            if "opclasses" not in index and "expression" not in index:
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


async def _normalize_grants(snapshot: Snapshot, read_back: _ReadBack) -> None:
    """``all`` as the privileges it stands for on this server."""
    relations = [*snapshot["tables"].values(), *snapshot.get("views", {}).values()]
    if not any("all" in p for r in relations for p in r.get("grants", {}).values()):
        return
    temp = f"pg_temp.{quote(_NORM)}"
    row = await read_back(
        [f"CREATE TEMP TABLE {quote(_NORM)} ()", f"GRANT ALL ON {temp} TO PUBLIC"],
        "SELECT array_agg(DISTINCT lower(privilege_type)) AS p FROM aclexplode("
        f"(SELECT relacl FROM pg_class WHERE oid = to_regclass('{temp}'))) WHERE grantee = 0",
    )
    if row is None:
        return
    every = sorted(_strs(row["p"]))
    for relation in relations:
        for role, privileges in relation.get("grants", {}).items():
            if "all" in privileges:
                relation.setdefault("grants", {})[role] = every


class _ReadBack(Protocol):
    async def __call__(self, statements: list[str], query: str, /) -> _Row | None: ...


async def _normalize_domain_checks(snapshot: Snapshot, read_back: _ReadBack) -> None:
    """Domain CHECKs as PostgreSQL prints them: a domain whose text was written by hand
    (``VALUE ~ '@'``) can differ from ``((VALUE ~ '@'::text))``, and generate would then
    refuse the change. Create each domain in pg_temp and read its checks back."""
    domains = snapshot.get("domains", {})
    for domain in domains.values():
        if not domain.get("checks"):
            continue
        create = _create_domain_for_norm(domain)
        row = await read_back(
            [create],
            "SELECT array_agg(pg_get_constraintdef(c.oid) ORDER BY c.conname) AS d "
            "FROM pg_constraint c "
            f"WHERE c.contypid = to_regtype('pg_temp.{quote(_NORM)}') AND c.contype = 'c'",
        )
        if row is None or row["d"] is None:
            continue
        domain["checks"] = sorted(_check_expression(str(d)) for d in _strs(row["d"]))


def _create_domain_for_norm(domain: DomainSnap) -> str:
    """The CREATE DOMAIN written with a throwaway name in pg_temp."""
    parts = [f"CREATE DOMAIN pg_temp.{quote(_NORM)} AS {domain['base_type']}"]
    if collation := domain.get("collation"):
        parts.append(f"COLLATE {type_ident(collation)}")
    if default := domain.get("default"):
        parts.append(f"DEFAULT {default}")
    parts.extend(f"CHECK ({check})" for check in domain.get("checks", []))
    return " ".join(parts)


class _Undo(Exception):
    """Rolls back a savepoint whose work was only needed to read something."""


_NORM = "__mizzle_norm"


async def normalize_snapshot(conn: Connection, snapshot: Snapshot) -> Snapshot:
    """Let the server print view queries, function signatures, policy expressions and
    trigger conditions, by creating each as a temporary object and reading it back.

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
            await conn.execute("SET LOCAL check_function_bodies = off", [])
            await _normalize(out, read_back, version)
            raise _Undo
    except _Undo:
        pass
    return out


async def _normalize_constraint_expressions(
    table: TableSnap, like: str, norm: str, rel: str, read_back: _ReadBack
) -> None:
    """CHECK and exclusion expressions as PostgreSQL prints them.

    A CHECK written by hand (``status <> ''``) reads back as ``(status <> ''::text)``, and
    a hand-written exclusion predicate gains its outer parentheses and casts. Without this
    ``generate`` after ``pull`` (or a hand-written constraint) drops and re-adds it forever.
    All the table's constraints are created in one savepoint, then read back together.
    """
    checks = {n: c for n, c in table.get("checks", {}).items() if "expression" in c}
    exclusions = {n: x for n, x in table.get("exclusions", {}).items() if "where" in x}
    if not checks and not exclusions:
        return
    statements = [like]
    for name, check in checks.items():
        add = f"ALTER TABLE {norm} ADD CONSTRAINT {quote(name)} CHECK ({check['expression']})"
        statements.append(add + (" NOT VALID" if check.get("not_valid") else ""))
    for name, exclusion in exclusions.items():
        keys: list[str] = []
        for element in exclusion["elements"]:
            column = element.get("column")
            key = quote(column) if column is not None else f"({element.get('expression', '')})"
            keys.append(f"{key} WITH {element['operator']}")
        elements = ", ".join(keys)
        statements.append(
            f"ALTER TABLE {norm} ADD CONSTRAINT {quote(name)} "
            f"EXCLUDE USING {exclusion['method']} ({elements}) WHERE ({exclusion['where']})"
        )
    row = await read_back(
        statements,
        "SELECT "
        "ARRAY(SELECT c.conname FROM pg_constraint c "
        f"WHERE c.conrelid = {rel} AND c.contype = 'c' ORDER BY c.conname) AS check_names, "
        "ARRAY(SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        f"WHERE c.conrelid = {rel} AND c.contype = 'c' ORDER BY c.conname) AS check_defs, "
        "ARRAY(SELECT c.conname FROM pg_constraint c "
        f"WHERE c.conrelid = {rel} AND c.contype = 'x' ORDER BY c.conname) AS excl_names, "
        "ARRAY(SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_constraint c "
        "JOIN pg_index i ON i.indexrelid = c.conindid "
        f"WHERE c.conrelid = {rel} AND c.contype = 'x' ORDER BY c.conname) AS excl_wheres",
    )
    if row is None:
        return
    for name, definition in zip(_strs(row["check_names"]), _strs(row["check_defs"]), strict=True):
        if name in checks:
            checks[name]["expression"] = _check_expression(definition)
    for name, predicate in zip(_strs(row["excl_names"]), _strs(row["excl_wheres"]), strict=True):
        if name in exclusions:
            exclusions[name]["where"] = predicate


async def _normalize(snapshot: Snapshot, read_back: _ReadBack, version: int) -> None:
    norm = f"pg_temp.{quote(_NORM)}"
    # Looked up when each query runs (a missing object reads as NULL, never as text).
    rel, proc = f"to_regclass('{norm}')", f"to_regproc('{norm}')"
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

    functions = snapshot.get("functions", {})
    for fn in functions.values():
        # Settings are printed back; a SQL-standard body is parsed, so it needs the real one.
        plain = copy.copy(fn)
        plain.pop("leakproof", None)  # LEAKPROOF needs a superuser
        options = function_options(plain)
        body = f"\n{fn['body']}" if sql_standard_body(fn) else " AS $$ $$"
        create = (
            f"CREATE FUNCTION {norm}({fn['args']}) RETURNS {fn['returns']} "
            f"LANGUAGE {type_ident(fn['language'])}{options}{body}"
        )
        row = await read_back(
            [create],
            f"SELECT n.nspname AS schema, p.proname AS name, {_FUNCTION_COLUMNS} "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"JOIN pg_language l ON l.oid = p.prolang WHERE p.oid = {proc}",
        )
        if row is None:
            continue
        printed = _function(fn["schema"], fn["name"], row)
        fn["args"], fn["returns"] = printed["args"], printed["returns"]
        if "settings" in printed:
            fn["settings"] = printed["settings"]
        if row["definition"] is not None:
            fn["body"] = printed["body"]
    if functions:  # keyed by the arguments as printed
        snapshot["functions"] = {function_key(f): f for f in functions.values()}

    await _normalize_indexes(snapshot, read_back, version)
    await _normalize_grants(snapshot, read_back)
    await _normalize_domain_checks(snapshot, read_back)

    for table in snapshot["tables"].values():
        like = (
            f"CREATE TEMP TABLE {quote(_NORM)} (LIKE {qualified(table['schema'], table['name'])})"
        )
        if (partition_by := table.get("partition_by")) is not None:
            # The key as PostgreSQL prints it, so RANGE (at) matches range (at).
            row = await read_back(
                [f"{like} PARTITION BY {partition_by}"],
                f"SELECT pg_get_partkeydef({rel}) AS k",
            )
            if row is not None and row["k"] is not None:
                table["partition_by"] = str(row["k"])
        if (partitions := table.get("partitions")) and partition_by is not None:
            # Create each partition (rolled back after) so a timestamptz bound reads back as
            # the database prints it, not as the user spelled it. Its parent is a temporary
            # copy: a partition of the real parent would overlap the one already there.
            columns = ", ".join(
                f"{quote(c['name'])} {SERIAL_TYPES.get(c['type'], c['type'])}"
                for c in table["columns"].values()
            )
            parent = f"CREATE TEMP TABLE {quote(_NORM)} ({columns}) PARTITION BY {partition_by}"
            part = quote(f"{_NORM}_part")
            for pname, bound in partitions.items():
                row = await read_back(
                    [parent, f"CREATE TEMP TABLE {part} PARTITION OF {norm} {bound}"],
                    "SELECT pg_get_expr(c.relpartbound, c.oid) AS b FROM pg_class c "
                    f"WHERE c.oid = to_regclass('pg_temp.{part}')",
                )
                if row is not None and row["b"] is not None:
                    partitions[pname] = str(row["b"])
        for policy in table.get("policies", {}).values():
            create = f"CREATE POLICY p ON {norm} FOR {policy['command'].upper()}"
            if policy["using"] is not None:
                create += f" USING ({policy['using']})"
            if policy["with_check"] is not None:
                create += f" WITH CHECK ({policy['with_check']})"
            row = await read_back(
                [like, create],
                "SELECT pg_get_expr(polqual, polrelid) AS using, "
                "pg_get_expr(polwithcheck, polrelid) AS with_check "
                f"FROM pg_policy WHERE polrelid = {rel}",
            )
            if row is not None:
                policy["using"], policy["with_check"] = (
                    _opt_str(row["using"]),
                    _opt_str(row["with_check"]),
                )
        for trigger in table.get("triggers", {}).values():
            if trigger["when"] is None:
                continue
            events = " OR ".join(e.upper() for e in trigger["events"])
            row = await read_back(
                [
                    like,
                    f"CREATE FUNCTION {norm}() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RETURN NULL; END $$",
                    f"CREATE TRIGGER t {trigger['timing'].upper()} {events} ON {norm} "
                    f"FOR EACH {trigger['for_each'].upper()} WHEN ({trigger['when']}) "
                    f"EXECUTE FUNCTION {norm}()",
                ],
                f"SELECT pg_get_triggerdef(oid) AS d FROM pg_trigger WHERE tgrelid = {rel}",
            )
            if row is not None and row["d"] is not None:
                trigger["when"] = _trigger_when(str(row["d"]))
        await _normalize_constraint_expressions(table, like, norm, rel, read_back)

"""Read a live PostgreSQL schema into a Snapshot (used by push, pull and studio)."""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from typing import Protocol, cast

from mizzle.core.errors import DatabaseError, SchemaError
from mizzle.core.snapshot import (
    ColumnSnap,
    EnumSnap,
    ExtensionSnap,
    FunctionSnap,
    GeneratedSnap,
    IdentitySnap,
    IndexSnap,
    PolicySnap,
    RlsSnap,
    RoleSnap,
    Snapshot,
    TableSnap,
    TriggerSnap,
    ViewSnap,
    empty_snapshot,
    key,
)
from mizzle.dialects.base import Connection
from mizzle.dialects.postgres.ddl import (
    DEFAULT_SCHEMA,
    SERIAL_TYPES,
    normalize_default,
    qualified,
    quote,
    type_ident,
)

MIGRATIONS_TABLE = "__mizzle_migrations"

_ACTIONS = {"a": None, "r": "restrict", "c": "cascade", "n": "set null", "d": "set default"}
_NEXTVAL = re.compile(r"nextval\('.*'::regclass\)")

_TABLES = """
SELECT c.oid, n.nspname AS schema, c.relname AS name,
       c.relrowsecurity AS rls, c.relforcerowsecurity AS rls_force
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY($1::text[]) AND c.relname <> $2
  AND NOT c.relispartition
ORDER BY n.nspname, c.relname
"""

_COLUMNS = """
SELECT a.attrelid AS table_oid, a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS default,
       a.attidentity::text AS identity, a.attgenerated::text AS generated,
       s.seqstart AS identity_start, s.seqincrement AS identity_increment
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
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
       -- Each key on its own, straight from the catalog (no parsing of definition text).
       ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
             FROM generate_series(1, i.indnkeyatts) AS k ORDER BY k) AS keys,
       am.amname AS method,
       ARRAY(SELECT CASE WHEN oc.opcdefault THEN NULL ELSE oc.opcname END
             FROM unnest(i.indclass::oid[]) WITH ORDINALITY k(o, ord)
             JOIN pg_opclass oc ON oc.oid = k.o ORDER BY k.ord) AS opclasses,
       coalesce(ic.reloptions, '{}') AS options
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

_FUNCTIONS = f"""
SELECT n.nspname AS schema, p.proname AS name, pg_get_function_arguments(p.oid) AS args,
       pg_get_function_result(p.oid) AS returns, l.lanname AS language, p.prosrc AS body,
       p.provolatile::text AS volatility, p.prosecdef AS security_definer
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
       pg_get_triggerdef(t.oid) AS definition
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


def _column(row: _Row) -> ColumnSnap:
    name, sql_type, not_null = str(row["name"]), str(row["type"]), bool(row["not_null"])
    default = _opt_str(row["default"])
    serial = next((s for s, base in SERIAL_TYPES.items() if base == sql_type), None)
    if serial is not None and default is not None and _NEXTVAL.fullmatch(default):
        return ColumnSnap(name=name, type=serial, not_null=not_null, default=None)
    if row["generated"] in ("s", "v"):  # pg_attrdef holds the expression, not a default
        assert default is not None
        column = ColumnSnap(name=name, type=sql_type, not_null=not_null, default=None)
        column["generated"] = GeneratedSnap(expression=default, stored=row["generated"] == "s")
        return column
    column = ColumnSnap(
        name=name,
        type=sql_type,
        not_null=not_null,
        default=normalize_default(default),
    )
    if row["identity"] in ("a", "d"):
        column["identity"] = IdentitySnap(
            always=row["identity"] == "a",
            start=_int(row["identity_start"]),
            increment=_int(row["identity_increment"]),
        )
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
    return TriggerSnap(
        name=str(row["name"]),
        timing=timing,
        events=[name for name, bit in _EVENTS if tgtype & bit],
        for_each="row" if tgtype & _ROW else "statement",
        function=key(str(row["function_schema"]), str(row["function_name"])),
        when=_trigger_when(str(row["definition"])),
    )


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
    if options := _strs(row["options"]):
        index["options"] = dict(o.split("=", 1) for o in options)
    return index


def _check_expression(definition: str) -> str:
    # pg_get_constraintdef: "CHECK ((title <> ''::text))" -> "(title <> ''::text)"
    expr = definition.removeprefix("CHECK ").removesuffix(" NOT VALID")
    return expr[1:-1] if expr.startswith("(") and expr.endswith(")") else expr


async def introspect(
    conn: Connection, schemas: Sequence[str] = (DEFAULT_SCHEMA,), roles: Sequence[str] = ()
) -> Snapshot:
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
        if t["rls"] or t["rls_force"]:
            snap["rls"] = RlsSnap(enabled=bool(t["rls"]), force=bool(t["rls_force"]))
        by_oid[_int(t["oid"])] = snap
        snapshot["tables"][key(schema, name)] = snap
        _add_schema(snapshot, schema)

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

    await _introspect_objects(conn, schema_list, snapshot, roles)

    for row in await conn.fetch_dicts(_ENUMS, [schema_list]):
        schema, name = str(row["schema"]), str(row["name"])
        enum = EnumSnap(schema=schema, name=name, values=_strs(row["values"]))
        snapshot["enums"][key(schema, name)] = enum
    return snapshot


def _add_schema(snapshot: Snapshot, schema: str) -> None:
    if schema != DEFAULT_SCHEMA and schema not in snapshot["schemas"]:
        snapshot["schemas"].append(schema)


async def _introspect_objects(
    conn: Connection, schemas: list[str], snapshot: Snapshot, roles: Sequence[str]
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
        for row in await conn.fetch_dicts(_INDEXES, [list(views)]):
            index = _index(row)
            views[_int(row["table_oid"])].setdefault("indexes", {})[index["name"]] = index

    for row in await conn.fetch_dicts(_FUNCTIONS, [schemas]):
        schema, name = str(row["schema"]), str(row["name"])
        # ponytail: one function per name; overloads after the first are not managed
        snapshot.setdefault("functions", {}).setdefault(
            key(schema, name),
            FunctionSnap(
                schema=schema,
                name=name,
                args=str(row["args"]),
                returns=str(row["returns"]),
                language=str(row["language"]),
                body=str(row["body"]),
                volatility=_VOLATILITY[str(row["volatility"])],
                security_definer=bool(row["security_definer"]),
            ),
        )
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


class _ReadBack(Protocol):
    async def __call__(self, statements: list[str], query: str, /) -> _Row | None: ...


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
            await _normalize(out, read_back)
            raise _Undo
    except _Undo:
        pass
    return out


async def _normalize(snapshot: Snapshot, read_back: _ReadBack) -> None:
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

    for fn in snapshot.get("functions", {}).values():
        create = (
            f"CREATE FUNCTION {norm}({fn['args']}) RETURNS {fn['returns']} "
            f"LANGUAGE {type_ident(fn['language'])} AS $$ $$"
        )
        row = await read_back(
            [create],
            f"SELECT pg_get_function_arguments({proc}) AS args, "
            f"pg_get_function_result({proc}) AS returns",
        )
        if row is not None and row["returns"] is not None:
            fn["args"], fn["returns"] = str(row["args"]), str(row["returns"])

    for table in snapshot["tables"].values():
        like = (
            f"CREATE TEMP TABLE {quote(_NORM)} (LIKE {qualified(table['schema'], table['name'])})"
        )
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

"""PostgreSQL DDL: normalized types/defaults for snapshots, and SQL for migration steps."""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from typing import cast

from mizzle.core.compiler import no_nul
from mizzle.core.errors import MigrationError, SchemaError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import MISSING, ColumnBase
from mizzle.core.snapshot import (
    ColumnSnap,
    ForeignKeySnap,
    GeneratedSnap,
    IdentitySnap,
    IndexSnap,
    PolicySnap,
    RoleSnap,
    TableSnap,
    UniqueSnap,
    keyword_default,
    sequence_defaults,
)
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import (
    AddCheck,
    AddColumn,
    AddEnumValue,
    AddForeignKey,
    AddPrimaryKey,
    AddUnique,
    AlterColumnCollation,
    AlterColumnDefault,
    AlterColumnGenerated,
    AlterColumnIdentity,
    AlterColumnNullability,
    AlterColumnType,
    AlterPolicy,
    AlterRole,
    AlterTableStorage,
    CreateEnum,
    CreateIndex,
    CreatePolicy,
    CreateRole,
    CreateSchema,
    CreateTable,
    CreateView,
    DropColumn,
    DropConstraint,
    DropEnum,
    DropIndex,
    DropPolicy,
    DropRole,
    DropTable,
    DropView,
    RenameColumn,
    RenameConstraint,
    RenameIndex,
    RenameTable,
    RestoreGrants,
    SaveGrants,
    SetColumnComment,
    SetRowLevelSecurity,
    SetTableComment,
    SetTableLogged,
    SetTableSchema,
    Statement,
    Step,
)
from mizzle.dialects.postgres.compiler import PostgresCompiler, temporal_text

DEFAULT_SCHEMA = "public"
MAX_IDENTIFIER = 63  # PostgreSQL silently truncates longer names

SERIAL_TYPES = {"serial": "integer", "bigserial": "bigint", "smallserial": "smallint"}

_TYPE_ALIASES = {
    "varchar": "character varying",
    "char": "character",
    "bpchar": "character",
    "timestamptz": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "timetz": "time with time zone",
    "time": "time without time zone",
    "int": "integer",
    "int4": "integer",
    "int8": "bigint",
    "int2": "smallint",
    "float8": "double precision",
    "float4": "real",
    "bool": "boolean",
    "varbit": "bit varying",
}
_PLAIN_IDENT = re.compile(r"[a-z_][a-z0-9_$]*")
# Keywords quote_ident()/format_type() quote (K4-8): PostgreSQL 18's pg_get_keywords()
# where catcode <> 'U'; older servers have a few less (json_table, merge_action), which
# introspect quotes back with requote_type() (K4-22).
_KEYWORDS = frozenset(
    """
    all analyse analyze and any array as asc asymmetric authorization between bigint
    binary bit boolean both case cast char character check coalesce collate collation
    column concurrently constraint create cross current_catalog current_date
    current_role current_schema current_time current_timestamp current_user dec decimal
    default deferrable desc distinct do else end except exists extract false fetch float
    for foreign freeze from full grant greatest group grouping having ilike in initially
    inner inout int integer intersect interval into is isnull join json json_array
    json_arrayagg json_exists json_object json_objectagg json_query json_scalar
    json_serialize json_table json_value lateral leading least left like limit localtime
    localtimestamp merge_action national natural nchar none normalize not notnull null
    nullif numeric offset on only or order out outer overlaps overlay placing position
    precision primary real references returning right row select session_user setof
    similar smallint some substring symmetric system_user table tablesample then time
    timestamp to trailing treat trim true union unique user using values varchar
    variadic verbose when where window with xmlattributes xmlconcat xmlelement xmlexists
    xmlforest xmlnamespaces xmlparse xmlpi xmlroot xmlserialize xmltable
    """.split()  # noqa: SIM905
)
# A type name is dotted parts, each plain or quoted: ::"App"."Mood" too (K4-4).
_TYPE_PART = r'(?:"(?:[^"]|"")+"|[a-z_][a-z0-9_ ]*)'
_TRAILING_CAST = re.compile(rf"::{_TYPE_PART}(?:\.{_TYPE_PART})*(?:\(\d+(?:,\s*\d+)?\))?(?:\[\])*$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:e[-+]?\d+)?", re.IGNORECASE)
# Types a bare number cannot be assigned to: '5'::jsonb and '0101'::"bit" keep their quotes.
_QUOTED_CAST = re.compile(r'::(?:jsonb?|"bit"|bit(?: varying)?|varbit)(?:\(\d+\))?$')


def quote(ident: str) -> str:
    return '"' + no_nul(ident).replace('"', '""') + '"'


# Studio renders with ``qualify=True``: a bare public name resolved through the role's
# search_path, so a "$user" schema's same-named table was the one changed (S8-2).
_QUALIFY = ContextVar("mizzle_qualify", default=False)


def qualified(schema: str, name: str) -> str:
    if schema == DEFAULT_SCHEMA and not _QUALIFY.get():
        return quote(name)
    return f"{quote(schema)}.{quote(name)}"


def truncate_name(name: str) -> str:
    return name.encode()[:MAX_IDENTIFIER].decode(errors="ignore")


def type_ident(name: str) -> str:
    """A type name as format_type() prints it: quoted only when necessary."""
    return name if _PLAIN_IDENT.fullmatch(name) and name not in _KEYWORDS else quote(name)


_IDENT_PART = r'"(?:[^"]|"")+"|[a-z_][a-z0-9_$]*'
_TYPE_PATH = re.compile(rf"(?:({_IDENT_PART})\.)?({_IDENT_PART})((?:\[\])*)")


def requote_type(text: str, server_keywords: frozenset[str]) -> str:
    """A type name as format_type() printed it, quoted as type_ident() quotes it: a server
    older than PostgreSQL 18 leaves a few of _KEYWORDS bare (json_table before 17), so a
    type named so differed from the schema there (K4-22)."""
    match = _TYPE_PATH.fullmatch(text)
    if match is None:
        return text
    schema, name, dims = cast(tuple[str | None, str, str], match.groups())
    if schema is None and name == "json":  # the built-in type, bare on every server
        return text
    stale = _KEYWORDS - server_keywords
    name = quote(name) if name in stale else name
    if schema is None:
        return name + dims
    return f"{quote(schema) if schema in stale else schema}.{name}{dims}"


# A string literal or quoted name (kept as is), or the type of a ::cast.
_CAST = re.compile(rf"""'(?:[^']|'')*'|"(?:[^"]|"")*"|::(?P<type>{_TYPE_PATH.pattern})""")


def requote_casts(text: str, server_keywords: frozenset[str]) -> str:
    """Default text with the type of each ``::`` cast requoted (K4-29)."""

    def one(match: re.Match[str]) -> str:
        cast_type = match["type"]
        return match[0] if cast_type is None else "::" + requote_type(cast_type, server_keywords)

    return _CAST.sub(one, text)


_TYPE_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_$ .]*?)\s*(\(.*\))?")


def normalize_type(sql_type: str) -> str:
    """``varchar(255)`` -> ``character varying(255)``, ``numeric(10, 2)`` -> ``numeric(10,2)``,
    ``integer[][]`` -> ``integer[]`` (PostgreSQL has one array type per element; Q5-8).

    Offline, so conservative: what the database would print is read from the database
    (``normalize_snapshot``). Unquoted names fold to lower case, and only an unqualified
    built-in name takes its canonical spelling; a qualified name keeps its schema
    (``public.int4`` can be a domain named int4, K7-7), and a spelling not known here is
    left as written."""
    sql_type, array = _array_base(sql_type.strip())
    match = _TYPE_NAME.fullmatch(sql_type.strip())
    if match is None:
        return sql_type + array
    base, args = " ".join(match.group(1).lower().split()), match.group(2) or ""
    args = args.replace(" ", "")
    if "." in base:
        return base + args + array
    base = _TYPE_ALIASES.get(base, base)
    if args and base.startswith(("timestamp with", "time with")):  # also "...without"
        base = base.replace(" with", f"{args} with")
        args = ""
    return base + args + array


# Built-in type names a bare spelling may resolve to (with _TYPE_ALIASES and _KEYWORDS).
_BUILTIN_TYPES = frozenset(
    """
    text uuid bytea inet cidr macaddr macaddr8 date interval json jsonb xml money name oid
    tsvector tsquery point line lseg box path polygon circle pg_lsn int4range int8range
    numrange tsrange tstzrange daterange record regclass regtype
    """.split()  # noqa: SIM905
)


def same_type(a: str, b: str) -> bool:
    """Whether two spellings name one type, offline: an older mizzle stored ``posint`` and
    ``integer`` for ``custom('public.posint')`` and ``custom('pg_catalog.int4')`` (K8-2).
    ``public.X`` is bare X unless X names a built-in (``public.int4`` can be a domain,
    K7-7); ``pg_catalog.X`` is X as a built-in."""
    return a == b or _comparable(a) == _comparable(b)


def _comparable(sql_type: str) -> str:
    sql_type = normalize_type(sql_type)
    base, array = _array_base(sql_type)
    schema, dot, rest = base.partition(".")
    if dot and schema == "pg_catalog":
        return normalize_type(rest) + array
    if dot and schema == DEFAULT_SCHEMA and "." not in rest:
        name = rest.split("(")[0].strip()
        builtin = {*_TYPE_ALIASES, *_TYPE_ALIASES.values(), *_KEYWORDS, *_BUILTIN_TYPES}
        if name not in builtin:
            return rest + array
    return sql_type


def _array_base(sql_type: str) -> tuple[str, str]:
    """``integer[][]`` -> ``("integer", "[]")``."""
    base = sql_type
    while base.endswith("[]"):
        base = base[:-2]
    return base, "[]" if base != sql_type else ""


def column_type(column: ColumnBase) -> str:
    if column.enum is not None:
        base, array = _array_base(column.sql_type)
        name = type_ident(base)
        if column.enum_schema not in (None, DEFAULT_SCHEMA):  # as format_type() prints it
            name = f"{type_ident(column.enum_schema)}.{name}"
        return name + array
    return normalize_type(column.sql_type)


def normalize_default(text: str | None) -> str | None:
    """Make a default comparable: ``'user'::role`` -> ``'user'``, ``'-1'::integer`` -> ``-1``."""
    if text is None:
        return None
    text = _TIMESTAMPTZ_DEFAULT.sub(_utc_timestamptz, text.strip())
    keep_quotes = _QUOTED_CAST.search(text) is not None  # (Q10, Q12)
    while True:
        stripped = _TRAILING_CAST.sub("", text)
        if stripped.startswith("(") and stripped.endswith(")") and stripped.count("(") == 1:
            stripped = stripped[1:-1]
        if stripped == text:
            break
        text = stripped
    quoted = len(text) >= 2 and text[0] == text[-1] == "'"
    if quoted and not keep_quotes and _NUMBER.fullmatch(text[1:-1]):
        return text[1:-1]
    return keyword_default(text)  # PostgreSQL prints current_user as CURRENT_USER


def compiler(casing: Casing | None) -> PostgresCompiler:
    return PostgresCompiler(casing, inline_literals=True, qualify_columns=False)


def expression(node: Node, casing: Casing | None = None) -> str:
    return compiler(casing).compile(node).sql


def view_definition(query: Node, casing: Casing | None = None) -> str:
    """A view's query as SQL; columns stay qualified, since it may join tables."""
    compiled = PostgresCompiler(casing, inline_literals=True).compile(query)
    if compiled.params:
        raise MigrationError("a view query cannot have placeholders or parameters")
    return compiled.sql


def column_default(column: ColumnBase, casing: Casing | None = None) -> str | None:
    if column.default_value is MISSING:
        return None
    value = column.default_value
    c = compiler(casing)
    if isinstance(value, Node):
        return normalize_default(c.visit(value))
    return normalize_default(c.column_literal(column, column.encode(value)))


# '2024-01-02 08:49:05+05:45'::timestamp with time zone: printed in the session time zone.
_TIMESTAMPTZ_DEFAULT = re.compile(r"'([^']+)'::timestamp(?:\(\d\))? with time zone")


def _utc_timestamptz(match: re.Match[str]) -> str:
    try:
        value = datetime.datetime.fromisoformat(match.group(1))
    except ValueError:  # BC dates, infinity: leave as printed
        return match.group(0)
    if value.tzinfo is None:
        return match.group(0)
    return f"'{temporal_text(value)}'"


# -- rendering ------------------------------------------------------------------------


def _columns(names: Sequence[str]) -> str:
    return ", ".join(quote(n) for n in names)


def _identity(identity: IdentitySnap, sql_type: str) -> str:
    text = f"GENERATED {'ALWAYS' if identity['always'] else 'BY DEFAULT'} AS IDENTITY"
    options = _sequence_options(identity, None, sql_type)
    return f"{text} ({' '.join(options)})" if options else text


def _sequence_options(new: IdentitySnap, old: IdentitySnap | None, sql_type: str) -> list[str]:
    """Options that differ from ``old`` (or from the database defaults)."""
    creating = old is None
    if old is None:  # what the database picks when nothing is given
        low, high = sequence_defaults(sql_type, new["increment"])
        old = IdentitySnap(
            always=new["always"],
            start=new.get("min_value", low) if new["increment"] > 0 else new.get("max_value", high),
            increment=1,
        )
    parts: list[str] = []
    if new["increment"] != old["increment"]:
        parts.append(f"INCREMENT BY {new['increment']}")
    # NO MINVALUE / NO MAXVALUE pick the bounds for the direction at that time.
    flipped = creating is False and (new["increment"] > 0) != (old["increment"] > 0)
    for option, word in (("min_value", "MINVALUE"), ("max_value", "MAXVALUE")):
        value = new.get(option)
        if value is not None and (value != old.get(option) or flipped):
            parts.append(f"{word} {value}")
        elif value is None and (option in old or flipped):  # back to the default
            parts.append(f"NO {word}")
    if new.get("cache", 1) != old.get("cache", 1):
        parts.append(f"CACHE {new.get('cache', 1)}")
    if new.get("cycle", False) != old.get("cycle", False):
        parts.append("CYCLE" if new.get("cycle") else "NO CYCLE")
    if new["start"] != old["start"]:
        parts.append(f"START WITH {new['start']}")
    return parts


def column_definition(column: ColumnSnap) -> str:
    parts = [quote(column["name"]), column["type"]]
    if collation := column.get("collation"):
        parts.append(f"COLLATE {quote(collation)}")
    if generated := column.get("generated"):
        kind = "STORED" if generated["stored"] else "VIRTUAL"
        parts.append(f"GENERATED ALWAYS AS ({generated['expression']}) {kind}")
    if identity := column.get("identity"):
        parts.append(_identity(identity, column["type"]))
    if column["not_null"]:
        parts.append("NOT NULL")
    if column["default"] is not None:
        parts.append(f"DEFAULT {default_sql(column['default'])}")
    return " ".join(parts)


def default_sql(text: str) -> str:
    """``text`` ready for DEFAULT, which takes no AT TIME ZONE, AND, IS ... unless in
    parentheses (K11): any space outside quotes and brackets adds them."""
    depth, in_quote = 0, ""
    for ch in text:
        if in_quote:
            in_quote = "" if ch == in_quote else in_quote
        elif ch in "'\"":
            in_quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch.isspace() and depth == 0:
            return f"({text})"
    return text


def _foreign_key(fk: ForeignKeySnap) -> str:
    text = (
        f"CONSTRAINT {quote(fk['name'])} FOREIGN KEY ({_columns(fk['columns'])}) "
        f"REFERENCES {qualified(fk['ref_schema'], fk['ref_table'])} ({_columns(fk['ref_columns'])})"
    )
    if fk["on_delete"]:
        text += f" ON DELETE {fk['on_delete'].upper()}"
    if fk["on_update"]:
        text += f" ON UPDATE {fk['on_update'].upper()}"
    text += _deferred(fk)
    if fk.get("not_valid"):
        text += " NOT VALID"
    return text


def _unique_fragment(u: UniqueSnap) -> str:
    return "UNIQUE NULLS NOT DISTINCT " if u.get("nulls_not_distinct") else "UNIQUE "


def _deferred(snap: Mapping[str, object]) -> str:
    if snap.get("initially_deferred"):
        return " DEFERRABLE INITIALLY DEFERRED"
    return " DEFERRABLE" if snap.get("deferrable") else ""


def _create_table(table: TableSnap) -> str:
    lines = [column_definition(c) for c in table["columns"].values()]
    if pk := table["primary_key"]:
        lines.append(
            f"CONSTRAINT {quote(pk['name'])} PRIMARY KEY ({_columns(pk['columns'])}){_deferred(pk)}"
        )
    lines += [
        f"CONSTRAINT {quote(u['name'])} {_unique_fragment(u)}({_columns(u['columns'])})"
        f"{_deferred(u)}"
        for u in table["uniques"].values()
    ]
    lines += [
        f"CONSTRAINT {quote(c['name'])} CHECK ({c['expression']})"
        for c in table["checks"].values()
        if not c.get("not_valid")  # NOT VALID is only valid on ALTER TABLE ADD CONSTRAINT
    ]
    body = ",\n    ".join(lines)
    kind = "CREATE UNLOGGED TABLE" if table.get("unlogged") else "CREATE TABLE"
    text = f"{kind} {qualified(table['schema'], table['name'])} (\n    {body}\n)"
    if options := table.get("options"):
        text += " WITH (" + ", ".join(_storage_option(k, v) for k, v in options.items()) + ")"
    return text


def create_index(schema: str, table: str, index: IndexSnap) -> str:
    unique = "UNIQUE " if index["unique"] else ""
    keys = index.get("expression")
    if keys is None:
        opclasses = index.get("opclasses") or [None] * len(index["columns"])
        keys = ", ".join(
            quote(c) + (f" {op}" if op else "")
            for c, op in zip(index["columns"], opclasses, strict=True)
        )
    using = f" USING {index['method']}" if "method" in index else ""
    text = f"CREATE {unique}INDEX {quote(index['name'])} ON {qualified(schema, table)}{using}"
    text += f" ({keys})"
    if index["unique"] and index.get("nulls_not_distinct"):
        text += " NULLS NOT DISTINCT"
    if options := index.get("options"):
        text += (
            " WITH ("
            + ", ".join(f"{_option_key(k)} = {literal(v)}" for k, v in options.items())
            + ")"
        )
    return text + (f" WHERE {index['where']}" if index["where"] else "")


def _role_options(role: RoleSnap) -> str:
    return " ".join(
        word if on else f"NO{word}"
        for word, on in (
            ("LOGIN", role["login"]),
            ("CREATEDB", role["create_db"]),
            ("CREATEROLE", role["create_role"]),
            ("INHERIT", role["inherit"]),
        )
    )


def literal(text: str) -> str:
    """``text`` as an SQL string literal, read the same with standard_conforming_strings
    off. There a backslash in a plain '...' escapes the next character, so a name ending in
    one ran the rest of the statement as SQL (S7-2): such text is an E'...' string instead."""
    body = no_nul(text).replace("'", "''")
    if "\\" in body:
        return "E'" + body.replace("\\", "\\\\") + "'"
    return f"'{body}'"


# What a statement's text holds, left to right: only a plain '...' string is rewritten.
_LEXEME = re.compile(
    r"""(?P<keep>(?:[Ee]|[Uu]&)'(?:[^'\\]|\\.|'')*'"""  # E'..' / U&'..' (backslash escapes)
    r"""|"(?:[^"]|"")*"|--[^\n]*|/\*.*?\*/"""  # a quoted name, a comment
    r"""|\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*|)\$.*?\$(?P=tag)\$"""  # a dollar-quoted body
    r"""|[^\W\d][\w$]*)"""  # a word, so name'...' is not an E'' string
    r"""|'(?P<plain>(?:[^']|'')*)'""",
    re.DOTALL,
)


def escape_backslashes(sql: str) -> str:
    """``sql`` read the same with standard_conforming_strings off: each plain '...' string
    holding a backslash becomes E'...' (as literal() writes it, K8-3). Defaults, checks,
    views and policies are SQL text, compiled or printed by the server (pulled), where a
    backslash in '...' was lost or escaped the closing quote; other text is unchanged."""

    def one(match: re.Match[str]) -> str:
        plain = match["plain"]
        if plain is None or "\\" not in plain:
            return match[0]
        glued = re.fullmatch(r"[\w$]", sql[match.start() - 1 : match.start()]) is not None
        return (" E'" if glued else "E'") + plain.replace("\\", "\\\\") + "'"

    return _LEXEME.sub(one, sql)


# Storage parameter names are identifiers, optionally in the toast namespace. Values are a
# number, a boolean or a quoted literal, so a value can never end the WITH (...) clause.
_OPTION_KEY = re.compile(r"(?:toast\.)?[a-z_][a-z0-9_]*\Z", re.IGNORECASE)
_OPTION_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


def _option_key(name: str) -> str:
    if not _OPTION_KEY.fullmatch(str(name)):
        raise SchemaError(f"invalid storage parameter name {name!r}")
    return str(name)


def _storage_option(name: str, value: object) -> str:
    key = _option_key(name)
    text = "true" if value is True else "false" if value is False else str(value)
    if _OPTION_NUMBER.fullmatch(text) or text in ("true", "false"):
        return f"{key}={text}"
    return f"{key}={literal(text)}"


def _alter_identity(step: AlterColumnIdentity, sql_type: str) -> str:
    col = f"ALTER TABLE {qualified(step.schema, step.table)} ALTER COLUMN {quote(step.column)}"
    old, new = step.old, step.new
    if new is None:
        return f"{col} DROP IDENTITY"
    if old is None:
        add = f"{col} ADD {_identity(new, sql_type)}"
        if new["increment"] < 0:
            return add
        # The sequence starts at START even when the column holds rows already, so the
        # next insert hit a duplicate key (C29). Move it past the largest value.
        table = qualified(step.schema, step.table)
        name = quote(step.column)
        sequence = f"pg_get_serial_sequence({literal(table)}, {literal(step.column)})"
        return (
            f"{add};\nSELECT setval({sequence}, "
            f"greatest(coalesce(max({name}) + 1, {new['start']}), {new['start']}), false) "
            f"FROM {table}"
        )
    actions: list[str] = []
    if old["always"] != new["always"]:
        actions.append(f"SET GENERATED {'ALWAYS' if new['always'] else 'BY DEFAULT'}")
    actions += [f"SET {option}" for option in _sequence_options(new, old, sql_type)]
    if (new["increment"] > 0) != (old["increment"] > 0):
        # The current value is on the wrong side of the new bounds: start over.
        actions.append("RESTART")
    return f"{col} {' '.join(actions)}"


def _alter_generated(step: AlterColumnGenerated) -> str:
    col = f"ALTER TABLE {qualified(step.schema, step.table)} ALTER COLUMN {quote(step.column)}"
    old, new = step.old, step.new
    where = f"{step.table}.{step.column}"
    if old is None or (new is not None and old["stored"] != new["stored"]):
        raise MigrationError(
            f"making {where} a generated column, or switching STORED/VIRTUAL, needs a custom "
            "migration (drop the column and add it again)"
        )
    if new is None:
        if not old["stored"]:
            raise MigrationError(
                f"{where} is VIRTUAL: making it a plain column needs a custom migration"
            )
        return f"{col} DROP EXPRESSION"
    return _set_expression(step, new)


def _set_expression(step: AlterColumnGenerated, new: GeneratedSnap) -> str:
    """SET EXPRESSION needs PostgreSQL 17. Older servers drop the column and add it again
    (the values are computed, so nothing is lost), then restore the indexes and
    constraints that used it, and the foreign keys of other tables that reference it."""
    table = qualified(step.schema, step.table)
    if step.definition is None:
        raise MigrationError(f"{step.table}.{step.column}: the column's definition is missing")
    add = f"ALTER TABLE {table} ADD COLUMN {column_definition(step.definition)}"
    set_expression = (
        f"ALTER TABLE {table} ALTER COLUMN {quote(step.column)} "
        f"SET EXPRESSION AS ({new['expression']})"
    )
    rel, att = literal(table), literal(step.column)
    return (
        "DO $mizzle$ DECLARE defs text[]; refs text[]; def text; r record; note text; BEGIN\n"
        "IF current_setting('server_version_num')::int >= 170000 THEN\n"
        f"  EXECUTE {literal(set_expression)};\n"
        "ELSE\n"
        # Constraints and indexes on the column are made again below; nothing else (K6-10).
        + _unmanaged_check(
            table,
            step.table,
            step.column,
            " AND d.classid <> 'pg_constraint'::regclass AND NOT EXISTS (SELECT 1 FROM pg_class "
            "i WHERE d.classid = 'pg_class'::regclass AND i.oid = d.objid "
            "AND i.relkind IN ('i', 'I'))",
        )
        + "  SELECT array_agg(d) INTO defs FROM (\n"
        "    SELECT format('ALTER TABLE %s ADD CONSTRAINT %I %s', "
        f"{rel}, conname, pg_get_constraintdef(c.oid)) AS d\n"
        "    FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid\n"
        f"    WHERE c.conrelid = {rel}::regclass AND a.attname = {att} "
        "AND a.attnum = ANY(c.conkey)\n"
        "    UNION ALL\n"
        "    SELECT DISTINCT pg_get_indexdef(i.indexrelid) FROM pg_index i\n"
        "    JOIN pg_depend dep ON dep.classid = 'pg_class'::regclass "
        "AND dep.objid = i.indexrelid AND dep.refobjid = i.indrelid\n"
        "    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = dep.refobjsubid\n"
        f"    WHERE i.indrelid = {rel}::regclass AND a.attname = {att}\n"
        "      AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)\n"
        "  ) s;\n"
        "  FOR r IN SELECT c.conrelid::regclass AS t, c.conname, pg_get_constraintdef(c.oid) AS d\n"
        "    FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.confrelid\n"
        f"    WHERE c.contype = 'f' AND c.confrelid = {rel}::regclass AND a.attname = {att}\n"
        "      AND a.attnum = ANY(c.confkey)\n"
        "      AND NOT (c.conrelid = c.confrelid AND a.attnum = ANY(c.conkey)) LOOP\n"
        "    refs := refs || format('ALTER TABLE %s ADD CONSTRAINT %I %s', r.t, r.conname, r.d);\n"
        "    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.t, r.conname);\n"
        "  END LOOP;\n"
        f"  note := col_description({rel}::regclass, (SELECT attnum FROM pg_attribute "
        f"WHERE attrelid = {rel}::regclass AND attname = {att}));\n"
        f"  EXECUTE {literal(f'ALTER TABLE {table} DROP COLUMN {quote(step.column)}')};\n"
        f"  EXECUTE {literal(add)};\n"
        # The comment goes with the column (K7-4).
        "  IF note IS NOT NULL THEN EXECUTE format('COMMENT ON COLUMN %s.%I IS %L', "
        f"{rel}, {att}, note); END IF;\n"
        "  FOREACH def IN ARRAY coalesce(defs, '{}') LOOP EXECUTE def; END LOOP;\n"
        "  FOREACH def IN ARRAY coalesce(refs, '{}') LOOP EXECUTE def; END LOOP;\n"
        "END IF; END $mizzle$"
    )


def _alter_type(step: AlterColumnType) -> str:
    if step.new_type in SERIAL_TYPES or step.old_type in SERIAL_TYPES:
        raise MigrationError(
            f"changing {step.table}.{step.column} to or from a serial type needs a custom migration"
        )
    col = quote(step.column)
    collate = f" COLLATE {quote(step.collation)}" if step.collation else ""
    text = (
        f"ALTER TABLE {qualified(step.schema, step.table)} ALTER COLUMN {col} "
        f"SET DATA TYPE {step.new_type}{collate}"
    )
    cast = f"{col}::text::{step.new_type}" if step.via_text else f"{col}::{step.new_type}"
    return text if step.generated else f"{text} USING {cast}"


def render_step(step: Step) -> Statement:
    def alter(schema: str, table: str, action: str) -> Statement:
        return Statement(f"ALTER TABLE {qualified(schema, table)} {action}")

    match step:
        case CreateSchema(name=name):
            return Statement(f"CREATE SCHEMA IF NOT EXISTS {quote(name)}")
        case CreateEnum(enum=enum):
            values = ", ".join(literal(v) for v in enum["values"])
            return Statement(
                f"CREATE TYPE {qualified(enum['schema'], enum['name'])} AS ENUM ({values})"
            )
        case AddEnumValue(schema=schema, name=name, value=value, after=after, before=before):
            # IF NOT EXISTS: it commits on its own, so a migration that fails later must
            # be able to run again (C22).
            text = f"ALTER TYPE {qualified(schema, name)} ADD VALUE IF NOT EXISTS {literal(value)}"
            if after is not None:
                text += f" AFTER {literal(after)}"
            elif before is not None:
                text += f" BEFORE {literal(before)}"
            # A new enum value cannot be used in the transaction that adds it.
            return Statement(text, transactional=False)
        case DropEnum(schema=schema, name=name):
            return Statement(f"DROP TYPE {qualified(schema, name)}")
        case CreateTable(table=table):
            return Statement(_create_table(table))
        case DropTable(schema=schema, name=name):
            return Statement(f"DROP TABLE {qualified(schema, name)}")
        case RenameTable(schema=schema, old=old, new=new):
            return alter(schema, old, f"RENAME TO {quote(new)}")
        case SetTableSchema(schema=schema, name=name, new_schema=new_schema):
            return alter(schema, name, f"SET SCHEMA {quote(new_schema)}")
        case SetTableLogged(schema=schema, table=table, unlogged=unlogged):
            return alter(schema, table, "SET UNLOGGED" if unlogged else "SET LOGGED")
        case AlterTableStorage(
            schema=schema, table=table, set_options=set_options, reset_options=reset_options
        ):
            actions: list[str] = []
            if reset_options:
                actions.append("RESET (" + ", ".join(_option_key(k) for k in reset_options) + ")")
            if set_options:
                actions.append(
                    "SET (" + ", ".join(_storage_option(k, v) for k, v in set_options.items()) + ")"
                )
            return alter(schema, table, ", ".join(actions))
        case AddColumn(schema=schema, table=table, column=column):
            return alter(schema, table, f"ADD COLUMN {column_definition(column)}")
        case DropColumn(schema=schema, table=table, name=name):
            return alter(schema, table, f"DROP COLUMN {quote(name)}")
        case RenameColumn(schema=schema, table=table, old=old, new=new):
            return alter(schema, table, f"RENAME COLUMN {quote(old)} TO {quote(new)}")
        case AlterColumnType():
            return Statement(_alter_type(step))
        case AlterColumnNullability(schema=schema, table=table, column=column, not_null=not_null):
            action = "SET NOT NULL" if not_null else "DROP NOT NULL"
            return alter(schema, table, f"ALTER COLUMN {quote(column)} {action}")
        case AlterColumnCollation(
            schema=schema, table=table, column=column, sql_type=sql_type, collation=collation
        ):
            collate = f" COLLATE {quote(collation)}" if collation else ""
            return alter(schema, table, f"ALTER COLUMN {quote(column)} TYPE {sql_type}{collate}")
        case AlterColumnIdentity():
            return Statement(_alter_identity(step, step.sql_type))
        case AlterColumnGenerated():
            return Statement(_alter_generated(step))
        case AlterColumnDefault(schema=schema, table=table, column=column, default=default):
            action = "DROP DEFAULT" if default is None else f"SET DEFAULT {default_sql(default)}"
            return alter(schema, table, f"ALTER COLUMN {quote(column)} {action}")
        case AddPrimaryKey(schema=schema, table=table, primary_key=pk):
            return alter(
                schema,
                table,
                f"ADD CONSTRAINT {quote(pk['name'])} PRIMARY KEY ({_columns(pk['columns'])})"
                f"{_deferred(pk)}",
            )
        case AddUnique(schema=schema, table=table, unique=u):
            return alter(
                schema,
                table,
                f"ADD CONSTRAINT {quote(u['name'])} {_unique_fragment(u)}"
                f"({_columns(u['columns'])}){_deferred(u)}",
            )
        case AddCheck(schema=schema, table=table, check=c):
            not_valid = " NOT VALID" if c.get("not_valid") else ""
            return alter(
                schema,
                table,
                f"ADD CONSTRAINT {quote(c['name'])} CHECK ({c['expression']}){not_valid}",
            )
        case AddForeignKey(schema=schema, table=table, foreign_key=fk):
            return alter(schema, table, f"ADD {_foreign_key(fk)}")
        case DropConstraint(schema=schema, table=table, name=name):
            return alter(schema, table, f"DROP CONSTRAINT {quote(name)}")
        case RenameConstraint(schema=schema, table=table, old=old, new=new):
            return alter(schema, table, f"RENAME CONSTRAINT {quote(old)} TO {quote(new)}")
        case CreateIndex(schema=schema, table=table, index=index):
            return Statement(create_index(schema, table, index))
        case DropIndex(schema=schema, name=name):
            return Statement(f"DROP INDEX {qualified(schema, name)}")
        case RenameIndex(schema=schema, old=old, new=new):
            return Statement(f"ALTER INDEX {qualified(schema, old)} RENAME TO {quote(new)}")
        case CreateRole(role=role):
            # Roles are shared by all databases on the server: another database (or an
            # earlier run) may have made it already. ALTER then sets the options.
            name = quote(role["name"])
            return Statement(  # one statement, so migration files keep one per step
                "DO $mizzle$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = "
                f"{literal(role['name'])}) THEN CREATE ROLE {name}; END IF; "
                f"ALTER ROLE {name} {_role_options(role)}; END $mizzle$"
            )
        case AlterRole(role=role):
            return Statement(f"ALTER ROLE {quote(role['name'])} {_role_options(role)}")
        case DropRole(name=name):
            return Statement(f"DROP ROLE IF EXISTS {quote(name)}")
        case CreateView(view=view):
            kind = "MATERIALIZED VIEW" if view["materialized"] else "VIEW"
            name = qualified(view["schema"], view["name"])
            # Options and check option are written each time, so a re-create keeps them (K6).
            opts = [o for o in ("security_invoker", "security_barrier") if view.get(o)]
            with_ = f" WITH ({', '.join(f'{o}=true' for o in opts)})" if opts else ""
            check = view.get("check_option")
            tail = f"\nWITH {check.upper()} CHECK OPTION" if check else ""
            return Statement(f"CREATE {kind} {name}{with_} AS\n{view['definition']}{tail}")
        case DropView(schema=schema, name=name, materialized=materialized):
            kind = "MATERIALIZED VIEW" if materialized else "VIEW"
            return Statement(f"DROP {kind} {qualified(schema, name)}")
        case SetRowLevelSecurity(schema=schema, table=table, enabled=enabled, force=force):
            return alter(
                schema,
                table,
                f"{'ENABLE' if enabled else 'DISABLE'} ROW LEVEL SECURITY, "
                f"{'FORCE' if force else 'NO FORCE'} ROW LEVEL SECURITY",
            )
        case SetTableComment(schema=schema, table=table, comment=comment):
            value = "NULL" if comment is None else literal(comment)
            return Statement(f"COMMENT ON TABLE {qualified(schema, table)} IS {value}")
        case SetColumnComment(schema=schema, table=table, column=column, comment=comment):
            value = "NULL" if comment is None else literal(comment)
            target = f"{qualified(schema, table)}.{quote(column)}"
            return Statement(f"COMMENT ON COLUMN {target} IS {value}")
        case CreatePolicy(schema=schema, table=table, policy=policy):
            kind = "PERMISSIVE" if policy["permissive"] else "RESTRICTIVE"
            text = (
                f"CREATE POLICY {quote(policy['name'])} ON {qualified(schema, table)} "
                f"AS {kind} FOR {policy['command'].upper()}"
            )
            return Statement(text + _policy_body(policy))
        case AlterPolicy(schema=schema, table=table, policy=policy):
            return Statement(
                f"ALTER POLICY {quote(policy['name'])} ON {qualified(schema, table)}"
                + _policy_body(policy)
            )
        case DropPolicy(schema=schema, table=table, name=name):
            return Statement(f"DROP POLICY {quote(name)} ON {qualified(schema, table)}")
        case SaveGrants(schema=schema, name=name):
            return Statement(_save_grants(qualified(schema, name)))
        case RestoreGrants(schema=schema, name=name, materialized=materialized):
            kind = "MATERIALIZED VIEW" if materialized else "VIEW"
            return Statement(_restore_grants(qualified(schema, name), kind))
        case _:
            raise MigrationError(f"PostgreSQL cannot render {type(step).__name__}")


_SAVED = "pg_temp.__mizzle_saved"
_SAVED_SQL = "pg_temp.__mizzle_saved_sql"


def _save_grants(name: str) -> str:
    """Keep a relation's ACL, owner and comment in a temporary table for _restore_grants:
    a view that is dropped and created again would lose them. So would its column grants,
    triggers and rules (not managed, K6-1), column comments and defaults (K7-1): kept as the
    statements that make them again. A grant is made again by the role that made it, so
    grant option chains hold (K7-1)."""
    rel = literal(name)
    grantee = (
        "CASE WHEN g.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(g.grantee)) END"
    )
    return (
        "DO $mizzle$ BEGIN\n"
        # PostgreSQL has no statement to disable a view's trigger or rule (only a catalog
        # edit does it): it would come back enabled, so stop before the view is dropped (K6-11).
        f"IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = {rel}::regclass "
        "AND NOT tgisinternal AND tgenabled <> 'O') OR EXISTS (SELECT 1 FROM pg_rewrite "
        f"WHERE ev_class = {rel}::regclass AND rulename <> '_RETURN' AND ev_enabled <> 'O') THEN\n"
        f"  RAISE EXCEPTION 'view % has a disabled trigger or rule, which would come back "
        f"enabled when it is created again; this needs a custom migration', {rel};\n"
        "END IF;\n"
        # PostgreSQL has no GRANTED BY another role for object privileges: SET ROLE to it,
        # which PostgreSQL 16+ allows only with the membership's SET option (K8-4).
        "IF EXISTS (SELECT 1 FROM pg_class c, LATERAL (SELECT c.relacl UNION ALL SELECT attacl "
        "FROM pg_attribute WHERE attrelid = c.oid) x(acl), aclexplode(x.acl) g "
        f"WHERE c.oid = {rel}::regclass AND g.grantor <> c.relowner "
        "AND NOT pg_has_role(g.grantor, CASE WHEN current_setting('server_version_num')::int "
        ">= 160000 THEN 'SET' ELSE 'MEMBER' END)) THEN\n"
        f"  RAISE EXCEPTION 'view % has privileges granted by a role the current user cannot "
        f"SET ROLE to, so it cannot grant them again; this needs a custom migration', {rel};\n"
        "END IF;\n"
        f"CREATE TEMP TABLE IF NOT EXISTS __mizzle_saved "
        "(rel text PRIMARY KEY, acl aclitem[], owner oid, comment text);\n"
        "CREATE TEMP TABLE IF NOT EXISTS __mizzle_saved_sql "
        "(id serial, rel text, col name, stmt text, grantor oid);\n"
        f"INSERT INTO {_SAVED} SELECT {rel}, c.relacl, c.relowner, "
        "obj_description(c.oid, 'pg_class') FROM pg_class c "
        f"WHERE c.oid = {rel}::regclass\n"
        "ON CONFLICT (rel) DO UPDATE SET acl = EXCLUDED.acl, owner = EXCLUDED.owner, "
        "comment = EXCLUDED.comment;\n"
        f"DELETE FROM {_SAVED_SQL} WHERE rel = {rel};\n"
        f"INSERT INTO {_SAVED_SQL} (rel, col, stmt, grantor) SELECT {rel}, a.attname, "
        "format('GRANT %s (%I) ON %s TO %s%s', "
        f"g.privilege_type, a.attname, {rel}, {grantee}, "
        "CASE WHEN g.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END), g.grantor "
        "FROM pg_attribute a, aclexplode(a.attacl) WITH ORDINALITY g "
        f"WHERE a.attrelid = {rel}::regclass AND a.attacl IS NOT NULL "
        "ORDER BY a.attnum, g.ordinality;\n"
        f"INSERT INTO {_SAVED_SQL} (rel, col, stmt) SELECT {rel}, a.attname, "
        f"format('COMMENT ON COLUMN %s.%I IS %L', {rel}, a.attname, d.description) "
        "FROM pg_attribute a JOIN pg_description d ON d.objoid = a.attrelid "
        "AND d.classoid = 'pg_class'::regclass AND d.objsubid = a.attnum "
        f"WHERE a.attrelid = {rel}::regclass;\n"
        f"INSERT INTO {_SAVED_SQL} (rel, col, stmt) SELECT {rel}, a.attname, "
        f"format('ALTER VIEW %s ALTER COLUMN %I SET DEFAULT %s', {rel}, a.attname, "
        "pg_get_expr(d.adbin, d.adrelid)) FROM pg_attrdef d JOIN pg_attribute a "
        f"ON a.attrelid = d.adrelid AND a.attnum = d.adnum WHERE d.adrelid = {rel}::regclass;\n"
        f"INSERT INTO {_SAVED_SQL} (rel, col, stmt) SELECT {rel}, NULL, pg_get_triggerdef(t.oid) "
        f"FROM pg_trigger t WHERE t.tgrelid = {rel}::regclass AND NOT t.tgisinternal;\n"
        f"INSERT INTO {_SAVED_SQL} (rel, col, stmt) SELECT {rel}, NULL, "
        "rtrim(pg_get_ruledef(r.oid), ';') "
        f"FROM pg_rewrite r WHERE r.ev_class = {rel}::regclass AND r.rulename <> '_RETURN';\n"
        "END $mizzle$"
    )


def _restore_grants(name: str, kind: str) -> str:
    rel = literal(name)
    return (
        "DO $mizzle$ DECLARE s record; a record; me text := current_setting('role'); BEGIN\n"
        "IF to_regclass('pg_temp.__mizzle_saved') IS NULL THEN RETURN; END IF;\n"
        f"SELECT * INTO s FROM {_SAVED} WHERE rel = {rel};\n"
        "IF NOT FOUND THEN RETURN; END IF;\n"
        f"IF s.owner <> (SELECT relowner FROM pg_class WHERE oid = {rel}::regclass) THEN\n"
        f"  EXECUTE format('ALTER {kind} %s OWNER TO %I', {rel}, pg_get_userbyid(s.owner));\n"
        "END IF;\n"
        # Exactly the saved ACL: the new view has the schema's default privileges (K5-1).
        "FOR a IN SELECT DISTINCT grantee FROM pg_class, aclexplode("
        f"coalesce(relacl, acldefault('r', relowner))) WHERE oid = {rel}::regclass LOOP\n"
        "  EXECUTE format('REVOKE ALL ON %s FROM %s CASCADE', "
        f"{rel}, CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
        "ELSE quote_ident(pg_get_userbyid(a.grantee)) END);\n"
        "END LOOP;\n"
        # The owner's grants first, then those its grantees made, in the order they were.
        "FOR a IN SELECT * FROM aclexplode(coalesce(s.acl, acldefault('r', s.owner))) "
        "WITH ORDINALITY g ORDER BY g.grantor <> s.owner, g.ordinality LOOP\n"
        "  IF a.grantor <> s.owner THEN "
        "PERFORM set_config('role', pg_get_userbyid(a.grantor), true); END IF;\n"
        "  EXECUTE format('GRANT %s ON %s TO %s%s', a.privilege_type, "
        f"{rel}, CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
        "ELSE quote_ident(pg_get_userbyid(a.grantee)) END, "
        "CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);\n"
        "  PERFORM set_config('role', me, true);\n"
        "END LOOP;\n"
        "IF s.comment IS NOT NULL THEN\n"
        f"  EXECUTE format('COMMENT ON {kind} %s IS %L', {rel}, s.comment);\n"
        "END IF;\n"
        # Column grants, comments and defaults on a column the view still has; triggers
        # and rules fail the migration if the new view cannot carry them, never vanish.
        f"FOR a IN SELECT stmt, grantor FROM {_SAVED_SQL} WHERE rel = {rel} AND (col IS NULL OR "
        f"EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = {rel}::regclass "
        "AND attname = col AND NOT attisdropped)) "
        "ORDER BY coalesce(grantor <> s.owner, false), id LOOP\n"
        "  IF a.grantor <> s.owner THEN "
        "PERFORM set_config('role', pg_get_userbyid(a.grantor), true); END IF;\n"
        "  EXECUTE a.stmt;\n"
        "  PERFORM set_config('role', me, true);\n"
        "END LOOP;\n"
        f"DELETE FROM {_SAVED_SQL} WHERE rel = {rel};\n"
        f"DELETE FROM {_SAVED} WHERE rel = {rel};\n"
        "END $mizzle$"
    )


def _role(name: str) -> str:
    return "PUBLIC" if name == "public" else quote(name)


def _policy_body(policy: PolicySnap) -> str:
    text = f" TO {', '.join(_role(r) for r in policy['roles'])}"
    if policy["using"] is not None:
        text += f" USING ({policy['using']})"
    if policy["with_check"] is not None:
        text += f" WITH CHECK ({policy['with_check']})"
    return text


def _rebuild_guard(step: DropColumn) -> str:
    """Stop before a column is dropped to be added again if anything mizzle does not manage
    uses it (column grants, exclusion constraints, triggers...): the drop would take it
    silently (K6-7). What mizzle manages on it (keys, indexes, views) is gone by then."""
    table = qualified(step.schema, step.table)
    return f"DO $mizzle$ BEGIN\n{_unmanaged_check(table, step.table, step.name)}END $mizzle$"


def _unmanaged_check(table: str, name: str, column: str, restored: str = "") -> str:
    """The IF ... RAISE that refuses to drop a column something unmanaged depends on.
    `restored` is an extra SQL condition on the dependency `d` for what the caller
    makes again after the drop."""
    rel, col = literal(table), literal(column)
    what = literal(f"{name}.{column}")
    return (
        "IF EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = "
        f"{rel}::regclass AND a.attname = {col} AND a.attacl IS NOT NULL)\n"
        "OR EXISTS (SELECT 1 FROM pg_depend d JOIN pg_attribute a ON a.attrelid = d.refobjid "
        "AND a.attnum = d.refobjsubid WHERE d.refclassid = 'pg_class'::regclass "
        f"AND d.refobjid = {rel}::regclass AND a.attname = {col} AND d.deptype IN ('a', 'n') "
        "AND d.classid <> 'pg_attrdef'::regclass "
        "AND NOT EXISTS (SELECT 1 FROM pg_class s WHERE d.classid = 'pg_class'::regclass "
        "AND s.oid = d.objid AND s.relkind = 'S') "
        "AND NOT EXISTS (SELECT 1 FROM pg_constraint n WHERE d.classid = "
        f"'pg_constraint'::regclass AND n.oid = d.objid AND n.contype = 'n'){restored}) THEN\n"
        f"  RAISE EXCEPTION 'column % is dropped and added again, which would drop the grants, "
        "constraints, views or triggers on it that mizzle does not manage; this needs a custom "
        f"migration', {what};\n"
        "END IF;\n"
    )


def render(steps: Sequence[Step], *, qualify: bool = False) -> list[Statement]:
    """``qualify`` names public tables and types as ``"public"."t"`` too."""
    token = _QUALIFY.set(qualify)
    try:
        out: list[Statement] = []
        for step in steps:
            if isinstance(step, DropColumn) and step.rebuilt:
                out.append(Statement(_rebuild_guard(step)))
            statement = render_step(step)
            out.append(Statement(escape_backslashes(statement.sql), statement.transactional))
        return out
    finally:
        _QUALIFY.reset(token)

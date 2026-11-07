"""PostgreSQL DDL: normalized types/defaults for snapshots, and SQL for migration steps."""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping, Sequence
from typing import cast

from mizzle.core.compiler import no_nul
from mizzle.core.errors import MigrationError, SchemaError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import MISSING, ColumnBase
from mizzle.core.snapshot import (
    ColumnSnap,
    CompositeSnap,
    DomainSnap,
    ExclusionElementSnap,
    ExclusionSnap,
    ForeignKeySnap,
    FunctionSnap,
    GeneratedSnap,
    IdentitySnap,
    IndexSnap,
    PolicySnap,
    RoleSnap,
    TableSnap,
    TriggerSnap,
    UniqueSnap,
    keyword_default,
    sequence_defaults,
)
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import (
    AddCheck,
    AddColumn,
    AddDomainChecks,
    AddEnumValue,
    AddExclusion,
    AddForeignKey,
    AddPrimaryKey,
    AddUnique,
    AlterColumnCollation,
    AlterColumnDefault,
    AlterColumnGenerated,
    AlterColumnIdentity,
    AlterColumnNullability,
    AlterColumnType,
    AlterExtension,
    AlterPolicy,
    AlterRole,
    AlterTableStorage,
    CreateComposite,
    CreateDomain,
    CreateEnum,
    CreateExtension,
    CreateFunction,
    CreateIndex,
    CreatePartition,
    CreatePolicy,
    CreateRole,
    CreateSchema,
    CreateTable,
    CreateTrigger,
    CreateView,
    DropColumn,
    DropComposite,
    DropConstraint,
    DropDomain,
    DropDomainChecks,
    DropEnum,
    DropExtension,
    DropFunction,
    DropIndex,
    DropPartition,
    DropPolicy,
    DropRole,
    DropTable,
    DropTrigger,
    DropView,
    GrantPrivileges,
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


def qualified(schema: str, name: str) -> str:
    return quote(name) if schema == DEFAULT_SCHEMA else f"{quote(schema)}.{quote(name)}"


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


# One printed function argument: [mode] [name] type [DEFAULT expr].
_ARG = re.compile(
    rf"(?P<head>(?:(?:IN|OUT|INOUT|VARIADIC) )?(?:(?:{_IDENT_PART}) )?)"
    rf"(?P<type>{_TYPE_PATH.pattern})(?P<tail>(?: DEFAULT .*)?)",
    re.DOTALL,
)


def requote_args(args: str, server_keywords: frozenset[str]) -> str:
    """pg_get_function_arguments() text with each argument's type requoted (K4-25)."""

    def one(arg: str) -> str:
        match = _ARG.fullmatch(arg)
        if match is None:  # a type of several words: never a keyword
            return arg
        tail = requote_casts(match["tail"], server_keywords)
        return match["head"] + requote_type(match["type"], server_keywords) + tail

    return ", ".join(one(a) for a in _split_args(args))


# A string literal or quoted name (kept as is), or the type of a ::cast.
_CAST = re.compile(rf"""'(?:[^']|'')*'|"(?:[^"]|"")*"|::(?P<type>{_TYPE_PATH.pattern})""")


def requote_casts(text: str, server_keywords: frozenset[str]) -> str:
    """Default text with the type of each ``::`` cast requoted (K4-29)."""

    def one(match: re.Match[str]) -> str:
        cast_type = match["type"]
        return match[0] if cast_type is None else "::" + requote_type(cast_type, server_keywords)

    return _CAST.sub(one, text)


def requote_returns(returns: str, server_keywords: frozenset[str]) -> str:
    """pg_get_function_result() text with its types requoted (K4-25)."""
    if returns.startswith("TABLE(") and returns.endswith(")"):
        return f"TABLE({requote_args(returns[6:-1], server_keywords)})"
    setof = "SETOF " if returns.startswith("SETOF ") else ""
    return setof + requote_type(returns.removeprefix(setof), server_keywords)


def normalize_type(sql_type: str) -> str:
    """``varchar(255)`` -> ``character varying(255)``, ``numeric(10, 2)`` -> ``numeric(10,2)``."""
    array = ""
    while sql_type.endswith("[]"):
        sql_type, array = sql_type[:-2], array + "[]"
    match = re.fullmatch(r"([a-z ]+?)\s*(\(.*\))?", sql_type.strip())
    if match is None:
        return sql_type + array
    base, args = match.group(1), match.group(2) or ""
    base = _TYPE_ALIASES.get(base, base)
    args = args.replace(" ", "")
    if args and base.startswith(("timestamp with", "time with")):  # also "...without"
        base = base.replace(" with", f"{args} with")
        args = ""
    return base + args + array


def column_type(column: ColumnBase) -> str:
    if column.enum is not None:
        array = "[]" if column.sql_type.endswith("[]") else ""
        name = type_ident(column.sql_type.removesuffix("[]"))
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


def _exclusion_key(e: ExclusionElementSnap) -> str:
    column = e.get("column")
    return quote(column) if column is not None else e.get("expression", "")


# An operator is made of PostgreSQL's operator characters, or names one with the
# ``OPERATOR(schema.op)`` form. Anything else (a `;`, a `)`, a quote) is rejected, and so is
# `--` / `/*` / `*/`, which PostgreSQL would read as the start of a comment, not an operator
# (B24).
_OPERATOR_SYMBOL = "+-*/<>=~!@#%^&|`?"
_EXCLUSION_OPERATOR = re.compile(
    rf"(?:[{re.escape(_OPERATOR_SYMBOL)}]+\Z"
    rf"|OPERATOR\([A-Za-z_][A-Za-z0-9_]*\."
    rf"(?:[{re.escape(_OPERATOR_SYMBOL)}]+|[A-Za-z_][A-Za-z0-9_]*)\)\Z)",
    re.IGNORECASE,
)
_COMMENT_START = re.compile(r"--|/\*|\*/")
_EXCLUSION_METHOD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _exclusion_fragment(excl: ExclusionSnap) -> str:
    keys = ", ".join(
        f"{_exclusion_key(e)} WITH {_exclusion_operator(e['operator'])}" for e in excl["elements"]
    )
    if not _EXCLUSION_METHOD.fullmatch(excl["method"]):
        raise SchemaError(f"invalid exclusion method {excl['method']!r}")
    text = f"EXCLUDE USING {excl['method']} ({keys})"
    if where := excl.get("where"):
        text += f" WHERE ({where})"
    return text + _deferred(excl)


def _exclusion_operator(operator: str) -> str:
    if not _EXCLUSION_OPERATOR.fullmatch(operator) or _COMMENT_START.search(operator):
        raise SchemaError(f"invalid exclusion operator {operator!r}")
    return operator


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
        f"CONSTRAINT {quote(e['name'])} {_exclusion_fragment(e)}"
        for e in table.get("exclusions", {}).values()
    ]
    lines += [
        f"CONSTRAINT {quote(c['name'])} CHECK ({c['expression']})"
        for c in table["checks"].values()
        if not c.get("not_valid")  # NOT VALID is only valid on ALTER TABLE ADD CONSTRAINT
    ]
    body = ",\n    ".join(lines)
    kind = "CREATE UNLOGGED TABLE" if table.get("unlogged") else "CREATE TABLE"
    text = f"{kind} {qualified(table['schema'], table['name'])} (\n    {body}\n)"
    if partition_by := table.get("partition_by"):
        text += f" PARTITION BY {partition_by}"
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
            + ", ".join(f"{_option_key(k)} = {_literal(v)}" for k, v in options.items())
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


def _literal(text: str) -> str:
    return "'" + no_nul(text).replace("'", "''") + "'"


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
    return f"{key}={_literal(text)}"


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
        sequence = "pg_get_serial_sequence('" + table.replace("'", "''") + "', '"
        sequence += step.column.replace("'", "''") + "')"
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
    rel, att = _literal(table), _literal(step.column)
    return (
        "DO $mizzle$ DECLARE defs text[]; refs text[]; def text; r record; BEGIN\n"
        "IF current_setting('server_version_num')::int >= 170000 THEN\n"
        f"  EXECUTE {_literal(set_expression)};\n"
        "ELSE\n"
        "  SELECT array_agg(d) INTO defs FROM (\n"
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
        f"  EXECUTE {_literal(f'ALTER TABLE {table} DROP COLUMN {quote(step.column)}')};\n"
        f"  EXECUTE {_literal(add)};\n"
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


def _create_domain(domain: DomainSnap) -> str:
    parts = [
        f"CREATE DOMAIN {qualified(domain['schema'], domain['name'])} AS {domain['base_type']}"
    ]
    if collation := domain.get("collation"):
        parts.append(f"COLLATE {type_ident(collation)}")
    if default := domain.get("default"):
        parts.append(f"DEFAULT {default_sql(default)}")
    if domain.get("not_null"):
        parts.append("NOT NULL")
    parts.extend(f"CHECK ({check})" for check in domain.get("checks", []))
    return " ".join(parts)


def _create_composite(composite: CompositeSnap) -> str:
    fields = ", ".join(f"{quote(f['name'])} {f['type']}" for f in composite["fields"])
    return f"CREATE TYPE {qualified(composite['schema'], composite['name'])} AS ({fields})"


def render_step(step: Step) -> Statement:
    def alter(schema: str, table: str, action: str) -> Statement:
        return Statement(f"ALTER TABLE {qualified(schema, table)} {action}")

    match step:
        case CreateSchema(name=name):
            return Statement(f"CREATE SCHEMA IF NOT EXISTS {quote(name)}")
        case CreateEnum(enum=enum):
            values = ", ".join("'" + v.replace("'", "''") + "'" for v in enum["values"])
            return Statement(
                f"CREATE TYPE {qualified(enum['schema'], enum['name'])} AS ENUM ({values})"
            )
        case AddEnumValue(schema=schema, name=name, value=value, after=after, before=before):
            # IF NOT EXISTS: it commits on its own, so a migration that fails later must
            # be able to run again (C22).
            text = (
                f"ALTER TYPE {qualified(schema, name)} ADD VALUE IF NOT EXISTS "
                f"'{value.replace("'", "''")}'"
            )
            if after is not None:
                text += f" AFTER '{after.replace("'", "''")}'"
            elif before is not None:
                text += f" BEFORE '{before.replace("'", "''")}'"
            # A new enum value cannot be used in the transaction that adds it.
            return Statement(text, transactional=False)
        case DropEnum(schema=schema, name=name):
            return Statement(f"DROP TYPE {qualified(schema, name)}")
        case CreateDomain(domain=domain):
            return Statement(_create_domain(domain))
        case DropDomain(schema=schema, name=name):
            return Statement(f"DROP DOMAIN {qualified(schema, name)}")
        case DropDomainChecks(schema=schema, name=name):
            target = qualified(schema, name)
            drop = f"ALTER DOMAIN {target} DROP CONSTRAINT %I"
            return Statement(
                f"DO $mizzle$ DECLARE c name; BEGIN\nALTER DOMAIN {target} DROP DEFAULT;\n"
                "FOR c IN SELECT conname FROM pg_constraint WHERE contype = 'c' "
                f"AND contypid = {_literal(target)}::regtype LOOP\n"
                f"EXECUTE format({_literal(drop)}, c);\nEND LOOP;\nEND $mizzle$"
            )
        case AddDomainChecks(domain=domain):
            target = qualified(domain["schema"], domain["name"])
            changes = [f"ADD CHECK ({check})" for check in domain.get("checks", [])]
            if default := domain.get("default"):
                changes.insert(0, f"SET DEFAULT {default_sql(default)}")
            body = "".join(f"ALTER DOMAIN {target} {a};\n" for a in changes)
            return Statement(f"DO {_dollar_quote(f'BEGIN\n{body}END')}")
        case CreateComposite(composite=composite):
            return Statement(_create_composite(composite))
        case DropComposite(schema=schema, name=name):
            return Statement(f"DROP TYPE {qualified(schema, name)}")
        case CreateTable(table=table):
            return Statement(_create_table(table))
        case CreatePartition(schema=schema, parent=parent, name=name, bound=bound):
            return Statement(
                f"CREATE TABLE {qualified(schema, name)} PARTITION OF "
                f"{qualified(schema, parent)} {bound}"
            )
        case DropPartition(schema=schema, name=name):
            return Statement(f"DROP TABLE {qualified(schema, name)}")
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
        case AddExclusion(schema=schema, table=table, exclusion=excl):
            return alter(
                schema,
                table,
                f"ADD CONSTRAINT {quote(excl['name'])} {_exclusion_fragment(excl)}",
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
                f"{_literal(role['name'])}) THEN CREATE ROLE {name}; END IF; "
                f"ALTER ROLE {name} {_role_options(role)}; END $mizzle$"
            )
        case AlterRole(role=role):
            return Statement(f"ALTER ROLE {quote(role['name'])} {_role_options(role)}")
        case DropRole(name=name):
            return Statement(f"DROP ROLE IF EXISTS {quote(name)}")
        case CreateExtension(extension=ext):
            text = f"CREATE EXTENSION IF NOT EXISTS {type_ident(ext['name'])}"
            if "schema" in ext:
                text += f" SCHEMA {quote(ext['schema'])}"
            if "version" in ext:
                text += f" VERSION {_literal(ext['version'])}"
            return Statement(text)
        case AlterExtension(name=name, schema=schema, version=version):
            action = (
                f"SET SCHEMA {quote(schema)}" if schema else f"UPDATE TO {_literal(version or '')}"
            )
            return Statement(f"ALTER EXTENSION {type_ident(name)} {action}")
        case DropExtension(name=name):
            return Statement(f"DROP EXTENSION IF EXISTS {type_ident(name)}")
        case CreateFunction(function=fn):
            return Statement(_create_function(fn))
        case DropFunction(schema=schema, name=name, args=args):
            return Statement(f"DROP FUNCTION {qualified(schema, name)}({_without_defaults(args)})")
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
            value = "NULL" if comment is None else _literal(comment)
            return Statement(f"COMMENT ON TABLE {qualified(schema, table)} IS {value}")
        case SetColumnComment(schema=schema, table=table, column=column, comment=comment):
            value = "NULL" if comment is None else _literal(comment)
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
        case CreateTrigger(schema=schema, table=table, trigger=trigger, replace=replace):
            return Statement(_create_trigger(schema, table, trigger, replace))
        case DropTrigger(schema=schema, table=table, name=name):
            return Statement(f"DROP TRIGGER {quote(name)} ON {qualified(schema, table)}")
        case GrantPrivileges(
            schema=schema, table=table, role=role, privileges=privileges, revoke=revoke
        ):
            what = ", ".join(p.upper() for p in privileges)
            on = qualified(schema, table)
            if revoke:
                return Statement(f"REVOKE {what} ON {on} FROM {_role(role)}")
            return Statement(f"GRANT {what} ON {on} TO {_role(role)}")
        case SaveGrants(schema=schema, name=name, args=str() as args):
            return Statement(_save_function_grants(schema, name, args))
        case RestoreGrants(schema=schema, name=name, args=str() as args):
            return Statement(_restore_function_grants(schema, name, args))
        case SaveGrants(schema=schema, name=name):
            return Statement(_save_grants(qualified(schema, name)))
        case RestoreGrants(schema=schema, name=name, materialized=materialized):
            kind = "MATERIALIZED VIEW" if materialized else "VIEW"
            return Statement(_restore_grants(qualified(schema, name), kind))
        case _:
            raise MigrationError(f"PostgreSQL cannot render {type(step).__name__}")


def _dollar_quote(body: str) -> str:
    tag = "$mizzle$"
    n = 0
    while tag in body:
        n += 1
        tag = f"$mizzle{n}$"
    return f"{tag}{body}{tag}"


_SAVED = "pg_temp.__mizzle_saved"


def _save_grants(name: str) -> str:
    """Keep a relation's ACL, owner and comment in a temporary table for _restore_grants:
    a view that is dropped and created again would lose them."""
    rel = _literal(name)
    return (
        "DO $mizzle$ BEGIN\n"
        f"CREATE TEMP TABLE IF NOT EXISTS __mizzle_saved "
        "(rel text PRIMARY KEY, acl aclitem[], owner oid, comment text);\n"
        f"INSERT INTO {_SAVED} SELECT {rel}, c.relacl, c.relowner, "
        "obj_description(c.oid, 'pg_class') FROM pg_class c "
        f"WHERE c.oid = {rel}::regclass\n"
        "ON CONFLICT (rel) DO UPDATE SET acl = EXCLUDED.acl, owner = EXCLUDED.owner, "
        "comment = EXCLUDED.comment;\n"
        "END $mizzle$"
    )


def _restore_grants(name: str, kind: str) -> str:
    rel = _literal(name)
    return (
        "DO $mizzle$ DECLARE s record; a record; BEGIN\n"
        "IF to_regclass('pg_temp.__mizzle_saved') IS NULL THEN RETURN; END IF;\n"
        f"SELECT * INTO s FROM {_SAVED} WHERE rel = {rel};\n"
        "IF NOT FOUND THEN RETURN; END IF;\n"
        f"IF s.owner <> (SELECT relowner FROM pg_class WHERE oid = {rel}::regclass) THEN\n"
        f"  EXECUTE format('ALTER {kind} %s OWNER TO %I', {rel}, pg_get_userbyid(s.owner));\n"
        "END IF;\n"
        "FOR a IN SELECT * FROM aclexplode(s.acl) WHERE grantee <> s.owner LOOP\n"
        "  EXECUTE format('GRANT %s ON %s TO %s%s', a.privilege_type, "
        f"{rel}, CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
        "ELSE quote_ident(pg_get_userbyid(a.grantee)) END, "
        "CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);\n"
        "END LOOP;\n"
        "IF s.comment IS NOT NULL THEN\n"
        f"  EXECUTE format('COMMENT ON {kind} %s IS %L', {rel}, s.comment);\n"
        "END IF;\n"
        f"DELETE FROM {_SAVED} WHERE rel = {rel};\n"
        "END $mizzle$"
    )


def _function_ref(schema: str, name: str, args: str) -> tuple[str, str]:
    """The signature as DROP/ALTER/GRANT take it, and the key it is saved under."""
    signature = f"{qualified(schema, name)}({_without_defaults(args)})"
    return signature, _literal(f"function {signature}")


def _save_function_grants(schema: str, name: str, args: str) -> str:
    """``_save_grants`` for a function about to be dropped and created again. A new
    function can be run by PUBLIC and is owned by its creator, so a SECURITY DEFINER
    function would come back runnable by everyone. ALTER FUNCTION takes the signature the
    way DROP FUNCTION does (argument names, modes), so a rename finds the one function it
    means; its row is read under that name, then the name is put back."""
    signature, saved = _function_ref(schema, name, args)
    tmp = "__mizzle_saving"
    moved = f"{qualified(schema, tmp)}({_without_defaults(args)})"
    return (
        "DO $mizzle$ DECLARE f record; BEGIN\n"
        "CREATE TEMP TABLE IF NOT EXISTS __mizzle_saved "
        "(rel text PRIMARY KEY, acl aclitem[], owner oid, comment text);\n"
        f"EXECUTE {_literal(f'ALTER FUNCTION {signature} RENAME TO {quote(tmp)}')};\n"
        "SELECT p.proacl AS acl, p.proowner AS owner, "
        "obj_description(p.oid, 'pg_proc') AS comment INTO STRICT f FROM pg_proc p "
        f"WHERE p.pronamespace = {_literal(quote(schema))}::regnamespace "
        f"AND p.proname = {_literal(tmp)};\n"
        f"EXECUTE {_literal(f'ALTER FUNCTION {moved} RENAME TO {quote(name)}')};\n"
        f"INSERT INTO {_SAVED} VALUES ({saved}, f.acl, f.owner, f.comment)\n"
        "ON CONFLICT (rel) DO UPDATE SET acl = EXCLUDED.acl, owner = EXCLUDED.owner, "
        "comment = EXCLUDED.comment;\n"
        "END $mizzle$"
    )


def _restore_function_grants(schema: str, name: str, args: str) -> str:
    signature, saved = _function_ref(schema, name, args)
    target = _literal(signature)
    return (
        "DO $mizzle$ DECLARE s record; a record; BEGIN\n"
        "IF to_regclass('pg_temp.__mizzle_saved') IS NULL THEN RETURN; END IF;\n"
        f"SELECT * INTO s FROM {_SAVED} WHERE rel = {saved};\n"
        "IF NOT FOUND THEN RETURN; END IF;\n"
        # Created just now, so owned by the current user.
        "IF s.owner <> (SELECT oid FROM pg_roles WHERE rolname = current_user) THEN\n"
        f"  EXECUTE format('ALTER FUNCTION %s OWNER TO %I', {target}, "
        "pg_get_userbyid(s.owner));\n"
        "END IF;\n"
        # NULL is the default (PUBLIC may run it), which the new function has already.
        "IF s.acl IS NOT NULL THEN\n"
        f"  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', {target});\n"
        "  FOR a IN SELECT * FROM aclexplode(s.acl) WHERE grantee <> s.owner LOOP\n"
        "    EXECUTE format('GRANT %s ON FUNCTION %s TO %s%s', a.privilege_type, "
        f"{target}, CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
        "ELSE quote_ident(pg_get_userbyid(a.grantee)) END, "
        "CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);\n"
        "  END LOOP;\n"
        "END IF;\n"
        "IF s.comment IS NOT NULL THEN\n"
        f"  EXECUTE format('COMMENT ON FUNCTION %s IS %L', {target}, s.comment);\n"
        "END IF;\n"
        f"DELETE FROM {_SAVED} WHERE rel = {saved};\n"
        "END $mizzle$"
    )


def _split_args(args: str) -> list[str]:
    """Function arguments split on their top-level commas."""
    parts: list[str] = []
    depth, start, quote_char = 0, 0, ""
    for i, ch in enumerate(args + ","):
        if quote_char:
            quote_char = "" if ch == quote_char else quote_char
        elif ch in "'\"":
            quote_char = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and ch == ",":
            parts.append(args[start:i].strip())
            start = i + 1
    return [p for p in parts if p]


def _without_defaults(args: str) -> str:
    """Function arguments as DROP FUNCTION takes them: ``a integer DEFAULT 1`` -> ``a integer``."""
    return ", ".join(re.split(r" DEFAULT | = ", p, maxsplit=1)[0] for p in _split_args(args))


# A SQL-standard body (PostgreSQL 14+): written as is, not as a string.
_SQL_BODY = re.compile(r"\s*(?:RETURN\b|BEGIN\s+ATOMIC\b)", re.IGNORECASE)


def sql_standard_body(fn: FunctionSnap) -> bool:
    return fn["language"] == "sql" and _SQL_BODY.match(fn["body"]) is not None


def function_options(fn: FunctionSnap) -> str:
    """Everything after ``LANGUAGE x`` and before the body."""
    text = ""
    if fn["volatility"] != "volatile":
        text += f" {fn['volatility'].upper()}"
    if fn.get("strict"):
        text += " STRICT"
    if fn["security_definer"]:
        text += " SECURITY DEFINER"
    if fn.get("leakproof"):
        text += " LEAKPROOF"
    if parallel := fn.get("parallel"):
        text += f" PARALLEL {parallel.upper()}"
    if (cost := fn.get("cost")) is not None:
        text += f" COST {cost:g}"
    for setting, value in fn.get("settings", {}).items():
        text += f" SET {setting} = {value}"
    return text


def _create_function(fn: FunctionSnap) -> str:
    text = (
        f"CREATE OR REPLACE FUNCTION {qualified(fn['schema'], fn['name'])}({fn['args']}) "
        f"RETURNS {fn['returns']} LANGUAGE {type_ident(fn['language'])}"
    )
    text += function_options(fn)
    if sql_standard_body(fn):
        return f"{text}\n{fn['body']}"
    return f"{text} AS {_dollar_quote(fn['body'])}"


def _role(name: str) -> str:
    return "PUBLIC" if name == "public" else quote(name)


def _policy_body(policy: PolicySnap) -> str:
    text = f" TO {', '.join(_role(r) for r in policy['roles'])}"
    if policy["using"] is not None:
        text += f" USING ({policy['using']})"
    if policy["with_check"] is not None:
        text += f" WITH CHECK ({policy['with_check']})"
    return text


def trigger_events(trigger: TriggerSnap) -> str:
    """``INSERT OR UPDATE OF a, b``."""
    events: list[str] = []
    for event in trigger["events"]:
        if event == "update" and (columns := trigger.get("columns")):
            events.append(f"UPDATE OF {_columns(columns)}")
        else:
            events.append(event.upper())
    return " OR ".join(events)


def _create_trigger(schema: str, table: str, trigger: TriggerSnap, replace: bool) -> str:
    fn_schema, _, fn_name = trigger["function"].partition(".")
    text = (
        f"CREATE {'OR REPLACE ' if replace else ''}TRIGGER {quote(trigger['name'])} "
        f"{trigger['timing'].upper()} {trigger_events(trigger)} ON {qualified(schema, table)}"
    )
    referencing = [
        f"{side} TABLE AS {quote(name)}"
        for side, name in (("OLD", trigger.get("old_table")), ("NEW", trigger.get("new_table")))
        if name is not None
    ]
    if referencing:
        text += f" REFERENCING {' '.join(referencing)}"
    text += f" FOR EACH {trigger['for_each'].upper()}"
    if trigger["when"] is not None:
        text += f" WHEN ({trigger['when']})"
    return f"{text} EXECUTE FUNCTION {qualified(fn_schema, fn_name)}()"


# Column types that need an extension, and the extension.
_EXTENSION_TYPES = re.compile(r"(?:vector|halfvec|sparsevec)(?:\(\d+\))?(?:\[\])*")


def type_extension(sql_type: str) -> str | None:
    return "vector" if _EXTENSION_TYPES.fullmatch(sql_type) else None


def render(steps: Sequence[Step]) -> list[Statement]:
    statements: list[Statement] = []
    for step in steps:
        # The schema an extension goes into may hold nothing else, so nothing created it.
        match step:
            case (
                CreateExtension(extension={"schema": str(schema)})
                | AlterExtension(schema=str(schema))
            ):
                statements.append(render_step(CreateSchema(schema)))
            case _:
                pass
        statements.append(render_step(step))
    return statements

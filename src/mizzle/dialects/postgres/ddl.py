"""PostgreSQL DDL: normalized types/defaults for snapshots, and SQL for migration steps."""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping, Sequence
from typing import cast

from mizzle.core.errors import MigrationError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import MISSING, ColumnBase
from mizzle.core.snapshot import (
    ColumnSnap,
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
    AlterExtension,
    AlterPolicy,
    AlterRole,
    AlterTableStorage,
    CreateEnum,
    CreateExtension,
    CreateFunction,
    CreateIndex,
    CreatePolicy,
    CreateRole,
    CreateSchema,
    CreateTable,
    CreateTrigger,
    CreateView,
    DropColumn,
    DropConstraint,
    DropEnum,
    DropExtension,
    DropFunction,
    DropIndex,
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
from mizzle.dialects.postgres.compiler import PostgresCompiler

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
}
_PLAIN_IDENT = re.compile(r"[a-z_][a-z0-9_$]*")
_TRAILING_CAST = re.compile(
    r"::(?:\"[^\"]+\"|[a-z_][a-z0-9_ .\"]*)(?:\(\d+(?:,\s*\d+)?\))?(?:\[\])*$"
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:e[-+]?\d+)?", re.IGNORECASE)


def quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def qualified(schema: str, name: str) -> str:
    return quote(name) if schema == DEFAULT_SCHEMA else f"{quote(schema)}.{quote(name)}"


def truncate_name(name: str) -> str:
    return name.encode()[:MAX_IDENTIFIER].decode(errors="ignore")


def type_ident(name: str) -> str:
    """A type name as format_type() prints it: quoted only when necessary."""
    return name if _PLAIN_IDENT.fullmatch(name) else quote(name)


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
    while True:
        stripped = _TRAILING_CAST.sub("", text)
        if stripped.startswith("(") and stripped.endswith(")") and stripped.count("(") == 1:
            stripped = stripped[1:-1]
        if stripped == text:
            break
        text = stripped
    if len(text) >= 2 and text[0] == text[-1] == "'" and _NUMBER.fullmatch(text[1:-1]):
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
        text = c.visit(value)
    else:
        encoded = column.encode(value)
        if column.item is not None and isinstance(encoded, list | tuple):
            encoded = _array_text(cast(Sequence[object], encoded))
        if isinstance(encoded, datetime.date | datetime.time):
            encoded = _temporal_text(encoded)
        text = c.literal(encoded)
    return normalize_default(text)


def _trim_fraction(text: str) -> str:
    """PostgreSQL prints 03:04:05.5, not 03:04:05.500000."""
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


def _offset_text(offset: datetime.timedelta) -> str:
    """UTC offsets as PostgreSQL prints them: +00, +05:45, -03:30."""
    seconds = int(offset.total_seconds())
    sign = "-" if seconds < 0 else "+"
    hours, rest = divmod(abs(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{sign}{hours:02d}"
    if minutes or secs:
        text += f":{minutes:02d}"
    if secs:
        text += f":{secs:02d}"
    return text


def _temporal_text(value: datetime.date | datetime.time) -> str:
    """A date/time default exactly as PostgreSQL prints it back, so a schema and its
    database compare equal. timestamptz values are stored as instants: written in UTC."""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            utc = value.astimezone(datetime.UTC).replace(tzinfo=None)
            return _trim_fraction(utc.isoformat(" ")) + "+00"
        return _trim_fraction(value.isoformat(" "))
    if isinstance(value, datetime.time):
        text = _trim_fraction(value.replace(tzinfo=None).isoformat())
        offset = value.utcoffset()
        return text + _offset_text(offset) if offset is not None else text
    return value.isoformat()


# '2024-01-02 08:49:05+05:45'::timestamp with time zone: printed in the session time zone.
_TIMESTAMPTZ_DEFAULT = re.compile(r"'([^']+)'::timestamp(?:\(\d\))? with time zone")


def _utc_timestamptz(match: re.Match[str]) -> str:
    try:
        value = datetime.datetime.fromisoformat(match.group(1))
    except ValueError:  # BC dates, infinity: leave as printed
        return match.group(0)
    if value.tzinfo is None:
        return match.group(0)
    return f"'{_temporal_text(value)}'"


def _array_text(values: Sequence[object]) -> str:
    """A 1-D array literal as PostgreSQL prints it back (``{a,"b c"}``), not JSON."""

    def item(value: object) -> str:
        if value is None:
            return "NULL"
        text = ("t" if value else "f") if isinstance(value, bool) else str(value)
        special = any(ch in '{}",\\' or ch.isspace() for ch in text)
        if special or text == "" or text.upper() == "NULL":
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return text

    return "{" + ",".join(item(v) for v in values) + "}"


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
        parts.append(f"DEFAULT {column['default']}")
    return " ".join(parts)


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
        text += " WITH (" + ", ".join(f"{k}={v}" for k, v in options.items()) + ")"
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
        text += " WITH (" + ", ".join(f"{k} = {_literal(v)}" for k, v in options.items()) + ")"
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
    return "'" + text.replace("'", "''") + "'"


def _alter_identity(step: AlterColumnIdentity, sql_type: str) -> str:
    col = f"ALTER TABLE {qualified(step.schema, step.table)} ALTER COLUMN {quote(step.column)}"
    old, new = step.old, step.new
    if new is None:
        return f"{col} DROP IDENTITY"
    if old is None:
        return f"{col} ADD {_identity(new, sql_type)}"
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
    text = (
        f"ALTER TABLE {qualified(step.schema, step.table)} ALTER COLUMN {col} "
        f"SET DATA TYPE {step.new_type}"
    )
    return text if step.generated else f"{text} USING {col}::{step.new_type}"


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
            text = f"ALTER TYPE {qualified(schema, name)} ADD VALUE '{value.replace("'", "''")}'"
            if after is not None:
                text += f" AFTER '{after.replace("'", "''")}'"
            elif before is not None:
                text += f" BEFORE '{before.replace("'", "''")}'"
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
                actions.append("RESET (" + ", ".join(reset_options) + ")")
            if set_options:
                actions.append(
                    "SET (" + ", ".join(f"{k}={v}" for k, v in set_options.items()) + ")"
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
            action = "DROP DEFAULT" if default is None else f"SET DEFAULT {default}"
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
            return Statement(f"CREATE {kind} {name} AS\n{view['definition']}")
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


def _without_defaults(args: str) -> str:
    """Function arguments as DROP FUNCTION takes them: ``a integer DEFAULT 1`` -> ``a integer``."""
    parts: list[str] = []
    depth, start, quoted = 0, 0, False
    for i, ch in enumerate(args + ","):
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch in "([":
            depth += 1
        elif not quoted and ch in ")]":
            depth -= 1
        elif not quoted and depth == 0 and ch == ",":
            parts.append(args[start:i].strip())
            start = i + 1
    return ", ".join(re.split(r" DEFAULT | = ", p, maxsplit=1)[0] for p in parts if p)


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

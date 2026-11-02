"""PostgreSQL DDL: normalized types/defaults for snapshots, and SQL for migration steps."""

from __future__ import annotations

import re
from collections.abc import Sequence

from mizzle.core.errors import MigrationError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import MISSING, ColumnBase
from mizzle.core.snapshot import ColumnSnap, ForeignKeySnap, IndexSnap, TableSnap
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import (
    AddCheck,
    AddColumn,
    AddEnumValue,
    AddForeignKey,
    AddPrimaryKey,
    AddUnique,
    AlterColumnDefault,
    AlterColumnNullability,
    AlterColumnType,
    CreateEnum,
    CreateIndex,
    CreateSchema,
    CreateTable,
    DropColumn,
    DropConstraint,
    DropEnum,
    DropIndex,
    DropTable,
    RenameColumn,
    RenameConstraint,
    RenameIndex,
    RenameTable,
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
    if base in ("timestamp with time zone", "timestamp without time zone") and args:
        base = base.replace(" with", f"{args} with")
        args = ""
    return base + args + array


def column_type(column: ColumnBase) -> str:
    if column.enum is not None:
        array = "[]" if column.sql_type.endswith("[]") else ""
        return type_ident(column.sql_type.removesuffix("[]")) + array
    return normalize_type(column.sql_type)


def normalize_default(text: str | None) -> str | None:
    """Make a default comparable: ``'user'::role`` -> ``'user'``, ``'-1'::integer`` -> ``-1``."""
    if text is None:
        return None
    text = text.strip()
    while True:
        stripped = _TRAILING_CAST.sub("", text)
        if stripped.startswith("(") and stripped.endswith(")") and stripped.count("(") == 1:
            stripped = stripped[1:-1]
        if stripped == text:
            break
        text = stripped
    if len(text) >= 2 and text[0] == text[-1] == "'" and _NUMBER.fullmatch(text[1:-1]):
        return text[1:-1]
    return text


def compiler(casing: Casing | None) -> PostgresCompiler:
    return PostgresCompiler(casing, inline_literals=True, qualify_columns=False)


def expression(node: Node, casing: Casing | None = None) -> str:
    return compiler(casing).compile(node).sql


def column_default(column: ColumnBase, casing: Casing | None = None) -> str | None:
    if column.default_value is MISSING:
        return None
    value = column.default_value
    c = compiler(casing)
    text = c.visit(value) if isinstance(value, Node) else c.literal(column.encode(value))
    return normalize_default(text)


# -- rendering ------------------------------------------------------------------------


def _columns(names: Sequence[str]) -> str:
    return ", ".join(quote(n) for n in names)


def column_definition(column: ColumnSnap) -> str:
    parts = [quote(column["name"]), column["type"]]
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
    return text


def _create_table(table: TableSnap) -> str:
    lines = [column_definition(c) for c in table["columns"].values()]
    if pk := table["primary_key"]:
        lines.append(f"CONSTRAINT {quote(pk['name'])} PRIMARY KEY ({_columns(pk['columns'])})")
    lines += [
        f"CONSTRAINT {quote(u['name'])} UNIQUE ({_columns(u['columns'])})"
        for u in table["uniques"].values()
    ]
    lines += [
        f"CONSTRAINT {quote(c['name'])} CHECK ({c['expression']})" for c in table["checks"].values()
    ]
    body = ",\n    ".join(lines)
    return f"CREATE TABLE {qualified(table['schema'], table['name'])} (\n    {body}\n)"


def _create_index(schema: str, table: str, index: IndexSnap) -> str:
    unique = "UNIQUE " if index["unique"] else ""
    text = (
        f"CREATE {unique}INDEX {quote(index['name'])} ON {qualified(schema, table)} "
        f"({_columns(index['columns'])})"
    )
    return text + (f" WHERE {index['where']}" if index["where"] else "")


def _alter_type(step: AlterColumnType) -> str:
    if step.new_type in SERIAL_TYPES or step.old_type in SERIAL_TYPES:
        raise MigrationError(
            f"changing {step.table}.{step.column} to or from a serial type needs a custom migration"
        )
    col = quote(step.column)
    return (
        f"ALTER TABLE {qualified(step.schema, step.table)} ALTER COLUMN {col} "
        f"SET DATA TYPE {step.new_type} USING {col}::{step.new_type}"
    )


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
        case AlterColumnDefault(schema=schema, table=table, column=column, default=default):
            action = "DROP DEFAULT" if default is None else f"SET DEFAULT {default}"
            return alter(schema, table, f"ALTER COLUMN {quote(column)} {action}")
        case AddPrimaryKey(schema=schema, table=table, primary_key=pk):
            return alter(
                schema,
                table,
                f"ADD CONSTRAINT {quote(pk['name'])} PRIMARY KEY ({_columns(pk['columns'])})",
            )
        case AddUnique(schema=schema, table=table, unique=u):
            return alter(
                schema,
                table,
                f"ADD CONSTRAINT {quote(u['name'])} UNIQUE ({_columns(u['columns'])})",
            )
        case AddCheck(schema=schema, table=table, check=c):
            return alter(
                schema, table, f"ADD CONSTRAINT {quote(c['name'])} CHECK ({c['expression']})"
            )
        case AddForeignKey(schema=schema, table=table, foreign_key=fk):
            return alter(schema, table, f"ADD {_foreign_key(fk)}")
        case DropConstraint(schema=schema, table=table, name=name):
            return alter(schema, table, f"DROP CONSTRAINT {quote(name)}")
        case RenameConstraint(schema=schema, table=table, old=old, new=new):
            return alter(schema, table, f"RENAME CONSTRAINT {quote(old)} TO {quote(new)}")
        case CreateIndex(schema=schema, table=table, index=index):
            return Statement(_create_index(schema, table, index))
        case DropIndex(schema=schema, name=name):
            return Statement(f"DROP INDEX {qualified(schema, name)}")
        case RenameIndex(schema=schema, old=old, new=new):
            return Statement(f"ALTER INDEX {qualified(schema, old)} RENAME TO {quote(new)}")
        case _:
            raise MigrationError(f"PostgreSQL cannot render {type(step).__name__}")


def render(steps: Sequence[Step]) -> list[Statement]:
    return [render_step(s) for s in steps]

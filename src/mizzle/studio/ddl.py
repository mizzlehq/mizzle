"""Allow-listed, generated DDL for Studio's structure editor.

The browser never sends SQL. It sends a small operation (create/drop table, add/alter/drop
column) as JSON; this module turns it into mizzle migration ``Step`` objects, which the
dialect validates and renders. Identifiers must be simple names, column types and default
expressions are checked against conservative allow-lists, and every name is quoted. An
operation that is not on the list, or carries a value that does not fit, is rejected before
any SQL is generated. ``--read-only`` refuses to apply the result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, cast

from mizzle.core.errors import SchemaError
from mizzle.core.snapshot import ColumnSnap, PrimaryKeySnap, TableSnap
from mizzle.core.steps import (
    AddColumn,
    AlterColumnDefault,
    AlterColumnNullability,
    AlterColumnType,
    CreateTable,
    DropColumn,
    DropTable,
    Step,
)
from mizzle.dialects.base import Dialect

type Operation = Literal["create_table", "drop_table", "add_column", "alter_column", "drop_column"]

_OPERATIONS: tuple[Operation, ...] = (
    "create_table",
    "drop_table",
    "add_column",
    "alter_column",
    "drop_column",
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")

# A PostgreSQL type, built from the shapes PostgreSQL accepts and nothing else: never trailing
# words, so ``int references victim on delete cascade`` (SF1) and ``int unique``, ``collate``,
# ``default``, ``not null`` and the like are rejected. A quoted name is a single identifier
# to PostgreSQL, whatever it contains, so it cannot smuggle a clause in.
_NAME = r'(?:[A-Za-z_][A-Za-z0-9_]*|"(?:[^"]|"")+")'
_MODIFIER = r"(?:\s*\(\s*[0-9]+\s*(?:,\s*[0-9]+\s*)?\))"
_PRECISION = r"(?:\s*\(\s*[0-9]+\s*\))"
_INTERVAL_FIELDS = (
    r"(?:year\s+to\s+month|day\s+to\s+(?:hour|minute|second)|hour\s+to\s+(?:minute|second)"
    r"|minute\s+to\s+second|year|month|day|hour|minute|second)"
)
_TYPE_SHAPE = (
    r"(?:(?:timestamp|time)"
    rf"{_PRECISION}?(?:\s+with(?:out)?\s+time\s+zone)?"
    rf"|interval(?:{_PRECISION}|\s+{_INTERVAL_FIELDS}{_PRECISION}?)?"
    rf"|(?:double\s+precision|(?:character|char|bit)\s+varying){_MODIFIER}?"
    rf"|{_NAME}(?:\.{_NAME})?{_MODIFIER}?)"
    r"(?:\[[0-9]*\])*"
)
_TYPE = re.compile(rf"{_TYPE_SHAPE}\Z", re.IGNORECASE)

# A default expression: terms joined by + or -, each with an optional cast to an allowed type.
# A term is a literal, a known function or keyword, ``interval '...'`` or ``nextval('...')``.
# The operators need spaces around them, so ``1--1`` (a ``--`` comment) cannot be formed.
# No backslash: with ``standard_conforming_strings = off`` the server reads ``\'`` as an
# escaped quote, so a literal holding one could end somewhere this pattern did not (B74).
_STRING = r"'(?:[^'\\]|'')*'"
_TERM = (
    rf"(?:NULL|TRUE|FALSE|-?[0-9]+(?:\.[0-9]+)?|{_STRING}"
    r"|CURRENT_TIMESTAMP|CURRENT_DATE|CURRENT_TIME|CURRENT_USER|SESSION_USER"
    r"|(?:now|gen_random_uuid|uuid_generate_v4)\(\)"
    rf"|interval\s+{_STRING}|nextval\(\s*{_STRING}(?:::regclass)?\s*\))"
    rf"(?:::{_TYPE_SHAPE})?"
)
_DEFAULT = re.compile(rf"{_TERM}(?:\s+[-+]\s+{_TERM})*\Z", re.IGNORECASE)


def _str(data: Mapping[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise SchemaError(f"{field} must be a string")
    return value


def _ident(data: Mapping[str, object], field: str) -> str:
    value = _str(data, field)
    if not _IDENT.fullmatch(value):
        raise SchemaError(f"{field} {value!r} is not a plain name")
    return value


def _schema(data: Mapping[str, object]) -> str:
    schema = data.get("schema", "public")
    if not isinstance(schema, str) or not _IDENT.fullmatch(schema):
        raise SchemaError("schema must be a plain name")
    return schema


def _type(data: Mapping[str, object], field: str = "type") -> str:
    value = _str(data, field)
    if len(value) > 100 or not _TYPE.fullmatch(value):
        raise SchemaError(f"unsupported column type {value!r}")
    return value


def _default(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError("default must be a string or null")
    text = value.strip()
    if not text:
        return None
    if not _DEFAULT.fullmatch(text):
        raise SchemaError(
            "default must be a literal, a known function or NULL "
            "(a hand-written migration can set anything else)"
        )
    return text


def _bool(data: Mapping[str, object], field: str) -> bool:
    value = data.get(field, False)
    if not isinstance(value, bool):
        raise SchemaError(f"{field} must be true or false")
    return value


def _mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SchemaError(f"{what} must be an object")
    return cast(dict[str, object], value)


def _column(data: Mapping[str, object]) -> ColumnSnap:
    return ColumnSnap(
        name=_ident(data, "name"),
        type=_type(data),
        not_null=_bool(data, "not_null"),
        default=_default(data.get("default")),
    )


def _columns(
    raw: object, table: str, dialect: Dialect
) -> tuple[dict[str, ColumnSnap], PrimaryKeySnap | None]:
    if not isinstance(raw, list) or not raw:
        raise SchemaError("columns must be a non-empty list")
    columns: dict[str, ColumnSnap] = {}
    primary: list[str] = []
    for item in cast(list[object], raw):
        data = _mapping(item, "column")
        column = _column(data)
        if column["name"] in columns:
            raise SchemaError(f"duplicate column {column['name']!r}")
        columns[column["name"]] = column
        if data.get("primary_key"):
            primary.append(column["name"])
    pk = (
        PrimaryKeySnap(name=dialect.truncate_name(f"{table}_pkey"), columns=primary)
        if primary
        else None
    )
    return columns, pk


def build_steps(
    dialect: Dialect, data: Mapping[str, object], current: TableSnap | None = None
) -> list[Step]:
    """Turn one operation into the migration steps that carry it out.

    ``current`` is the table's snapshot from the live database; operations that change an
    existing table need it (and raise if it is missing).
    """
    operation = _str(data, "operation")
    if operation not in _OPERATIONS:
        raise SchemaError(f"unsupported operation {operation!r}")
    schema = _schema(data)
    match operation:
        case "create_table":
            if current is not None:
                raise SchemaError(f"table {schema}.{_ident(data, 'name')} already exists")
            name = _ident(data, "name")
            columns, pk = _columns(data.get("columns"), name, dialect)
            return [
                CreateTable(
                    TableSnap(
                        schema=schema,
                        name=name,
                        columns=columns,
                        primary_key=pk,
                        uniques={},
                        checks={},
                        foreign_keys={},
                        indexes={},
                    )
                )
            ]
        case "drop_table":
            if current is None:
                raise SchemaError(f"table {schema}.{_ident(data, 'name')} does not exist")
            return [DropTable(schema, name=_ident(data, "name"))]
        case _:
            table = _ident(data, "table")
            if current is None:
                raise SchemaError(f"table {schema}.{table} does not exist")
            return _change_column(operation, data, schema, table, current)


def _change_column(
    operation: str,
    data: Mapping[str, object],
    schema: str,
    table: str,
    current: TableSnap,
) -> list[Step]:
    name = _ident(data, "name")
    before = current["columns"].get(name)
    match operation:
        case "add_column":
            if before is not None:
                raise SchemaError(f"column {table}.{name} already exists")
            column = _column(_mapping(data.get("column"), "column"))
            if column["name"] != name:
                raise SchemaError("column.name must match name")
            return [AddColumn(schema, table, column)]
        case "drop_column":
            if before is None:
                raise SchemaError(f"column {table}.{name} does not exist")
            return [DropColumn(schema, table, name)]
        case "alter_column":
            if before is None:
                raise SchemaError(f"column {table}.{name} does not exist")
            steps: list[Step] = []
            if "type" in data:
                new_type = _type(data)
                if new_type != before["type"]:
                    steps.append(AlterColumnType(schema, table, name, before["type"], new_type))
            if "not_null" in data:
                not_null = _bool(data, "not_null")
                if not_null != before["not_null"]:
                    steps.append(AlterColumnNullability(schema, table, name, not_null))
            if "default" in data:
                default = _default(data.get("default"))
                if default != before["default"]:
                    steps.append(AlterColumnDefault(schema, table, name, default))
            if not steps:
                raise SchemaError("nothing to change")
            return steps
        case _:  # pragma: no cover - guarded by build_steps
            raise SchemaError(f"unsupported operation {operation!r}")


def statement_rows(dialect: Dialect, steps: list[Step]) -> list[dict[str, object]]:
    """Preview rows for the review screen: the SQL, what it does, and whether it loses data."""
    return [
        {"sql": statement.sql, "description": step.describe(), "data_loss": step.data_loss}
        for step, statement in zip(steps, dialect.render(steps), strict=True)
    ]

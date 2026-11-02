"""Source generation: ``pull`` (snapshot -> schema module) and ``typegen`` (TypedDicts)."""

from __future__ import annotations

import datetime
import decimal
import ipaddress
import keyword
import re
import uuid
from collections.abc import Sequence

from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table
from mizzle.core.snapshot import ColumnSnap, Snapshot, TableSnap

_RESERVED_ATTRS = {"config", "from_db"}

_SIMPLE_TYPES = {
    "integer": "integer",
    "bigint": "bigint",
    "smallint": "smallint",
    "text": "text",
    "boolean": "boolean",
    "real": "real",
    "double precision": "double_precision",
    "date": "date",
    "uuid": "uuid",
    "bytea": "bytea",
    "inet": "inet",
    "interval": "interval",
    "serial": "serial",
    "bigserial": "bigserial",
    "smallserial": "smallserial",
    "numeric": "numeric",
    "character varying": "varchar",
}


def _identifier(name: str) -> str:
    ident = re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit():
        ident = f"c_{ident}"
    if keyword.iskeyword(ident) or ident in _RESERVED_ATTRS:
        ident += "_"
    return ident


def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^a-zA-Z0-9]+", name) if part) or "T"


def _sql(text: str) -> str:
    """A ``sql("...")`` call; without values, sql() takes its text literally."""
    return f"sql({text!r})"


class _SchemaWriter:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot: Snapshot = snapshot
        self.pg_imports: set[str] = set()
        self.enum_classes: dict[str, str] = {}  # db type name -> class name
        self.table_classes: dict[str, str] = {}  # "schema.table" -> class name
        self.attrs: dict[str, dict[str, str]] = {}  # "schema.table" -> column -> attribute
        self.uses_column: bool = False
        self.uses_any: bool = False

    def factory(self, sql_type: str, kwargs: str) -> str:
        if sql_type.endswith("[]"):
            self.pg_imports.add("array")
            item = self.factory(sql_type[:-2], "")
            return f"array({item}{', ' + kwargs if kwargs else ''})"
        if sql_type in self.enum_classes:
            self.pg_imports.add("pg_enum")
            cls = self.enum_classes[sql_type]
            extra = f", {kwargs}" if kwargs else ""
            return f"pg_enum({cls}, type_name={sql_type.strip(chr(34))!r}{extra})"
        if sql_type in ("jsonb", "json"):
            self.pg_imports.add(sql_type)
            self.uses_any = True
            return f"{sql_type}[Any]({kwargs})"
        if match := re.fullmatch(r"timestamp(\(\d+\))? (with|without) time zone", sql_type):
            self.pg_imports.add("timestamp")
            args = ["tz=True"] if match.group(2) == "with" else []
            return f"timestamp({', '.join([*args, kwargs] if kwargs else args)})"
        if match := re.fullmatch(r"time (with|without) time zone", sql_type):
            self.pg_imports.add("time")
            args = ["tz=True"] if match.group(1) == "with" else []
            return f"time({', '.join([*args, kwargs] if kwargs else args)})"
        if match := re.fullmatch(r"(character varying|numeric)\(([\d,]+)\)", sql_type):
            fn = _SIMPLE_TYPES[match.group(1)]
            self.pg_imports.add(fn)
            numbers = match.group(2).replace(",", ", ")
            return f"{fn}({numbers}{', ' + kwargs if kwargs else ''})"
        if sql_type in _SIMPLE_TYPES:
            fn = _SIMPLE_TYPES[sql_type]
            self.pg_imports.add(fn)
            return f"{fn}({kwargs})"
        self.uses_column = True
        self.uses_any = True
        extra = f", {kwargs}" if kwargs else ""
        return f"ColumnBase({sql_type!r}, None{extra})"

    def column(self, table: TableSnap, column: ColumnSnap, attr: str) -> str:
        kwargs = f"name={column['name']!r}" if attr != column["name"] else ""
        code = self.factory(column["type"], kwargs)
        tname, cname = table["name"], column["name"]
        pk = table["primary_key"]
        is_pk = pk is not None and pk["columns"] == [cname] and pk["name"] == f"{tname}_pkey"
        if is_pk:
            code += ".primary_key()"
        elif column["not_null"] and column["type"] not in ("serial", "bigserial", "smallserial"):
            code += ".not_null()"
        unique = table["uniques"].get(f"{tname}_{cname}_key")
        if unique is not None and unique["columns"] == [cname]:
            code += ".unique()"
        if column["default"] is not None:
            code += f".default({_sql(column['default'])})"
        fk = table["foreign_keys"].get(f"{tname}_{cname}_fkey")
        if fk is not None and fk["columns"] == [cname] and len(fk["ref_columns"]) == 1:
            target = self.table_classes.get(f"{fk['ref_schema']}.{fk['ref_table']}")
            if target is not None:
                ref_attr = self.attrs[f"{fk['ref_schema']}.{fk['ref_table']}"][fk["ref_columns"][0]]
                options = "".join(
                    f", {opt}={fk[opt]!r}" for opt in ("on_delete", "on_update") if fk[opt]
                )
                code += f".references(lambda: {target}.{ref_attr}{options})"
        return code

    def config_items(self, table: TableSnap, attrs: dict[str, str]) -> list[str]:
        tname = table["name"]
        items: list[str] = []

        def cols(names: Sequence[str]) -> str:
            return ", ".join(f"cls.{attrs[n]}" for n in names)

        pk = table["primary_key"]
        if pk is not None and not (len(pk["columns"]) == 1 and pk["name"] == f"{tname}_pkey"):
            items.append(f"primary_key({cols(pk['columns'])}, name={pk['name']!r})")
        for u in table["uniques"].values():
            if not (len(u["columns"]) == 1 and u["name"] == f"{tname}_{u['columns'][0]}_key"):
                items.append(f"unique({cols(u['columns'])}, name={u['name']!r})")
        for fk in table["foreign_keys"].values():
            col = fk["columns"][0]
            if len(fk["columns"]) == 1 and fk["name"] == f"{tname}_{col}_fkey":
                continue  # written as .references()
            ref_key = f"{fk['ref_schema']}.{fk['ref_table']}"
            target = self.table_classes.get(ref_key)
            if target is None:
                # Names come from the database: repr() keeps them on one escaped line.
                items.append(f"# foreign key {fk['name']!r} references {ref_key!r}, not pulled")
                continue
            refs = ", ".join(f"{target}.{self.attrs[ref_key][c]}" for c in fk["ref_columns"])
            options = "".join(f", {o}={fk[o]!r}" for o in ("on_delete", "on_update") if fk[o])
            items.append(
                f"foreign_key([{cols(fk['columns'])}], lambda: [{refs}], "
                f"name={fk['name']!r}{options})"
            )
        for c in table["checks"].values():
            items.append(f"check({c['name']!r}, {_sql(c['expression'])})")
        for i in table["indexes"].values():
            fn = "unique_index" if i["unique"] else "index"
            code = f"{fn}({i['name']!r}).on({cols(i['columns'])})"
            if i["where"]:
                code += f".where({_sql(i['where'])})"
            items.append(code)
        return items

    def render(self) -> str:
        enums_code: list[str] = []
        for enum in self.snapshot["enums"].values():
            cls = _class_name(enum["name"])
            self.enum_classes[enum["name"]] = cls
            self.enum_classes[f'"{enum["name"]}"'] = cls
            members = [f"    {_identifier(v).upper()} = {v!r}" for v in enum["values"]]
            enums_code.append(f"class {cls}(enum.StrEnum):\n" + "\n".join(members))

        for k, table in self.snapshot["tables"].items():
            name = _class_name(table["name"])
            if name in self.table_classes.values() or name in self.enum_classes.values():
                name = _class_name(f"{table['schema']}_{table['name']}")
            self.table_classes[k] = name
            self.attrs[k] = {c: _identifier(c) for c in table["columns"]}

        tables_code: list[str] = []
        uses_config: set[str] = set()
        has_config = False
        for k, table in self.snapshot["tables"].items():
            attrs = self.attrs[k]
            kw = f"name={table['name']!r}"
            if table["schema"] != "public":
                kw += f", schema={table['schema']!r}"
            lines = ["@final", f"class {self.table_classes[k]}(Table, {kw}):"]
            for cname, column in table["columns"].items():
                lines.append(f"    {attrs[cname]} = {self.column(table, column, attrs[cname])}")
            items = self.config_items(table, attrs)
            if items:
                for fn in (
                    "primary_key",
                    "unique",
                    "foreign_key",
                    "check",
                    "unique_index",
                    "index",
                ):
                    if any(i.startswith(f"{fn}(") for i in items):
                        uses_config.add(fn)
                self.uses_any = True
                body = "".join(
                    f"            {i}\n" if i.startswith("#") else f"            {i},\n"
                    for i in items
                )
                has_config = True
                lines += [
                    "",
                    "    @classmethod",
                    "    @override",
                    "    def config(cls) -> list[Any]:",
                    "        return [\n" + body + "        ]",
                ]
            tables_code.append("\n".join(lines))

        header = ['"""Generated by `mizzle pull`. Review before committing."""', ""]
        if enums_code:
            header.append("import enum")
        typing_names: set[str] = {"final"}
        if self.uses_any:
            typing_names.add("Any")
        if has_config:  # config() methods are marked @override
            typing_names.add("override")
        header.append(f"from typing import {', '.join(sorted(typing_names))}")
        header.append("")
        mizzle_names = sorted(
            {"Table", "sql", *uses_config} | ({"Column"} if self.uses_column else set())
        )
        header.append(f"from mizzle import {', '.join(mizzle_names)}")
        if self.pg_imports:
            header.append(f"from mizzle.pg import {', '.join(sorted(self.pg_imports))}")
        return "\n".join(header) + "\n\n\n" + "\n\n\n".join([*enums_code, *tables_code]) + "\n"


def render_schema_module(snapshot: Snapshot) -> str:
    return _SchemaWriter(snapshot).render()


# -- typegen --------------------------------------------------------------------------

_PY_TYPES: dict[type[object], str] = {
    int: "int",
    str: "str",
    bool: "bool",
    float: "float",
    bytes: "bytes",
    decimal.Decimal: "decimal.Decimal",
    datetime.datetime: "datetime.datetime",
    datetime.date: "datetime.date",
    datetime.time: "datetime.time",
    datetime.timedelta: "datetime.timedelta",
    uuid.UUID: "uuid.UUID",
    ipaddress.IPv4Address: "ipaddress.IPv4Address",
    ipaddress.IPv6Address: "ipaddress.IPv6Address",
    ipaddress.IPv4Interface: "ipaddress.IPv4Interface",
    ipaddress.IPv6Interface: "ipaddress.IPv6Interface",
    ipaddress.IPv4Network: "ipaddress.IPv4Network",
    ipaddress.IPv6Network: "ipaddress.IPv6Network",
}


def _py_type(column: ColumnBase, imports: set[str]) -> str:
    if column.enum is not None:
        imports.add(f"from {column.enum.__module__} import {column.enum.__qualname__}")
        base = column.enum.__qualname__
        if column.sql_type.endswith("[]"):
            base = f"list[{base}]"
    elif column.accepts is None or list in column.accepts:
        base = "list[Any]" if column.accepts else "Any"
    else:
        # The first accepted type is the column's own; extra ones (int for float) are leniency.
        base = _PY_TYPES.get(column.accepts[0], "Any")
    for module in ("decimal", "datetime", "uuid", "ipaddress"):
        if base.startswith(f"{module}."):
            imports.add(f"import {module}")
    return f"{base} | None" if column.nullable else base


def render_types_module(tables: Sequence[type[Table]]) -> str:
    """TypedDicts for insert/update values: ``db.insert(Users).values(UsersInsert(...))``."""
    imports: set[str] = set()
    blocks: list[str] = []
    for table in tables:
        columns = table.__table__.columns
        insert = [f"class {table.__name__}Insert(TypedDict):"]
        update = [f"class {table.__name__}Update(TypedDict, total=False):"]
        for attr, column in columns.items():
            py = _py_type(column, imports)
            insert.append(
                f"    {attr}: {py if column.required_on_insert else f'NotRequired[{py}]'}"
            )
            update.append(f"    {attr}: {py}")
        blocks += ["\n".join(insert), "\n".join(update)]
    head = [
        '"""Generated by `mizzle typegen`. Do not edit; rerun `mizzle typegen` instead."""',
        "",
        *sorted(i for i in imports if i.startswith("import ")),
        "from typing import Any, NotRequired, TypedDict",
        *sorted(i for i in imports if i.startswith("from ")),
    ]
    return "\n".join(head) + "\n\n\n" + "\n\n\n".join(blocks) + "\n"

from __future__ import annotations

import datetime
import enum
import ipaddress
import json
import math
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, cast, override

from asyncpg import BitString, Line, Point

from mizzle.core.compiler import CompiledQuery, Compiler, interval_text
from mizzle.core.errors import QueryError
from mizzle.core.sql.alias import real_table
from mizzle.core.sql.nodes import ExprBase, Node
from mizzle.dialects.postgres.types import CustomColumn

if TYPE_CHECKING:
    from mizzle.core.query.insert import OnConflict
    from mizzle.core.query.merge import Merge, MergeWhen
    from mizzle.core.query.relational import LoadPlan, RelationalStatement
    from mizzle.core.query.returning import Returning
    from mizzle.core.schema.column import ColumnBase
    from mizzle.core.schema.relations import Link
    from mizzle.core.schema.table import Table

MAX_PARAMS = 32767  # PostgreSQL wire protocol limit on bind parameters per statement


_AS_TEXT = frozenset({"cidr", "bit", "varbit", "point", "line"})


def _num(value: float) -> str:
    """A float as PostgreSQL prints it: 1, not 1.0."""
    return repr(float(value)).removesuffix(".0")


def _point(point: Sequence[float]) -> str:
    return f"({_num(point[0])},{_num(point[1])})"


def _quoted(text: str, special: str, quote: str = '""') -> str:
    """An element of an array as PostgreSQL prints it: quoted when
    needed, with a quote inside as ``quote`` (an array's is backslashed)."""
    if text and not any(ch in special or ch.isspace() for ch in text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', quote) + '"'


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


def temporal_text(value: datetime.date | datetime.time) -> str:
    """A date/time exactly as PostgreSQL prints it back, so a schema and its database
    compare equal. timestamptz values are stored as instants: written in UTC."""
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


def _json_key(key: object) -> str:
    return key if isinstance(key, str) else json.dumps(key)


def json_text(value: object) -> str:
    """JSON as PostgreSQL prints jsonb: keys by length then bytes, no \\u escapes, numbers
    in full (Q4-7). json keeps its text, so it reads back as written there too."""
    match value:
        case dict():
            pairs = [(_json_key(k), v) for k, v in cast(dict[object, object], value).items()]
            pairs.sort(key=lambda kv: (len(kv[0].encode()), kv[0].encode()))
            return "{" + ", ".join(f"{json_text(k)}: {json_text(v)}" for k, v in pairs) + "}"
        case list() | tuple():
            return "[" + ", ".join(json_text(v) for v in cast(Sequence[object], value)) + "]"
        case float() if math.isfinite(value):
            return format(Decimal(repr(value)), "f")  # 1e+20 is 100000000000000000000
        case Decimal() if value.is_finite():
            return format(value, "f")
        case _:
            return json.dumps(value, ensure_ascii=False, default=pg_text)


def array_text(values: Sequence[object]) -> str:
    """An array literal as PostgreSQL prints it back (``{a,"b c"}``), each element in its
    own type's text form (Q4-11)."""

    def item(value: object) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, list):
            return array_text(cast(list[object], value))
        text = pg_text(value)
        return f'"{text}"' if text.upper() == "NULL" else _quoted(text, '{}",\\', '\\"')

    return "{" + ",".join(item(v) for v in values) + "}"


def pg_text(value: object) -> str:
    """``value`` in its type's text form, as PostgreSQL prints it (Q4-4, Q4-11, Q4-12)."""
    geometry = _geometry_text(value)
    if geometry is not None:
        return geometry
    match value:
        case bool():  # as in an array
            return "t" if value else "f"
        case enum.Enum():
            return pg_text(cast(object, value.value))
        case float() if not math.isfinite(value):
            return "NaN" if math.isnan(value) else ("-Infinity" if value < 0 else "Infinity")
        case float():
            return _num(value)
        case datetime.timedelta():
            return interval_text(value)
        case bytes():
            return "\\x" + value.hex()
        case datetime.date() | datetime.time():
            return temporal_text(value)
        case BitString():  # without the spaces as_string() puts every 8 bits (Q4-10)
            return value.as_string().replace(" ", "")
        case list():
            return array_text(cast(list[object], value))
        case dict():
            return json_text(cast(object, value))
        case _:
            return str(value)


def _geometry_text(value: object) -> str | None:
    """PostgreSQL's text form of an asyncpg geometric value; None for any other value."""
    match value:
        case Point():
            return _point(value)
        case Line():
            return "{" + ",".join(_num(n) for n in value) + "}"
        case _:
            return None


class PostgresCompiler(Compiler):
    @override
    def param_marker(self, index: int, /) -> str:
        return f"${index}"

    @override
    def literal(self, value: object) -> str:
        # Quoted text cast to the type, as for interval and bytea (Q12).
        match value:
            case ipaddress.IPv4Network() | ipaddress.IPv6Network():
                return self.literal(str(value)) + "::cidr"
            case (
                ipaddress.IPv4Address()
                | ipaddress.IPv6Address()
                | ipaddress.IPv4Interface()
                | ipaddress.IPv6Interface()
            ):
                return self.literal(str(value)) + "::inet"
            case BitString():
                return self.literal(pg_text(value)) + "::varbit"  # Q4-10
            case _:
                geometry = _geometry_text(value)
                if geometry is None:
                    return super().literal(value)
                return f"{self.literal(geometry)}::{type(value).__name__.lower()}"

    @override
    def column_literal(self, column: ColumnBase, value: object) -> str:
        # As PostgreSQL prints it back, so a default compares equal (Q4-4, Q4-7, Q4-11); an
        # array as '{a}', which takes the column's type where ARRAY['a'] is text[] (Q4-1).
        if value is None:
            return "NULL"
        if column.sql_type in ("json", "jsonb"):  # JSON text, even 5 (Q10)
            return f"{self.literal(json_text(value))}::{column.sql_type}"
        if column.item is not None and isinstance(value, list | tuple):
            items = cast(Sequence[object], value)
            if column.item.sql_type in ("json", "jsonb"):  # a list item is JSON, not a dimension
                items = [None if v is None else json_text(v) for v in items]
            return self.literal(array_text(items))
        if isinstance(value, datetime.date | datetime.time):
            return self.literal(temporal_text(value))
        return self.literal(value)

    @override
    def compile(self, node: Node) -> CompiledQuery:
        compiled = super().compile(node)
        if len(compiled.params) > MAX_PARAMS:
            raise QueryError(
                f"query has {len(compiled.params)} parameters; PostgreSQL allows at most "
                f"{MAX_PARAMS}. Split it into smaller batches."
            )
        return compiled

    @override
    def typed_param(self, column: ColumnBase | None, marker: str, /) -> str:
        if column is None or self.inline_literals:
            return marker
        if isinstance(column, CustomColumn):
            return f"{marker}::text::{column.sql_type}"  # sent as text, whatever the type
        if isinstance(column.item, CustomColumn):  # = ANY(...) of custom values
            return f"{marker}::text[]::{column.sql_type}"
        return marker

    @override
    def output(self, node: Node) -> str:
        text = self.visit(node)
        return f"{text}::text" if isinstance(node, CustomColumn) else text  # read as text

    @override
    def returning_clause(self, node: Returning[object], /) -> str:
        projection = node.projection
        if projection is None:
            exprs: Iterable[ExprBase] = node.table.__table__.columns.values()
            return "RETURNING " + ", ".join(self.output(expr) for expr in exprs)
        if isinstance(projection, dict):
            # A named projection: alias each expression, or a CTE over it cannot name
            # the output (``m.c("c", ...)`` -> column m.c does not exist).
            items = [f"{self.output(e)} AS {self.quote(k)}" for k, e in projection.items()]
            return "RETURNING " + ", ".join(items)
        return "RETURNING " + ", ".join(self.output(expr) for expr in projection)

    def visit_Merge(self, node: Merge[Table]) -> str:
        self._note_write()
        if node.source_ is None:
            raise QueryError(f"merge({node.table.__name__}) needs .using(...)")
        if node.on_ is None:
            raise QueryError(f"merge({node.table.__name__}) needs .on(...)")
        if not node.when_:
            raise QueryError(f"merge({node.table.__name__}) needs at least one when_* clause")
        prefix = self._with_ctes(node.ctes)
        text = (
            f"MERGE INTO {self.source(node.table)} USING {self.source(node.source_)}"  # (Q7)
            f" ON {self.visit(node.on_)}"
        )
        return prefix + text + "".join(f" {self._merge_when(node.table, w)}" for w in node.when_)

    def _merge_when(self, table: type[Table], clause: MergeWhen) -> str:
        if clause.matched:
            head = "WHEN MATCHED"
        elif clause.by_source:
            head = "WHEN NOT MATCHED BY SOURCE"
        else:
            head = "WHEN NOT MATCHED"
        if clause.condition is not None:
            head += f" AND {self.visit(clause.condition)}"
        columns = table.__table__.columns
        if clause.action == "update":
            changes = self.set_list(table, clause.set_)
            if not changes:
                raise QueryError("MERGE ... THEN UPDATE SET needs at least one column")
            body = f"UPDATE SET {changes}"
        elif clause.action == "delete":
            body = "DELETE"
        elif clause.action == "insert":
            names = ", ".join(self.column_name(columns[key]) for key in clause.insert_)
            values = ", ".join(
                self.value(columns[key], value) for key, value in clause.insert_.items()
            )
            body = f"INSERT ({names}) VALUES ({values})"
        else:
            body = "DO NOTHING"
        return f"{head} THEN {body}"

    @override
    def on_conflict_clause(self, table: type[Table], conflict: OnConflict, /) -> str:
        if conflict.constraint is not None:
            target = f"ON CONSTRAINT {self.quote(conflict.constraint)} "
        elif conflict.target:
            target = "(" + ", ".join(self.column_name(c) for c in conflict.target) + ") "
        else:
            target = ""
        if conflict.action == "nothing":
            return f"ON CONFLICT {target}DO NOTHING"
        text = f"ON CONFLICT {target}DO UPDATE SET {self.set_list(table, conflict.set_)}"
        if conflict.where is not None:
            text += f" WHERE {self.visit(conflict.where)}"
        return text

    # -- relational queries -----------------------------------------------------------
    # Related rows are nested as JSON arrays: json_build_array(col1, col2, ..., child1...).
    # Types JSON cannot carry exactly are cast so Python can restore them losslessly.

    def json_value(self, ref: str, column: ColumnBase) -> str:
        if isinstance(column, CustomColumn):
            return f"{ref}::text"
        if column.item is not None:
            item = self.json_value("x.v", column.item)
            if item == "x.v":
                return ref  # elements are exact in JSON already
            # Convert each element, keeping order and NULL arrays (ARRAY(...) is never NULL);
            # as json, so elements that nest as arrays may differ in length.
            elements = (
                f"ARRAY(SELECT to_json({item}) FROM unnest({ref}) WITH ORDINALITY AS x(v, n)"
                " ORDER BY x.n)"
            )
            return f"CASE WHEN {ref} IS NULL THEN NULL ELSE {elements} END"
        base = column.sql_type.split("(", 1)[0]
        if base in _AS_TEXT:
            # JSON has no form for these: send the text, which the column's json_decoder
            # parses (C30).
            return f"{ref}::text"
        kind = column.accepts[0] if column.accepts else None
        if kind is float:
            # JSON prints floats per extra_float_digits (0 rounds to 15 digits) and float4 in
            # its own shortest form; the binary float8 is exact under any setting.
            return f"encode(float8send({ref}::float8), 'hex')"
        if kind is Decimal:
            return f"{ref}::text"
        if kind is bytes:
            return f"encode({ref}, 'hex')"
        if kind is datetime.timedelta:
            # Exact parts as text ("years months days microseconds"); a float of seconds
            # loses microseconds. EXTRACT returns numeric (exact) on PostgreSQL 14+.
            parts = (
                f"format('%s %s %s %s', extract(year from {ref}), extract(month from {ref}), "
                f"extract(day from {ref}), "
                f"(extract(epoch from {ref}) - extract(epoch from date_trunc('day', {ref})))"
                " * 1000000)"
            )
            return f"CASE WHEN {ref} IS NULL THEN NULL ELSE {parts} END"
        return ref

    def _clauses(self, plan: LoadPlan) -> tuple[str, str]:
        order = ", ".join(self.visit(o) for o in plan.order_by)
        tail = f" ORDER BY {order}" if order else ""
        if plan.limit is not None:
            tail += f" LIMIT {self.bind(plan.limit)}"
        if plan.offset is not None:
            tail += f" OFFSET {self.bind(plan.offset)}"
        return order, tail

    def _child(self, link: Link, plan: LoadPlan, parent: str) -> str:
        alias = self.new_alias()
        self._scopes.append({plan.table: alias})
        try:
            a = self.quote(alias)
            # ponytail: json_build_array takes at most 100 arguments, so a table loaded
            # through with_ can have at most 100 columns + relations.
            values = [self.json_value(f"{a}.{self.column_name(c)}", c) for _, c in plan.columns]
            values += [self._child(lk, child, alias) for _, lk, child in plan.children]
            row = f"json_build_array({', '.join(values)})"
            conditions = [
                f"{a}.{self.column_name(t)} = {self.quote(parent)}.{self.column_name(s)}"
                for s, t in zip(link.source_columns, link.target_columns, strict=True)
            ]
            if plan.where is not None:
                conditions.append(f"({self.visit(plan.where)})")
            source = f"{self.table_name(plan.table)} AS {a}"
            where = " AND ".join(conditions)
            if not link.many:
                return f"(SELECT {row} FROM {source} WHERE {where} LIMIT 1)"
            if plan.limit is None and plan.offset is None:
                # No LIMIT: aggregate directly, ~15% faster than numbering rows first.
                order = ", ".join(self.visit(o) for o in plan.order_by)
                agg = f"json_agg({row} ORDER BY {order})" if order else f"json_agg({row})"
                return f"(SELECT coalesce({agg}, '[]'::json) FROM {source} WHERE {where})"
            order, tail = self._clauses(plan)
            sub = self.quote(f"{alias}_rows")
            numbered = f"row_number() OVER ({f'ORDER BY {order}' if order else ''})"
            inner = f"SELECT {row} AS r, {numbered} AS n FROM {source} WHERE {where}{tail}"
            return (
                f"(SELECT coalesce(json_agg({sub}.r ORDER BY {sub}.n), '[]'::json) "
                f"FROM ({inner}) AS {sub})"
            )
        finally:
            self._scopes.pop()

    def visit_RelationalStatement(self, node: RelationalStatement) -> str:
        plan = node.plan
        # The WITH prefix comes first in the text, so its parameters must be numbered first.
        prefix = self._with_ctes(node.ctes)
        alias = self.new_alias()
        # Columns of the table and of an alias of it (``db.query(alias(Users, "u"))``)
        # both mean the root row.
        real = real_table(plan.table)
        self._scopes.append({plan.table: alias, real: alias})
        try:
            columns = [self.visit(c) for _, c in plan.columns]
            columns += [self._child(link, child, alias) for _, link, child in plan.children]
            text = f"SELECT {', '.join(columns)} FROM {self.table_name(real)} AS "
            text += self.quote(alias)
            if plan.where is not None:
                text += f" WHERE {self.visit(plan.where)}"
            return prefix + text + self._clauses(plan)[1]
        finally:
            self._scopes.pop()

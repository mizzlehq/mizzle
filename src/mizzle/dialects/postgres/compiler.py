from __future__ import annotations

import datetime
import ipaddress
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, cast, override

from asyncpg import BitString, Box, Circle, Line, LineSegment, Path, Point, Range

from mizzle.core.compiler import CompiledQuery, Compiler
from mizzle.core.errors import QueryError
from mizzle.core.sql.alias import real_table
from mizzle.core.sql.nodes import ExprBase, Node

if TYPE_CHECKING:
    from mizzle.core.query.insert import OnConflict
    from mizzle.core.query.merge import Merge, MergeWhen
    from mizzle.core.query.relational import LoadPlan, RelationalStatement
    from mizzle.core.query.returning import Returning
    from mizzle.core.schema.column import ColumnBase
    from mizzle.core.schema.relations import Link
    from mizzle.core.schema.table import Table

MAX_PARAMS = 32767  # PostgreSQL wire protocol limit on bind parameters per statement


_RANGES = frozenset({"int4range", "int8range", "numrange", "tsrange", "tstzrange", "daterange"})
_AS_TEXT = frozenset(
    {"cidr", "bit", "varbit", "point", "line", "lseg", "box", "path", "polygon", "circle", "hstore"}
)


def _range_parts(ref: str) -> str:
    """A range as [lower, upper, lower_inc, upper_inc, empty]; the bounds as text, so a
    numeric or timestamp bound is exact (C30). to_json gives ISO dates whatever the
    session's DateStyle, which ::text follows (Q13)."""
    lower, upper = (f"to_json({end}({ref})) #>> '{{}}'" for end in ("lower", "upper"))
    return f"json_build_array({lower}, {upper}, lower_inc({ref}), upper_inc({ref}), isempty({ref}))"


def _num(value: float) -> str:
    """A float as PostgreSQL prints it: 1, not 1.0."""
    return repr(float(value)).removesuffix(".0")


def _point(point: Sequence[float]) -> str:
    return f"({_num(point[0])},{_num(point[1])})"


def _bound(value: object) -> str:
    text = str(value)
    if any(ch in text for ch in ' ,()[]"\\'):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


type _Range = Range[int] | Range[Decimal] | Range[datetime.date]


def _all_ranges(values: list[object]) -> bool:
    return bool(values) and all(isinstance(v, Range) for v in values)


def _range_text(value: _Range) -> str:
    if value.isempty:
        return "empty"
    lower = "" if value.lower is None else _bound(value.lower)
    upper = "" if value.upper is None else _bound(value.upper)
    return f"{'[' if value.lower_inc else '('}{lower},{upper}{']' if value.upper_inc else ')'}"


def _geometry_text(value: object) -> str | None:
    """PostgreSQL's text form of an asyncpg geometric value; None for any other value."""
    match value:
        case Point():
            return _point(value)
        case Line():
            return "{" + ",".join(_num(n) for n in value) + "}"
        case LineSegment():
            return f"[{_point(value[0])},{_point(value[1])}]"
        case Box():  # PostgreSQL keeps the upper right corner first
            (x1, y1), (x2, y2) = value
            return f"{_point((max(x1, x2), max(y1, y2)))},{_point((min(x1, x2), min(y1, y2)))}"
        case Path():  # and Polygon, always closed
            points = ",".join(_point(p) for p in cast(Iterable[Point], value))
            return f"({points})" if value.is_closed else f"[{points}]"
        case Circle():
            return f"<{_point(value[0])},{_num(value[1])}>"
        case _:
            return None


class PostgresCompiler(Compiler):
    @override
    def param_marker(self, index: int, /) -> str:
        return f"${index}"

    @override
    def literal(self, value: object) -> str:
        # Quoted text cast to the type, as for interval and bytea (Q12). A range takes its
        # type from context: its Python value does not say int4range or int8range.
        match value:
            case Range():
                return self.literal(_range_text(cast(_Range, value)))
            case list() if _all_ranges(cast(list[object], value)):  # a multirange
                ranges = cast(list[_Range], value)
                return self.literal("{" + ",".join(_range_text(r) for r in ranges) + "}")
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
                return self.literal(value.as_string()) + "::varbit"
            case _:
                geometry = _geometry_text(value)
                if geometry is None:
                    return super().literal(value)
                name = "lseg" if isinstance(value, LineSegment) else type(value).__name__.lower()
                return f"{self.literal(geometry)}::{name}"

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
    def returning_clause(self, node: Returning[object], /) -> str:
        projection = node.projection
        if projection is None:
            exprs: Iterable[ExprBase] = node.table.__table__.columns.values()
            return "RETURNING " + ", ".join(self.visit(expr) for expr in exprs)
        if isinstance(projection, dict):
            # A named projection: alias each expression, or a CTE over it cannot name
            # the output (``m.c("c", ...)`` -> column m.c does not exist).
            items = [f"{self.visit(e)} AS {self.quote(k)}" for k, e in projection.items()]
            return "RETURNING " + ", ".join(items)
        return "RETURNING " + ", ".join(self.visit(expr) for expr in projection)

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
        if column.item is not None:
            item = self.json_value("x.v", column.item)
            if item == "x.v":
                return ref  # elements are exact in JSON already
            # Convert each element, keeping order and NULL arrays (ARRAY(...) is never NULL).
            elements = (
                f"ARRAY(SELECT {item} FROM unnest({ref}) WITH ORDINALITY AS x(v, n) ORDER BY x.n)"
            )
            return f"CASE WHEN {ref} IS NULL THEN NULL ELSE {elements} END"
        base = column.sql_type.split("(", 1)[0]
        if base in _RANGES:
            return f"CASE WHEN {ref} IS NULL THEN NULL ELSE {_range_parts(ref)} END"
        if base.endswith("multirange"):
            parts = _range_parts("r.v")
            return (
                f"CASE WHEN {ref} IS NULL THEN NULL "
                f"ELSE ARRAY(SELECT {parts} FROM unnest({ref}) AS r(v)) END"
            )
        if base in _AS_TEXT:
            # JSON has no form for these (or, for hstore, one a plain select does not
            # return): send the text, which the column's json_decoder parses (C30).
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

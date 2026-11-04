from __future__ import annotations

import datetime
from collections.abc import Iterable
from decimal import Decimal
from typing import TYPE_CHECKING, override

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


class PostgresCompiler(Compiler):
    @override
    def param_marker(self, index: int, /) -> str:
        return f"${index}"

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
        exprs: Iterable[ExprBase]
        if projection is None:
            exprs = node.table.__table__.columns.values()
        else:
            exprs = projection.values() if isinstance(projection, dict) else projection
        return "RETURNING " + ", ".join(self.visit(expr) for expr in exprs)

    def visit_Merge(self, node: Merge[Table]) -> str:
        if node.source_ is None:
            raise QueryError(f"merge({node.table.__name__}) needs .using(...)")
        if node.on_ is None:
            raise QueryError(f"merge({node.table.__name__}) needs .on(...)")
        if not node.when_:
            raise QueryError(f"merge({node.table.__name__}) needs at least one when_* clause")
        text = (
            f"MERGE INTO {self.table_name(node.table)} USING {self.source(node.source_)}"
            f" ON {self.visit(node.on_)}"
        )
        return text + "".join(f" {self._merge_when(node.table, w)}" for w in node.when_)

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
            changes = ", ".join(
                f"{self.column_name(columns[key])} = {self.value(columns[key], value)}"
                for key, value in clause.set_.items()
            )
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
            return text + self._clauses(plan)[1]
        finally:
            self._scopes.pop()

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, override

from mizzle.core.compiler import CompiledQuery, Compiler
from mizzle.core.errors import QueryError
from mizzle.core.sql.nodes import Node

if TYPE_CHECKING:
    from mizzle.core.query.insert import OnConflict
    from mizzle.core.query.relational import LoadPlan, RelationalStatement
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
    def returning_clause(self, table: type[Table], /) -> str:
        return "RETURNING " + ", ".join(self.visit(c) for c in table.__table__.columns.values())

    @override
    def on_conflict_clause(self, table: type[Table], conflict: OnConflict, /) -> str:
        target = ""
        if conflict.target:
            target = "(" + ", ".join(self.column_name(c) for c in conflict.target) + ") "
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
        kind = column.accepts[0] if column.accepts else None
        if column.item is not None:
            item = column.item.accepts[0] if column.item.accepts else None
            if item in (Decimal, bytes, datetime.timedelta):
                raise QueryError(
                    f"{column.qualified_name}: arrays of {item.__name__} cannot be loaded "
                    "through with_ yet; select them with db.select()"
                )
            return ref
        if kind is Decimal:
            return f"{ref}::text"
        if kind is bytes:
            return f"encode({ref}, 'hex')"
        if kind is datetime.timedelta:
            return f"extract(epoch from {ref})"
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
            values = [
                self.json_value(f"{a}.{self.column_name(c)}", c)
                for c in plan.table.__table__.columns.values()
            ]
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
        self._scopes.append({plan.table: alias})
        try:
            columns = [self.visit(c) for c in plan.table.__table__.columns.values()]
            columns += [self._child(link, child, alias) for _, link, child in plan.children]
            text = f"SELECT {', '.join(columns)} FROM {self.table_name(plan.table)} AS "
            text += self.quote(alias)
            if plan.where is not None:
                text += f" WHERE {self.visit(plan.where)}"
            return text + self._clauses(plan)[1]
        finally:
            self._scopes.pop()

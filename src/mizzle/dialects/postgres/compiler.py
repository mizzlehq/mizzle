from __future__ import annotations

from typing import TYPE_CHECKING

from mizzle.core.compiler import CompiledQuery, Compiler
from mizzle.core.errors import QueryError
from mizzle.core.sql.nodes import Node

if TYPE_CHECKING:
    from mizzle.core.query.insert import OnConflict
    from mizzle.core.schema.table import Table

MAX_PARAMS = 32767  # PostgreSQL wire protocol limit on bind parameters per statement


class PostgresCompiler(Compiler):
    def param_marker(self, index: int) -> str:
        return f"${index}"

    def compile(self, node: Node) -> CompiledQuery:
        compiled = super().compile(node)
        if len(compiled.params) > MAX_PARAMS:
            raise QueryError(
                f"query has {len(compiled.params)} parameters; PostgreSQL allows at most "
                f"{MAX_PARAMS}. Split it into smaller batches."
            )
        return compiled

    def returning_clause(self, table: type[Table]) -> str:
        return "RETURNING " + ", ".join(self.visit(c) for c in table.__table__.columns.values())

    def on_conflict_clause(self, table: type[Table], conflict: OnConflict) -> str:
        target = ""
        if conflict.target:
            target = "(" + ", ".join(self.column_name(c) for c in conflict.target) + ") "
        if conflict.action == "nothing":
            return f"ON CONFLICT {target}DO NOTHING"
        text = f"ON CONFLICT {target}DO UPDATE SET {self.set_list(table, conflict.set_)}"
        if conflict.where is not None:
            text += f" WHERE {self.visit(conflict.where)}"
        return text

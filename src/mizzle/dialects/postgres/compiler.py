from __future__ import annotations

from mizzle.core.compiler import CompiledQuery, Compiler
from mizzle.core.errors import QueryError
from mizzle.core.sql.nodes import Node

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

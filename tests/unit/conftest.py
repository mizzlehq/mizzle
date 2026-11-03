from typing import Any

import pytest

from mizzle.core.compiler import CompiledQuery
from mizzle.core.query.base import Row
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.sql.nodes import Node
from mizzle.dialects.postgres.compiler import PostgresCompiler


class FakeRunner:
    """Compiles with the Postgres compiler and returns canned rows instead of hitting a DB."""

    def __init__(self) -> None:
        self.rows: list[tuple[Any, ...]] = []
        self.rowcount = 0
        self.executed: list[CompiledQuery] = []
        self.read_only: list[bool] = []

    def compile(self, node: Node) -> CompiledQuery:
        return PostgresCompiler().compile(node)

    async def fetch(self, query: CompiledQuery, *, read_only: bool = False) -> list[Row]:
        self.executed.append(query)
        self.read_only.append(read_only)
        return list(self.rows)

    async def run(self, query: CompiledQuery) -> int:
        self.executed.append(query)
        return self.rowcount


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def qb(runner: FakeRunner) -> QueryBuilder:
    return QueryBuilder(runner)

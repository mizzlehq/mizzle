from __future__ import annotations

from mizzle.core.naming import Casing
from mizzle.dialects.postgres.compiler import PostgresCompiler
from mizzle.dialects.postgres.driver import PostgresDriver


class PostgresDialect:
    name = "postgresql"

    def compiler(self, casing: Casing | None = None) -> PostgresCompiler:
        return PostgresCompiler(casing)

    async def connect(
        self, url: str, *, pool_size: int, statement_cache_size: int
    ) -> PostgresDriver:
        return await PostgresDriver.connect(
            url, pool_size=pool_size, statement_cache_size=statement_cache_size
        )

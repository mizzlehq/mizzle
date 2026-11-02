from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from mizzle.core.errors import MigrationLockTimeout
from mizzle.core.naming import Casing
from mizzle.core.schema.column import Column
from mizzle.core.snapshot import Snapshot
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import Statement, Step
from mizzle.dialects.base import Connection
from mizzle.dialects.postgres import ddl
from mizzle.dialects.postgres.compiler import PostgresCompiler
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.dialects.postgres.introspect import MIGRATIONS_TABLE, introspect

# Fixed key for pg_advisory_lock so concurrent `migrate` runs serialize.
MIGRATION_LOCK_KEY = 0x6D697A7A6C65  # "mizzle"


class PostgresDialect:
    name = "postgresql"
    default_schema = ddl.DEFAULT_SCHEMA

    def compiler(self, casing: Casing | None = None) -> PostgresCompiler:
        return PostgresCompiler(casing)

    async def connect(
        self, url: str, *, pool_size: int, statement_cache_size: int
    ) -> PostgresDriver:
        return await PostgresDriver.connect(
            url, pool_size=pool_size, statement_cache_size=statement_cache_size
        )

    def column_type(self, column: Column[Any]) -> str:
        return ddl.column_type(column)

    def column_default(self, column: Column[Any], casing: Casing | None) -> str | None:
        return ddl.column_default(column, casing)

    def expression(self, node: Node, casing: Casing | None) -> str:
        return ddl.expression(node, casing)

    def truncate_name(self, name: str) -> str:
        return ddl.truncate_name(name)

    async def introspect(self, conn: Connection, schemas: Sequence[str]) -> Snapshot:
        return await introspect(conn, schemas)

    def render(self, steps: Sequence[Step]) -> list[Statement]:
        return ddl.render(steps)

    async def acquire_migration_lock(self, conn: Connection, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            [row] = await conn.fetch("SELECT pg_try_advisory_lock($1)", [MIGRATION_LOCK_KEY])
            if row[0]:
                return
            if time.monotonic() >= deadline:
                raise MigrationLockTimeout(
                    f"another migration run held the lock for more than {timeout:g}s"
                )
            await asyncio.sleep(0.2)

    async def release_migration_lock(self, conn: Connection) -> None:
        await conn.fetch("SELECT pg_advisory_unlock($1)", [MIGRATION_LOCK_KEY])

    async def ensure_migrations_table(self, conn: Connection, schema: str) -> None:
        table = ddl.qualified(schema, MIGRATIONS_TABLE)
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {ddl.quote(schema)}", [])
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "id serial PRIMARY KEY, name text NOT NULL UNIQUE, hash text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())",
            [],
        )

    async def applied_migrations(self, conn: Connection, schema: str) -> dict[str, str]:
        table = ddl.qualified(schema, MIGRATIONS_TABLE)
        rows = await conn.fetch_dicts(f"SELECT name, hash FROM {table} ORDER BY id", [])
        return {row["name"]: row["hash"] for row in rows}

    def record_migration(self, schema: str, name: str, hash: str) -> tuple[str, list[Any]]:
        table = ddl.qualified(schema, MIGRATIONS_TABLE)
        return f"INSERT INTO {table} (name, hash) VALUES ($1, $2)", [name, hash]

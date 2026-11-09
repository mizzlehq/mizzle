from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence

from mizzle.core.errors import MigrationLockTimeout
from mizzle.core.naming import Casing
from mizzle.core.schema.column import ColumnBase
from mizzle.core.snapshot import Snapshot
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import Statement, Step
from mizzle.dialects.base import Connection, ResetMode
from mizzle.dialects.postgres import ddl
from mizzle.dialects.postgres.compiler import PostgresCompiler
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.dialects.postgres.introspect import (
    MIGRATIONS_TABLE,
    introspect,
    normalize_snapshot,
    skipped_objects,
)

# Fixed key for pg_advisory_lock so concurrent `migrate` runs serialize.
MIGRATION_LOCK_KEY = 0x6D697A7A6C65  # "mizzle"


class PostgresDialect:
    name: str = "postgresql"
    default_schema: str = ddl.DEFAULT_SCHEMA

    def compiler(self, casing: Casing | None = None) -> PostgresCompiler:
        return PostgresCompiler(casing)

    async def connect(
        self,
        url: str,
        *,
        pool_size: int,
        statement_cache_size: int,
        pool_min_size: int = 1,
        max_idle: float = 300.0,
        query_timeout: float | None = None,
        reset_connections: ResetMode = "auto",
        on_connect: Callable[[Connection], Awaitable[None]] | None = None,
        server_settings: Mapping[str, str] | None = None,
        connect_timeout: float = 10.0,
        acquire_timeout: float | None = 30.0,
    ) -> PostgresDriver:
        return await PostgresDriver.connect(
            url,
            pool_size=pool_size,
            statement_cache_size=statement_cache_size,
            pool_min_size=pool_min_size,
            max_idle=max_idle,
            query_timeout=query_timeout,
            reset_connections=reset_connections,
            on_connect=on_connect,
            server_settings=server_settings,
            connect_timeout=connect_timeout,
            acquire_timeout=acquire_timeout,
        )

    def column_type(self, column: ColumnBase) -> str:
        return ddl.column_type(column)

    def column_default(self, column: ColumnBase, casing: Casing | None) -> str | None:
        return ddl.column_default(column, casing)

    def normalize_default(self, text: str | None) -> str | None:
        return ddl.normalize_default(text)

    def normalize_type(self, sql_type: str) -> str:
        return ddl.normalize_type(sql_type)

    def same_type(self, a: str, b: str) -> bool:
        return ddl.same_type(a, b)

    def expression(self, node: Node, casing: Casing | None) -> str:
        return ddl.expression(node, casing)

    def truncate_name(self, name: str) -> str:
        return ddl.truncate_name(name)

    def view_definition(self, query: Node, casing: Casing | None) -> str:
        return ddl.view_definition(query, casing)

    async def introspect(
        self, conn: Connection, schemas: Sequence[str], roles: Sequence[str] = ()
    ) -> Snapshot:
        return await introspect(conn, schemas, roles)

    async def normalize_snapshot(self, conn: Connection, snapshot: Snapshot) -> Snapshot:
        return await normalize_snapshot(conn, snapshot)

    async def skipped_objects(self, conn: Connection, schemas: Sequence[str]) -> list[str]:
        return await skipped_objects(conn, schemas)

    def render(self, steps: Sequence[Step], *, qualify: bool = False) -> list[Statement]:
        return ddl.render(steps, qualify=qualify)

    async def acquire_migration_lock(self, conn: Connection, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            [row] = await conn.fetch(
                "SELECT pg_catalog.pg_try_advisory_lock($1)", [MIGRATION_LOCK_KEY]
            )
            if row[0]:
                return
            if time.monotonic() >= deadline:
                raise MigrationLockTimeout(
                    f"another migration run held the lock for more than {timeout:g}s"
                )
            await asyncio.sleep(0.2)

    async def release_migration_lock(self, conn: Connection) -> None:
        await conn.fetch("SELECT pg_catalog.pg_advisory_unlock($1)", [MIGRATION_LOCK_KEY])

    async def ensure_migrations_table(self, conn: Connection, schema: str) -> None:
        table = ddl.qualified(schema, MIGRATIONS_TABLE)
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {ddl.quote(schema)}", [])
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "id serial PRIMARY KEY, name pg_catalog.text NOT NULL UNIQUE, "
            "hash pg_catalog.text NOT NULL, "
            "applied_at pg_catalog.timestamptz NOT NULL DEFAULT pg_catalog.now())",
            [],
        )

    async def drop_migrations_table(self, conn: Connection, schema: str) -> None:
        table = ddl.qualified(schema, MIGRATIONS_TABLE)
        await conn.execute(f"DROP TABLE IF EXISTS {table}", [])

    async def applied_migrations(self, conn: Connection, schema: str) -> dict[str, str]:
        table = ddl.qualified(schema, MIGRATIONS_TABLE)
        rows = await conn.fetch_dicts(f"SELECT name, hash FROM {table} ORDER BY id", [])
        return {str(row["name"]): str(row["hash"]) for row in rows}

    def record_migration(self, schema: str, name: str, hash: str) -> tuple[str, list[object]]:
        table = ddl.qualified(schema, MIGRATIONS_TABLE)
        return f"INSERT INTO {table} (name, hash) VALUES ($1, $2)", [name, hash]

    def forget_migration(self, schema: str, name: str) -> tuple[str, list[object]]:
        table = ddl.qualified(schema, MIGRATIONS_TABLE)
        return f"DELETE FROM {table} WHERE name {ddl.EQ} $1", [name]

"""The dialect interface. ``core`` and ``kit`` talk to databases only through these protocols.

Adding a database means a new ``dialects/<name>/`` package that implements them.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol

from mizzle.core.compiler import Compiler
from mizzle.core.naming import Casing
from mizzle.core.query.base import Row
from mizzle.core.schema.column import ColumnBase
from mizzle.core.snapshot import Snapshot
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import Statement, Step

type IsolationLevel = Literal["read committed", "repeatable read", "serializable"]


class Connection(Protocol):
    """One database connection. Errors are raised as mizzle errors, with SQL attached.
    ``timeout`` (seconds) overrides the pool's ``query_timeout`` for one statement."""

    async def fetch(
        self, sql: str, params: Sequence[object], timeout: float | None = None
    ) -> list[Row]: ...
    async def fetch_dicts(
        self, sql: str, params: Sequence[object], timeout: float | None = None
    ) -> list[dict[str, object]]: ...
    async def execute(
        self, sql: str, params: Sequence[object], timeout: float | None = None
    ) -> int: ...
    def transaction(
        self, isolation: IsolationLevel | None = None, readonly: bool = False
    ) -> AbstractAsyncContextManager[None]: ...


class Driver(Protocol):
    """A connection pool."""

    def acquire(self) -> AbstractAsyncContextManager[Connection]: ...
    async def close(self) -> None: ...


class Dialect(Protocol):
    name: str
    default_schema: str

    # -- queries ----------------------------------------------------------------------
    def compiler(self, casing: Casing | None = None) -> Compiler: ...
    async def connect(
        self,
        url: str,
        *,
        pool_size: int,
        statement_cache_size: int,
        pool_min_size: int = 1,
        max_idle: float = 300.0,
        query_timeout: float | None = None,
    ) -> Driver: ...

    # -- schema snapshots (normalized so a schema and its database compare equal) -----
    def column_type(self, column: ColumnBase) -> str: ...
    def column_default(self, column: ColumnBase, casing: Casing | None) -> str | None: ...
    def expression(self, node: Node, casing: Casing | None) -> str: ...
    def truncate_name(self, name: str) -> str: ...
    async def introspect(self, conn: Connection, schemas: Sequence[str]) -> Snapshot: ...

    # -- migrations -------------------------------------------------------------------
    def render(self, steps: Sequence[Step]) -> list[Statement]: ...
    async def acquire_migration_lock(self, conn: Connection, timeout: float) -> None: ...
    async def release_migration_lock(self, conn: Connection) -> None: ...
    async def ensure_migrations_table(self, conn: Connection, schema: str) -> None: ...
    async def applied_migrations(self, conn: Connection, schema: str) -> dict[str, str]: ...
    def record_migration(self, schema: str, name: str, hash: str) -> tuple[str, list[object]]: ...

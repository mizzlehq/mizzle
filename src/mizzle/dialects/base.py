"""The dialect interface. ``core`` and ``kit`` talk to databases only through these protocols.

Adding a database means a new ``dialects/<name>/`` package that implements them.
"""

from __future__ import annotations

from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from mizzle.core.compiler import Compiler
from mizzle.core.naming import Casing
from mizzle.core.query.base import Row
from mizzle.core.schema.column import ColumnBase
from mizzle.core.snapshot import Snapshot
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import Statement, Step

type IsolationLevel = Literal["read committed", "repeatable read", "serializable"]
# "auto" (default): reset session state (SET, LISTEN, advisory locks...) when a connection
# goes back to the pool only after statements that may have changed it: raw SQL, functions
# named with func(), direct connection use. "always": after every use (see Database.connect
# for the one case "auto" cannot see).
type ResetMode = Literal["always", "auto"]


@dataclass(frozen=True)
class PoolStats:
    """A snapshot of one connection pool, from ``db.pool_stats()``."""

    size: int  # connections currently open
    idle: int  # idle in the pool
    in_use: int  # handed out right now
    max_size: int
    min_size: int
    wait_ms: float  # how long the most recent acquire() had to wait for a connection


class Connection(Protocol):
    """One database connection. Errors are raised as mizzle errors, with SQL attached.
    ``timeout`` (seconds) overrides the pool's ``query_timeout`` for one statement.
    ``session_safe=True`` promises the statement cannot change session state (SET, LISTEN,
    advisory locks, cursors), so a pool may skip resetting the connection afterwards."""

    async def fetch(
        self,
        sql: str,
        params: Sequence[object],
        timeout: float | None = None,
        *,
        session_safe: bool = False,
    ) -> list[Row]: ...
    async def fetch_dicts(
        self, sql: str, params: Sequence[object], timeout: float | None = None
    ) -> list[dict[str, object]]: ...
    async def execute(
        self,
        sql: str,
        params: Sequence[object],
        timeout: float | None = None,
        *,
        session_safe: bool = False,
    ) -> int: ...
    async def executemany(
        self, sql: str, params: Sequence[Sequence[object]], timeout: float | None = None
    ) -> int:
        """Run one parameterized statement for each parameter set, pipelined; returns the
        number of sets."""
        ...

    def transaction(
        self, isolation: IsolationLevel | None = None, readonly: bool = False
    ) -> AbstractAsyncContextManager[None]: ...
    def cursor(
        self,
        sql: str,
        params: Sequence[object],
        batch_size: int,
        timeout: float | None = None,
        *,
        session_safe: bool = False,
    ) -> AsyncGenerator[list[Row]]:
        """Rows in batches from a server-side cursor; only inside a transaction."""
        ...


class Driver(Protocol):
    """A connection pool."""

    def acquire(self) -> AbstractAsyncContextManager[Connection]: ...
    def stats(self) -> PoolStats:
        """Current pool counters and the last acquire() wait time."""
        ...

    async def close(self) -> None: ...
    async def expire_connections(self) -> None:
        """Replace every pooled connection (idle ones now, busy ones when released)."""
        ...


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
        reset_connections: ResetMode = "auto",
        on_connect: Callable[[Connection], Awaitable[None]] | None = None,
        server_settings: Mapping[str, str] | None = None,
        connect_timeout: float = 10.0,
        acquire_timeout: float | None = 30.0,
    ) -> Driver: ...

    # -- schema snapshots (normalized so a schema and its database compare equal) -----
    def column_type(self, column: ColumnBase) -> str: ...
    def column_default(self, column: ColumnBase, casing: Casing | None) -> str | None: ...
    def normalize_default(self, text: str | None) -> str | None:
        """A SQL default expression as it compares in snapshots (casts normalized)."""
        ...

    def normalize_type(self, sql_type: str) -> str:
        """A SQL type as it compares in snapshots (``varchar`` -> ``character varying``)."""
        ...

    def same_type(self, a: str, b: str) -> bool:
        """Whether two normalized spellings name one type (an older snapshot's, K8-2)."""
        ...

    def expression(self, node: Node, casing: Casing | None) -> str: ...
    def truncate_name(self, name: str) -> str: ...
    def view_definition(self, query: Node, casing: Casing | None) -> str: ...

    async def introspect(
        self, conn: Connection, schemas: Sequence[str], roles: Sequence[str] = ()
    ) -> Snapshot:
        """The database's schema. ``roles``: the role names to read (roles belong to the
        whole server, so only the ones a schema manages are ever read)."""
        ...

    async def skipped_objects(self, conn: Connection, schemas: Sequence[str]) -> list[str]:
        """Objects ``introspect`` does not read, described for a warning (K8)."""
        ...

    async def normalize_snapshot(self, conn: Connection, snapshot: Snapshot) -> Snapshot:
        """``snapshot`` with view queries and other expressions as the database prints
        them back, so it compares equal to an introspected one. Nothing is changed."""
        ...

    # -- migrations -------------------------------------------------------------------
    def render(self, steps: Sequence[Step], *, qualify: bool = False) -> list[Statement]: ...
    async def acquire_migration_lock(self, conn: Connection, timeout: float) -> None: ...
    async def release_migration_lock(self, conn: Connection) -> None: ...
    async def ensure_migrations_table(self, conn: Connection, schema: str) -> None: ...
    async def drop_migrations_table(self, conn: Connection, schema: str) -> None: ...
    async def applied_migrations(self, conn: Connection, schema: str) -> dict[str, str]: ...
    def record_migration(self, schema: str, name: str, hash: str) -> tuple[str, list[object]]: ...
    def forget_migration(self, schema: str, name: str) -> tuple[str, list[object]]: ...

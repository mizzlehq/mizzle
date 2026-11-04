"""The dialect interface. ``core`` and ``kit`` talk to databases only through these protocols.

Adding a database means a new ``dialects/<name>/`` package that implements them.
"""

from __future__ import annotations

import os
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol

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
type CopyFormat = Literal["csv", "text", "binary"]
# What a listener does when ``max_queue`` notifications wait unread: "raise" ends the
# iteration with ListenerOverflow, "drop_oldest" discards the oldest one (with a warning).
type Overflow = Literal["raise", "drop_oldest", "block"]
# Where copy_from() writes: a file path, a binary file, or an async function given each chunk.
type CopyOutput = str | os.PathLike[str] | BinaryIO | Callable[[bytes], Awaitable[None]]


@dataclass(frozen=True)
class Notification:
    """A ``NOTIFY`` received by ``db.listen()``."""

    channel: str
    payload: str
    pid: int  # server process ID of the session that sent it
    data: object = None  # the payload run through ``listen(decoder=...)``; None otherwise


@dataclass(frozen=True)
class Reconnected:
    """Yielded by ``db.listen(..., reconnect=True)`` after the connection was lost and
    LISTEN started again on a new one. Notifications sent in between were missed: reload
    whatever state they would have told you about."""


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

    async def copy_records(
        self,
        table: str,
        schema: str | None,
        columns: Sequence[str],
        records: Iterable[Sequence[object]],
        timeout: float | None = None,
    ) -> int:
        """Bulk load with COPY; returns the number of rows."""
        ...

    async def copy_out(
        self,
        sql: str,
        params: Sequence[object],
        output: CopyOutput,
        format: CopyFormat,
        header: bool,
        timeout: float | None = None,
        *,
        session_safe: bool = False,
    ) -> int:
        """``COPY (sql) TO`` ``output``; returns the number of rows."""
        ...

    def listen(
        self,
        channels: Sequence[str],
        max_queue: int | None = None,
        overflow: Overflow = "raise",
        probe: Callable[[str], Awaitable[None]] | None = None,
        pooler_session_mode: bool = False,
        decoder: Callable[[str], object] | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[Notification]]:
        """LISTEN until the context exits. The iterator raises ConnectionError if the
        connection is lost. Behind a pooler it raises unless ``pooler_session_mode``; then
        ``probe(channel)`` sends a notification from another connection to check that
        notifications reach this one. ``decoder`` (e.g. ``json.loads``) decodes each payload
        into ``Notification.data``."""
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

    async def notify_unpooled(self, channel: str, payload: str = "") -> None:
        """``pg_notify`` on a short-lived connection outside the pool."""
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

    def expression(self, node: Node, casing: Casing | None) -> str: ...
    def truncate_name(self, name: str) -> str: ...
    def view_definition(self, query: Node, casing: Casing | None) -> str: ...
    def type_extension(self, sql_type: str) -> str | None:
        """The extension a column type needs (``vector`` -> ``"vector"``), if any."""
        ...

    async def introspect(
        self, conn: Connection, schemas: Sequence[str], roles: Sequence[str] = ()
    ) -> Snapshot:
        """The database's schema. ``roles``: the role names to read (roles belong to the
        whole server, so only the ones a schema manages are ever read)."""
        ...

    async def normalize_snapshot(self, conn: Connection, snapshot: Snapshot) -> Snapshot:
        """``snapshot`` with view queries and other expressions as the database prints
        them back, so it compares equal to an introspected one. Nothing is changed."""
        ...

    # -- migrations -------------------------------------------------------------------
    def render(self, steps: Sequence[Step]) -> list[Statement]: ...
    async def acquire_migration_lock(self, conn: Connection, timeout: float) -> None: ...
    async def release_migration_lock(self, conn: Connection) -> None: ...
    async def ensure_migrations_table(self, conn: Connection, schema: str) -> None: ...
    async def drop_migrations_table(self, conn: Connection, schema: str) -> None: ...
    async def applied_migrations(self, conn: Connection, schema: str) -> dict[str, str]: ...
    def record_migration(self, schema: str, name: str, hash: str) -> tuple[str, list[object]]: ...
    def forget_migration(self, schema: str, name: str) -> tuple[str, list[object]]: ...

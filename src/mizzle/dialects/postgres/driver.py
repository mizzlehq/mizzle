"""asyncpg-backed driver: pool, connections, transactions, and error mapping."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Literal

import asyncpg
from asyncpg import exceptions as pg
from asyncpg.pool import PoolConnectionProxy

from mizzle.core.errors import (
    CheckViolation,
    DatabaseError,
    ForeignKeyViolation,
    MizzleError,
    NotNullViolation,
    SerializationFailure,
    UniqueViolation,
)
from mizzle.core.errors import ConnectionError as DbConnectionError
from mizzle.core.query.base import Row
from mizzle.dialects.base import IsolationLevel

type _AsyncpgIsolation = Literal["read_committed", "repeatable_read", "serializable"]
_ISOLATION: dict[IsolationLevel, _AsyncpgIsolation] = {
    "read committed": "read_committed",
    "repeatable read": "repeatable_read",
    "serializable": "serializable",
}


def map_error(exc: BaseException, sql: str) -> MizzleError | None:
    """Translate an asyncpg error. The message never includes ``exc.detail``, which can
    contain row values (e.g. "Key (email)=(a@b.c) already exists")."""
    match exc:
        case pg.UniqueViolationError():
            return UniqueViolation(
                exc.message, sql=sql, original=exc, constraint=exc.constraint_name
            )
        case pg.ForeignKeyViolationError():
            return ForeignKeyViolation(
                exc.message, sql=sql, original=exc, constraint=exc.constraint_name
            )
        case pg.NotNullViolationError():
            return NotNullViolation(exc.message, sql=sql, original=exc, column=exc.column_name)
        case pg.CheckViolationError():
            return CheckViolation(
                exc.message, sql=sql, original=exc, constraint=exc.constraint_name
            )
        case pg.SerializationError() | pg.DeadlockDetectedError():
            return SerializationFailure(exc.message, sql=sql, original=exc)
        case pg.PostgresConnectionError() | pg.ConnectionDoesNotExistError() | OSError():
            return DbConnectionError(f"{type(exc).__name__}: {exc}", sql=sql, original=exc)
        case asyncpg.PostgresError() if not exc.message:
            # Raised client-side by asyncpg while encoding params; its text includes the value.
            message = f"{type(exc).__name__}: a query parameter has the wrong type (value hidden)"
            return DatabaseError(message, sql=sql, original=exc)
        case asyncpg.PostgresError() if (exc.sqlstate or "").startswith("22"):
            # SQLSTATE class 22 (data exception) messages quote the bad value, e.g.
            # 'invalid input value for enum role: "x"'. The full text stays on .original.
            message = (
                f"{type(exc).__name__} (SQLSTATE {exc.sqlstate}): invalid value (value hidden)"
            )
            return DatabaseError(message, sql=sql, original=exc)
        case asyncpg.PostgresError():
            return DatabaseError(exc.message, sql=sql, original=exc)
        case _:
            return None


def _translate(exc: Exception, sql: str) -> Exception:
    """Map ``exc``; callers raise the result ``from None`` so tracebacks do not print the
    driver error, whose DETAIL can hold row values. It stays reachable as ``.original``."""
    mapped = map_error(exc, sql)
    if mapped is None:
        return exc
    return mapped


def _rowcount(status: str) -> int:
    # Command tags: "INSERT 0 3", "UPDATE 2", "DELETE 1", "SELECT 5"
    last = status.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else 0


class PostgresConnection:
    _conn: asyncpg.Connection | PoolConnectionProxy

    def __init__(self, conn: asyncpg.Connection | PoolConnectionProxy) -> None:
        self._conn = conn

    async def fetch(self, sql: str, params: Sequence[object]) -> list[Row]:
        try:
            return list(await self._conn.fetch(sql, *params))
        except Exception as exc:
            raise _translate(exc, sql) from None

    async def fetch_dicts(self, sql: str, params: Sequence[object]) -> list[dict[str, object]]:
        try:
            return [dict(record) for record in await self._conn.fetch(sql, *params)]
        except Exception as exc:
            raise _translate(exc, sql) from None

    async def execute(self, sql: str, params: Sequence[object]) -> int:
        try:
            return _rowcount(await self._conn.execute(sql, *params))
        except Exception as exc:
            raise _translate(exc, sql) from None

    @asynccontextmanager
    async def transaction(
        self, isolation: IsolationLevel | None = None, readonly: bool = False
    ) -> AsyncGenerator[None]:
        level = _ISOLATION[isolation] if isolation is not None else None
        try:
            async with self._conn.transaction(isolation=level, readonly=readonly):
                yield
        except asyncpg.PostgresError as exc:  # BEGIN / COMMIT failures (body errors are mapped)
            raise _translate(exc, "COMMIT") from None


async def _init_connection(conn: asyncpg.Connection) -> None:
    for name in ("json", "jsonb"):
        await conn.set_type_codec(name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class PostgresDriver:
    _pool: asyncpg.Pool

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(
        cls, url: str, *, pool_size: int = 10, statement_cache_size: int = 100
    ) -> PostgresDriver:
        """``statement_cache_size=0`` is needed behind PgBouncer in transaction mode."""
        try:
            pool = await asyncpg.create_pool(
                url,
                min_size=1,
                max_size=pool_size,
                statement_cache_size=statement_cache_size,
                init=_init_connection,
            )
        except (OSError, asyncpg.PostgresError) as exc:
            # The URL is left out on purpose: it may contain a password.
            raise DbConnectionError(f"could not connect: {type(exc).__name__}: {exc}") from exc
        return cls(pool)

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[PostgresConnection]:
        try:
            conn = await self._pool.acquire()
        except (OSError, asyncpg.PostgresError) as exc:
            raise DbConnectionError(f"could not acquire a connection: {exc}") from exc
        try:
            yield PostgresConnection(conn)
        finally:
            await self._pool.release(conn)

    async def close(self) -> None:
        await self._pool.close()

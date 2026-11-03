"""asyncpg-backed driver: pool, connections, transactions, and error mapping."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal, Self, cast, final

import asyncpg
from asyncpg import exceptions as pg
from asyncpg.pool import PoolConnectionProxy

from mizzle.core.errors import (
    CheckViolation,
    DatabaseError,
    ForeignKeyViolation,
    MizzleError,
    NotNullViolation,
    QueryTimeout,
    SerializationFailure,
    UniqueViolation,
)
from mizzle.core.errors import ConnectionError as DbConnectionError
from mizzle.core.query.base import Row
from mizzle.dialects.base import (
    CopyFormat,
    CopyOutput,
    IsolationLevel,
    Notification,
    ResetMode,
)

if TYPE_CHECKING:
    # Stub-only. It leaves out str paths, which asyncpg accepts (os.fspath).
    from asyncpg.connection import _OutputType  # pyright: ignore[reportPrivateUsage]

type _AsyncpgIsolation = Literal["read_committed", "repeatable_read", "serializable"]
log = logging.getLogger("mizzle")

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
        case pg.QueryCanceledError():  # statement_timeout on the server
            return QueryTimeout(exc.message, sql=sql, original=exc)
        case TimeoutError():  # client-side timeout; asyncpg cancels the statement
            return QueryTimeout("query timed out", sql=sql, original=exc)
        case pg.PostgresConnectionError() | pg.ConnectionDoesNotExistError() | OSError():
            return DbConnectionError(f"{type(exc).__name__}: {exc}", sql=sql, original=exc)
        case asyncpg.InterfaceError() | pg.InternalClientError() if _connection_broken(exc):
            # "connection is closed", or a connection the server closed mid-operation
            # ("cannot switch to state ...; another operation is in progress").
            return DbConnectionError(f"{type(exc).__name__}: {exc}", sql=sql, original=exc)
        case asyncpg.InterfaceError() | pg.InternalClientError():
            # Misuse such as a wrong number of arguments: not a lost connection, so the pool
            # is kept and reads are not retried. These messages hold no values.
            return DatabaseError(f"{type(exc).__name__}: {exc}", sql=sql, original=exc)
        case asyncpg.PostgresError() if not exc.message:
            # Raised client-side by asyncpg while encoding params; its text includes the value.
            message = (
                f"{type(exc).__name__}: a query parameter has the wrong type or cannot be "
                "encoded (value hidden)"
            )
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


_BROKEN_CONNECTION = ("closed", "another operation", "switch to state", "released back")


def _connection_broken(exc: BaseException) -> bool:
    text = str(exc)
    return any(marker in text for marker in _BROKEN_CONNECTION)


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
    dirty: bool  # ran a statement that may have changed session state

    def __init__(self, conn: asyncpg.Connection | PoolConnectionProxy) -> None:
        self._conn = conn
        self.dirty = False

    async def fetch(
        self,
        sql: str,
        params: Sequence[object],
        timeout: float | None = None,
        *,
        session_safe: bool = False,
    ) -> list[Row]:
        self.dirty |= not session_safe
        try:
            return list(await self._conn.fetch(sql, *params, timeout=timeout))
        except Exception as exc:
            raise _translate(exc, sql) from None

    async def fetch_dicts(
        self, sql: str, params: Sequence[object], timeout: float | None = None
    ) -> list[dict[str, object]]:
        self.dirty = True
        try:
            return [
                dict(record) for record in await self._conn.fetch(sql, *params, timeout=timeout)
            ]
        except Exception as exc:
            raise _translate(exc, sql) from None

    async def execute(
        self,
        sql: str,
        params: Sequence[object],
        timeout: float | None = None,
        *,
        session_safe: bool = False,
    ) -> int:
        self.dirty |= not session_safe
        try:
            return _rowcount(await self._conn.execute(sql, *params, timeout=timeout))
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
        # BEGIN / COMMIT failures (body errors are mapped), and a connection lost before
        # ROLLBACK (asyncpg's InterfaceError: "the underlying connection is closed").
        except (asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            raise _translate(exc, "COMMIT") from None

    async def cursor(
        self,
        sql: str,
        params: Sequence[object],
        batch_size: int,
        timeout: float | None = None,
        *,
        session_safe: bool = False,
    ) -> AsyncGenerator[list[Row]]:
        # A protocol-level portal: it ends with the transaction, so it needs no reset.
        self.dirty |= not session_safe
        try:
            cursor = await self._conn.cursor(sql, *params, timeout=timeout)
            while batch := await cursor.fetch(batch_size, timeout=timeout):
                yield list(batch)
        except Exception as exc:
            raise _translate(exc, sql) from None

    async def copy_records(
        self,
        table: str,
        schema: str | None,
        columns: Sequence[str],
        records: Iterable[Sequence[object]],
        timeout: float | None = None,
    ) -> int:
        sql = f"COPY {table} FROM STDIN"
        try:
            status = await self._conn.copy_records_to_table(
                table, records=records, columns=columns, schema_name=schema, timeout=timeout
            )
        except MizzleError:
            raise  # a row the caller's checks rejected
        except (ValueError, TypeError, OverflowError) as exc:
            # asyncpg encodes COPY values in Python; its messages can quote the value.
            raise DatabaseError(
                f"{type(exc).__name__}: a COPY value has the wrong type or cannot be encoded "
                "(value hidden)",
                sql=sql,
                original=exc,
            ) from None
        except Exception as exc:
            raise _translate(exc, sql) from None
        return _rowcount(status)

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
        self.dirty |= not session_safe
        try:
            # With params, asyncpg inlines them: the server quotes each one (quote_literal).
            status = await self._conn.copy_from_query(
                sql,
                *params,
                output=cast("_OutputType", output),
                format=format,
                header=header or None,
                timeout=timeout,
            )
        except Exception as exc:
            raise _translate(exc, sql) from None
        return _rowcount(status)

    @asynccontextmanager
    async def listen(self, channels: Sequence[str]) -> AsyncGenerator[AsyncIterator[Notification]]:
        self.dirty = True  # the pool's reset runs UNLISTEN * even if removing a listener fails
        conn = self._conn
        # ponytail: unbounded; a consumer that never keeps up grows it without limit.
        queue = asyncio.Queue[Notification | None]()

        def on_notify(_conn: object, pid: int, channel: str, payload: object, /) -> None:
            queue.put_nowait(Notification(channel, str(payload), pid))

        def on_lost(_conn: object, /) -> None:
            queue.put_nowait(None)

        conn.add_termination_listener(on_lost)
        added: list[str] = []
        try:
            for channel in dict.fromkeys(channels):
                try:
                    await conn.add_listener(channel, on_notify)
                except Exception as exc:
                    raise _translate(exc, "LISTEN") from None
                added.append(channel)
            yield _Notifications(queue)
        finally:
            try:
                conn.remove_termination_listener(on_lost)
                lost = conn.is_closed()
            except asyncpg.InterfaceError:  # lost: the pool already took the proxy back
                lost = True
            for channel in () if lost else added:
                try:
                    await conn.remove_listener(channel, on_notify)
                except Exception:
                    log.warning(
                        "mizzle: could not UNLISTEN; the connection is reset", exc_info=True
                    )
                    break


@final
class _Notifications:
    """The iterator ``listen()`` yields. A class, not an async generator: a timeout or
    cancellation while waiting must not end the iteration."""

    __slots__ = ("_queue",)
    _queue: asyncio.Queue[Notification | None]

    def __init__(self, queue: asyncio.Queue[Notification | None]) -> None:
        self._queue = queue

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Notification:
        event = await self._queue.get()
        if event is None:
            self._queue.put_nowait(None)  # stays failed
            raise DbConnectionError(
                "the listen() connection was lost; notifications sent meanwhile were missed"
            )
        return event


@final
class _PoolConnection(asyncpg.Connection):
    """Remembers whether it needs a reset before the next user gets it.

    asyncpg resets every released connection (``SELECT pg_advisory_unlock_all(); CLOSE ALL;
    UNLISTEN *; RESET ALL``): one extra round trip per query, ~40% of a primary key lookup.
    With ``reset_connections="auto"`` mizzle only pays it when a statement may have changed
    session state: raw SQL, or any direct use of the connection. Open transactions are
    always rolled back by asyncpg.
    """

    _mizzle_dirty: bool = False  # no __slots__ here, so instances can set it

    def mark_dirty(self) -> None:
        self._mizzle_dirty = True

    def take_dirty(self) -> bool:
        dirty, self._mizzle_dirty = self._mizzle_dirty, False
        return dirty


async def _reset_connection(conn: asyncpg.Connection) -> None:
    if isinstance(conn, _PoolConnection) and conn.take_dirty():
        await conn.execute(conn.get_reset_query())


def _jsonb_encode(value: object) -> bytes:
    return b"\x01" + json.dumps(value).encode()  # binary jsonb: a version byte, then text


def _jsonb_decode(data: bytes) -> object:
    return json.loads(data[1:])  # pyright: ignore[reportAny]


def _json_encode(value: object) -> bytes:
    return json.dumps(value).encode()


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Binary format, so binary COPY (copy_into) can send JSON too.
    await conn.set_type_codec(
        "json", encoder=_json_encode, decoder=json.loads, schema="pg_catalog", format="binary"
    )
    await conn.set_type_codec(
        "jsonb", encoder=_jsonb_encode, decoder=_jsonb_decode, schema="pg_catalog", format="binary"
    )


@final
class _Acquire:
    """``async with driver.acquire() as conn``. A plain class: this runs for every query,
    and is cheaper than an ``@asynccontextmanager`` generator."""

    __slots__ = ("_conn", "_pool", "_wrapper")
    _pool: asyncpg.Pool
    _conn: PoolConnectionProxy | None
    _wrapper: PostgresConnection | None

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._conn = None
        self._wrapper = None

    async def __aenter__(self) -> PostgresConnection:
        try:
            self._conn = await self._pool.acquire()
        except (OSError, asyncpg.PostgresError) as exc:
            raise DbConnectionError(f"could not acquire a connection: {exc}") from exc
        self._wrapper = PostgresConnection(self._conn)
        return self._wrapper

    async def __aexit__(self, *exc_info: object) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            dirty = self._wrapper is None or self._wrapper.dirty
            self._wrapper = None
            try:
                if dirty:
                    # The proxy forwards to the _PoolConnection the pool was created with.
                    cast(_PoolConnection, cast(object, conn)).mark_dirty()
                await self._pool.release(conn)
            except Exception:
                # The connection was lost (the proxy is detached: AttributeError) or its
                # reset after use failed; either way asyncpg has terminated it already.
                # Not raised: the statement itself finished, and raising here would make a
                # completed write look failed.
                log.warning("mizzle: could not return a connection to the pool", exc_info=True)


class PostgresDriver:
    _pool: asyncpg.Pool

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        pool_size: int = 10,
        statement_cache_size: int = 100,
        pool_min_size: int = 1,
        max_idle: float = 300.0,
        query_timeout: float | None = None,
        reset_connections: ResetMode = "always",
    ) -> PostgresDriver:
        """``statement_cache_size=0`` is needed behind PgBouncer in transaction mode.
        ``max_idle`` closes connections unused for that many seconds (0 keeps them)."""
        try:
            pool = await asyncpg.create_pool(
                url,
                min_size=pool_min_size,
                max_size=pool_size,
                max_inactive_connection_lifetime=max_idle,
                command_timeout=query_timeout,
                statement_cache_size=statement_cache_size,
                init=_init_connection,
                connection_class=_PoolConnection,
                # None: asyncpg's own full reset on every release.
                reset=_reset_connection if reset_connections == "auto" else None,
            )
        except (OSError, asyncpg.PostgresError) as exc:
            # The URL is left out on purpose: it may contain a password.
            raise DbConnectionError(f"could not connect: {type(exc).__name__}: {exc}") from exc
        return cls(pool)

    def acquire(self) -> _Acquire:
        return _Acquire(self._pool)

    async def close(self) -> None:
        await self._pool.close()

    async def expire_connections(self) -> None:
        await self._pool.expire_connections()

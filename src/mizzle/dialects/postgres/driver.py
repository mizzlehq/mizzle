"""asyncpg-backed driver: pool, connections, transactions, and error mapping."""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import math
import re
import time
import uuid
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Self, cast, final

import asyncpg
from asyncpg import exceptions as pg
from asyncpg.pool import PoolConnectionProxy

from mizzle.core.errors import (
    CheckViolation,
    DatabaseError,
    ForeignKeyViolation,
    ListenerOverflow,
    MizzleError,
    NotNullViolation,
    QueryError,
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
    Overflow,
    PoolStats,
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
                exc.message,
                sql=sql,
                original=exc,
                constraint=exc.constraint_name,
                table=exc.table_name,
            )
        case pg.ForeignKeyViolationError() | pg.RestrictViolationError():
            return ForeignKeyViolation(
                exc.message,
                sql=sql,
                original=exc,
                constraint=exc.constraint_name,
                table=exc.table_name,
            )
        case pg.NotNullViolationError():
            return NotNullViolation(
                exc.message, sql=sql, original=exc, column=exc.column_name, table=exc.table_name
            )
        case pg.CheckViolationError():
            return CheckViolation(
                exc.message,
                sql=sql,
                original=exc,
                constraint=exc.constraint_name,
                table=exc.table_name,
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
        case pg.PostgresSyntaxError() if needs := _needs_newer_server(exc.message, sql):
            return DatabaseError(f"{needs} ({exc.message})", sql=sql, original=exc)
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


# SQL that older servers reject as a syntax error: (word they stop at, feature, version,
# another token the SQL must contain so the word is not matched in an unrelated statement).
_NEWER_SYNTAX = (
    ("EXPRESSION", "ALTER COLUMN ... SET EXPRESSION", 17, None),
    ("VIRTUAL", "VIRTUAL generated columns", 18, None),
    ("MERGE", "MERGE", 15, None),
    ("RETURNING", "MERGE ... RETURNING", 17, "MERGE"),
    ("SOURCE", "MERGE ... WHEN NOT MATCHED BY SOURCE", 17, "MERGE"),
    # PG15/16 stop one word earlier, at BY (B12).
    ("BY", "MERGE ... WHEN NOT MATCHED BY SOURCE", 17, "NOT MATCHED BY SOURCE"),
    ("NULLS", "a unique constraint or index with NULLS NOT DISTINCT", 15, "NULLS NOT DISTINCT"),
)


def _needs_newer_server(message: str, sql: str) -> str | None:
    upper = sql.upper()
    near = message.upper()  # the server quotes the word as it was written
    for word, feature, version, also in _NEWER_SYNTAX:
        if f'AT OR NEAR "{word}"' in near and word in upper and (also is None or also in upper):
            return f"{feature} needs PostgreSQL {version} or later"
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


def _float_text(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return str(Decimal(value))  # the exact binary value: float4 then rounds it correctly


def _float_literal(value: object, type_name: str) -> str | None:
    """An exact literal for a float4/float8 (or array) parameter; None for other types
    (and for ints too big for a float, which the server then rejects as usual)."""
    try:
        return _exact_float_literal(value, type_name)
    except OverflowError:
        return None


def _exact_float_literal(value: object, type_name: str) -> str | None:
    if type_name in ("float4", "float8") and isinstance(value, float | int):
        return f"'{_float_text(float(value))}'"
    if type_name in ("float4[]", "float8[]") and isinstance(value, list | tuple):
        items = cast(Sequence[object], value)
        if all(isinstance(v, float | int) for v in items):
            body = ",".join(_float_text(float(cast(float, v))) for v in items)
            return f"'{{{body}}}'"
    return None


_PARAM_OR_QUOTED = re.compile(
    r"\$(\d+)\b"
    r"|[eE]'(?:[^'\\]|\\.|'')*'"
    r"|'(?:[^']|'')*'"
    r'|"(?:[^"]|"")*"'
    r"|--[^\n]*"
    r"|/\*.*?\*/"
    r"|\$([A-Za-z_][A-Za-z0-9_]*)?\$.*?\$\2\$",
    re.DOTALL,
)


def _qualified(schema: str, name: str) -> str:
    return ".".join('"' + part.replace('"', '""') + '"' for part in (schema, name))


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

    async def executemany(
        self, sql: str, params: Sequence[Sequence[object]], timeout: float | None = None
    ) -> int:
        self.dirty = True
        try:
            # asyncpg pipelines these in as few round trips as it can.
            await self._conn.executemany(sql, params, timeout=timeout)
        except Exception as exc:
            raise _translate(exc, sql) from None
        return len(params)

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
        # InternalClientError: BEGIN on a connection the server closed under load.
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            pg.InternalClientError,
            OSError,
        ) as exc:
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
            # COPY takes no bind parameters, so their values are written into the SQL.
            if params:
                async with asyncio.timeout(timeout):
                    query = await self._inline(sql, params)
            else:
                query = sql
            status = await self._conn.copy_from_query(
                query,
                output=cast("_OutputType", output),
                format=format,
                header=header or None,
                timeout=timeout,
            )
        except Exception as exc:
            raise _translate(exc, sql) from None
        return _rowcount(status)

    async def _inline(self, sql: str, params: Sequence[object]) -> str:
        """``sql`` with each ``$n`` replaced by its value as a quoted literal. Like asyncpg's
        own inlining (the server quotes each value), except floats: the server prints those
        per ``extra_float_digits`` (0 rounds to 15 digits), so they are written here exactly."""
        statement = await self._conn.prepare(sql)
        literals: list[str | None] = []
        casts: list[str] = []
        values: list[object] = []
        for value, param in zip(params, statement.get_parameters(), strict=True):
            exact = _float_literal(value, param.name)
            if exact is not None or value is None:
                literals.append(exact or "NULL")
                continue
            name = f"_{param.name[:-2]}" if param.name.endswith("[]") else param.name
            values.append(value)
            casts.append(f"quote_literal(${len(values)}::{_qualified(param.schema, name)}::text)")
            literals.append(None)
        row = await self._conn.fetchrow(f"SELECT {', '.join(casts)}", *values) if casts else None
        quoted = iter(row or ())
        texts = [text if text is not None else cast(str, next(quoted)) for text in literals]
        # Quoted text, identifiers, comments and dollar-quoted bodies keep their "$n" as is.
        return _PARAM_OR_QUOTED.sub(
            lambda m: texts[int(m.group(1)) - 1] if m.group(1) else m.group(0), sql
        )

    @asynccontextmanager
    async def listen(
        self,
        channels: Sequence[str],
        max_queue: int | None = None,
        overflow: Overflow = "raise",
        probe: Callable[[str], Awaitable[None]] | None = None,
        pooler_session_mode: bool = False,
        decoder: Callable[[str], object] | None = None,
    ) -> AsyncGenerator[AsyncIterator[Notification]]:
        self.dirty = True  # the pool's reset runs UNLISTEN * even if removing a listener fails
        conn = self._conn
        transport = _read_transport(conn) if overflow == "block" else None
        if overflow == "block" and transport is None:
            raise QueryError(
                'listen(overflow="block") needs to pause reading the connection, which this '
                'asyncpg version does not allow; use "raise" or "drop_oldest"'
            )
        paused = False

        def resume() -> None:
            nonlocal paused
            if paused and transport is not None:
                paused = False
                transport.resume_reading()

        # Unbounded so the final _LOST / _OVERFLOW always fits; max_queue is checked here.
        queue = asyncio.Queue[Notification | _End]()
        ended = False
        dropped = 0

        def end(signal: _End) -> None:
            nonlocal ended
            if not ended:
                ended = True
                queue.put_nowait(signal)

        def on_notify(_conn: object, pid: int, channel: str, payload: object, /) -> None:
            nonlocal dropped
            if ended:
                return
            if max_queue is not None and queue.qsize() + 1 >= max_queue and transport:
                # "block": keep this one, then stop reading the socket; PostgreSQL holds
                # later notifications until the consumer catches up (resume()).
                nonlocal paused
                queue.put_nowait(Notification(channel, str(payload), pid))
                if not paused:
                    paused = True
                    transport.pause_reading()
                return
            if max_queue is not None and queue.qsize() >= max_queue:
                if overflow == "raise":
                    end(_End.OVERFLOW)
                    return
                queue.get_nowait()  # a Notification: nothing follows an _End
                dropped += 1
                if dropped & (dropped - 1) == 0:  # 1, 2, 4, 8...: not once per message
                    log.warning(
                        "mizzle: listen() consumer is %d notifications behind; dropped the "
                        "oldest (%d so far)",
                        max_queue,
                        dropped,
                    )
            queue.put_nowait(Notification(channel, str(payload), pid))

        def on_lost(_conn: object, /) -> None:
            end(_End.LOST)

        conn.add_termination_listener(on_lost)
        added: list[str] = []
        try:
            pooled = probe is not None and await self._behind_pooler()
            if pooled and not pooler_session_mode:
                # A test notification cannot prove delivery in transaction mode: one can
                # arrive while PgBouncer happens to keep this client on the LISTEN session.
                raise DatabaseError(
                    "listen() is connected through a connection pooler. PgBouncer in "
                    "transaction or statement mode drops notifications. Connect listen() "
                    "directly to PostgreSQL, or pass pooler_session_mode=True if the "
                    "pooler runs in session mode"
                )
            for channel in dict.fromkeys(channels):
                try:
                    await conn.add_listener(channel, on_notify)
                except Exception as exc:
                    raise _translate(exc, "LISTEN") from None
                added.append(channel)
            if pooled and probe is not None:  # session mode: still check it works
                await self._check_delivery(probe)
            yield _Notifications(queue, resume if max_queue is not None else None, decoder)
        finally:
            resume()  # UNLISTEN below needs the socket read again
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

    async def _behind_pooler(self) -> bool:
        # PgBouncer and other poolers report their own process ID at startup.
        try:
            backend = cast(int, await self._conn.fetchval("SELECT pg_backend_pid()"))
        except Exception as exc:
            raise _translate(exc, "SELECT pg_backend_pid()") from None
        return backend != self._conn.get_server_pid()

    async def _check_delivery(self, probe: Callable[[str], Awaitable[None]]) -> None:
        """Through a pooler, check that a notification sent from another connection
        arrives. PgBouncer in transaction mode drops them: the server session that ran
        LISTEN goes back to its pool, and notifications to an idle one are discarded."""
        conn = self._conn
        channel = f"mizzle_probe_{uuid.uuid4().hex}"
        arrived = asyncio.Event()

        def on_probe(*_: object) -> None:
            arrived.set()

        sent = False
        try:
            await conn.add_listener(channel, on_probe)
            async with asyncio.timeout(_PROBE_TIMEOUT):
                await probe(channel)
                sent = True
                await arrived.wait()
        except TimeoutError:
            if sent:
                raise DatabaseError(
                    "listen() cannot receive notifications through this connection: a test "
                    f"notification sent from another connection did not arrive within "
                    f"{_PROBE_TIMEOUT:g}s. PgBouncer in transaction or statement mode drops "
                    "them; connect listen() directly to PostgreSQL (or through session mode)"
                ) from None
            raise DatabaseError(
                "listen() is connected through a connection pooler and could not send a "
                f"test notification within {_PROBE_TIMEOUT:g}s to check that notifications "
                "arrive; connect listen() directly to PostgreSQL (or through session mode)"
            ) from None
        except Exception as exc:
            raise _translate(exc, "LISTEN") from None
        finally:
            try:
                await conn.remove_listener(channel, on_probe)
            except Exception:
                log.debug("mizzle: could not UNLISTEN the probe channel", exc_info=True)


# How long listen() waits for its test notification when connected through a pooler.
_PROBE_TIMEOUT = 2.0


class _End(enum.Enum):
    LOST = "lost"
    OVERFLOW = "overflow"


def _read_transport(conn: object) -> asyncio.ReadTransport | None:
    """The asyncio transport under an asyncpg connection (or pool proxy), to pause reading.
    asyncpg has no public API for this; None if its internals change."""
    transport: object = getattr(conn, "_transport", None)
    return transport if isinstance(transport, asyncio.ReadTransport) else None


@final
class _Notifications:
    """The iterator ``listen()`` yields. A class, not an async generator: a timeout or
    cancellation while waiting must not end the iteration."""

    __slots__ = ("_decoder", "_on_take", "_queue")
    _queue: asyncio.Queue[Notification | _End]
    _on_take: Callable[[], None] | None
    _decoder: Callable[[str], object] | None

    def __init__(
        self,
        queue: asyncio.Queue[Notification | _End],
        on_take: Callable[[], None] | None = None,
        decoder: Callable[[str], object] | None = None,
    ) -> None:
        self._queue = queue
        self._on_take = on_take
        self._decoder = decoder

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Notification:
        event = await self._queue.get()
        if isinstance(event, Notification):
            if self._on_take is not None and self._queue.empty():
                self._on_take()  # overflow="block": caught up, read the socket again
            if self._decoder is not None:
                return Notification(
                    event.channel, event.payload, event.pid, self._decoder(event.payload)
                )
            return event
        self._queue.put_nowait(event)  # stays failed
        if event is _End.OVERFLOW:
            raise ListenerOverflow(
                "the listen() consumer fell max_queue notifications behind; later ones "
                "were not kept"
            )
        raise DbConnectionError(
            "the listen() connection was lost; notifications sent meanwhile were missed"
        )


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
    # The on_connect hook the pool was created with, so a reset can re-run it (R1).
    _mizzle_on_connect: Callable[[PostgresConnection], Awaitable[None]] | None = None
    # "auto": reset only after a statement that may have changed session state. "always":
    # reset (and re-run the hook) on every release, as asyncpg does by default.
    _mizzle_reset_mode: ResetMode = "auto"

    def mark_dirty(self) -> None:
        self._mizzle_dirty = True

    def take_dirty(self) -> bool:
        dirty, self._mizzle_dirty = self._mizzle_dirty, False
        return dirty

    def set_on_connect(self, hook: Callable[[PostgresConnection], Awaitable[None]] | None) -> None:
        """Remember the pool's ``on_connect``, so a reset can run it again."""
        self._mizzle_on_connect = hook

    def set_reset_mode(self, mode: ResetMode) -> None:
        self._mizzle_reset_mode = mode

    async def reset_session(self) -> None:
        """Undo session state, then re-apply the ``on_connect`` hook.

        ``RESET ALL`` undoes whatever the hook set (a ``search_path``, a role), and asyncpg
        only runs ``init`` on a new connection, so the hook has to run again here. Otherwise
        it works on the first query and is silently gone after any raw statement.

        The hook runs *only* when the connection was actually reset (B1): running it on
        every release made each query pay for it, and ran a non-idempotent hook once per
        query. In ``"always"`` mode every release resets, so the hook re-runs every time.
        """
        if self._mizzle_reset_mode == "always":
            # asyncpg's full reset (open transaction rollback, RESET ALL, UNLISTEN, ...).
            # ``reset()`` calls ``_reset()`` and then the reset query; it does not call this
            # hook again, so there is no recursion.
            await self.reset()
            reset = True
        else:
            reset = self.take_dirty()
            if reset:
                await self.execute(self.get_reset_query())
        hook = self._mizzle_on_connect
        if hook is not None and reset:
            await hook(PostgresConnection(self))


async def _reset_connection(conn: asyncpg.Connection) -> None:
    if isinstance(conn, _PoolConnection):
        await conn.reset_session()


# SQLSTATE classes and codes that a retry cannot fix: bad credentials, no such database, a
# rejected connection. Sleeping and retrying these only delays the error.
_PERMANENT_SQLSTATES = (
    "28",  # invalid authorization specification (28P01 bad password, 28000 no auth)
    "3D",  # invalid catalog name (3D000: database does not exist)
    "0A",  # feature not supported
)


def _retryable(exc: BaseException) -> bool:
    """Whether connecting again could succeed. A superuser/failed-auth/unknown-database
    error is permanent; a refused or timed-out TCP connection is not."""
    code = str(getattr(exc, "sqlstate", "") or "")
    return not code.startswith(_PERMANENT_SQLSTATES)


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

    __slots__ = ("_conn", "_driver", "_pool", "_wrapper")
    _pool: asyncpg.Pool
    _driver: PostgresDriver
    _conn: PoolConnectionProxy | None
    _wrapper: PostgresConnection | None

    def __init__(self, pool: asyncpg.Pool, driver: PostgresDriver) -> None:
        self._pool = pool
        self._driver = driver
        self._conn = None
        self._wrapper = None

    async def __aenter__(self) -> PostgresConnection:
        start = time.perf_counter()
        try:
            self._conn = await self._pool.acquire()
        except (OSError, asyncpg.PostgresError) as exc:
            raise DbConnectionError(f"could not acquire a connection: {exc}") from exc
        self._driver.last_acquire_ms = (time.perf_counter() - start) * 1000
        self._wrapper = PostgresConnection(self._conn)
        return self._wrapper

    async def __aexit__(self, *exc_info: object) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            dirty = self._wrapper is None or self._wrapper.dirty
            self._wrapper = None
            # Checked before the release: asyncpg terminates a connection whose reset fails,
            # so afterwards it is always closed (B38).
            closed = _is_closed(conn)
            try:
                if dirty:
                    # The proxy forwards to the _PoolConnection the pool was created with.
                    cast(_PoolConnection, cast(object, conn)).mark_dirty()
                await self._pool.release(conn)
            except Exception:
                # The connection was lost (the proxy is detached: AttributeError) or its
                # reset after use failed; either way asyncpg has terminated it already.
                # Not raised: the statement itself finished, and raising here would make a
                # completed write look failed. A connection the server already closed (a
                # killed backend, a restart) is expected to fail here: not worth a warning.
                # A reset or on_connect hook that fails on a live connection is.
                level = logging.DEBUG if closed else logging.WARNING
                log.log(level, "mizzle: could not return a connection to the pool", exc_info=True)


def _is_closed(conn: asyncpg.Connection | PoolConnectionProxy) -> bool:
    try:
        return conn.is_closed()
    except Exception:  # a detached pool proxy: its connection is gone
        return True


class PostgresDriver:
    _pool: asyncpg.Pool
    _url: str | None  # for one-off connections outside the pool (never logged)
    last_acquire_ms: float

    def __init__(self, pool: asyncpg.Pool, url: str | None = None) -> None:
        self._pool = pool
        self._url = url
        self.last_acquire_ms = 0.0

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
        reset_connections: ResetMode = "auto",
        on_connect: Callable[[PostgresConnection], Awaitable[None]] | None = None,
        server_settings: Mapping[str, str] | None = None,
    ) -> PostgresDriver:
        """``statement_cache_size=0`` is needed behind PgBouncer in transaction mode.
        ``max_idle`` closes connections unused for that many seconds (0 keeps them).
        ``on_connect`` runs on each new connection (session settings, ``search_path``);
        ``server_settings`` sets PostgreSQL GUCs for every connection."""

        async def init(conn: asyncpg.Connection) -> None:
            await _init_connection(conn)
            if isinstance(conn, _PoolConnection):
                conn.set_on_connect(on_connect)
                conn.set_reset_mode(reset_connections)
            if on_connect is not None:
                await on_connect(PostgresConnection(conn))

        try:
            pool = await asyncpg.create_pool(
                url,
                min_size=pool_min_size,
                max_size=pool_size,
                max_inactive_connection_lifetime=max_idle,
                command_timeout=query_timeout,
                statement_cache_size=statement_cache_size,
                server_settings=None if server_settings is None else dict(server_settings),
                init=init,
                connection_class=_PoolConnection,
                # Never asyncpg's default reset: ours runs the reset only when needed ("auto",
                # after a dirty statement) or every time ("always"), and re-runs on_connect.
                reset=_reset_connection,
            )
        except (OSError, asyncpg.PostgresError) as exc:
            # The URL is left out on purpose: it may contain a password.
            error = DbConnectionError(f"could not connect: {type(exc).__name__}: {exc}")
            # A wrong password or a missing database does not fix itself: the caller's
            # retry loop must not sleep and try again (R4).
            error.retryable = _retryable(exc)
            raise error from exc
        return cls(pool, url)

    def acquire(self) -> _Acquire:
        return _Acquire(self._pool, self)

    def stats(self) -> PoolStats:
        size = self._pool.get_size()
        idle = self._pool.get_idle_size()
        return PoolStats(
            size=size,
            idle=idle,
            in_use=size - idle,
            max_size=self._pool.get_max_size(),
            min_size=self._pool.get_min_size(),
            wait_ms=self.last_acquire_ms,
        )

    async def close(self) -> None:
        await self._pool.close()

    async def expire_connections(self) -> None:
        await self._pool.expire_connections()

    async def notify_unpooled(self, channel: str, payload: str = "") -> None:
        """Send a notification on a short-lived connection outside the pool, so it works
        even when every pooled connection is busy (listen()'s pooler check)."""
        if self._url is None:
            raise DbConnectionError("no connection URL for a connection outside the pool")
        try:
            conn = await asyncpg.connect(self._url, statement_cache_size=0)
        except (OSError, asyncpg.PostgresError) as exc:
            raise DbConnectionError(f"could not connect: {type(exc).__name__}: {exc}") from exc
        try:
            await conn.execute("SELECT pg_notify($1, $2)", channel, payload)
        except Exception as exc:
            raise _translate(exc, "SELECT pg_notify($1, $2)") from None
        finally:
            await conn.close()

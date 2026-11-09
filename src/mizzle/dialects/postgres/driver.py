"""asyncpg-backed driver: pool, connections, transactions, and error mapping."""

from __future__ import annotations

import itertools
import json
import logging
import re
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import cast, final
from urllib.parse import parse_qsl, urlsplit

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
    IsolationLevel,
    PoolStats,
    ResetMode,
)
from mizzle.dialects.postgres.types import JsonText

log = logging.getLogger("mizzle")

# Also the check that a level is valid: it is written into the BEGIN statement.
_ISOLATION: dict[IsolationLevel, str] = {
    "read committed": "READ COMMITTED",
    "repeatable read": "REPEATABLE READ",
    "serializable": "SERIALIZABLE",
}


def map_error(exc: BaseException, sql: str, server: int | None = None) -> MizzleError | None:
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
        case pg.QueryCanceledError() if "timeout" in exc.message:  # statement_timeout
            return QueryTimeout(exc.message, sql=sql, original=exc)
        case pg.QueryCanceledError():  # pg_cancel_backend or a client cancel: not a timeout
            return DatabaseError(exc.message, sql=sql, original=exc)
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
        case pg.PostgresSyntaxError() if needs := _needs_newer_server(exc.message, sql, server):
            return DatabaseError(f"{needs} ({exc.message})", sql=sql, original=exc)
        case asyncpg.PostgresError() if not exc.message:
            # Raised client-side by asyncpg while encoding params; its text includes the value.
            message = (
                f"{type(exc).__name__}: a query parameter has the wrong type or cannot be "
                "encoded (value hidden)"
            )
            cause = exc.__cause__
            if isinstance(cause, TypeError):  # names types, not values: "Object of type set
                message += f": {cause}"  # is not JSON serializable" (Q6-6)
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


_MERGE = r"^\s*(?:WITH\b.*?\b)?MERGE\s+INTO\b"
_BY_SOURCE = r"\bNOT\s+MATCHED\s+BY\s+SOURCE\b"
# SQL that older servers reject as a syntax error: (word they stop at, feature, version,
# the shape the SQL must have, so the word is not matched in an unrelated statement).
_NEWER_SYNTAX = tuple(
    (word, feature, version, re.compile(shape, re.IGNORECASE | re.DOTALL))
    for word, feature, version, shape in (
        ("EXPRESSION", "ALTER COLUMN ... SET EXPRESSION", 17, r"\bSET\s+EXPRESSION\b"),
        ("VIRTUAL", "VIRTUAL generated columns", 18, r"\bVIRTUAL\b"),
        ("MERGE", "MERGE", 15, _MERGE),
        ("RETURNING", "MERGE ... RETURNING", 17, _MERGE),
        ("SOURCE", "MERGE ... WHEN NOT MATCHED BY SOURCE", 17, _BY_SOURCE),
        # PG15/16 stop one word earlier, at BY (B12).
        ("BY", "MERGE ... WHEN NOT MATCHED BY SOURCE", 17, _BY_SOURCE),
        (
            "NULLS",
            "a unique constraint or index with NULLS NOT DISTINCT",
            15,
            r"\bNULLS\s+NOT\s+DISTINCT\b",
        ),
    )
)


def _needs_newer_server(message: str, sql: str, server: int | None = None) -> str | None:
    """Name the feature an older server lacks. Never on a server new enough for it: there
    the syntax error is a real one (B44)."""
    near = message.upper()  # the server quotes the word as it was written
    for word, feature, version, shape in _NEWER_SYNTAX:
        if server is not None and server >= version:
            continue
        if f'AT OR NEAR "{word}"' in near and shape.search(sql):
            return f"{feature} needs PostgreSQL {version} or later"
    return None


_BROKEN_CONNECTION = ("closed", "another operation", "switch to state", "released back")


def _connection_broken(exc: BaseException) -> bool:
    text = str(exc)
    return any(marker in text for marker in _BROKEN_CONNECTION)


def _translate(exc: Exception, sql: str, server: int | None = None) -> Exception:
    """Map ``exc``; callers raise the result ``from None`` so tracebacks do not print the
    driver error, whose DETAIL can hold row values. It stays reachable as ``.original``."""
    mapped = map_error(exc, sql, server)
    if mapped is None:
        return exc
    return mapped


def _aborted(phase: str) -> DatabaseError:
    return DatabaseError(
        "a statement in this transaction failed and its error was caught, so PostgreSQL "
        "aborted the transaction and nothing was committed. Wrap a statement that may fail "
        "in a savepoint (tx.transaction())",
        sql=phase,
    )


_ENDED = (
    "the transaction was already ended by a COMMIT or ROLLBACK sent as raw SQL inside it, "
    "so statements after that ran outside it (autocommit). Leave COMMIT and ROLLBACK to "
    "the transaction block"
)


def _rowcount(status: str) -> int:
    # Command tags: "INSERT 0 3", "UPDATE 2", "DELETE 1", "SELECT 5"
    last = status.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else 0


class PostgresConnection:
    _conn: asyncpg.Connection | PoolConnectionProxy
    dirty: bool  # ran a statement that may have changed session state
    _savepoint_ids: Iterator[int]

    def __init__(self, conn: asyncpg.Connection | PoolConnectionProxy) -> None:
        self._conn = conn
        self.dirty = False
        self._savepoint_ids = itertools.count(1)

    def _translate(self, exc: Exception, sql: str) -> Exception:
        try:
            server = self._conn.get_server_version().major
        except Exception:  # a detached pool proxy: the version is not needed then
            server = None
        return _translate(exc, sql, server)

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
            raise self._translate(exc, sql) from None

    async def fetch_dicts(
        self, sql: str, params: Sequence[object], timeout: float | None = None
    ) -> list[dict[str, object]]:
        self.dirty = True
        try:
            return [
                dict(record) for record in await self._conn.fetch(sql, *params, timeout=timeout)
            ]
        except Exception as exc:
            raise self._translate(exc, sql) from None

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
            raise self._translate(exc, sql) from None

    async def executemany(
        self, sql: str, params: Sequence[Sequence[object]], timeout: float | None = None
    ) -> int:
        self.dirty = True
        try:
            # asyncpg pipelines these in as few round trips as it can.
            await self._conn.executemany(sql, params, timeout=timeout)
        except Exception as exc:
            raise self._translate(exc, sql) from None
        return len(params)

    @asynccontextmanager
    async def transaction(
        self, isolation: IsolationLevel | None = None, readonly: bool = False
    ) -> AsyncGenerator[None]:
        # BEGIN / COMMIT are sent here, not through asyncpg's Transaction, to read the
        # COMMIT tag: PostgreSQL answers COMMIT on an aborted transaction with the tag
        # ROLLBACK and no error. Counting failed statements missed some (COPY errors,
        # cancelled statements) and counted client-side errors that abort nothing (R2, R4).
        nested, phase = False, "BEGIN"  # the phase is named in the error (C13)
        body_error: BaseException | None = None
        try:
            nested = self._conn.is_in_transaction()  # inside: a detached proxy raises
            if nested:
                name = f"mizzle_sp_{next(self._savepoint_ids)}"
                phase = "SAVEPOINT"
                await self._conn.execute(f"SAVEPOINT {name}")
            else:
                name = ""
                begin = "BEGIN"
                if isolation is not None:
                    begin += f" ISOLATION LEVEL {_ISOLATION[isolation]}"
                await self._conn.execute(begin + " READ ONLY" if readonly else begin)
            try:
                yield
            except BaseException as exc:
                body_error = exc
                phase = "ROLLBACK TO SAVEPOINT" if nested else "ROLLBACK"
                if not self._conn.is_in_transaction():
                    exc.add_note(_ENDED)  # R4-4
                    raise
                if nested:
                    await self._conn.execute(
                        f"ROLLBACK TO SAVEPOINT {name}; RELEASE SAVEPOINT {name}"
                    )
                else:
                    await self._conn.execute("ROLLBACK")
                raise
            phase = "RELEASE SAVEPOINT" if nested else "COMMIT"
            if not self._conn.is_in_transaction():
                raise DatabaseError(_ENDED, sql=phase)  # a COMMIT here would report success (R4-4)
            if nested:
                try:
                    await self._conn.execute(f"RELEASE SAVEPOINT {name}")
                except pg.InFailedSQLTransactionError:
                    # A statement inside the savepoint failed and its error was caught.
                    await self._conn.execute(
                        f"ROLLBACK TO SAVEPOINT {name}; RELEASE SAVEPOINT {name}"
                    )
                    raise _aborted(phase) from None
            elif await self._conn.execute("COMMIT") != "COMMIT":
                raise _aborted(phase)
        # BEGIN / COMMIT failures (body errors are mapped), and a connection lost before
        # ROLLBACK (asyncpg's InterfaceError: "the underlying connection is closed").
        # InternalClientError: BEGIN on a connection the server closed under load.
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            pg.InternalClientError,
            OSError,
        ) as exc:
            if body_error is not None:
                # The rollback after the body's error failed: the body's error is the one
                # that matters; the failed rollback is a note on it (C13).
                body_error.add_note(f"{phase} also failed: {type(exc).__name__}: {exc}")
                raise body_error from None
            raise _translate(exc, phase) from None

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
            raise self._translate(exc, sql) from None


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

        ``RESET ALL`` undoes the settings the hook (or a raw statement) set, such as a
        ``search_path``, but not the role: ``SET ROLE`` and ``SET SESSION AUTHORIZATION``
        are left alone by it, so they are reset on their own (S7-5). asyncpg only runs
        ``init`` on a new connection, so the hook has to run again here. Otherwise it works
        on the first query and is silently gone after any raw statement.

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
        if reset:
            # RESET ALL skips the role, so a console's SET ROLE outlived the release and the
            # next user of the connection ran as that role (S7-5). Resetting the session
            # authorization back to the login role needs no privilege.
            # asyncpg's reset keeps temp tables, so their rows reached the next user of the
            # connection (C14). Not DEALLOCATE ALL: asyncpg's statement cache needs its own.
            await self.execute("RESET SESSION AUTHORIZATION; RESET ROLE; DISCARD TEMP")
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


def _json_text(value: object) -> str:
    if isinstance(value, JsonText):  # Q4-5
        return value
    try:
        return json.dumps(value)
    except TypeError:  # a Decimal, or a value JSON has no form for
        return _json_exact(value)


def _json_exact(value: object) -> str:
    """``json.dumps`` with a finite Decimal in its exact digits, as ``json_text`` writes a
    DDL default (Q6-6). Anything else JSON has no form for still raises TypeError."""
    match value:
        case Decimal() if value.is_finite():
            return format(value, "f")
        case dict():
            pairs = cast(dict[object, object], value).items()
            keys = [(k if isinstance(k, str) else json.dumps(k), v) for k, v in pairs]
            return "{" + ", ".join(f"{json.dumps(k)}: {_json_exact(v)}" for k, v in keys) + "}"
        case list() | tuple():
            items = cast(Sequence[object], value)
            return "[" + ", ".join(_json_exact(v) for v in items) + "]"
        case _:
            return json.dumps(value)


def _jsonb_encode(value: object) -> bytes:
    return b"\x01" + _json_text(value).encode()  # binary jsonb: a version byte, then text


def _jsonb_decode(data: bytes) -> object:
    return json.loads(data[1:])  # pyright: ignore[reportAny]


def _json_encode(value: object) -> bytes:
    return _json_text(value).encode()


async def _init_connection(conn: asyncpg.Connection) -> None:
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
            self._conn = await self._pool.acquire(timeout=self._driver.acquire_timeout)
        except TimeoutError:
            # Every connection stayed busy (or a new one did not open) for that long (R9).
            error = DbConnectionError(
                "could not get a connection in time: all "
                f"{self._pool.get_max_size()} pooled connections stayed busy, or the server "
                "did not answer; raise pool_size, acquire_timeout or connect_timeout"
            )
            error.lost = False
            raise error from None
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            # InterfaceError: "pool is closed", for a view kept after Database.close() (C18).
            error = DbConnectionError(f"could not acquire a connection: {exc}")
            error.lost = False  # the pooled connections are fine (C12)
            raise error from exc
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


def _malformed(url: str, exc: Exception) -> DbConnectionError:
    """A malformed database URL as a clean, permanent error (C18). The parser's message
    quotes pieces of the URL, so it is shown only for a URL with no user name or password
    in it: masking pieces of a password in it missed some (R5-1, R6-4). A bad query option
    (``?sslmode=bogus``) is named, without its value or the parser's message (R9-4)."""
    if option := _bad_option(url, str(exc)):
        detail = f"invalid {option} option"
    elif "@" in url or "password" in url.lower():
        detail = "percent-encode special characters in the user name and password"
    else:
        detail = str(exc)
    error = DbConnectionError(f"the database URL is malformed: {detail}")
    error.retryable = False
    return error


# Options asyncpg reads from the URL that hold no secret. Only these are named: with an
# unencoded '&' in a password, a piece of it can look like another option or its value.
_URL_OPTIONS = frozenset(
    [
        "host",
        "port",
        "dbname",
        "database",
        "user",
        "passfile",
        "options",
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "sslcrl",
        "ssl_min_protocol_version",
        "ssl_max_protocol_version",
        "sslnegotiation",
        "target_session_attrs",
        "krbsrvname",
        "gsslib",
        "service",
    ]
)


def _bad_option(url: str, message: str) -> str | None:
    """The known URL query option ``message`` names (R9-4)."""
    try:
        keys = [key for key, _ in parse_qsl(urlsplit(url).query)]
    except ValueError:
        return None
    for key in keys:
        if key in _URL_OPTIONS and re.search(rf"\b{key}\b", message):
            return key
    return None


def _is_closed(conn: asyncpg.Connection | PoolConnectionProxy) -> bool:
    try:
        return conn.is_closed()
    except Exception:  # a detached pool proxy: its connection is gone
        return True


class PostgresDriver:
    _pool: asyncpg.Pool
    acquire_timeout: float | None
    last_acquire_ms: float

    def __init__(self, pool: asyncpg.Pool, acquire_timeout: float | None = 30.0) -> None:
        self._pool = pool
        self.acquire_timeout = acquire_timeout
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
        connect_timeout: float = 10.0,
        acquire_timeout: float | None = 30.0,
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

        bad: DbConnectionError | None = None
        try:
            parts = urlsplit(url)
            _ = parts.port  # a bad port or IPv6 address raises here, before connecting
            if "@" in parts.netloc.rpartition("@")[0]:
                # asyncpg splits at the first '@' and quoted the rest in its error (R6-4).
                raise ValueError("an unencoded '@' in the user name or password")
        except ValueError as exc:
            bad = _malformed(url, exc)
        if bad is not None:  # raised here: the parser's error must not ride along (R7-2)
            raise bad
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
                timeout=connect_timeout,  # asyncpg's default waits 60 s on a hung server (R7)
            )
        except (asyncpg.ClientConfigurationError, ValueError) as exc:
            # A bad option (sslmode), or asyncpg's own URL parser ("bad query field") with
            # pieces of the URL in its message (R7-2): raised below, outside this block, so
            # the parser's error is not kept as __context__ either.
            malformed = _malformed(url, exc)
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            # The URL is left out on purpose: it may contain a password.
            error = DbConnectionError(f"could not connect: {type(exc).__name__}: {exc}")
            # A wrong password or a missing database does not fix itself: the caller's
            # retry loop must not sleep and try again (R4).
            error.retryable = _retryable(exc)
            raise error from exc
        else:
            return cls(pool, acquire_timeout)
        raise malformed

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

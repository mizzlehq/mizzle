"""asyncpg-backed driver: pool, connections, transactions, and error mapping."""

from __future__ import annotations

import asyncio
import collections
import itertools
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import BinaryIO, Self, cast, final
from urllib.parse import parse_qsl, urlsplit

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
    PoolStats,
    Reconnected,
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


# How copy_to() finds the "$n" to write values into: only in SQL whose quoting this reads
# exactly as PostgreSQL does. Without backslashes, '...' (also E'', U&''), "..." end only at
# a single quote; comments, dollar quotes and unclosed quotes are refused, since a scanner
# that read them differently would put a value where the server sees SQL.
_PARAM_OR_QUOTED = re.compile(
    r"""\$(\d+)\b|'(?:[^']|'')*'|"(?:[^"]|"")*"|(?P<refused>--|/\*|\$|'|")"""
)


def _qualified(schema: str, name: str) -> str:
    return ".".join('"' + part.replace('"', '""') + '"' for part in (schema, name))


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

    async def _fresh[R](self, run: Callable[[], Awaitable[R]]) -> R:
        """``run()``, again once if the connection's cached plans named a type that is gone.

        A migration that creates a type again (an enum's values removed or reordered) leaves
        every open connection with prepared statements for the old type; before PostgreSQL
        17 each fails with "cache lookup failed for type" until the app restarts (H15-1).
        The caches are cleared and the statement runs again, unless in a transaction (it is
        aborted then: the next one works)."""
        try:
            return await run()
        except asyncpg.InternalServerError as exc:
            if not str(exc).startswith("cache lookup failed for type"):
                raise
            await self._conn.reload_schema_state()
            if self._conn.is_in_transaction():
                raise
        return await run()

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
            return list(await self._fresh(lambda: self._conn.fetch(sql, *params, timeout=timeout)))
        except Exception as exc:
            raise self._translate(exc, sql) from None

    async def fetch_dicts(
        self, sql: str, params: Sequence[object], timeout: float | None = None
    ) -> list[dict[str, object]]:
        self.dirty = True
        try:
            rows = await self._fresh(lambda: self._conn.fetch(sql, *params, timeout=timeout))
            return [dict(record) for record in rows]
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
            status = await self._fresh(lambda: self._conn.execute(sql, *params, timeout=timeout))
            return _rowcount(status)
        except Exception as exc:
            raise self._translate(exc, sql) from None

    async def executemany(
        self, sql: str, params: Sequence[Sequence[object]], timeout: float | None = None
    ) -> int:
        self.dirty = True
        try:
            # asyncpg pipelines these in as few round trips as it can.
            await self._fresh(lambda: self._conn.executemany(sql, params, timeout=timeout))
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
            cursor = await self._fresh(lambda: self._conn.cursor(sql, *params, timeout=timeout))
            while batch := await cursor.fetch(batch_size, timeout=timeout):
                yield list(batch)
        except Exception as exc:
            raise self._translate(exc, sql) from None

    async def _inline(self, sql: str, params: Sequence[object]) -> str:
        """``sql`` with each ``$n`` replaced by its value as a quoted literal. Like asyncpg's
        own inlining (the server quotes each value), except floats: the server prints those
        per ``extra_float_digits`` (0 rounds to 15 digits), so they are written here exactly."""
        if "\\" in sql or any(m["refused"] for m in _PARAM_OR_QUOTED.finditer(sql)):
            raise QueryError(
                "copy_to() with values takes SQL without comments, dollar quotes, "
                "backslashes or unclosed quotes; pass the values in the query text instead"
            )
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
            casts.append(
                f"pg_catalog.quote_literal(${len(values)}::{_qualified(param.schema, name)}"
                "::pg_catalog.text)"
            )
            literals.append(None)
        row = await self._conn.fetchrow(f"SELECT {', '.join(casts)}", *values) if casts else None
        quoted = iter(row or ())
        texts = [text if text is not None else cast(str, next(quoted)) for text in literals]
        # Quoted text and identifiers keep their "$n" as is.
        return _PARAM_OR_QUOTED.sub(
            lambda m: texts[int(m.group(1)) - 1] if m.group(1) else m.group(0), sql
        )

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
            raise self._translate(exc, sql) from None
        return _rowcount(status)

    async def copy_out(
        self,
        sql: str,
        params: Sequence[object],
        output: CopyOutput,
        format: CopyFormat,
        header: bool,
        timeout: float | None = None,
    ) -> int:
        # Errors of the output (disk full, a callback that raises) are raised as they are:
        # mapped like socket errors they would look like a lost connection.
        output_errors: list[Exception] = []
        file: BinaryIO | None = None
        sink: BinaryIO | Callable[[bytes], Awaitable[None]]
        if isinstance(output, str | os.PathLike):
            file = await asyncio.to_thread(open, output, "wb")
            sink = file
        else:
            sink = output

        async def write(chunk: bytes) -> None:
            try:
                if callable(sink):
                    await sink(chunk)
                else:
                    await asyncio.to_thread(sink.write, chunk)
            except Exception as exc:
                output_errors.append(exc)
                raise

        try:
            # COPY takes no bind parameters, so their values are written into the SQL here
            # (asyncpg's own version also replaced "$1" inside quoted text).
            if params:
                async with asyncio.timeout(timeout):
                    query = await self._inline(sql, params)
            else:
                query = sql
            status = await self._conn.copy_from_query(
                query, output=write, format=format, header=header or None, timeout=timeout
            )
        except MizzleError:
            raise  # SQL copy_to() will not write values into
        except Exception as exc:
            if output_errors:
                raise output_errors[0] from None
            raise self._translate(exc, sql) from None
        finally:
            if file is not None:
                file.close()
        return _rowcount(status)


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


def _orjson_loads() -> Callable[[bytes], object] | None:
    """orjson's parser, when installed (pip install "mizzle[speed]")."""
    try:
        import orjson
    except ImportError:  # pragma: no cover - orjson is in the dev group
        return None
    return cast(Callable[[bytes], object], orjson.loads)


_FAST_LOADS = _orjson_loads()
# orjson reads an integer outside 64 bits as a float, without an error. Only a run of 19
# digits can hold one, so such text (rare: a long number, or digits inside a string) goes
# to json, which keeps it exact. Mapping digits to "0" first keeps the check in C.
_DIGITS = bytes(48 if 48 <= i <= 57 else 32 for i in range(256))
_LONG_RUN = b"0" * 19


def _json_loads(data: bytes) -> object:
    """A json/jsonb value: with orjson when installed (and it reads the text exactly),
    else json. Both give the same Python values."""
    if _FAST_LOADS is not None and _LONG_RUN not in data.translate(_DIGITS):
        try:
            return _FAST_LOADS(data)
        except ValueError:  # a lone surrogate, 1e400: json reads those
            pass
    return cast(object, json.loads(data))


def _jsonb_encode(value: object) -> bytes:
    return b"\x01" + _json_text(value).encode()  # binary jsonb: a version byte, then text


def _jsonb_decode(data: bytes) -> object:
    return _json_loads(data[1:])


def _json_encode(value: object) -> bytes:
    return _json_text(value).encode()


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "json", encoder=_json_encode, decoder=_json_loads, schema="pg_catalog", format="binary"
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


# How long listen() waits for its test notification when connected through a pooler.
_PROBE_TIMEOUT = 2.0


async def _behind_pooler(conn: asyncpg.Connection) -> bool:
    """PgBouncer and other poolers report their own process ID at startup."""
    backend = cast(int, await conn.fetchval("SELECT pg_catalog.pg_backend_pid()"))
    return backend != conn.get_server_pid()


@final
class _Listener[T]:
    """The iterator ``db.listen()`` yields from. LISTENs on a connection of its own (never
    a pooled one: a pool resets it, and a pooler in transaction mode loses notifications).

    Notifications are queued by asyncpg's callback and decoded when read, so a ``decode``
    that raises fails that ``async for`` step only. A lost connection is noticed by
    asyncpg, or by a ``SELECT 1`` after ``keepalive`` quiet seconds (a dropped network sends
    no close); the queued notifications are read first, then the listener connects again,
    LISTENs again and yields ``Reconnected``."""

    def __init__(
        self,
        open_connection: Callable[[], Awaitable[asyncpg.Connection]],
        channels: Sequence[str],
        decode: Callable[[str], T],
        max_queue: int,
        keepalive: float,
    ) -> None:
        self._open_connection = open_connection
        self._channels = tuple(channels)
        self._decode = decode
        self._max_queue = max_queue
        self._keepalive = keepalive
        self._queue: collections.deque[tuple[str, str, int]] = collections.deque()
        self._wake = asyncio.Event()
        self._conn: asyncpg.Connection | None = None
        self._lost = False
        self._overflow = False
        self._closed = False

    async def start(self) -> None:
        self._conn = await self._listen()

    async def _listen(self) -> asyncpg.Connection:
        conn = await self._open_connection()
        try:
            for channel in self._channels:  # asyncpg quotes the channel name
                await conn.add_listener(channel, self._on_notify)
            conn.add_termination_listener(self._on_lost)
            if await _behind_pooler(conn):
                await self._check_delivery(conn)
        except MizzleError:
            conn.terminate()
            raise
        except Exception as exc:
            conn.terminate()
            raise _translate(exc, "LISTEN") from None
        except BaseException:
            conn.terminate()
            raise
        return conn

    async def _check_delivery(self, conn: asyncpg.Connection) -> None:
        """Through a pooler, check that a notification sent from another connection
        arrives. PgBouncer in transaction mode drops them: the server session that ran
        LISTEN goes back to its pool, and notifications to an idle one are discarded."""
        channel = f"mizzle_probe_{uuid.uuid4().hex}"
        arrived = asyncio.Event()

        def on_probe(*_: object) -> None:
            arrived.set()

        await conn.add_listener(channel, on_probe)
        try:
            sender = await self._open_connection()
            try:
                await sender.execute("SELECT pg_catalog.pg_notify($1, '')", channel)
            finally:
                await sender.close()
            async with asyncio.timeout(_PROBE_TIMEOUT):
                await arrived.wait()
        except TimeoutError:
            raise DatabaseError(
                "listen() cannot receive notifications through this connection pooler: a "
                f"test notification did not arrive within {_PROBE_TIMEOUT:g}s (PgBouncer in "
                "transaction mode drops them). Pass listen(url=...) with a URL straight to "
                "PostgreSQL, or to the pooler in session mode"
            ) from None
        finally:
            await conn.remove_listener(channel, on_probe)

    def _on_notify(self, conn: object, pid: int, channel: str, payload: object, /) -> None:
        if conn is not self._conn:
            return  # a connection given up on: its LISTENs are gone
        if len(self._queue) >= self._max_queue:
            self._overflow = True
        else:
            self._queue.append((channel, str(payload), pid))
        self._wake.set()

    def _on_lost(self, conn: object, /) -> None:
        if conn is self._conn:
            self._lost = True
            self._wake.set()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Notification[T] | Reconnected:
        while True:
            if self._closed:  # read after the block: it would wait forever (H13-4)
                raise QueryError("the listener is closed: read its events inside its block")
            if self._queue:
                channel, payload, pid = self._queue.popleft()
                return Notification(channel, payload, self._decode(payload), pid)
            if self._overflow:
                raise ListenerOverflow(
                    f"the listener fell {self._max_queue} notifications behind (max_queue); "
                    "later ones were not kept"
                )
            if self._lost:
                await self._reconnect()
                return Reconnected()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self._keepalive)
            except TimeoutError:
                await self._probe()

    async def _probe(self) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            await conn.execute("SELECT 1", timeout=self._keepalive)
        except Exception:
            self._lost = True

    async def _reconnect(self) -> None:
        old, self._conn = self._conn, None
        if old is not None:
            old.terminate()
        delay = 0.2
        while True:
            try:
                self._conn = await self._listen()
            except DbConnectionError as exc:
                if not exc.retryable:
                    raise  # a wrong password or a dropped database does not fix itself
                log.warning("mizzle: listen() could not reconnect (%s); retrying", exc)
            else:
                self._lost = False
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await asyncio.wait_for(conn.close(), 5.0)
        except Exception:
            conn.terminate()


class PostgresDriver:
    _pool: asyncpg.Pool
    acquire_timeout: float | None
    last_acquire_ms: float
    _connect_one: Callable[[str | None], Awaitable[asyncpg.Connection]] | None

    def __init__(
        self,
        pool: asyncpg.Pool,
        acquire_timeout: float | None = 30.0,
        connect_one: Callable[[str | None], Awaitable[asyncpg.Connection]] | None = None,
    ) -> None:
        self._pool = pool
        self.acquire_timeout = acquire_timeout
        self.last_acquire_ms = 0.0
        self._connect_one = connect_one

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

            async def connect_one(target: str | None) -> asyncpg.Connection:
                """One connection outside the pool, set up as the pool's are."""
                link = target or url
                try:
                    conn = await asyncpg.connect(
                        link,
                        timeout=connect_timeout,
                        statement_cache_size=statement_cache_size,
                        server_settings=None if server_settings is None else dict(server_settings),
                    )
                except (asyncpg.ClientConfigurationError, ValueError) as exc:
                    bad_url = _malformed(link, exc)
                except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
                    error = DbConnectionError(f"could not connect: {type(exc).__name__}: {exc}")
                    error.retryable = _retryable(exc)
                    raise error from exc
                else:
                    try:
                        await _init_connection(conn)
                        if on_connect is not None:
                            await on_connect(PostgresConnection(conn))
                    except BaseException:
                        conn.terminate()
                        raise
                    return conn
                raise bad_url  # outside the except: the parser's error may quote the URL

            return cls(pool, acquire_timeout, connect_one)
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

    @asynccontextmanager
    async def listen[T](
        self,
        channels: Sequence[str],
        decode: Callable[[str], T],
        *,
        url: str | None = None,
        max_queue: int = 10_000,
        keepalive: float = 30.0,
    ) -> AsyncGenerator[_Listener[T]]:
        connect_one = self._connect_one
        if connect_one is None:
            raise QueryError("listen() needs a driver made with PostgresDriver.connect()")
        listener = _Listener(lambda: connect_one(url), channels, decode, max_queue, keepalive)
        await listener.start()
        try:
            yield listener
        finally:
            await listener.close()

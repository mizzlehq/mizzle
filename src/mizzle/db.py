"""Database and Session: the query API bound to a connection pool or one connection."""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Sequence,
)
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    aclosing,
    asynccontextmanager,
    contextmanager,
)
from dataclasses import dataclass
from typing import Literal, Self, final, overload

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import ConnectionError as DbConnectionError
from mizzle.core.errors import MizzleError, QueryError, SerializationFailure
from mizzle.core.naming import Casing
from mizzle.core.query.base import Row, evolve
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.query.insert import Insert, RowLike, as_mapping, prepare_row
from mizzle.core.query.relational import RelationalStatement
from mizzle.core.query.select import SelectBase
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table, validate_tables
from mizzle.core.schema.view import View
from mizzle.core.sql.nodes import Identifier, Node, Raw
from mizzle.dialects.base import (
    Connection,
    CopyFormat,
    CopyOutput,
    Dialect,
    Driver,
    IsolationLevel,
    Notification,
    Overflow,
    Reconnected,
    ResetMode,
)
from mizzle.dialects.postgres import PostgresDialect

log = logging.getLogger("mizzle")


@dataclass(frozen=True)
class QueryEvent:
    """Passed to the ``logger`` hook after every statement. ``params`` is None unless
    ``log_params=True``: parameter values may hold passwords or personal data."""

    sql: str
    params: tuple[object, ...] | None
    duration_ms: float
    error: BaseException | None


type QueryLogger = Callable[[QueryEvent], None]
type _CallKind = Literal["fetch", "execute", "dicts"]
# The most bind parameters one statement can have (asyncpg / the wire protocol).
MAX_PARAMS = 32767


class Session(QueryBuilder):
    """Query API. ``Database`` runs each statement on a pooled connection; inside
    ``transaction()`` a Session is bound to one connection.

    With read replicas, lock-free selects and relational queries outside a transaction go
    to a replica (in turn); everything else goes to the primary."""

    _dialect: Dialect
    _driver: Driver | None
    _replicas: tuple[Driver, ...]
    _next_replica: itertools.count[int]
    _use_primary: bool
    _timeout: float | None
    _casing: Casing | None
    _logger: QueryLogger | None
    _log_params: bool
    _conn: Connection | None
    _lock: asyncio.Lock

    def __init__(
        self,
        *,
        dialect: Dialect,
        driver: Driver | None,
        casing: Casing | None = None,
        logger: QueryLogger | None = None,
        log_params: bool = False,
        connection: Connection | None = None,
        replicas: Sequence[Driver] = (),
    ) -> None:
        super().__init__(self)
        self._dialect = dialect
        self._driver = driver
        self._replicas = tuple(replicas)
        self._next_replica = itertools.count()
        self._use_primary = False
        self._timeout = None
        self._casing = casing
        self._logger = logger
        self._log_params = log_params
        self._conn = connection
        # One connection runs one statement at a time; the lock lets asyncio.gather()
        # inside a transaction queue up instead of failing.
        self._lock = asyncio.Lock()

    # -- Runner protocol --------------------------------------------------------------

    def compile(self, node: Node) -> CompiledQuery:
        return self._dialect.compiler(self._casing).compile(node)

    def fetch(self, query: CompiledQuery, *, read_only: bool = False) -> Awaitable[list[Row]]:
        # FOR UPDATE anywhere in the query (CTE, subquery) needs the primary.
        return self._call(query, "fetch", read_only and not query.locking)

    def run(self, query: CompiledQuery) -> Awaitable[int]:
        return self._call(query, "execute")

    async def stream(
        self, query: CompiledQuery, batch_size: int, *, read_only: bool = False
    ) -> AsyncGenerator[list[Row]]:
        if batch_size < 1:
            raise QueryError("stream() needs batch_size >= 1")
        safe = not query.raw
        with self._logged(query):
            if self._conn is not None:
                # Locked per batch, not for the whole loop: its body may run other
                # statements in the same transaction.
                cursor = self._conn.cursor(
                    query.sql, query.params, batch_size, self._timeout, session_safe=safe
                )
                async with aclosing(cursor) as batches:
                    while True:
                        async with self._lock:
                            batch = await anext(batches, None)
                        if batch is None:
                            return
                        yield batch
            try:
                # Cursors only live inside a transaction; it ends (and closes the cursor)
                # when the loop ends, breaks or is cancelled.
                async with (
                    self._connection(read_only and not query.locking) as conn,
                    conn.transaction(),
                ):
                    cursor = conn.cursor(
                        query.sql, query.params, batch_size, self._timeout, session_safe=safe
                    )
                    async with aclosing(cursor) as batches:
                        async for batch in batches:
                            yield batch
            except DbConnectionError:
                await self._after_lost_connection(query, False)  # never resumed mid-stream
                raise

    # -- public API -------------------------------------------------------------------

    async def execute(self, statement: Raw) -> list[dict[str, object]]:
        """Run raw SQL built with ``sql(...)``; rows come back as dicts."""
        compiled = self.compile(statement)
        if compiled.has_placeholders:
            raise QueryError("execute() does not take placeholders")
        return await self._call(compiled, "dicts")

    async def refresh(self, view: type[View], *, concurrently: bool = False) -> None:
        """``REFRESH MATERIALIZED VIEW``. ``concurrently=True`` keeps the view readable
        meanwhile; it needs a unique index on the view."""
        if not view.__materialized__:
            raise QueryError(f"{view.__name__} is not a materialized view")
        info = view.__table__
        name: list[str | Node] = [Identifier(info.name)]
        if info.schema is not None:
            name[:0] = [Identifier(info.schema), "."]
        how = "CONCURRENTLY " if concurrently else ""
        await self.execute(Raw((f"REFRESH MATERIALIZED VIEW {how}", *name)))

    async def copy_into(
        self,
        table: type[Table],
        rows: Iterable[RowLike],
        *,
        columns: Sequence[ColumnBase] | None = None,
    ) -> int:
        """Bulk load ``rows`` with COPY, much faster than INSERT for many rows; returns
        the row count. Rows are checked like ``insert().values()``. Every row must set
        the same columns: ``columns`` (default: those the first row sets). Columns left
        out get their database default. All rows or none are stored. Tables with
        vector or tsvector columns are loaded with ``insert_many()`` instead."""
        it = iter(rows)
        first = next(it, None)
        if first is None:
            return 0
        if columns is not None:
            attrs = _copy_attrs(table, columns)
        else:
            attrs = tuple(prepare_row(table, as_mapping(first)))
        if not attrs:
            raise QueryError("copy_into() needs at least one column")
        info = table.__table__
        if any(_text_only(info.columns[a]) for a in attrs):
            # asyncpg's COPY sends binary values; these types only have a text encoding.
            return await self.insert_many(table, itertools.chain((first,), it))
        names = [info.columns[a].db_name(self._casing) for a in attrs]
        compiled = CompiledQuery(f"COPY {info.name} ({', '.join(names)}) FROM STDIN", (), ())
        records = _copy_records(table, itertools.chain((first,), it), attrs)
        with self._logged(compiled):
            try:
                async with self._connection() as conn:
                    return await conn.copy_records(
                        info.name, info.schema, names, records, self._timeout
                    )
            except DbConnectionError:
                await self._after_lost_connection(compiled, False)
                raise

    async def copy_from(
        self,
        query: Node,  # a select or relational query, or raw sql(...)
        output: CopyOutput,
        *,
        format: CopyFormat = "csv",
        header: bool = False,
    ) -> int:
        """Export the rows of ``query`` with ``COPY (query) TO STDOUT`` into ``output``: a
        file path, a binary file, or ``async def write(chunk: bytes)``. Returns the row
        count. Lock-free selects read from a replica, like plain reads."""
        compiled = self.compile(query)
        if compiled.has_placeholders:
            raise QueryError("copy_from() does not take placeholders")
        read_only = isinstance(query, SelectBase | RelationalStatement) and not compiled.locking
        with self._logged(compiled):
            try:
                async with self._connection(read_only) as conn:
                    return await conn.copy_out(
                        compiled.sql,
                        compiled.params,
                        output,
                        format,
                        header,
                        self._timeout,
                        session_safe=not compiled.raw,
                    )
            except DbConnectionError:
                await self._after_lost_connection(compiled, False)
                raise

    @overload
    async def insert_many[T: Table](
        self,
        table: type[T],
        rows: Iterable[RowLike],
        *,
        batch_size: int | None = None,
        returning: Literal[False] = False,
        on_conflict: Insert[T] | None = None,
    ) -> int: ...
    @overload
    async def insert_many[T: Table](
        self,
        table: type[T],
        rows: Iterable[RowLike],
        *,
        batch_size: int | None = None,
        returning: Literal[True],
        on_conflict: Insert[T] | None = None,
    ) -> list[T]: ...
    async def insert_many[T: Table](
        self,
        table: type[T],
        rows: Iterable[RowLike],
        *,
        batch_size: int | None = None,
        returning: bool = False,
        on_conflict: Insert[T] | None = None,
    ) -> int | list[T]:
        """INSERT any number of rows, in as few statements as the bind parameter limit
        (32767) allows, inside one transaction: all rows or none. ``batch_size`` caps the
        rows per statement. Returns the row count, or the rows with ``returning=True``.

        ``on_conflict`` is an insert without values whose conflict clause every statement
        gets: ``on_conflict=db.insert(Users).on_conflict_do_nothing(Users.email)`` or
        ``.on_conflict_do_update(...)``. Rows skipped by DO NOTHING are not counted or
        returned. Two rows with the same key in one batch fail with DO UPDATE (PostgreSQL
        cannot update a row twice in one statement)."""
        if batch_size is not None and batch_size < 1:
            raise QueryError("insert_many() needs batch_size >= 1")
        template = Insert[T](self, table) if on_conflict is None else on_conflict
        if template.table is not table:
            raise QueryError(f"on_conflict must be an insert into {table.__name__}")
        if template.rows:
            raise QueryError("on_conflict must not have values(); pass the rows to insert_many()")
        count = 0
        inserted: list[T] = []
        async with self.transaction() as tx:
            template = evolve(template, runner=tx)
            # Bind parameters the conflict clause takes (DO UPDATE SET values, WHERE).
            no_values: dict[str, object] = {}
            reserved = len(tx.compile(evolve(template, rows=(no_values,))).params)
            allow_identity = template.overriding is not None
            batches = _insert_batches(table, rows, batch_size, reserved, allow_identity)
            for batch in batches:
                insert = evolve(template, rows=batch)
                if returning:
                    inserted += await insert.returning()
                else:
                    count += await insert
        return inserted if returning else count

    @overload
    def listen(
        self,
        *channels: str,
        reconnect: Literal[False] = False,
        max_queue: int | None = None,
        overflow: Overflow = "raise",
        pooler_session_mode: bool = False,
    ) -> AbstractAsyncContextManager[AsyncIterator[Notification]]: ...
    @overload
    def listen(
        self,
        *channels: str,
        reconnect: Literal[True],
        max_queue: int | None = None,
        overflow: Overflow = "raise",
        pooler_session_mode: bool = False,
    ) -> AbstractAsyncContextManager[AsyncIterator[Notification | Reconnected]]: ...
    def listen(
        self,
        *channels: str,
        reconnect: bool = False,
        max_queue: int | None = None,
        overflow: Overflow = "raise",
        pooler_session_mode: bool = False,
    ) -> AbstractAsyncContextManager[AsyncIterator[Notification | Reconnected]]:
        """Receive ``NOTIFY`` messages sent to ``channels``, on a pooled connection held
        until the block exits::

            async with db.listen("orders") as events:
                async for event in events:
                    print(event.channel, event.payload)

        If the connection is lost, iterating raises ``ConnectionError`` (notifications
        sent meanwhile are missed). With ``reconnect=True`` it LISTENs again on a new
        connection instead (retrying until the database is back) and yields a
        ``Reconnected`` event first, so the gap is never silent.

        ``max_queue`` bounds the notifications waiting to be read (default: no bound).
        When it is reached, ``overflow="raise"`` makes iteration raise ``ListenerOverflow``
        after the queued ones; ``"drop_oldest"`` discards the oldest and logs a warning;
        ``"block"`` stops reading the connection until the consumer catches up, so
        PostgreSQL holds the rest and nothing is lost (the queue can briefly exceed
        ``max_queue`` by the notifications already in one network read).

        Through a connection pooler, listen() raises ``DatabaseError``: PgBouncer in
        transaction or statement mode drops notifications. Connect it directly to
        PostgreSQL, or pass ``pooler_session_mode=True`` when the pooler runs in session
        mode (listen() then checks that a test notification arrives)."""
        if self._conn is not None:
            raise QueryError("listen() needs its own connection; call it outside a transaction")
        if not channels:
            raise QueryError("listen() needs at least one channel")
        if max_queue is not None and max_queue < 1:
            raise QueryError("listen() needs max_queue >= 1")
        driver = self._require_driver()

        async def open_listener(stack: AsyncExitStack) -> AsyncIterator[Notification]:
            conn = await stack.enter_async_context(driver.acquire())
            return await stack.enter_async_context(
                # The pooler check's test notification must not wait for a pooled
                # connection: with pool_size=1 the listener holds the only one.
                conn.listen(
                    channels, max_queue, overflow, driver.notify_unpooled, pooler_session_mode
                )
            )

        if reconnect:
            return _reconnecting(_Relistener(open_listener, driver))
        return _listening(open_listener)

    async def notify(self, channel: str, payload: str = "") -> None:
        """Send a notification to ``channel`` (``pg_notify``; the payload is a bind
        parameter). Inside a transaction it is sent on commit."""
        await self._call(
            CompiledQuery("SELECT pg_notify($1, $2)", (channel, payload), (None, None)), "execute"
        )

    @property
    def primary(self) -> Session:
        """This session with every query sent to the primary, e.g. to read your own
        writes when replicas may lag: ``await db.primary.select().from_(Users)``."""
        view = self._view()
        view._use_primary = True
        return view

    def with_timeout(self, seconds: float | None) -> Session:
        """This session with a different statement timeout (None: the pool default).
        A statement that runs longer is cancelled and raises ``QueryTimeout``."""
        view = self._view()
        view._timeout = seconds
        return view

    async def retry[R](
        self, fn: Callable[[], Awaitable[R]], *, attempts: int = 3, backoff: float = 0.05
    ) -> R:
        """Call ``fn`` again when it raises ``SerializationFailure`` (a serialization
        conflict or a deadlock), waiting a little longer each time. ``fn`` should run its
        own whole transaction, so each attempt starts fresh::

            async def transfer() -> None:
                async with db.transaction("serializable") as tx: ...

            await db.retry(transfer)
        """
        if self._conn is not None:
            raise QueryError("retry() inside a transaction cannot work: the transaction is aborted")
        if attempts < 1:
            raise QueryError("retry() needs attempts >= 1")
        for attempt in itertools.count(1):
            try:
                return await fn()
            except SerializationFailure:
                if attempt >= attempts:
                    raise
                # Exponential backoff with jitter so conflicting callers spread out.
                await asyncio.sleep(backoff * 2.0 ** (attempt - 1) * random.uniform(0.5, 1.5))
        raise AssertionError("unreachable")

    @asynccontextmanager
    async def transaction(
        self, isolation: IsolationLevel | None = None, *, readonly: bool = False
    ) -> AsyncGenerator[Session]:
        """Commit on success, roll back on error. Nested calls create savepoints."""
        if self._conn is not None:
            if isolation is not None or readonly:
                raise QueryError("isolation and readonly apply to the outermost transaction only")
            async with self._conn.transaction():
                yield self
            return
        driver = self._require_driver()
        async with driver.acquire() as conn, conn.transaction(isolation, readonly):
            tx = Session(
                dialect=self._dialect,
                driver=driver,
                casing=self._casing,
                logger=self._logger,
                log_params=self._log_params,
                connection=conn,
            )
            tx._timeout = self._timeout
            yield tx

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    @property
    def driver(self) -> Driver:
        """The connection pool (raises if not connected)."""
        return self._require_driver()

    # -- internals --------------------------------------------------------------------

    def _swap_drivers(self, driver: Driver | None, replicas: Sequence[Driver] = ()) -> list[Driver]:
        old = [*([self._driver] if self._driver else []), *self._replicas]
        self._driver, self._replicas = driver, tuple(replicas)
        return old

    def _view(self) -> Session:
        view = Session(
            dialect=self._dialect,
            driver=self._driver,
            casing=self._casing,
            logger=self._logger,
            log_params=self._log_params,
            connection=self._conn,
            replicas=self._replicas,
        )
        # Shared, so a view of a transaction still runs one statement at a time.
        view._lock = self._lock
        view._next_replica = self._next_replica
        view._use_primary = self._use_primary
        view._timeout = self._timeout
        return view

    def _require_driver(self) -> Driver:
        if self._driver is None:
            raise MizzleError("database is not connected; use await Database.connect(url)")
        return self._driver

    def _connection(self, read_only: bool = False) -> AbstractAsyncContextManager[Connection]:
        if self._conn is not None:
            return self._locked(self._conn)
        primary = self._require_driver()
        if not read_only or self._use_primary or not self._replicas:
            return primary.acquire()  # the common case: no extra wrapper
        replica = self._replicas[next(self._next_replica) % len(self._replicas)]
        return self._replica_connection(replica, primary)

    @asynccontextmanager
    async def _locked(self, conn: Connection) -> AsyncGenerator[Connection]:
        async with self._lock:
            yield conn

    @asynccontextmanager
    async def _replica_connection(
        self, replica: Driver, primary: Driver
    ) -> AsyncGenerator[Connection]:
        async with AsyncExitStack() as stack:
            try:
                conn = await stack.enter_async_context(replica.acquire())
            except DbConnectionError:
                # No statement ran yet, so the primary can take the read.
                log.warning("mizzle: read replica unavailable; reading from the primary")
                conn = await stack.enter_async_context(primary.acquire())
            yield conn

    @overload
    async def _call(
        self, query: CompiledQuery, kind: Literal["fetch"], read_only: bool = ...
    ) -> list[Row]: ...
    @overload
    async def _call(self, query: CompiledQuery, kind: Literal["execute"]) -> int: ...
    @overload
    async def _call(
        self, query: CompiledQuery, kind: Literal["dicts"]
    ) -> list[dict[str, object]]: ...
    async def _call(
        self, query: CompiledQuery, kind: _CallKind, read_only: bool = False
    ) -> list[Row] | int | list[dict[str, object]]:
        if self._logger is None:
            try:
                async with self._connection(read_only) as conn:
                    return await self._send(conn, query, kind)
            except DbConnectionError:
                if not await self._after_lost_connection(query, read_only):
                    raise
            return await self._retry_read(query, kind, read_only)
        with self._logged(query):
            try:
                async with self._connection(read_only) as conn:
                    return await self._send(conn, query, kind)
            except DbConnectionError:
                if not await self._after_lost_connection(query, read_only):
                    raise
            return await self._retry_read(query, kind, read_only)

    @contextmanager
    def _logged(self, query: CompiledQuery) -> Generator[None]:
        """Pass a QueryEvent to the logger hook when the block ends."""
        if self._logger is None:
            yield
            return
        start = time.perf_counter()
        error: BaseException | None = None
        try:
            yield
        except GeneratorExit:  # a stream the caller stopped early: not an error
            raise
        except BaseException as exc:
            error = exc
            raise
        finally:
            event = QueryEvent(
                query.sql,
                query.params if self._log_params else None,
                (time.perf_counter() - start) * 1000,
                error,
            )
            try:
                self._logger(event)
            except Exception:
                log.exception("mizzle query logger raised")

    async def _after_lost_connection(self, query: CompiledQuery, read_only: bool) -> bool:
        """Handle a lost connection; return whether the statement may run again.

        The usual cause is a server restart, failover or idle timeout, which kills every
        pooled connection, so all of them are replaced. A plain read outside a transaction
        can safely run again; writes and raw SQL never do, as they may have taken effect.
        """
        if self._conn is not None:
            return False  # inside a transaction: the caller must retry the transaction
        for driver in (self._driver, *self._replicas):
            if driver is not None:
                await driver.expire_connections()
        return read_only and not query.raw

    async def _retry_read(
        self, query: CompiledQuery, kind: _CallKind, read_only: bool
    ) -> list[Row] | int | list[dict[str, object]]:
        log.warning("mizzle: connection lost during a read; retrying once on a new connection")
        async with self._connection(read_only) as conn:
            return await self._send(conn, query, kind)

    def _send(
        self, conn: Connection, query: CompiledQuery, kind: _CallKind
    ) -> Awaitable[list[Row] | int | list[dict[str, object]]]:
        # A plain function returning the driver's coroutine: no extra await layer.
        safe = not query.raw
        if kind == "fetch":
            return conn.fetch(query.sql, query.params, self._timeout, session_safe=safe)
        if kind == "execute":
            return conn.execute(query.sql, query.params, self._timeout, session_safe=safe)
        return conn.fetch_dicts(query.sql, query.params, self._timeout)


type _OpenListener = Callable[[AsyncExitStack], Awaitable[AsyncIterator[Notification]]]


@asynccontextmanager
async def _listening(open_listener: _OpenListener) -> AsyncGenerator[AsyncIterator[Notification]]:
    async with AsyncExitStack() as stack:
        yield await open_listener(stack)


@asynccontextmanager
async def _reconnecting(
    listener: _Relistener,
) -> AsyncGenerator[AsyncIterator[Notification | Reconnected]]:
    try:
        await listener.connect()  # the first connection's errors are raised as they are
        yield listener
    finally:
        await listener.close()


@final
class _Relistener:
    """The iterator ``listen(reconnect=True)`` yields. A class, not an async generator: a
    timeout or cancellation while waiting must not end the iteration (a reconnect it
    interrupts starts again on the next call)."""

    __slots__ = ("_driver", "_events", "_open", "_stack")
    _open: _OpenListener
    _driver: Driver
    _stack: AsyncExitStack | None
    _events: AsyncIterator[Notification] | None

    def __init__(self, open_listener: _OpenListener, driver: Driver) -> None:
        self._open = open_listener
        self._driver = driver
        self._stack = None
        self._events = None

    async def connect(self) -> None:
        stack = AsyncExitStack()
        try:
            self._events = await self._open(stack)
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack

    async def close(self) -> None:
        stack, self._stack, self._events = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Notification | Reconnected:
        if self._events is not None:
            try:
                return await anext(self._events)
            except DbConnectionError:
                log.warning("mizzle: the listen() connection was lost; reconnecting")
                await self.close()
                await self._driver.expire_connections()  # like any lost connection
        delay = 0.1
        while True:
            try:
                await self.connect()
            except DbConnectionError:
                log.warning("mizzle: listen() could not reconnect; retrying in %gs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
            else:
                return Reconnected()


def _insert_batches(
    table: type[Table],
    rows: Iterable[RowLike],
    batch_size: int | None,
    reserved: int = 0,
    allow_identity: bool = False,
) -> Iterator[tuple[dict[str, object], ...]]:
    """Checked rows in batches that fit one INSERT's bind parameters (less ``reserved``
    ones the rest of the statement uses)."""
    batch: list[dict[str, object]] = []
    params = 0
    limit = MAX_PARAMS - reserved
    for row in rows:
        values = prepare_row(table, as_mapping(row), allow_identity=allow_identity)
        size = sum(not isinstance(v, Node) for v in values.values())  # SQL is not bound
        if batch and (params + size > limit or len(batch) == batch_size):
            yield tuple(batch)
            batch, params = [], 0
        batch.append(values)
        params += size
    if batch:
        yield tuple(batch)


# Extension and text-search types: asyncpg has no binary codec for them, so COPY cannot
# send them; copy_into() falls back to insert_many() for tables that use them.
_TEXT_ONLY_TYPES = ("vector", "halfvec", "sparsevec", "tsvector", "tsquery")


def _text_only(column: ColumnBase) -> bool:
    base = column.sql_type.removesuffix("[]").split("(", 1)[0].strip()
    return base in _TEXT_ONLY_TYPES


def _copy_attrs(table: type[Table], columns: Sequence[ColumnBase]) -> tuple[str, ...]:
    for column in columns:
        if column.table is not table:
            raise QueryError(f"{column.qualified_name} is not a column of {table.__name__}")
    wanted = {column.attr for column in columns}
    for attr, column in table.__table__.columns.items():
        if column.required_on_insert and attr not in wanted:
            raise QueryError(f"{column.qualified_name} is required")
    return tuple(attr for attr in table.__table__.columns if attr in wanted)


def _copy_records(
    table: type[Table], rows: Iterable[RowLike], attrs: tuple[str, ...]
) -> Iterator[tuple[object, ...]]:
    """Check and encode each row while COPY sends them (rows are never all in memory)."""
    columns = table.__table__.columns
    for number, row in enumerate(rows):
        given = as_mapping(row)
        values = prepare_row(table, given)  # unknown columns, types, NOT NULL, defaults
        if missing := [a for a in attrs if a not in values]:
            raise QueryError(
                f"copy_into() row {number} has no value for {', '.join(missing)}; "
                "every row must set the same columns"
            )
        if extra := [a for a in given if a not in attrs]:
            raise QueryError(
                f"copy_into() row {number} sets {', '.join(extra)}, which the first row "
                "(or columns=) does not"
            )
        record: list[object] = []
        for attr in attrs:
            value = values[attr]
            if isinstance(value, Node):
                raise QueryError("copy_into() cannot send SQL expressions; use insert()")
            record.append(columns[attr].encode(value))
        yield tuple(record)


def dialect_for_url(url: str) -> Dialect:
    scheme = url.split("://", 1)[0]
    if scheme in ("postgresql", "postgres"):
        return PostgresDialect()
    raise MizzleError(f"unsupported database URL scheme {scheme!r}")


class Database(Session):
    """Entry point.

    ``db = await Database.connect(url, schema=[Users, Posts])``. Without ``connect`` a
    Database can still build SQL (``to_sql()``) but cannot run it.
    """

    tables: tuple[type[Table], ...]

    def __init__(
        self,
        *,
        dialect: Dialect | None = None,
        schema: Sequence[type[Table]] = (),
        casing: Casing | None = None,
        logger: QueryLogger | None = None,
        log_params: bool = False,
        driver: Driver | None = None,
    ) -> None:
        validate_tables(schema, casing)
        self.tables = tuple(schema)
        super().__init__(
            dialect=dialect or PostgresDialect(),
            driver=driver,
            casing=casing,
            logger=logger,
            log_params=log_params,
        )

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        schema: Sequence[type[Table]] = (),
        replicas: Sequence[str] = (),
        pool_size: int = 10,
        pool_min_size: int = 1,
        max_idle: float = 300.0,
        query_timeout: float | None = None,
        statement_cache_size: int = 100,
        reset_connections: ResetMode = "always",
        casing: Casing | None = None,
        logger: QueryLogger | None = None,
        log_params: bool = False,
    ) -> Self:
        """Open a pool to ``url`` and one to each read replica URL (same pool settings).

        ``max_idle`` closes connections unused for that many seconds. ``query_timeout``
        cancels statements that run longer (seconds; None: no limit).
        ``statement_cache_size=0`` is needed behind PgBouncer in transaction mode.

        ``reset_connections="auto"`` makes small queries up to ~40% faster: the session reset
        that normally follows every query is skipped when mizzle built the statement itself
        (raw SQL still resets). Leave it at ``"always"`` if triggers or functions your
        queries call change session settings (``set_config(..., false)``, ``SET``), because
        mizzle cannot see those."""
        dialect = dialect_for_url(url)
        for replica in replicas:
            if dialect_for_url(replica).name != dialect.name:
                raise MizzleError("replica URLs must use the same database as the primary")
        db = cls(
            dialect=dialect, schema=schema, casing=casing, logger=logger, log_params=log_params
        )
        drivers: list[Driver] = []
        try:
            for target in (url, *replicas):
                drivers.append(
                    await dialect.connect(
                        target,
                        pool_size=pool_size,
                        pool_min_size=pool_min_size,
                        max_idle=max_idle,
                        query_timeout=query_timeout,
                        statement_cache_size=statement_cache_size,
                        reset_connections=reset_connections,
                    )
                )
        except BaseException:
            for driver in drivers:
                await driver.close()
            raise
        db._swap_drivers(drivers[0], drivers[1:])
        return db

    async def close(self) -> None:
        for driver in self._swap_drivers(None):
            await driver.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

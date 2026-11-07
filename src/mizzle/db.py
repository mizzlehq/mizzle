"""Database and Session: the query API bound to a connection pool or one connection."""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging
import math
import random
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    aclosing,
    asynccontextmanager,
    contextmanager,
    nullcontext,
)
from dataclasses import dataclass, field
from typing import Literal, Self, final, overload

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import (
    AfterCommitError,
    AfterRollbackError,
    MizzleError,
    QueryError,
    QueryTimeout,
    SerializationFailure,
)
from mizzle.core.errors import ConnectionError as DbConnectionError
from mizzle.core.naming import Casing
from mizzle.core.query.base import Row, evolve
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.query.insert import Insert, RowLike, as_mapping, prepare_row
from mizzle.core.query.relational import RelationalStatement
from mizzle.core.query.select import SelectBase
from mizzle.core.schema.table import Table, validate_tables
from mizzle.core.schema.view import View
from mizzle.core.sql.nodes import Identifier, Node, Raw
from mizzle.dialects.base import (
    Connection,
    Dialect,
    Driver,
    IsolationLevel,
    PoolStats,
    ResetMode,
)
from mizzle.dialects.postgres import PostgresDialect
from mizzle.tracing import Tracer

log = logging.getLogger("mizzle")
# Seconds a replica that failed to give a connection is skipped.
_REPLICA_PAUSE = 30.0

# Pushed onto Session._savepoints when the transaction ends: statements are refused.
_ENDED = object()
# Replaces a savepoint's marker when its ROLLBACK TO / RELEASE was cancelled before or
# while it ran: its state is unknown, so the transaction refuses statements and COMMIT (R4-1).
_BROKEN = object()
_ISOLATION_LEVELS: tuple[IsolationLevel, ...] = (
    "read committed",
    "repeatable read",
    "serializable",
)


@final
class _Marker:
    """An open savepoint; knows its transaction, so a task holding a marker of a
    savepoint that has ended is refused there and nowhere else (R4-3)."""

    __slots__ = ("savepoints",)

    def __init__(self, savepoints: list[object]) -> None:
        self.savepoints = savepoints


_OPEN_SAVEPOINT = (
    "a savepoint opened inside this block (by a task it started) is still open or was "
    "interrupted, so the block was rolled back; await such tasks inside the block"
)


_UNDONE = (
    "an enclosing savepoint was rolled back while this one was open, which undid this one "
    "too; await tasks that open savepoints inside the block that started them"
)


def _timeout_ok(seconds: float | None) -> bool:
    """A timeout is a finite number of seconds > 0, or None. 0, a negative, NaN or inf
    failed every statement or meant no limit at all (R4-6)."""
    return seconds is None or (math.isfinite(seconds) and seconds > 0)


def _busy(driver: Driver) -> bool:
    """Every pooled connection is handed out: the server is fine, just loaded (R4-7)."""
    stats = driver.stats()
    return stats.in_use >= stats.max_size


def _commit_unknown(error: BaseException) -> bool:
    """Whether ``error``, raised while COMMIT was in flight, leaves its outcome unknown."""
    return isinstance(error, asyncio.CancelledError | DbConnectionError | QueryTimeout)


# The savepoints the running code is inside of, innermost last (see Session._savepoints).
# A stack, not one marker: code inside savepoints of two transactions still owns the
# innermost one of each (R5-3).
_SAVEPOINT: contextvars.ContextVar[tuple[_Marker, ...]] = contextvars.ContextVar(
    "mizzle_savepoint", default=()
)


@dataclass(frozen=True)
class QueryEvent:
    """Passed to the ``logger`` hook after every statement. ``params`` is None unless
    ``log_params=True``: parameter values may hold passwords or personal data. ``tags``
    are the key/values set with ``Session.tag()``."""

    sql: str
    params: tuple[object, ...] | None
    duration_ms: float
    error: BaseException | None
    tags: Mapping[str, object] = field(default_factory=lambda: {})


type QueryLogger = Callable[[QueryEvent], None]
type _CallKind = Literal["fetch", "execute", "dicts"]
# The most bind parameters one statement can have (asyncpg / the wire protocol).
MAX_PARAMS = 32767


async def _run_commit_hooks(hooks: Sequence[Callable[[], Awaitable[None]]]) -> None:
    """Run every after-commit hook; a failure is reported without suggesting the write failed.

    One failing hook must not hide the others (B6): all of them run, and the first error is
    the cause of the ``AfterCommitError``; later errors are only logged.
    """
    error: BaseException | None = None
    for hook in hooks:
        try:
            await hook()
        except Exception as exc:
            log.error("mizzle: an after_commit hook failed", exc_info=exc)
            if error is None:
                error = exc
    if error is not None:
        raise AfterCommitError(
            "an after_commit hook failed after the transaction committed; the data is saved. "
            "Do not retry the transaction."
        ) from error


async def _run_rollback_hooks(
    hooks: Sequence[Callable[[], Awaitable[None]]], original: BaseException
) -> None:
    """Run after-rollback hooks without replacing the error that caused the rollback.

    The hook error is chained with its traceback (``AfterRollbackError.__cause__``) and
    attached to the original as context. When the original already has a context, or hides
    it (``raise X from Y`` sets ``__suppress_context__``, B40), a note is added instead.
    """
    error: BaseException | None = None
    for hook in hooks:
        try:
            await hook()
        except Exception as exc:
            log.error("mizzle: an after_rollback hook failed", exc_info=exc)
            if error is None:
                error = exc
    if error is None:
        return
    if error.__context__ is original:
        # The hook ran while ``original`` was being handled; keeping that link would make the
        # chain a cycle (original -> AfterRollbackError -> hook error -> original).
        error.__context__ = None
    hook_error = AfterRollbackError(f"an after_rollback hook failed: {error}")
    hook_error.__cause__ = error  # keep the hook's traceback reachable
    if original.__context__ is None and not original.__suppress_context__:
        original.__context__ = hook_error
    else:
        original.add_note(f"an after_rollback hook also failed: {error!r}")


class Session(QueryBuilder):
    """Query API. ``Database`` runs each statement on a pooled connection; inside
    ``transaction()`` a Session is bound to one connection.

    With read replicas, lock-free selects and relational queries outside a transaction go
    to a replica (in turn); everything else goes to the primary."""

    _dialect: Dialect
    _driver: Driver | None
    _replicas: tuple[Driver, ...]
    _next_replica: itertools.count[int]
    # A replica that failed to give a connection, and when it may be tried again (R7).
    _replica_down: dict[Driver, float]
    _use_primary: bool
    _timeout: float | None
    _casing: Casing | None
    _logger: QueryLogger | None
    _log_params: bool
    _slow_query_ms: float | None
    _tracer: Tracer | None
    _tags: Mapping[str, object]
    _conn: Connection | None
    _lock: asyncio.Lock
    # Callbacks a savepoint registered, to run when the *outermost* transaction commits.
    # A savepoint is released, not committed, so running them there fires a side effect for
    # data the outer transaction can still roll back (R2).
    _deferred: list[Callable[[], Awaitable[None]]]
    # ``after_rollback`` hooks of savepoints that were released: they must run if the
    # outermost transaction rolls back (their data is undone after all).
    _rollbacks: list[Callable[[], Awaitable[None]]]
    # A marker per open savepoint, innermost last. The code inside a savepoint (and tasks
    # it starts) carries its marker in _SAVEPOINT; anything else would run inside a
    # savepoint it does not own, and is refused (B39, C11).
    _savepoints: list[object]

    def __init__(
        self,
        *,
        dialect: Dialect,
        driver: Driver | None,
        casing: Casing | None = None,
        logger: QueryLogger | None = None,
        log_params: bool = False,
        slow_query_ms: float | None = None,
        tracer: Tracer | None = None,
        tags: Mapping[str, object] | None = None,
        connection: Connection | None = None,
        replicas: Sequence[Driver] = (),
    ) -> None:
        super().__init__(self)
        self._dialect = dialect
        self._driver = driver
        self._replicas = tuple(replicas)
        self._next_replica = itertools.count()
        self._replica_down = {}
        self._use_primary = False
        self._timeout = None
        self._casing = casing
        self._logger = logger
        self._log_params = log_params
        self._slow_query_ms = slow_query_ms
        self._tracer = tracer
        self._tags = dict(tags) if tags else {}
        self._conn = connection
        # One connection runs one statement at a time; the lock lets asyncio.gather()
        # inside a transaction queue up instead of failing.
        self._lock = asyncio.Lock()
        self._deferred = []
        self._rollbacks = []
        self._savepoints = []

    # -- Runner protocol --------------------------------------------------------------

    def compile(self, node: Node) -> CompiledQuery:
        return self._dialect.compiler(self._casing).compile(node)

    def fetch(self, query: CompiledQuery, *, read_only: bool = False) -> Awaitable[list[Row]]:
        # A write anywhere in the query (FOR UPDATE, or a data-modifying CTE) needs the
        # primary and must never be retried.
        return self._call(query, "fetch", read_only and query.pure_read)

    def run(self, query: CompiledQuery) -> Awaitable[int]:
        return self._call(query, "execute")

    async def stream(
        self, query: CompiledQuery, batch_size: int, *, read_only: bool = False
    ) -> AsyncGenerator[list[Row]]:
        if batch_size < 1:
            raise QueryError("stream() needs batch_size >= 1")
        safe = query.session_safe
        # Traced per batch, not around the loop: a span made current here stayed current in
        # the caller between batches and after a break, and its token was reset from
        # another context when the generator was finalized (C16).
        paused = [0.0]
        with self._logged(query, trace=False, paused=paused):
            if self._conn is not None:
                # Locked per batch, not for the whole loop: its body may run other
                # statements in the same transaction.
                cursor = self._conn.cursor(
                    query.sql, query.params, batch_size, self._timeout, session_safe=safe
                )
                async with aclosing(cursor) as batches:
                    while True:
                        async with self._lock:
                            self._check_savepoint_owner()
                            with self._span(query):
                                batch = await anext(batches, None)
                        if batch is None:
                            return
                        handed = time.perf_counter()
                        try:
                            yield batch
                        finally:
                            paused[0] += time.perf_counter() - handed
            try:
                # Cursors only live inside a transaction; it ends (and closes the cursor)
                # when the loop ends, breaks or is cancelled.
                async with (
                    self._connection(read_only and query.pure_read) as conn,
                    conn.transaction(),
                ):
                    cursor = conn.cursor(
                        query.sql, query.params, batch_size, self._timeout, session_safe=safe
                    )
                    async with aclosing(cursor) as batches:
                        while True:
                            with self._span(query):
                                batch = await anext(batches, None)
                            if batch is None:
                                return
                            handed = time.perf_counter()
                            try:
                                yield batch
                            finally:
                                paused[0] += time.perf_counter() - handed
            except DbConnectionError as lost:
                await self._after_lost_connection(query, False, lost)  # never resumed mid-stream
                raise

    # -- public API -------------------------------------------------------------------

    async def execute(self, statement: Raw) -> list[dict[str, object]]:
        """Run raw SQL built with ``sql(...)``; rows come back as dicts."""
        compiled = self.compile(statement)
        if compiled.has_placeholders:
            raise QueryError("execute() does not take placeholders")
        return await self._call(compiled, "dicts")

    async def executemany(self, statement: Raw, params: Sequence[Sequence[object]]) -> int:
        """Run one statement (with ``$1``, ``$2``, ... placeholders) for each parameter set,
        pipelined in as few round trips as there can be. Returns how many sets ran. For
        inserting many rows prefer ``insert_many()``."""
        compiled = self.compile(statement)
        if compiled.params or compiled.has_placeholders:
            raise QueryError(
                "executemany() takes plain SQL with $1, $2, ... placeholders and their values"
            )
        with self._logged(compiled):
            try:
                async with self._connection() as conn:
                    return await conn.executemany(compiled.sql, params, self._timeout)
            except DbConnectionError as lost:
                await self._after_lost_connection(compiled, False, lost)
                raise

    async def explain(self, statement: Node, *, analyze: bool = False) -> list[dict[str, object]]:
        """The query plan (``EXPLAIN``) as rows of dicts, one per plan line.

        ``analyze=True`` adds real row counts and timings but runs ``statement``, so only
        selects are allowed."""
        compiled = self.compile(statement)
        if compiled.has_placeholders:
            raise QueryError("explain() does not take placeholders; inline the values instead")
        is_read = isinstance(statement, SelectBase | RelationalStatement)
        if analyze and (not is_read or compiled.writes):
            # Plain selects only: analyze runs the statement, and a data-modifying CTE writes.
            raise QueryError("explain(analyze=True) runs the statement; only selects are allowed")
        options = "ANALYZE, " if analyze else ""
        explained = CompiledQuery(
            f"EXPLAIN ({options}FORMAT TEXT) {compiled.sql}",
            compiled.params,
            compiled.placeholders,
            compiled.raw,
            compiled.locking,
            compiled.dynamic,
            compiled.opaque,
            compiled.writes,
        )
        return await self._call(explained, "dicts", is_read and compiled.pure_read)

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
        if self._conn is None:
            return await self._insert_rows(template, table, rows, batch_size, returning)
        # In a transaction, its hidden savepoint holds the statement lock from SAVEPOINT to
        # RELEASE: a sibling task's statement (asyncio.gather) waits for it instead of being
        # refused for landing in another task's savepoint (R5-2).
        async with self._lock:
            self._check_savepoint_owner()
            session = self._view()
            session._lock = asyncio.Lock()
            return await session._insert_rows(template, table, rows, batch_size, returning)

    async def _insert_rows[T: Table](
        self,
        template: Insert[T],
        table: type[T],
        rows: Iterable[RowLike],
        batch_size: int | None,
        returning: bool,
    ) -> int | list[T]:
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

    async def ping(self, timeout: float = 5.0) -> bool:
        """``True`` when the database answers a trivial query, ``False`` when it cannot be
        reached or does not answer within ``timeout`` seconds. For readiness probes; runs
        ``SELECT 1`` on the primary. A hung server returns False instead of raising or
        waiting forever (C17)."""
        try:
            async with asyncio.timeout(timeout):
                await self._call(CompiledQuery("SELECT 1", (), ()), "execute")
        except (DbConnectionError, QueryTimeout, TimeoutError):
            return False
        return True

    def pool_stats(self) -> PoolStats:
        """Connection-pool counters for the primary pool (see ``PoolStats``)."""
        return self._require_driver().stats()

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
        if not _timeout_ok(seconds):
            raise QueryError("with_timeout() needs seconds > 0 or None")  # R4-6
        view = self._view()
        view._timeout = seconds
        return view

    def tag(self, **tags: object) -> Session:
        """This session with key/values attached to every statement it runs (a request id,
        a route, a user): they arrive on the ``logger`` QueryEvent and on tracer spans. The
        original session is unchanged, and tags merge with any already set::

            db.tag(request_id=rid).insert(Users).values(...)
        """
        view = self._view()
        view._tags = {**self._tags, **tags}
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
        self,
        isolation: IsolationLevel | None = None,
        *,
        readonly: bool = False,
        after_commit: Callable[[], Awaitable[None]] | None = None,
        after_rollback: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[Session]:
        """Commit on success, roll back on error. Nested calls create savepoints.

        ``after_commit`` runs only once the data is committed, and ``after_rollback`` after
        a rollback, both outside the transaction: use them for side effects (send an email,
        enqueue a job) that must not happen when the transaction did not commit.

        Hook errors do not change what happened to the data:

        - an ``after_commit`` hook that raises is re-raised as
          :class:`AfterCommitError` naming it, because the write *did* commit and a caller
          must not treat the failure as "nothing was saved" and retry;
        - an ``after_rollback`` hook that raises does not replace the original error: the
          original is re-raised, and the hook's error is attached as its context (or, when
          the original already has a context, as a note on it).
        """
        if isolation is not None and isolation not in _ISOLATION_LEVELS:
            raise QueryError(  # not a bare KeyError from the driver (R4-5)
                f"unknown isolation level {isolation!r}; use one of {', '.join(_ISOLATION_LEVELS)}"
            )
        if self._conn is not None:
            if isolation is not None or readonly:
                raise QueryError("isolation and readonly apply to the outermost transaction only")
            # A rolled-back savepoint drops only what *it* queued: hooks queued by sibling
            # savepoints that were already released stay (B2).
            committed = len(self._deferred)
            released = len(self._rollbacks)
            started = False
            savepoint = self._conn.transaction()
            marker = _Marker(self._savepoints)
            with self._transaction_span(isolation):
                try:
                    # SAVEPOINT, RELEASE and ROLLBACK TO run under the statement lock, so
                    # they never overlap a sibling task's statement (C11).
                    async with self._lock:
                        self._check_savepoint_owner()
                        await savepoint.__aenter__()
                        self._savepoints.append(marker)
                    started = True
                    token = _SAVEPOINT.set((*_SAVEPOINT.get(), marker))
                    try:
                        try:
                            yield self
                        finally:
                            _SAVEPOINT.reset(token)
                        # The marker stays until ROLLBACK TO / RELEASE ran: a sibling
                        # task's statement queued for the lock would otherwise run inside
                        # the savepoint and be undone with it (R1).
                    except BaseException as exc:
                        broken = False
                        try:
                            async with self._lock:
                                if marker not in self._savepoints:
                                    raise  # an enclosing savepoint's ROLLBACK TO undid it
                                if not await savepoint.__aexit__(type(exc), exc, exc.__traceback__):
                                    raise
                        except asyncio.CancelledError as cancel:
                            broken = cancel is not exc  # cancelled before / during ROLLBACK TO
                            raise
                        finally:
                            self._end_marker(marker, broken)
                    else:
                        broken = False
                        try:
                            async with self._lock:
                                if marker not in self._savepoints:
                                    raise QueryError(_UNDONE)
                                if self._savepoints[-1] is not marker:
                                    # A task started inside still has a savepoint open (or
                                    # one was interrupted): roll back, never release it (R4-2).
                                    error = QueryError(_OPEN_SAVEPOINT)
                                    await savepoint.__aexit__(QueryError, error, None)
                                    raise error
                                await savepoint.__aexit__(None, None, None)
                        except asyncio.CancelledError:
                            broken = True  # cancelled before / during RELEASE (R4-1)
                            raise
                        finally:
                            self._end_marker(marker, broken)
                except BaseException as original:
                    del self._deferred[committed:]
                    # Savepoints released inside this one are undone with it: their
                    # after_rollback hooks run now (B37). A savepoint that never started
                    # (its transaction already ended) undid nothing, so none run (B39).
                    hooks = self._rollbacks[released:]
                    del self._rollbacks[released:]
                    if after_rollback is not None:
                        hooks.append(after_rollback)
                    if started:
                        await _run_rollback_hooks(hooks, original)
                    raise
            if after_commit is not None:
                # Released, not committed: run it only when the outermost transaction commits.
                self._deferred.append(after_commit)
            if after_rollback is not None:
                # ...and if the outer transaction rolls back instead (B6).
                self._rollbacks.append(after_rollback)
            return
        driver = self._require_driver()
        deferred: list[Callable[[], Awaitable[None]]] = []
        rollbacks: list[Callable[[], Awaitable[None]]] = []
        started = committing = done = False
        with self._transaction_span(isolation):
            try:
                async with driver.acquire() as conn:
                    tx = Session(
                        dialect=self._dialect,
                        driver=driver,
                        casing=self._casing,
                        logger=self._logger,
                        log_params=self._log_params,
                        slow_query_ms=self._slow_query_ms,
                        tracer=self._tracer,
                        tags=self._tags,
                        connection=conn,
                    )
                    tx._timeout = self._timeout
                    deferred = tx._deferred
                    rollbacks = tx._rollbacks
                    block = conn.transaction(isolation, readonly)
                    await block.__aenter__()
                    started = True
                    try:
                        yield tx
                    except BaseException as exc:
                        # COMMIT / ROLLBACK under the statement lock: a sibling task's
                        # statement may still be running (C11).
                        async with tx._lock:
                            tx._savepoints.append(_ENDED)
                            if not await block.__aexit__(type(exc), exc, exc.__traceback__):
                                raise
                    else:
                        async with tx._lock:
                            still_open = bool(tx._savepoints)
                            # From here on the transaction refuses statements: one sent
                            # after COMMIT would run in autocommit on this connection, or
                            # on a pooled connection someone else now uses (R3).
                            tx._savepoints.append(_ENDED)
                            if still_open:
                                # A task's savepoint is still open (or was interrupted):
                                # committing would keep rows it may yet undo (R4-1, R4-2).
                                error = QueryError(_OPEN_SAVEPOINT)
                                await block.__aexit__(QueryError, error, None)
                                raise error
                            committing = True
                            await block.__aexit__(None, None, None)
                    done = True
            except BaseException as original:
                if committing and not done and _commit_unknown(original):
                    # Cancelled or disconnected while COMMIT was in flight: the server may
                    # have committed or not, so neither hook list runs (R5).
                    original.add_note(
                        "COMMIT was interrupted: the transaction may or may not have been "
                        "committed, so no after_commit or after_rollback hook ran"
                    )
                    raise
                if done:
                    # COMMIT succeeded; the error (a cancel, a timeout) came while the
                    # connection was being released. The data is saved, so the commit hooks
                    # run and the rollback hooks do not (B36). The error still propagates.
                    if after_commit is not None:
                        deferred.append(after_commit)
                    try:
                        await _run_commit_hooks(deferred)
                    except AfterCommitError as hook_error:
                        original.add_note(f"an after_commit hook also failed: {hook_error}")
                    original.add_note("the transaction was committed before this error")
                    raise
                hooks = [*rollbacks, *([after_rollback] if after_rollback is not None else [])]
                deferred.clear()
                rollbacks.clear()
                if started:  # BEGIN never ran (no connection): nothing was rolled back (C15)
                    await _run_rollback_hooks(hooks, original)
                raise
        if after_commit is not None:
            deferred.append(after_commit)
        await _run_commit_hooks(deferred)

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
            slow_query_ms=self._slow_query_ms,
            tracer=self._tracer,
            tags=self._tags,
            connection=self._conn,
            replicas=self._replicas,
        )
        # Shared, so a view of a transaction still runs one statement at a time, and so
        # hooks registered through a view land in the transaction's queues (B3).
        view._lock = self._lock
        view._deferred = self._deferred
        view._rollbacks = self._rollbacks
        view._savepoints = self._savepoints
        view._next_replica = self._next_replica
        view._replica_down = self._replica_down
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
        now = time.monotonic()
        for _ in self._replicas:
            replica = self._replicas[next(self._next_replica) % len(self._replicas)]
            if self._replica_down.get(replica, 0.0) <= now:
                return self._replica_connection(replica, primary)
        return primary.acquire()  # every replica is down

    @asynccontextmanager
    async def _locked(self, conn: Connection) -> AsyncGenerator[Connection]:
        async with self._lock:
            self._check_savepoint_owner()
            yield conn

    def _end_marker(self, marker: _Marker, broken: bool) -> None:
        if marker not in self._savepoints:
            return  # undone already by an enclosing savepoint's ROLLBACK TO
        at = self._savepoints.index(marker)
        if broken:
            self._savepoints[at] = _BROKEN
        else:
            # Its ROLLBACK TO / RELEASE also ended every savepoint opened inside it, a
            # broken one included: their markers go too, or the transaction stayed
            # refused for good (R5-4).
            del self._savepoints[at:]

    def _check_savepoint_owner(self) -> None:
        """Refuse a statement that would land in another task's savepoint: a rollback of
        that savepoint would undo it silently (C11). Refuse any statement once the
        transaction has ended (R3)."""
        if self._savepoints and self._savepoints[-1] is _ENDED:
            raise QueryError(
                "this transaction has ended; run the statement inside its "
                "`async with db.transaction()` block, or on db"
            )
        if _BROKEN in self._savepoints:
            raise QueryError(
                "a savepoint of this transaction was cancelled before its ROLLBACK TO / "
                "RELEASE completed, so its state is unknown; the transaction must roll back"
            )
        mine = [m for m in _SAVEPOINT.get() if m.savepoints is self._savepoints]
        current = mine[-1] if mine else None
        if current is not None and current not in self._savepoints:
            raise QueryError(  # R4-3
                "the savepoint this task was started in has ended; its statements cannot "
                "run in the transaction any more"
            )
        if self._savepoints and current is not self._savepoints[-1]:
            raise QueryError(
                "another task has a savepoint open on this transaction; while it is open, "
                "only that task (and tasks it starts) may use the transaction. Await the "
                "savepoint first, or give each task its own transaction"
            )

    @asynccontextmanager
    async def _replica_connection(
        self, replica: Driver, primary: Driver
    ) -> AsyncGenerator[Connection]:
        async with AsyncExitStack() as stack:
            # A replica whose pool is all busy is healthy: this read goes to the primary
            # without waiting acquire_timeout, and the replica is not marked down (R4-7).
            conn: Connection | None = None
            if not _busy(replica):
                try:
                    conn = await stack.enter_async_context(replica.acquire())
                except DbConnectionError:
                    if not _busy(replica):  # it filled up while we waited (R4-7)
                        # No statement ran yet, so the primary can take the read. Reads
                        # skip the replica for a while: a hung one made every read wait (R7).
                        self._replica_down[replica] = time.monotonic() + _REPLICA_PAUSE
                        log.warning(
                            "mizzle: read replica unavailable; reading from the primary for %.0f s",
                            _REPLICA_PAUSE,
                        )
            on_replica = conn is not None
            if conn is None:
                conn = await stack.enter_async_context(primary.acquire())
            try:
                yield conn
            except DbConnectionError as lost:
                lost.on_replica = on_replica
                raise

    @overload
    async def _call(
        self, query: CompiledQuery, kind: Literal["fetch"], read_only: bool = ...
    ) -> list[Row]: ...
    @overload
    async def _call(self, query: CompiledQuery, kind: Literal["execute"]) -> int: ...
    @overload
    async def _call(
        self, query: CompiledQuery, kind: Literal["dicts"], read_only: bool = ...
    ) -> list[dict[str, object]]: ...
    async def _call(
        self, query: CompiledQuery, kind: _CallKind, read_only: bool = False
    ) -> list[Row] | int | list[dict[str, object]]:
        if self._logger is None and self._slow_query_ms is None and self._tracer is None:
            try:
                async with self._connection(read_only) as conn:
                    return await self._send(conn, query, kind)
            except DbConnectionError as lost:
                if not await self._after_lost_connection(query, read_only, lost):
                    raise
            return await self._retry_read(query, kind, read_only)
        with self._logged(query):
            try:
                async with self._connection(read_only) as conn:
                    return await self._send(conn, query, kind)
            except DbConnectionError as lost:
                if not await self._after_lost_connection(query, read_only, lost):
                    raise
            return await self._retry_read(query, kind, read_only)

    def _span(self, query: CompiledQuery) -> AbstractContextManager[object]:
        if self._tracer is None:
            return nullcontext()
        return self._tracer.statement(
            query.sql, query.params if self._log_params else None, self._tags
        )

    @contextmanager
    def _transaction_span(self, isolation: IsolationLevel | None) -> Generator[None]:
        if self._tracer is None:
            yield
            return
        with self._tracer.transaction(isolation):
            yield

    @contextmanager
    def _logged(
        self, query: CompiledQuery, *, trace: bool = True, paused: list[float] | None = None
    ) -> Generator[None]:
        """Time the block; trace it, log slow queries and pass a QueryEvent to the logger.
        ``paused`` holds seconds the block spent outside the database (a stream's caller)
        that do not count (R10)."""
        if self._logger is None and self._slow_query_ms is None and self._tracer is None:
            yield
            return
        with self._span(query) if trace else nullcontext():
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
                duration_ms = (time.perf_counter() - start - sum(paused or ())) * 1000
                if self._slow_query_ms is not None and duration_ms >= self._slow_query_ms:
                    log.warning("mizzle: slow query (%.1f ms): %s", duration_ms, query.sql)
                if self._logger is not None:
                    event = QueryEvent(
                        query.sql,
                        query.params if self._log_params else None,
                        duration_ms,
                        error,
                        self._tags,
                    )
                    try:
                        self._logger(event)
                    except Exception:
                        log.exception("mizzle query logger raised")

    async def _after_lost_connection(
        self, query: CompiledQuery, read_only: bool, error: DbConnectionError
    ) -> bool:
        """Handle a lost connection; return whether the statement may run again.

        The usual cause is a server restart, failover or idle timeout, which kills every
        pooled connection, so all of them are replaced. A plain read outside a transaction
        can safely run again; writes, raw SQL and func() calls never do, as they may have
        taken effect.
        A connection that could not be opened (too many connections) leaves the pool alone:
        replacing healthy connections then only made it worse (C12).
        """
        if self._conn is not None or not error.lost:
            return False  # in a transaction the caller must retry the whole transaction
        for driver in (self._driver, *self._replicas):
            if driver is not None:
                await driver.expire_connections()
        # A func() call may write, so it runs again only when it ran on a replica, which
        # cannot write (R6).
        return read_only and not query.raw and (not query.opaque or error.on_replica)

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
        safe = query.session_safe
        if kind == "fetch":
            return conn.fetch(query.sql, query.params, self._timeout, session_safe=safe)
        if kind == "execute":
            return conn.execute(query.sql, query.params, self._timeout, session_safe=safe)
        return conn.fetch_dicts(query.sql, query.params, self._timeout)


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
        slow_query_ms: float | None = None,
        tracer: Tracer | None = None,
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
            slow_query_ms=slow_query_ms,
            tracer=tracer,
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
        reset_connections: ResetMode = "auto",
        casing: Casing | None = None,
        logger: QueryLogger | None = None,
        log_params: bool = False,
        slow_query_ms: float | None = None,
        tracer: Tracer | None = None,
        on_connect: Callable[[Connection], Awaitable[None]] | None = None,
        server_settings: Mapping[str, str] | None = None,
        connect_retries: int = 0,
        connect_backoff: float = 0.5,
        connect_timeout: float = 10.0,
        acquire_timeout: float | None = 30.0,
    ) -> Self:
        """Open a pool to ``url`` and one to each read replica URL (same pool settings).

        ``max_idle`` closes connections unused for that many seconds. ``query_timeout``
        cancels statements that run longer (seconds; None: no limit).
        ``statement_cache_size=0`` is needed behind PgBouncer in transaction mode.

        ``slow_query_ms`` logs a warning for statements that take at least that long.
        ``tracer`` (see ``mizzle.tracing``) opens a span around each statement and
        transaction. ``on_connect`` runs on each new connection (e.g. ``SET search_path`` or
        session settings), and again after a connection is reset (see ``reset_connections``),
        because a reset undoes what the hook set; ``server_settings`` sets PostgreSQL GUCs for
        every connection. ``connect_retries`` (with ``connect_backoff`` seconds, doubling each
        time) retries a failed connection, to survive a database that is not up yet.
        ``connect_timeout`` limits how long opening one connection may take, and
        ``acquire_timeout`` how long a query waits for a free pooled connection (None: no
        limit); both raise ``ConnectionError``.

        ``reset_connections="auto"`` (the default) resets a connection's session state when
        it goes back to the pool only after a statement that may have changed it: raw
        ``sql(...)``, a function named with ``func()``, direct connection use.
        Skipping the reset after mizzle-built statements saves a round trip per query (2x on
        a real network). Use ``"always"`` if triggers or database functions change session
        settings themselves (``SET`` or ``set_config(..., false)`` without LOCAL, session
        advisory locks, ``LISTEN``), because mizzle cannot see inside them; the reset then
        (and the ``on_connect`` hook with it) runs on every release. The reset is ``RESET ALL``,
        advisory unlock, ``UNLISTEN *``, ``CLOSE ALL`` and ``DISCARD TEMP``; ``PREPARE``d
        statements survive it."""
        # Checked here: asyncpg raises a bare ValueError, or hangs (R11).
        rules = {
            "pool_size >= 1": pool_size >= 1,
            "0 <= pool_min_size <= pool_size": 0 <= pool_min_size <= pool_size,
            "max_idle >= 0": max_idle >= 0,
            "query_timeout > 0 or None": _timeout_ok(query_timeout),
            "statement_cache_size >= 0": statement_cache_size >= 0,
            "connect_retries >= 0": connect_retries >= 0,
            "connect_backoff >= 0": connect_backoff >= 0,
            "connect_timeout > 0": _timeout_ok(connect_timeout),
            "acquire_timeout > 0 or None": _timeout_ok(acquire_timeout),
        }
        for rule, ok in rules.items():
            if not ok:
                raise QueryError(f"Database.connect() needs {rule}")
        dialect = dialect_for_url(url)
        for replica in replicas:
            if dialect_for_url(replica).name != dialect.name:
                raise MizzleError("replica URLs must use the same database as the primary")
        db = cls(
            dialect=dialect,
            schema=schema,
            casing=casing,
            logger=logger,
            log_params=log_params,
            slow_query_ms=slow_query_ms,
            tracer=tracer,
        )
        drivers: list[Driver] = []
        try:
            for attempt in itertools.count():
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
                                on_connect=on_connect,
                                server_settings=server_settings,
                                connect_timeout=connect_timeout,
                                acquire_timeout=acquire_timeout,
                            )
                        )
                except DbConnectionError as exc:
                    for driver in drivers:
                        await driver.close()
                    drivers.clear()
                    if attempt >= connect_retries or not exc.retryable:
                        raise
                    delay = connect_backoff * 2.0**attempt
                    log.warning("mizzle: could not connect; retrying in %.1fs", delay)
                    await asyncio.sleep(delay)
                else:
                    break
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

"""Database and Session: the query API bound to a connection pool or one connection."""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Self

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import ConnectionError as DbConnectionError
from mizzle.core.errors import MizzleError, QueryError, SerializationFailure
from mizzle.core.naming import Casing
from mizzle.core.query.base import Row
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.table import Table, validate_tables
from mizzle.core.sql.nodes import Node, Raw
from mizzle.dialects.base import Connection, Dialect, Driver, IsolationLevel
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

    async def fetch(self, query: CompiledQuery, *, read_only: bool = False) -> list[Row]:
        return await self._call(
            query, lambda conn: conn.fetch(query.sql, query.params, self._timeout), read_only
        )

    async def run(self, query: CompiledQuery) -> int:
        return await self._call(
            query, lambda conn: conn.execute(query.sql, query.params, self._timeout)
        )

    # -- public API -------------------------------------------------------------------

    async def execute(self, statement: Raw) -> list[dict[str, object]]:
        """Run raw SQL built with ``sql(...)``; rows come back as dicts."""
        compiled = self.compile(statement)
        if compiled.has_placeholders:
            raise QueryError("execute() does not take placeholders")
        return await self._call(
            compiled, lambda conn: conn.fetch_dicts(compiled.sql, compiled.params, self._timeout)
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

    @asynccontextmanager
    async def _connection(self, read_only: bool = False) -> AsyncGenerator[Connection]:
        if self._conn is not None:
            async with self._lock:
                yield self._conn
            return
        primary = self._require_driver()
        if not read_only or self._use_primary or not self._replicas:
            async with primary.acquire() as conn:
                yield conn
            return
        replica = self._replicas[next(self._next_replica) % len(self._replicas)]
        async with AsyncExitStack() as stack:
            try:
                conn = await stack.enter_async_context(replica.acquire())
            except DbConnectionError:
                # No statement ran yet, so the primary can take the read.
                log.warning("mizzle: read replica unavailable; reading from the primary")
                conn = await stack.enter_async_context(primary.acquire())
            yield conn

    async def _call[R](
        self,
        query: CompiledQuery,
        op: Callable[[Connection], Awaitable[R]],
        read_only: bool = False,
    ) -> R:
        start = time.perf_counter()
        error: BaseException | None = None
        try:
            async with self._connection(read_only) as conn:
                return await op(conn)
        except BaseException as exc:
            error = exc
            raise
        finally:
            if self._logger is not None:
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
        casing: Casing | None = None,
        logger: QueryLogger | None = None,
        log_params: bool = False,
    ) -> Self:
        """Open a pool to ``url`` and one to each read replica URL (same pool settings).

        ``max_idle`` closes connections unused for that many seconds. ``query_timeout``
        cancels statements that run longer (seconds; None: no limit)."""
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

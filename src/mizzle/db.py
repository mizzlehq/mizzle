"""Database and Session: the query API bound to a connection pool or one connection."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Self

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import MizzleError, QueryError
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
    params: tuple[Any, ...] | None
    duration_ms: float
    error: BaseException | None


type QueryLogger = Callable[[QueryEvent], None]


class Session(QueryBuilder):
    """Query API. ``Database`` runs each statement on a pooled connection; inside
    ``transaction()`` a Session is bound to one connection."""

    def __init__(
        self,
        *,
        dialect: Dialect,
        driver: Driver | None,
        casing: Casing | None = None,
        logger: QueryLogger | None = None,
        log_params: bool = False,
        connection: Connection | None = None,
    ) -> None:
        super().__init__(self)
        self._dialect = dialect
        self._driver: Driver | None = driver
        self._casing: Casing | None = casing
        self._logger = logger
        self._log_params = log_params
        self._conn: Connection | None = connection
        # One connection runs one statement at a time; the lock lets asyncio.gather()
        # inside a transaction queue up instead of failing.
        self._lock = asyncio.Lock()

    # -- Runner protocol --------------------------------------------------------------

    def compile(self, node: Node) -> CompiledQuery:
        return self._dialect.compiler(self._casing).compile(node)

    async def fetch(self, query: CompiledQuery) -> list[Row]:
        return await self._call(query, lambda conn: conn.fetch(query.sql, query.params))

    async def run(self, query: CompiledQuery) -> int:
        return await self._call(query, lambda conn: conn.execute(query.sql, query.params))

    # -- public API -------------------------------------------------------------------

    async def execute(self, statement: Raw) -> list[dict[str, Any]]:
        """Run raw SQL built with ``sql(...)``; rows come back as dicts."""
        compiled = self.compile(statement)
        if compiled.has_placeholders:
            raise QueryError("execute() does not take placeholders")
        return await self._call(
            compiled, lambda conn: conn.fetch_dicts(compiled.sql, compiled.params)
        )

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
            yield Session(
                dialect=self._dialect,
                driver=driver,
                casing=self._casing,
                logger=self._logger,
                log_params=self._log_params,
                connection=conn,
            )

    # -- internals --------------------------------------------------------------------

    def _require_driver(self) -> Driver:
        if self._driver is None:
            raise MizzleError("database is not connected; use await Database.connect(url)")
        return self._driver

    @asynccontextmanager
    async def _connection(self) -> AsyncGenerator[Connection]:
        if self._conn is not None:
            async with self._lock:
                yield self._conn
            return
        async with self._require_driver().acquire() as conn:
            yield conn

    async def _call[R](self, query: CompiledQuery, op: Callable[[Connection], Awaitable[R]]) -> R:
        start = time.perf_counter()
        error: BaseException | None = None
        try:
            async with self._connection() as conn:
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
        pool_size: int = 10,
        statement_cache_size: int = 100,
        casing: Casing | None = None,
        logger: QueryLogger | None = None,
        log_params: bool = False,
    ) -> Self:
        dialect = dialect_for_url(url)
        db = cls(
            dialect=dialect, schema=schema, casing=casing, logger=logger, log_params=log_params
        )
        db._driver = await dialect.connect(
            url, pool_size=pool_size, statement_cache_size=statement_cache_size
        )
        return db

    async def close(self) -> None:
        driver = self._driver
        if driver is not None:
            self._driver = None
            await driver.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

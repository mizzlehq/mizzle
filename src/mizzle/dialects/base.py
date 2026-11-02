"""The dialect interface. ``core`` and ``kit`` talk to databases only through these protocols.

Adding a database means a new ``dialects/<name>/`` package that implements them.
Migration DDL and introspection are added here with the kit.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol

from mizzle.core.compiler import Compiler
from mizzle.core.naming import Casing
from mizzle.core.query.base import Row

type IsolationLevel = Literal["read committed", "repeatable read", "serializable"]


class Connection(Protocol):
    """One database connection. Errors are raised as mizzle errors, with SQL attached."""

    async def fetch(self, sql: str, params: Sequence[Any]) -> list[Row]: ...
    async def fetch_dicts(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]: ...
    async def execute(self, sql: str, params: Sequence[Any]) -> int: ...
    def transaction(
        self, isolation: IsolationLevel | None = None, readonly: bool = False
    ) -> AbstractAsyncContextManager[None]: ...


class Driver(Protocol):
    """A connection pool."""

    def acquire(self) -> AbstractAsyncContextManager[Connection]: ...
    async def close(self) -> None: ...


class Dialect(Protocol):
    name: str

    def compiler(self, casing: Casing | None = None) -> Compiler: ...
    async def connect(self, url: str, *, pool_size: int, statement_cache_size: int) -> Driver: ...

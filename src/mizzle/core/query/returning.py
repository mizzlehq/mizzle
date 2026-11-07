from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast, overload, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import Executable, Row, table_decoder
from mizzle.core.schema.table import Table
from mizzle.core.sql.alias import real_table
from mizzle.core.sql.functions import column_of
from mizzle.core.sql.nodes import Expr, ExprBase

if TYPE_CHECKING:
    from mizzle.core.query.select import Cte


def _value(index: int, convert: Callable[[object], object] | None) -> Callable[[Row], object]:
    if convert is None:
        return lambda row: row[index]

    def decode(row: Row) -> object:
        value = row[index]
        return None if value is None else convert(value)

    return decode


@dataclass(frozen=True, eq=False)
class ReturningMixin[T: Table](Executable[int], ABC):
    """``returning()`` for INSERT, UPDATE and DELETE: whole rows, or a projection."""

    table: type[T]

    @overload
    def returning(self) -> Returning[T]: ...
    @overload
    def returning(self, fields: Mapping[str, ExprBase], /) -> Returning[dict[str, object]]: ...
    @overload
    def returning[A](self, c1: Expr[A], /) -> Returning[tuple[A]]: ...
    @overload
    def returning[A, B](self, c1: Expr[A], c2: Expr[B], /) -> Returning[tuple[A, B]]: ...
    @overload
    def returning[A, B, C](
        self, c1: Expr[A], c2: Expr[B], c3: Expr[C], /
    ) -> Returning[tuple[A, B, C]]: ...
    @overload
    def returning[A, B, C, D](
        self, c1: Expr[A], c2: Expr[B], c3: Expr[C], c4: Expr[D], /
    ) -> Returning[tuple[A, B, C, D]]: ...
    @overload
    def returning[A, B, C, D, E](
        self, c1: Expr[A], c2: Expr[B], c3: Expr[C], c4: Expr[D], c5: Expr[E], /
    ) -> Returning[tuple[A, B, C, D, E]]: ...
    @overload
    def returning[A, B, C, D, E, F](
        self,
        c1: Expr[A],
        c2: Expr[B],
        c3: Expr[C],
        c4: Expr[D],
        c5: Expr[E],
        c6: Expr[F],
        /,
    ) -> Returning[tuple[A, B, C, D, E, F]]: ...
    @overload
    def returning(self, first: ExprBase, /, *rest: ExprBase) -> Returning[Any]: ...
    def returning(self, *columns: object) -> Returning[Any]:
        """``returning()`` decodes whole table rows; ``returning(Users.id, count())`` decodes
        just those expressions, as a tuple (or a dict for a name-to-expression mapping)."""
        if not columns:
            return Returning(self.runner, self, self.table)
        if len(columns) == 1 and isinstance(columns[0], Mapping):
            given = cast("Mapping[object, object]", columns[0])
            fields: dict[str, ExprBase] = {}
            for key, value in given.items():
                if not isinstance(key, str) or not isinstance(value, ExprBase):
                    raise QueryError(f"returning field {key!r} must map a name to an expression")
                fields[key] = value
            if not fields:
                raise QueryError("returning({}) needs at least one field")
            return Returning(self.runner, self, self.table, fields)
        slots: list[ExprBase] = []
        for column in columns:
            if not isinstance(column, ExprBase):
                raise QueryError(f"returning() takes expressions, got {column!r}")
            slots.append(column)
        return Returning(self.runner, self, self.table, tuple(slots))


@dataclass(frozen=True, eq=False)
class Returning[R](Executable[list[R]]):
    """An INSERT/UPDATE/DELETE that returns rows: whole table rows, or a projection built
    by ``returning(...)``."""

    query: Executable[int]
    table: type[Table]
    projection: tuple[ExprBase, ...] | dict[str, ExprBase] | None = None

    def cte(self, name: str, *, recursive: bool = False) -> Cte:
        """A data-modifying CTE: ``WITH moved AS (DELETE ... RETURNING ...) ...``."""
        from mizzle.core.query.select import Cte

        return Cte(name, cast("Returning[Table]", self), recursive=recursive)

    def decoder(self) -> Callable[[Row], object]:
        """A function that decodes one result row, built once per statement."""
        projection = self.projection
        if projection is None:  # rows of an alias decode into the real table (Q6)
            return table_decoder(real_table(self.table))
        slots = tuple(projection.values()) if isinstance(projection, dict) else projection
        parts: list[Callable[[Row], object]] = []
        for index, slot in enumerate(slots):
            column = column_of(slot)
            parts.append(_value(index, column.decoder if column is not None else None))
        if isinstance(projection, dict):
            keys = tuple(projection)
            return lambda row: dict(zip(keys, [part(row) for part in parts], strict=True))
        return lambda row: tuple([part(row) for part in parts])

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> list[R]:
        rows = await self.runner.fetch(compiled)
        decode = self.decoder()
        return cast("list[R]", [decode(row) for row in rows])

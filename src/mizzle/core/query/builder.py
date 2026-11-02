"""Entry points for building statements: select, insert, update, delete, with_."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, overload

from mizzle.core.errors import QueryError
from mizzle.core.query.base import Runner
from mizzle.core.query.insert import Insert
from mizzle.core.query.relational import RelationalQuery
from mizzle.core.query.select import Cte, Projection, Select
from mizzle.core.query.update import Delete, Update
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Expr


class SelectAll:
    """``db.select()``: rows of whole tables."""

    def __init__(self, runner: Runner, ctes: tuple[Cte, ...]) -> None:
        self._runner = runner
        self._ctes = ctes

    def from_[T: Table](self, table: type[T]) -> Select[T]:
        return Select(self._runner, None, table, self._ctes)


class SelectFrom[R]:
    """``db.select(cols...)``: rows shaped by the projection."""

    def __init__(self, runner: Runner, projection: Projection, ctes: tuple[Cte, ...]) -> None:
        self._runner = runner
        self._projection = projection
        self._ctes = ctes

    def from_(self, source: type[Table] | Cte) -> Select[R]:
        return Select(self._runner, self._projection, source, self._ctes)


def _selectable(item: object) -> bool:
    return isinstance(item, Expr) or (isinstance(item, type) and issubclass(item, Table))


class QueryBuilder:
    def __init__(self, runner: Runner, ctes: tuple[Cte, ...] = ()) -> None:
        self.runner = runner
        self.ctes = ctes

    def with_(self, *ctes: Cte) -> QueryBuilder:
        """``WITH`` clause: ``db.with_(cte).select(...).from_(cte)``."""
        return QueryBuilder(self.runner, (*self.ctes, *ctes))

    def query[T: Table](self, table: type[T]) -> RelationalQuery[T]:
        """Relational queries: ``db.query(Users).find_many(with_={"posts": True})``."""
        return RelationalQuery(self.runner, table)

    def insert[T: Table](self, table: type[T]) -> Insert[T]:
        return Insert(self.runner, table)

    def update[T: Table](self, table: type[T]) -> Update[T]:
        return Update(self.runner, table)

    def delete[T: Table](self, table: type[T]) -> Delete[T]:
        return Delete(self.runner, table)

    @overload
    def select(self) -> SelectAll: ...
    @overload
    def select(self, fields: Mapping[str, Expr[Any]], /) -> SelectFrom[dict[str, Any]]: ...
    @overload
    def select[A](self, c1: Expr[A] | type[A], /) -> SelectFrom[tuple[A]]: ...
    @overload
    def select[A, B](
        self,
        c1: Expr[A] | type[A],
        c2: Expr[B] | type[B],
        /,
    ) -> SelectFrom[tuple[A, B]]: ...
    @overload
    def select[A, B, C](
        self,
        c1: Expr[A] | type[A],
        c2: Expr[B] | type[B],
        c3: Expr[C] | type[C],
        /,
    ) -> SelectFrom[tuple[A, B, C]]: ...
    @overload
    def select[A, B, C, D](
        self,
        c1: Expr[A] | type[A],
        c2: Expr[B] | type[B],
        c3: Expr[C] | type[C],
        c4: Expr[D] | type[D],
        /,
    ) -> SelectFrom[tuple[A, B, C, D]]: ...
    @overload
    def select[A, B, C, D, E](
        self,
        c1: Expr[A] | type[A],
        c2: Expr[B] | type[B],
        c3: Expr[C] | type[C],
        c4: Expr[D] | type[D],
        c5: Expr[E] | type[E],
        /,
    ) -> SelectFrom[tuple[A, B, C, D, E]]: ...
    @overload
    def select[A, B, C, D, E, F](
        self,
        c1: Expr[A] | type[A],
        c2: Expr[B] | type[B],
        c3: Expr[C] | type[C],
        c4: Expr[D] | type[D],
        c5: Expr[E] | type[E],
        c6: Expr[F] | type[F],
        /,
    ) -> SelectFrom[tuple[A, B, C, D, E, F]]: ...
    @overload
    def select[A, B, C, D, E, F, G](
        self,
        c1: Expr[A] | type[A],
        c2: Expr[B] | type[B],
        c3: Expr[C] | type[C],
        c4: Expr[D] | type[D],
        c5: Expr[E] | type[E],
        c6: Expr[F] | type[F],
        c7: Expr[G] | type[G],
        /,
    ) -> SelectFrom[tuple[A, B, C, D, E, F, G]]: ...
    @overload
    def select[A, B, C, D, E, F, G, H](
        self,
        c1: Expr[A] | type[A],
        c2: Expr[B] | type[B],
        c3: Expr[C] | type[C],
        c4: Expr[D] | type[D],
        c5: Expr[E] | type[E],
        c6: Expr[F] | type[F],
        c7: Expr[G] | type[G],
        c8: Expr[H] | type[H],
        /,
    ) -> SelectFrom[tuple[A, B, C, D, E, F, G, H]]: ...
    @overload
    def select[A, B, C, D, E, F, G, H, I](
        self,
        c1: Expr[A] | type[A],
        c2: Expr[B] | type[B],
        c3: Expr[C] | type[C],
        c4: Expr[D] | type[D],
        c5: Expr[E] | type[E],
        c6: Expr[F] | type[F],
        c7: Expr[G] | type[G],
        c8: Expr[H] | type[H],
        c9: Expr[I] | type[I],
        /,
    ) -> SelectFrom[tuple[A, B, C, D, E, F, G, H, I]]: ...
    @overload
    def select[A, B, C, D, E, F, G, H, I, J](
        self,
        c1: Expr[A] | type[A],
        c2: Expr[B] | type[B],
        c3: Expr[C] | type[C],
        c4: Expr[D] | type[D],
        c5: Expr[E] | type[E],
        c6: Expr[F] | type[F],
        c7: Expr[G] | type[G],
        c8: Expr[H] | type[H],
        c9: Expr[I] | type[I],
        c10: Expr[J] | type[J],
        /,
    ) -> SelectFrom[tuple[A, B, C, D, E, F, G, H, I, J]]: ...
    @overload
    def select(
        self, first: Expr[Any] | type[Table], /, *rest: Expr[Any] | type[Table]
    ) -> SelectFrom[tuple[Any, ...]]: ...
    def select(self, *columns: Any) -> Any:
        if not columns:
            return SelectAll(self.runner, self.ctes)
        if len(columns) == 1 and isinstance(columns[0], Mapping):
            fields: dict[str, Any] = dict(columns[0])  # pyright: ignore[reportUnknownArgumentType]
            if not fields:
                raise QueryError("select({}) needs at least one field")
            for key, value in fields.items():
                if not isinstance(value, Expr):
                    raise QueryError(f"select field {key!r} must be a column or expression")
            return SelectFrom[dict[str, Any]](self.runner, fields, self.ctes)
        for item in columns:
            if not _selectable(item):
                raise QueryError(f"cannot select {item!r}; pass columns, expressions or tables")
        return SelectFrom[tuple[Any, ...]](self.runner, tuple(columns), self.ctes)

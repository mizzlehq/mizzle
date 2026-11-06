"""SELECT builder."""

from __future__ import annotations

import math
import types
import typing
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from typing import Literal, Self, cast, overload, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import (
    Executable,
    Row,
    Runner,
    evolve,
    row_decoder,
    table_columns,
    table_decoder,
)
from mizzle.core.query.returning import Returning
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table
from mizzle.core.sql.alias import alias_table, real_table
from mizzle.core.sql.functions import column_of
from mizzle.core.sql.nodes import (
    Condition,
    CteColumn,
    Cube,
    Expr,
    ExprBase,
    Frame,
    GroupingSets,
    Node,
    OrderBy,
    Placeholder,
    Rollup,
    TypedCteColumn,
    Window,
    window,
)
from mizzle.core.sql.operators import and_

type JoinKind = Literal["INNER", "LEFT", "RIGHT", "FULL", "CROSS"]
type Source = type[Table] | Cte
type Slot = ExprBase | type[Table]
# None: whole rows of the source table. Tuple: one result slot per item. Dict: named fields.
type Projection = tuple[Slot, ...] | dict[str, ExprBase] | None
# FOR UPDATE / FOR NO KEY UPDATE / FOR SHARE / FOR KEY SHARE.
type LockStrength = Literal["UPDATE", "NO KEY UPDATE", "SHARE", "KEY SHARE"]


@dataclass(frozen=True)
class Join:
    kind: JoinKind
    source: Source
    on: Condition | None  # None: CROSS JOIN
    lateral: bool = False


@dataclass(frozen=True)
class Lock:
    of: tuple[type[Table], ...] = ()
    skip_locked: bool = False
    nowait: bool = False
    strength: LockStrength = "UPDATE"


@dataclass(frozen=True)
class Tablesample:
    """``TABLESAMPLE``: read a random sample of a table's pages/rows."""

    method: Literal["SYSTEM", "BERNOULLI"]
    percent: float
    seed: float | None = None


@dataclass(frozen=True, eq=False)
class Cte(Node):
    """A named subquery: a ``WITH`` query (``query.cte(name)``), or with ``inline`` a
    subquery in FROM (``query.subquery(name)``). Reference its columns with ``c()``."""

    name: str
    query: SelectBase | CompoundBase | Returning[Table]
    alias: str | None = None  # see alias(cte, name)
    inline: bool = False
    recursive: bool = False  # WITH RECURSIVE

    def c[T](self, column: str, type_: type[T]) -> TypedCteColumn[T]:
        """A column of this query, e.g. ``cte.c("id", int)``. The type is not checked;
        values decode like the column of the inner query they come from."""
        return TypedCteColumn[T](self, column, type_)

    def source_column(self, name: str) -> ColumnBase | None:
        """The column whose values the output column ``name`` holds, if known."""
        query = self.query
        while isinstance(query, CompoundBase):
            query = query.left  # output columns are named by the first query
        if isinstance(query, Returning):
            if query.projection is not None:
                projection = query.projection
                if isinstance(projection, dict):
                    node = projection.get(name)
                    return None if node is None else column_of(node)
                for expr_node in projection:
                    column = column_of(expr_node)
                    if column is not None and name in (column.attr, column.db_name()):
                        return column
                return None
            for attr, column in query.table.__table__.columns.items():
                if name in (attr, column.db_name()):
                    return column
            return None
        if isinstance(query.projection, dict):
            node = query.projection.get(name)
            return None if node is None else column_of(node)
        for slot in query.slots():
            if isinstance(slot, type):
                columns: tuple[ExprBase, ...] = tuple(slot.__table__.columns.values())
            else:
                columns = (slot,)
            for item in columns:
                if isinstance(item, ColumnBase) and name in (item.attr, item.db_name()):
                    return item
                if isinstance(item, CteColumn) and item.name == name:
                    return column_of(item)
        return None


def _hint_types(hint: object) -> list[type[object]] | None:
    """The classes a type hint allows (``X | None`` gives ``[X, NoneType]``), or None when
    the hint is not a plain class union (``Any``, ``Literal``, a type variable)."""
    # ``X | None`` has origin ``types.UnionType``; ``Optional[X]`` a special form that is not
    # a class (``list[str]``'s is ``list``). A Literal also lands here; its values are not
    # classes, so it gives None below.
    origin = typing.get_origin(hint)
    union = origin is types.UnionType or (origin is not None and not isinstance(origin, type))
    out: list[type[object]] = []
    for arg in typing.get_args(hint) if union else (hint,):
        if arg is None:
            out.append(type(None))
            continue
        origin = typing.get_origin(arg) or arg  # list[str] -> list
        if not isinstance(origin, type):
            return None
        out.append(cast("type[object]", origin))
    return out


def _check_row_types(shape: type[object], projection: Mapping[str, object]) -> None:
    """A plain column's value type and nullability must fit the TypedDict field (B14)."""
    try:
        hints = cast("dict[str, object]", typing.get_type_hints(shape))
    except Exception:  # an unresolvable forward reference: the names were checked
        return
    label = getattr(shape, "__name__", shape)
    for name, value in projection.items():
        if not isinstance(value, ColumnBase) or not value.accepts or name not in hints:
            continue
        allowed = _hint_types(hints[name])
        if allowed is None:
            continue
        if value.nullable and type(None) not in allowed:
            raise QueryError(
                f"as_rows({label}): field {name!r}: the column can be NULL, but the "
                "TypedDict does not allow None"
            )
        classes = [t for t in allowed if t is not type(None)]
        if classes and not any(
            issubclass(a, t) or issubclass(t, a) for a in value.accepts for t in classes
        ):
            holds = " | ".join(a.__name__ for a in value.accepts)
            says = " | ".join(t.__name__ for t in classes)
            raise QueryError(
                f"as_rows({label}): field {name!r}: the column holds {holds}, but the "
                f"TypedDict says {says}"
            )


def _count(name: str, value: int | Expr[int]) -> int | Placeholder:
    if isinstance(value, Placeholder):
        return value
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise QueryError(f"{name}() needs a non-negative int or placeholder(); got {value!r}")
    return value


@dataclass(frozen=True, eq=False)
class SelectBase(Node):
    """A SELECT and its clauses, without the result type (see ``Select[R]``)."""

    runner: Runner = field(repr=False)
    projection: Projection
    source: Source
    ctes: tuple[Cte, ...] = ()
    joins: tuple[Join, ...] = ()
    tablesample_: Tablesample | None = None
    where_: Condition | None = None
    group_by_: tuple[Node, ...] = ()
    having_: Condition | None = None
    order_by_: tuple[ExprBase | OrderBy, ...] = ()
    limit_: int | Placeholder | None = None
    offset_: int | Placeholder | None = None
    distinct_: bool = False
    distinct_on_: tuple[ExprBase, ...] = ()
    lock: Lock | None = None
    windows_: tuple[tuple[str, Window], ...] = ()

    # -- clauses ----------------------------------------------------------------------

    def where(self, condition: Condition) -> Self:
        """Add a filter. Calling ``where`` again ANDs the conditions."""
        combined = condition if self.where_ is None else and_(self.where_, condition)
        return evolve(self, where_=combined)

    def _join(
        self, kind: JoinKind, source: Source, on: Condition | None, *, lateral: bool = False
    ) -> Self:
        if kind in ("RIGHT", "FULL") and self.projection is None:
            raise QueryError(
                f"{kind.lower()} join needs an explicit select(A, B) so missing rows are typed"
            )
        return evolve(self, joins=(*self.joins, Join(kind, source, on, lateral)))

    def join(self, source: Source, on: Condition) -> Self:
        return self._join("INNER", source, on)

    def join_lateral(self, source: Source, on: Condition | None = None) -> Self:
        """``JOIN LATERAL``: ``source`` (a subquery) may reference columns of the tables
        before it. Without ``on`` the join is ``ON true`` (keep the subquery's own filter)."""
        return self._join("INNER", source, on, lateral=True)

    def left_join_lateral(self, source: Source, on: Condition | None = None) -> Self:
        """``LEFT JOIN LATERAL``; without ``on`` the join is ``ON true``."""
        return self._join("LEFT", source, on, lateral=True)

    def cross_join(self, source: Source) -> Self:
        """Every row paired with every row of ``source`` (``CROSS JOIN``, no condition)."""
        return self._join("CROSS", source, None)

    def cross_join_lateral(self, source: Source) -> Self:
        """``CROSS JOIN LATERAL``: one evaluation of ``source`` per outer row."""
        return self._join("CROSS", source, None, lateral=True)

    def tablesample(
        self,
        method: Literal["SYSTEM", "BERNOULLI"],
        percent: float,
        *,
        seed: float | None = None,
    ) -> Self:
        """``TABLESAMPLE method (percent)``: read only a random part of the table.
        ``SYSTEM`` samples pages (fast), ``BERNOULLI`` samples rows (slower, finer)."""
        if method not in ("SYSTEM", "BERNOULLI"):
            raise QueryError(f"tablesample() method must be SYSTEM or BERNOULLI; got {method!r}")
        try:
            bad_percent = not 0 <= percent <= 100
            bad_seed = seed is not None and not math.isfinite(seed)
        except (ArithmeticError, ValueError):  # Decimal("sNaN") cannot even be compared (B16)
            raise QueryError("tablesample() percent and seed must be finite numbers") from None
        if bad_percent:
            raise QueryError("tablesample() percent must be between 0 and 100")
        if bad_seed:
            raise QueryError("tablesample() seed must be a finite number")
        return evolve(self, tablesample_=Tablesample(method, float(percent), seed))

    def group_by(self, *exprs: ExprBase) -> Self:
        return evolve(self, group_by_=(*self.group_by_, *exprs))

    def rollup(self, *exprs: ExprBase) -> Self:
        """``GROUP BY ROLLUP (...)``: the grouped rows plus subtotals and a grand total."""
        if not exprs:
            raise QueryError("rollup() needs at least one expression")
        return evolve(self, group_by_=(*self.group_by_, Rollup(exprs)))

    def cube(self, *exprs: ExprBase) -> Self:
        """``GROUP BY CUBE (...)``: subtotals for every combination of the expressions."""
        if not exprs:
            raise QueryError("cube() needs at least one expression")
        return evolve(self, group_by_=(*self.group_by_, Cube(exprs)))

    def grouping_sets(self, *sets: Sequence[ExprBase]) -> Self:
        """``GROUP BY GROUPING SETS (...)``: one entry per set of expressions; an empty
        sequence means the grand total."""
        if not sets:
            raise QueryError("grouping_sets() needs at least one set")
        return evolve(
            self, group_by_=(*self.group_by_, GroupingSets(tuple(tuple(s) for s in sets)))
        )

    def having(self, condition: Condition) -> Self:
        combined = condition if self.having_ is None else and_(self.having_, condition)
        return evolve(self, having_=combined)

    def window(
        self,
        name: str,
        /,
        *,
        partition_by: ExprBase | Sequence[ExprBase] = (),
        order_by: ExprBase | OrderBy | Sequence[ExprBase | OrderBy] = (),
        frame: Frame | None = None,
    ) -> Self:
        """A named window (``WINDOW name AS (...)``) for ``.over(name)`` in this query."""
        spec = window(None, partition_by, order_by, frame)
        return evolve(self, windows_=(*self.windows_, (name, spec)))

    def order_by(self, *items: ExprBase | OrderBy) -> Self:
        return evolve(self, order_by_=(*self.order_by_, *items))

    def limit(self, n: int | Expr[int]) -> Self:
        return evolve(self, limit_=_count("limit", n))

    def offset(self, n: int | Expr[int]) -> Self:
        return evolve(self, offset_=_count("offset", n))

    def distinct(self) -> Self:
        return evolve(self, distinct_=True)

    def distinct_on(self, *exprs: ExprBase) -> Self:
        """``DISTINCT ON (exprs)``: keep one row per distinct combination of ``exprs``.
        PostgreSQL wants ``order_by`` to start with the same expressions."""
        if not exprs:
            raise QueryError("distinct_on() needs at least one expression")
        return evolve(self, distinct_on_=exprs)

    def for_update(
        self, *, of: Sequence[type[Table]] = (), skip_locked: bool = False, nowait: bool = False
    ) -> Self:
        return self._lock("UPDATE", of, skip_locked=skip_locked, nowait=nowait)

    def for_no_key_update(
        self, *, of: Sequence[type[Table]] = (), skip_locked: bool = False, nowait: bool = False
    ) -> Self:
        """``FOR NO KEY UPDATE``: like ``FOR UPDATE``, but does not block foreign-key checks."""
        return self._lock("NO KEY UPDATE", of, skip_locked=skip_locked, nowait=nowait)

    def for_share(
        self, *, of: Sequence[type[Table]] = (), skip_locked: bool = False, nowait: bool = False
    ) -> Self:
        """``FOR SHARE``: a shared lock; several readers can hold it at once."""
        return self._lock("SHARE", of, skip_locked=skip_locked, nowait=nowait)

    def for_key_share(
        self, *, of: Sequence[type[Table]] = (), skip_locked: bool = False, nowait: bool = False
    ) -> Self:
        """``FOR KEY SHARE``: the weakest row lock; blocks only key-changing updates."""
        return self._lock("KEY SHARE", of, skip_locked=skip_locked, nowait=nowait)

    def _lock(
        self,
        strength: LockStrength,
        of: Sequence[type[Table]],
        *,
        skip_locked: bool,
        nowait: bool,
    ) -> Self:
        if skip_locked and nowait:
            raise QueryError("a row lock takes skip_locked or nowait, not both")
        return evolve(self, lock=Lock(tuple(of), skip_locked, nowait, strength))

    def cte(self, name: str, *, recursive: bool = False) -> Cte:
        """Use this query as a named CTE: ``db.with_(cte).select(...).from_(cte)``. Mark it
        ``recursive`` for ``WITH RECURSIVE`` (a query that refers to itself)."""
        return Cte(name, self, recursive=recursive)

    def subquery(self, name: str) -> Cte:
        """Use this query in FROM or a join: ``db.select(sub.c("n", int)).from_(sub)``."""
        return Cte(name, self, inline=True)

    # -- results ----------------------------------------------------------------------

    def slots(self) -> tuple[Slot, ...]:
        if self.projection is None:
            assert not isinstance(self.source, Cte)  # select() only accepts tables
            return (self.source,)
        if isinstance(self.projection, dict):
            return tuple(self.projection.values())
        return self.projection

    def decoder(self) -> Callable[[Row], object]:
        """A function that decodes one result row. Built once per query, used per row."""
        if self.projection is None and not self.joins and isinstance(self.source, type):
            return table_decoder(real_table(self.source))
        outer = self._outer_sources()
        parts: list[Callable[[Row], object]] = []
        index = 0
        for slot in self.slots():
            if isinstance(slot, type):
                columns = table_columns(slot)
                decode = row_decoder(real_table(slot), columns, index)
                parts.append(_or_none(decode, index, len(columns)) if slot in outer else decode)
                index += len(columns)
            else:
                column = column_of(slot)
                parts.append(_value(index, column.decoder if column is not None else None))
                index += 1
        if self.projection is None:
            return parts[0]
        if isinstance(self.projection, dict):
            keys = tuple(self.projection)
            return lambda row: dict(zip(keys, [part(row) for part in parts], strict=True))
        return lambda row: tuple([part(row) for part in parts])

    def _outer_sources(self) -> set[Source]:
        """Sources an outer join can leave empty (their columns come back all NULL)."""
        nullable: set[Source] = set()
        seen: list[Source] = [self.source]
        for join in self.joins:
            if join.kind in ("LEFT", "FULL"):
                nullable.add(join.source)
            if join.kind in ("RIGHT", "FULL"):
                nullable.update(seen)
            seen.append(join.source)
        return nullable


def _or_none(
    decode: Callable[[Row], Table], start: int, width: int
) -> Callable[[Row], Table | None]:
    # ponytail: a real row whose columns are all NULL (only possible in a table with no
    # NOT NULL column) is indistinguishable from "no match" and decodes to None.
    stop = start + width

    def decode_or_none(row: Row) -> Table | None:
        return None if all(v is None for v in row[start:stop]) else decode(row)

    return decode_or_none


def _value(index: int, convert: Callable[[object], object] | None) -> Callable[[Row], object]:
    if convert is None:
        return lambda row: row[index]

    def decode(row: Row) -> object:
        value = row[index]
        return None if value is None else convert(value)

    return decode


class SetOps[R]:
    """``union``, ``intersect`` and ``except_`` between queries with the same row type."""

    def union(self, other: Select[R] | Compound[R]) -> Compound[R]:
        """Rows of both queries, without duplicates."""
        return self._set_op("UNION", other)

    def union_all(self, other: Select[R] | Compound[R]) -> Compound[R]:
        """Rows of both queries, duplicates kept."""
        return self._set_op("UNION ALL", other)

    def intersect(self, other: Select[R] | Compound[R]) -> Compound[R]:
        """Rows in both queries, without duplicates."""
        return self._set_op("INTERSECT", other)

    def intersect_all(self, other: Select[R] | Compound[R]) -> Compound[R]:
        return self._set_op("INTERSECT ALL", other)

    def except_(self, other: Select[R] | Compound[R]) -> Compound[R]:
        """Rows of this query that are not in ``other``, without duplicates."""
        return self._set_op("EXCEPT", other)

    def except_all(self, other: Select[R] | Compound[R]) -> Compound[R]:
        return self._set_op("EXCEPT ALL", other)

    def _set_op(self, op: str, other: Select[R] | Compound[R]) -> Compound[R]:
        assert isinstance(self, SelectBase | CompoundBase)
        return Compound[R](self.runner, self, op, other)


@dataclass(frozen=True, eq=False)
class Recursive[R]:
    """A ``WITH RECURSIVE`` CTE being built: the seed query plus a step that refers to it.

    Built by ``qb.recursive(name, seed)``; ``.step(lambda self_ref: ...)`` returns the CTE
    to hand to ``qb.with_(...)``. The self-reference is created for you, so the name is
    written once instead of twice.
    """

    name: str
    seed: Select[R] | Compound[R]

    def step(self, build: Callable[[Cte], Select[R] | Compound[R]]) -> Cte:
        """``build`` receives the CTE and returns the recursive term only (a select that
        reads from it); ``step`` itself joins it to the seed with ``UNION ALL``."""
        reference = Cte(self.name, self.seed)
        term = build(reference)
        return Cte(self.name, self.seed.union_all(term), recursive=True)


@dataclass(frozen=True, eq=False)
class CompoundBase(Node):
    """Queries combined with UNION / INTERSECT / EXCEPT (see ``Compound[R]``)."""

    runner: Runner = field(repr=False)
    left: SelectBase | CompoundBase
    op: str
    right: SelectBase | CompoundBase
    order_by_: tuple[ExprBase | OrderBy, ...] = ()
    limit_: int | Placeholder | None = None
    offset_: int | Placeholder | None = None

    def order_by(self, *items: ExprBase | OrderBy) -> Self:
        """Order the combined rows. Columns are matched by name to the result columns;
        expressions may use them too (``abs_(Items.a - 3)``)."""
        return evolve(self, order_by_=(*self.order_by_, *items))

    def limit(self, n: int | Expr[int]) -> Self:
        return evolve(self, limit_=_count("limit", n))

    def offset(self, n: int | Expr[int]) -> Self:
        return evolve(self, offset_=_count("offset", n))

    def cte(self, name: str, *, recursive: bool = False) -> Cte:
        return Cte(name, self, recursive=recursive)

    def subquery(self, name: str) -> Cte:
        return Cte(name, self, inline=True)

    def decoder(self) -> Callable[[Row], object]:
        return self.left.decoder()  # rows have the shape of the first query


@dataclass(frozen=True, eq=False)
class Compound[R](CompoundBase, Executable[list[R]], SetOps[R]):
    @override
    async def run_compiled(self, compiled: CompiledQuery) -> list[R]:
        rows = await self.runner.fetch(compiled, read_only=True)
        decode = self.decoder()
        return cast(list[R], [decode(row) for row in rows])


@overload
def alias[T: Table](source: type[T], name: str, /) -> type[T]: ...
@overload
def alias(source: Cte, name: str, /) -> Cte: ...
def alias(source: type[Table] | Cte, name: str, /) -> type[Table] | Cte:
    """The same table (or CTE) under another name, to use it twice in one query:
    ``manager = alias(Employees, "manager")``, then ``manager.id`` and ``.join(manager, ...)``.
    Rows of an aliased table decode into the table's own class."""
    if isinstance(source, Cte):
        return replace(source, alias=name)
    return alias_table(source, name)


@dataclass(frozen=True, eq=False)
class Select[R](SelectBase, Executable[list[R]], SetOps[R]):
    """A SELECT whose rows are ``R`` (a table row, a tuple, or a dict)."""

    @overload
    def left_join[A, J: Table](
        self: Select[tuple[A, J]], source: type[J], on: Condition
    ) -> Select[tuple[A, J | None]]: ...
    @overload
    def left_join[A, B, J: Table](  # type: ignore[overload-overlap]
        self: Select[tuple[A, B, J]], source: type[J], on: Condition
    ) -> Select[tuple[A, B, J | None]]: ...
    @overload
    def left_join[A, B, J: Table](
        self: Select[tuple[A, J, B]], source: type[J], on: Condition
    ) -> Select[tuple[A, J | None, B]]: ...
    @overload
    def left_join(self, source: Source, on: Condition) -> Self: ...
    def left_join(self, source: Source, on: Condition) -> SelectBase:
        return self._join("LEFT", source, on)

    @overload
    def right_join[A, J: Table](
        self: Select[tuple[A, J]], source: type[J], on: Condition
    ) -> Select[tuple[A | None, J]]: ...
    @overload
    def right_join(self, source: Source, on: Condition) -> Self: ...
    def right_join(self, source: Source, on: Condition) -> SelectBase:
        return self._join("RIGHT", source, on)

    @overload
    def full_join[A, J: Table](
        self: Select[tuple[A, J]], source: type[J], on: Condition
    ) -> Select[tuple[A | None, J | None]]: ...
    @overload
    def full_join(self, source: Source, on: Condition) -> Self: ...
    def full_join(self, source: Source, on: Condition) -> SelectBase:
        return self._join("FULL", source, on)

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> list[R]:
        # decode() builds exactly the shape R describes (rows, tuples or dicts).
        rows = await self.runner.fetch(compiled, read_only=True)
        decode = self.decoder()
        return cast(list[R], [decode(row) for row in rows])

    def as_rows[RowT](self, shape: type[RowT], /) -> Select[RowT]:
        """Re-type a named projection as a ``typegen`` ``<Table>Row`` TypedDict::

            class UsersRow(TypedDict):
                id: int
                email: str

            rows = await (
                db.select({"id": Users.id, "email": Users.email})
                .from_(Users)
                .as_rows(UsersRow)
            )  # list[UsersRow]

        Nothing changes at runtime: the rows are already dicts keyed by the projection
        names. A TypedDict whose fields do not match the projection is a ``QueryError``, and
        so is a plain column whose value type or nullability the field does not allow (B14).
        Computed expressions are checked by name only: their Python type is not known here.
        """
        if not isinstance(self.projection, dict):
            raise QueryError("as_rows() needs select({...}) with named fields")
        required = cast("frozenset[str] | None", getattr(shape, "__required_keys__", None))
        if required is not None:  # a TypedDict: the names must line up
            optional: frozenset[str] = getattr(shape, "__optional_keys__", frozenset())
            declared = set(required) | optional
            if declared != set(self.projection):
                raise QueryError(
                    f"as_rows({getattr(shape, '__name__', shape)}): the projection has "
                    f"{sorted(self.projection)} but the TypedDict has {sorted(declared)}"
                )
            _check_row_types(shape, self.projection)
        return cast("Select[RowT]", self)

    async def first(self) -> R | None:
        """The first row, or ``None`` when the query returns none. Adds ``LIMIT 1`` when the
        query has no limit of its own."""
        rows = await (self if self.limit_ is not None else self.limit(1))
        return rows[0] if rows else None

    async def one(self) -> R:
        """Exactly one row. Raises ``QueryError`` when there are none or more than one."""
        # Keep a stricter limit the caller set (limit(0) or limit(1) is already exact).
        limit = self.limit_
        rows = await (self if isinstance(limit, int) and limit <= 2 else self.limit(2))
        if not rows:
            raise QueryError("one() found no rows; use first() for an optional row")
        if len(rows) > 1:
            raise QueryError("one() found more than one row")
        return rows[0]

    @overload
    async def scalar[A](self: Select[tuple[A]]) -> A | None: ...
    @overload
    async def scalar(self) -> object: ...
    async def scalar(self) -> object:
        """The single selected value of the first row, or ``None``. The query must select
        exactly one column."""
        projection = self.projection
        if isinstance(projection, tuple | dict):
            width = len(projection)
        else:
            raise QueryError("scalar() needs an explicit select(expr)")
        if width != 1:
            raise QueryError(f"scalar() needs exactly one selected column; got {width}")
        row = await self.first()
        if row is None:
            return None
        if isinstance(row, dict):
            return next(iter(cast(dict[str, object], row).values()))
        return cast(tuple[object, ...], row)[0]

    async def stream(self, batch_size: int = 1000) -> AsyncGenerator[R]:
        """Rows one by one, read ``batch_size`` at a time from a server-side cursor, so
        large results never sit in memory at once. Holds a connection (and a transaction)
        until the loop ends; wrap in ``contextlib.aclosing()`` to release it right away
        when breaking out early."""
        decode = self.decoder()
        compiled = self._compile_for_run()
        async with aclosing(self.runner.stream(compiled, batch_size, read_only=True)) as batches:
            async for batch in batches:
                for row in batch:
                    yield cast(R, decode(row))

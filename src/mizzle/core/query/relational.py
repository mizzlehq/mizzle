"""Relational queries: ``db.query(Users).find_many(with_={"posts": True})``.

One SQL statement per call. Related rows come back as nested JSON arrays (built by the
dialect compiler) and are converted back into typed table instances here.
"""

from __future__ import annotations

import datetime
import ipaddress
import struct
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypedDict, cast, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import Converter, Executable, Row, Runner, column_decoder, row_decoder
from mizzle.core.query.select import Cte
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.relations import Link
from mizzle.core.schema.table import Table
from mizzle.core.sql.alias import real_table
from mizzle.core.sql.nodes import Condition, ExprBase, Json, Node, OrderBy


class WithOptions(TypedDict, total=False):
    columns: Sequence[ColumnBase]
    where: Condition
    order_by: OrderItem | Sequence[OrderItem]
    limit: int
    with_: Mapping[str, bool | WithOptions]


type OrderItem = ExprBase | OrderBy
type With = Mapping[str, bool | WithOptions]
type OrderArg = OrderItem | Sequence[OrderItem] | None


@dataclass(frozen=True)
class LoadPlan:
    table: type[Table]
    columns: tuple[tuple[str, ColumnBase], ...]  # (attribute, column) actually loaded
    where: Condition | None = None
    order_by: tuple[OrderItem, ...] = ()
    limit: int | None = None
    offset: int | None = None
    children: tuple[tuple[str, Link, LoadPlan], ...] = field(default=())


def _order(arg: OrderArg) -> tuple[OrderItem, ...]:
    if arg is None:
        return ()
    if isinstance(arg, ExprBase | OrderBy):
        return (arg,)
    return tuple(arg)


def _count(name: str, value: int | None) -> int | None:
    if value is not None and (isinstance(value, bool) or value < 0):
        raise QueryError(f"{name} must be a non-negative int; got {value!r}")
    return value


def _columns(
    table: type[Table], columns: Sequence[ColumnBase] | None
) -> tuple[tuple[str, ColumnBase], ...]:
    """All columns, or only the requested ones (in table order, each once)."""
    available = table.__table__.columns
    if columns is None:
        return tuple(available.items())
    if not columns:
        raise QueryError(f"columns for {table.__name__} must not be empty")
    for column in columns:
        if column.table not in (table, real_table(table)):  # an alias takes both
            raise QueryError(f"{column.qualified_name} is not a column of {table.__name__}")
    wanted = {c.attr for c in columns}
    return tuple((attr, c) for attr, c in available.items() if attr in wanted)


def build_plan(
    table: type[Table],
    *,
    columns: Sequence[ColumnBase] | None = None,
    where: Condition | None = None,
    order_by: OrderArg = None,
    limit: int | None = None,
    offset: int | None = None,
    with_: With | None = None,
) -> LoadPlan:
    children: list[tuple[str, Link, LoadPlan]] = []
    relations = real_table(table).__table__.relations
    for name, options in (with_ or {}).items():
        if name not in relations:
            available = ", ".join(relations) or "none"
            raise QueryError(f"{table.__name__} has no relation {name!r} (available: {available})")
        if options is False:
            continue
        link = relations[name].link()
        opts: WithOptions = {} if options is True else options
        if not link.many and ("limit" in opts or "order_by" in opts):
            raise QueryError(
                f"{table.__name__}.{name} is a one() relation; limit/order_by need many()"
            )
        child = build_plan(
            link.target,
            columns=opts.get("columns"),
            where=opts.get("where"),
            order_by=opts.get("order_by"),
            limit=opts.get("limit"),
            with_=opts.get("with_"),
        )
        children.append((name, link, child))
    return LoadPlan(
        table,
        _columns(table, columns),
        where,
        _order(order_by),
        _count("limit", limit),
        _count("offset", offset),
        tuple(children),
    )


# -- decoding -------------------------------------------------------------------------


def _from_json(column: ColumnBase, value: Json) -> object:
    """Turn a JSON value back into the column's Python type (before ``column.decode``)."""
    if value is None:
        return None
    if column.item is not None:
        if not isinstance(value, list):
            raise QueryError(f"{column.qualified_name}: expected a JSON array, got {value!r}")
        return [_from_json(column.item, v) for v in value]
    if column.json_decoder is not None:  # a range, geometry, cidr, bit string (C30)
        return column.json_decoder(value)
    kind = column.accepts[0] if column.accepts else None
    if kind is None or kind in (int, str, bool) or column.enum is not None:
        return value
    if isinstance(value, list | dict):
        raise QueryError(f"{column.qualified_name}: unexpected JSON value {value!r}")
    if kind is float:
        return struct.unpack(">d", bytes.fromhex(str(value)))[0]  # float8send, as hex
    if kind is Decimal:
        return Decimal(str(value))  # numerics are sent as text: no float rounding
    text = str(value)
    if kind in (datetime.datetime, datetime.date) and text in ("infinity", "-infinity"):
        # asyncpg reads (and writes) min / max as -infinity / infinity; so must JSON.
        end = datetime.datetime if kind is datetime.datetime else datetime.date
        return end.max if text == "infinity" else end.min
    if kind is datetime.datetime:
        return datetime.datetime.fromisoformat(text)
    if kind is datetime.date:
        return datetime.date.fromisoformat(text)
    if kind is datetime.time:
        return datetime.time.fromisoformat(text)
    if kind is datetime.timedelta:
        # Same day count as asyncpg's interval codec: a year is 365 days, a month 30.
        years, months, days, micros = text.split()
        return datetime.timedelta(
            days=int(days) + int(years) * 365 + int(months) * 30,
            microseconds=int(Decimal(micros)),
        )
    if kind is uuid.UUID:
        return uuid.UUID(text)
    if kind is bytes:
        return bytes.fromhex(text)
    if kind in (ipaddress.IPv4Address, ipaddress.IPv4Interface):
        return ipaddress.ip_interface(text) if "/" in text else ipaddress.ip_address(text)
    return value


def _json_list(value: Json, what: str) -> list[Json]:
    if not isinstance(value, list):
        raise QueryError(f"expected a JSON array for {what}, got {value!r}")
    return value


def _json_converter(column: ColumnBase) -> Converter | None:
    """What turns a non-NULL JSON value into the column's Python value (None: keep it)."""
    kind = column.accepts[0] if column.accepts else None
    plain = kind is None or kind in (int, str, bool) or column.enum is not None
    if column.item is None and plain and column.json_decoder is None:
        return column.decoder  # JSON already has the right type; enums still convert
    return lambda value: column.decode(_from_json(column, cast(Json, value)))


type RowDecoder = Callable[[Row], Table]


def plan_decoder(plan: LoadPlan, *, nested: bool = False) -> RowDecoder:
    """Decode a root row (``nested=False``) or one nested JSON row of ``plan``.

    Built once per query; columns that were not loaded are absent from the result, so
    reading them raises a clear AttributeError.
    """
    decode = row_decoder(
        real_table(plan.table),
        plan.columns,
        converter=_json_converter if nested else column_decoder,
    )
    children = [
        (name, link.many, plan_decoder(child, nested=True), child.table.__name__)
        for name, link, child in plan.children
    ]
    if not children:
        return decode
    width = len(plan.columns)

    def decode_with_children(row: Row) -> Table:
        obj = decode(row)
        values = obj.__dict__
        for index, (name, many, child, what) in enumerate(children, width):
            value = cast(Json, row[index])  # built with json_agg / json_build_array
            if many:
                items = [] if value is None else _json_list(value, what)
                values[name] = [child(_json_list(item, what)) for item in items]
            else:
                values[name] = None if value is None else child(_json_list(value, what))
        return obj

    return decode_with_children


# -- statements -----------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class RelationalStatement(Node):
    """A relational query, without its result type (compilers handle this class)."""

    runner: Runner = field(repr=False)
    plan: LoadPlan
    # A WITH clause passed with db.with_(...): emitted before the SELECT (B9).
    ctes: tuple[Cte, ...] = ()


@dataclass(frozen=True, eq=False)
class RelationalSelect[T: Table](RelationalStatement, Executable[list[T]]):
    @override
    async def run_compiled(self, compiled: CompiledQuery) -> list[T]:
        rows = await self.runner.fetch(compiled, read_only=True)
        decode = plan_decoder(self.plan)
        return cast(list[T], [decode(row) for row in rows])

    async def stream(self, batch_size: int = 1000) -> AsyncGenerator[T]:
        """Like ``Select.stream()``: rows from a server-side cursor, ``batch_size`` at a time."""
        decode = plan_decoder(self.plan)
        compiled = self._compile_for_run()
        async with aclosing(self.runner.stream(compiled, batch_size, read_only=True)) as batches:
            async for batch in batches:
                for row in batch:
                    yield cast(T, decode(row))


@dataclass(frozen=True, eq=False)
class RelationalFirst[T: Table](RelationalStatement, Executable[T | None]):
    @override
    async def run_compiled(self, compiled: CompiledQuery) -> T | None:
        rows = await self.runner.fetch(compiled, read_only=True)
        return cast(T, plan_decoder(self.plan)(rows[0])) if rows else None


class RelationalQuery[T: Table]:
    _runner: Runner
    _table: type[T]
    _ctes: tuple[Cte, ...]

    def __init__(self, runner: Runner, table: type[T], ctes: tuple[Cte, ...] = ()) -> None:
        self._runner = runner
        self._table = table
        self._ctes = ctes

    def find_many(
        self,
        *,
        columns: Sequence[ColumnBase] | None = None,
        where: Condition | None = None,
        with_: With | None = None,
        order_by: OrderArg = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> RelationalSelect[T]:
        """``columns=[...]`` loads only those columns; reading others raises AttributeError."""
        plan = build_plan(
            self._table,
            columns=columns,
            where=where,
            order_by=order_by,
            limit=limit,
            offset=offset,
            with_=with_,
        )
        return RelationalSelect(self._runner, plan, self._ctes)

    def find_first(
        self,
        *,
        columns: Sequence[ColumnBase] | None = None,
        where: Condition | None = None,
        with_: With | None = None,
        order_by: OrderArg = None,
    ) -> RelationalFirst[T]:
        plan = build_plan(
            self._table, columns=columns, where=where, order_by=order_by, limit=1, with_=with_
        )
        return RelationalFirst(self._runner, plan, self._ctes)

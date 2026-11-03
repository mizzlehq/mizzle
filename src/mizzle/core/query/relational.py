"""Relational queries: ``db.query(Users).find_many(with_={"posts": True})``.

One SQL statement per call. Related rows come back as nested JSON arrays (built by the
dialect compiler) and are converted back into typed table instances here.
"""

from __future__ import annotations

import datetime
import ipaddress
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypedDict, cast, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import Executable, Row, Runner
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.relations import Link
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Condition, ExprBase, Node, OrderBy


class WithOptions(TypedDict, total=False):
    columns: Sequence[ColumnBase]
    where: Condition
    order_by: OrderItem | Sequence[OrderItem]
    limit: int
    with_: Mapping[str, bool | WithOptions]


type OrderItem = ExprBase | OrderBy
type With = Mapping[str, bool | WithOptions]
type OrderArg = OrderItem | Sequence[OrderItem] | None
# JSON as returned by the driver's json codec (nested rows of relational queries).
type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


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
        if column.table is not table:
            raise QueryError(f"{column.qualified_name} is not a column of {table.__name__}")
    wanted = {id(c) for c in columns}
    return tuple((attr, c) for attr, c in available.items() if id(c) in wanted)


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
    relations = table.__table__.relations
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
    kind = column.accepts[0] if column.accepts else None
    if kind is None or kind in (int, str, bool) or column.enum is not None:
        return value
    if isinstance(value, list | dict):
        raise QueryError(f"{column.qualified_name}: unexpected JSON value {value!r}")
    if kind is float:
        return float(value)  # also handles "NaN" / "Infinity" strings
    if kind is Decimal:
        return Decimal(str(value))  # numerics are sent as text: no float rounding
    text = str(value)
    if kind is datetime.datetime:
        return datetime.datetime.fromisoformat(text)
    if kind is datetime.date:
        return datetime.date.fromisoformat(text)
    if kind is datetime.time:
        return datetime.time.fromisoformat(text)
    if kind is datetime.timedelta:
        return datetime.timedelta(seconds=float(text))
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


def _decode_json_row(plan: LoadPlan, value: Json) -> Table:
    values = _json_list(value, plan.table.__name__)
    data = {
        attr: col.decode(_from_json(col, v))
        for (attr, col), v in zip(plan.columns, values, strict=False)
    }
    row = plan.table.from_db(data)
    nested_values = values[len(plan.columns) :]
    for (name, link, child), nested in zip(plan.children, nested_values, strict=True):
        row.__dict__[name] = _decode_child(link, child, nested)
    return row


def _decode_child(link: Link, plan: LoadPlan, value: Json) -> list[Table] | Table | None:
    if link.many:
        items = [] if value is None else _json_list(value, plan.table.__name__)
        return [_decode_json_row(plan, v) for v in items]
    return None if value is None else _decode_json_row(plan, value)


def decode_root(plan: LoadPlan, row: Row) -> Table:
    # Columns that were not loaded are absent; reading them raises a clear AttributeError.
    obj = plan.table.from_db(
        {attr: col.decode(row[i]) for i, (attr, col) in enumerate(plan.columns)}
    )
    index = len(plan.columns)
    for name, link, child in plan.children:
        nested = cast(Json, row[index])  # built with json_agg / json_build_array
        obj.__dict__[name] = _decode_child(link, child, nested)
        index += 1
    return obj


# -- statements -----------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class RelationalStatement(Node):
    """A relational query, without its result type (compilers handle this class)."""

    runner: Runner = field(repr=False)
    plan: LoadPlan


@dataclass(frozen=True, eq=False)
class RelationalSelect[T: Table](RelationalStatement, Executable[list[T]]):
    @override
    async def run_compiled(self, compiled: CompiledQuery) -> list[T]:
        rows = await self.runner.fetch(compiled, read_only=True)
        return [cast(T, decode_root(self.plan, row)) for row in rows]


@dataclass(frozen=True, eq=False)
class RelationalFirst[T: Table](RelationalStatement, Executable[T | None]):
    @override
    async def run_compiled(self, compiled: CompiledQuery) -> T | None:
        rows = await self.runner.fetch(compiled, read_only=True)
        return cast(T, decode_root(self.plan, rows[0])) if rows else None


class RelationalQuery[T: Table]:
    _runner: Runner
    _table: type[T]

    def __init__(self, runner: Runner, table: type[T]) -> None:
        self._runner = runner
        self._table = table

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
        return RelationalSelect(self._runner, plan)

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
        return RelationalFirst(self._runner, plan)

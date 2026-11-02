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
from typing import Any, TypedDict, cast

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import Executable, Row, Runner, decode_table
from mizzle.core.schema.column import Column
from mizzle.core.schema.relations import Link
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Expr, OrderBy


class WithOptions(TypedDict, total=False):
    where: Expr[bool]
    order_by: Expr[Any] | OrderBy | Sequence[Expr[Any] | OrderBy]
    limit: int
    with_: Mapping[str, bool | WithOptions]


type With = Mapping[str, bool | WithOptions]
type OrderArg = Expr[Any] | OrderBy | Sequence[Expr[Any] | OrderBy] | None


@dataclass(frozen=True)
class LoadPlan:
    table: type[Table]
    where: Expr[bool] | None = None
    order_by: tuple[Expr[Any] | OrderBy, ...] = ()
    limit: int | None = None
    offset: int | None = None
    children: tuple[tuple[str, Link, LoadPlan], ...] = field(default=())


def _order(arg: OrderArg) -> tuple[Expr[Any] | OrderBy, ...]:
    if arg is None:
        return ()
    if isinstance(arg, Expr | OrderBy):
        return (arg,)
    return tuple(arg)


def _count(name: str, value: int | None) -> int | None:
    if value is not None and (isinstance(value, bool) or value < 0):
        raise QueryError(f"{name} must be a non-negative int; got {value!r}")
    return value


def build_plan(
    table: type[Table],
    *,
    where: Expr[bool] | None = None,
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
            where=opts.get("where"),
            order_by=opts.get("order_by"),
            limit=opts.get("limit"),
            with_=opts.get("with_"),
        )
        children.append((name, link, child))
    return LoadPlan(
        table,
        where,
        _order(order_by),
        _count("limit", limit),
        _count("offset", offset),
        tuple(children),
    )


# -- decoding -------------------------------------------------------------------------


def _from_json(column: Column[Any], value: Any) -> Any:
    """Turn a JSON value back into the column's Python type (before ``column.decode``)."""
    if value is None:
        return None
    if column.item is not None:
        return [_from_json(column.item, v) for v in value]
    kind = column.accepts[0] if column.accepts else None
    if kind is None or kind in (int, str, bool) or column.enum is not None:
        return value
    if kind is float:
        return float(value)  # also handles "NaN" / "Infinity" strings
    if kind is Decimal:
        return Decimal(str(value))
    if kind is datetime.datetime:
        return datetime.datetime.fromisoformat(value)
    if kind is datetime.date:
        return datetime.date.fromisoformat(value)
    if kind is datetime.time:
        return datetime.time.fromisoformat(value)
    if kind is datetime.timedelta:
        return datetime.timedelta(seconds=float(value))
    if kind is uuid.UUID:
        return uuid.UUID(value)
    if kind is bytes:
        return bytes.fromhex(value)
    if kind in (ipaddress.IPv4Address, ipaddress.IPv4Interface):
        return ipaddress.ip_interface(value) if "/" in value else ipaddress.ip_address(value)
    return value


def _decode_json_row(plan: LoadPlan, values: list[Any]) -> Table:
    columns = plan.table.__table__.columns
    data = {
        attr: col.decode(_from_json(col, v))
        for (attr, col), v in zip(columns.items(), values, strict=False)
    }
    row = plan.table.from_db(data)
    for (name, link, child), value in zip(plan.children, values[len(columns) :], strict=True):
        row.__dict__[name] = _decode_child(link, child, value)
    return row


def _decode_child(link: Link, plan: LoadPlan, value: Any) -> Any:
    if link.many:
        items: list[list[Any]] = value or []
        return [_decode_json_row(plan, v) for v in items]
    return None if value is None else _decode_json_row(plan, value)


def decode_root(plan: LoadPlan, row: Row) -> Table:
    obj = decode_table(plan.table, row, 0)
    assert obj is not None  # a root row always has its primary key
    index = len(plan.table.__table__.columns)
    for name, link, child in plan.children:
        obj.__dict__[name] = _decode_child(link, child, row[index])
        index += 1
    return obj


# -- statements -----------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class RelationalSelect[T: Table](Executable[list[T]]):
    runner: Runner = field(repr=False)
    plan: LoadPlan

    async def run_compiled(self, compiled: CompiledQuery) -> list[T]:
        rows = await self.runner.fetch(compiled)
        return [cast(T, decode_root(self.plan, row)) for row in rows]


@dataclass(frozen=True, eq=False)
class RelationalFirst[T: Table](Executable[T | None]):
    runner: Runner = field(repr=False)
    plan: LoadPlan

    async def run_compiled(self, compiled: CompiledQuery) -> T | None:
        rows = await self.runner.fetch(compiled)
        return cast(T, decode_root(self.plan, rows[0])) if rows else None


class RelationalQuery[T: Table]:
    def __init__(self, runner: Runner, table: type[T]) -> None:
        self._runner = runner
        self._table = table

    def find_many(
        self,
        *,
        where: Expr[bool] | None = None,
        with_: With | None = None,
        order_by: OrderArg = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> RelationalSelect[T]:
        plan = build_plan(
            self._table, where=where, order_by=order_by, limit=limit, offset=offset, with_=with_
        )
        return RelationalSelect(self._runner, plan)

    def find_first(
        self,
        *,
        where: Expr[bool] | None = None,
        with_: With | None = None,
        order_by: OrderArg = None,
    ) -> RelationalFirst[T]:
        plan = build_plan(self._table, where=where, order_by=order_by, limit=1, with_=with_)
        return RelationalFirst(self._runner, plan)

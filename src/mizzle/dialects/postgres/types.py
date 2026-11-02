"""PostgreSQL-only column types."""

from __future__ import annotations

import datetime as _dt
import enum
import ipaddress
import uuid as _uuid
from collections.abc import Iterable
from typing import cast

from mizzle.core.naming import snake_case
from mizzle.core.schema.column import Column

type IPAddress = (
    ipaddress.IPv4Address
    | ipaddress.IPv6Address
    | ipaddress.IPv4Interface
    | ipaddress.IPv6Interface
    | ipaddress.IPv4Network
    | ipaddress.IPv6Network
)

_IP_TYPES: tuple[type[object], ...] = (
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Interface,
    ipaddress.IPv6Interface,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
)


def serial(name: str | None = None) -> Column[int]:
    return Column("serial", (int,), name, nullable=False, generated=True)


def bigserial(name: str | None = None) -> Column[int]:
    return Column("bigserial", (int,), name, nullable=False, generated=True)


def smallserial(name: str | None = None) -> Column[int]:
    return Column("smallserial", (int,), name, nullable=False, generated=True)


def uuid(name: str | None = None) -> Column[_uuid.UUID | None]:
    return Column("uuid", (_uuid.UUID,), name)


def bytea(name: str | None = None) -> Column[bytes | None]:
    return Column("bytea", (bytes,), name)


def interval(name: str | None = None) -> Column[_dt.timedelta | None]:
    return Column("interval", (_dt.timedelta,), name)


def time(name: str | None = None, *, tz: bool = False) -> Column[_dt.time | None]:
    return Column("timetz" if tz else "time", (_dt.time,), name)


def inet(name: str | None = None) -> Column[IPAddress | None]:
    return Column("inet", _IP_TYPES, name)


class jsonb[T](Column[T | None]):
    """``jsonb[dict[str, int]]()``. ``T`` is a static hint only; it is not checked at runtime."""

    def __init__(self, name: str | None = None) -> None:
        super().__init__("jsonb", None, name)


class json[T](Column[T | None]):
    """``json[list[str]]()``. ``T`` is a static hint only; it is not checked at runtime."""

    def __init__(self, name: str | None = None) -> None:
        super().__init__("json", None, name)


def array[T](item: Column[T], name: str | None = None) -> Column[list[T] | None]:
    # Only called with list/tuple values (column accepts) or arrays decoded by asyncpg.
    def encode(values: object) -> object:
        return [item.encode(v) for v in cast(Iterable[object], values)]

    def decode(values: object) -> object:
        return [item.decode(v) for v in cast(Iterable[object], values)]

    column: Column[list[T] | None] = Column(
        f"{item.sql_type}[]",
        (list, tuple),
        name,
        encoder=encode if item.encoder else None,
        decoder=decode if item.decoder else None,
    )
    column.enum = item.enum
    column.item = item
    return column


def _enum_value(value: object) -> object:
    # Only called with members of the column's enum. Enum.value is typed Any in typeshed.
    return cast(enum.Enum, value).value  # pyright: ignore[reportAny]


def pg_enum[E: enum.Enum](
    enum_cls: type[E], name: str | None = None, *, type_name: str | None = None
) -> Column[E | None]:
    """A PostgreSQL enum column. The DB type name defaults to the class name in snake_case."""
    column: Column[E | None] = Column(
        type_name or snake_case(enum_cls.__name__),
        (enum_cls,),
        name,
        encoder=_enum_value,
        decoder=enum_cls,
    )
    column.enum = enum_cls
    return column

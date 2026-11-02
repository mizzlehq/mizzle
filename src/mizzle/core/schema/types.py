"""Column types shared by every SQL dialect."""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime as _datetime
from decimal import Decimal

from mizzle.core.schema.column import Column


def integer(name: str | None = None) -> Column[int | None]:
    return Column("integer", (int,), name)


def smallint(name: str | None = None) -> Column[int | None]:
    return Column("smallint", (int,), name)


def bigint(name: str | None = None) -> Column[int | None]:
    return Column("bigint", (int,), name)


def text(name: str | None = None) -> Column[str | None]:
    return Column("text", (str,), name)


def varchar(length: int | None = None, name: str | None = None) -> Column[str | None]:
    return Column("varchar" if length is None else f"varchar({length})", (str,), name)


def boolean(name: str | None = None) -> Column[bool | None]:
    return Column("boolean", (bool,), name)


def real(name: str | None = None) -> Column[float | None]:
    return Column("real", (float, int), name)


def double_precision(name: str | None = None) -> Column[float | None]:
    return Column("double precision", (float, int), name)


def numeric(
    precision: int | None = None, scale: int | None = None, name: str | None = None
) -> Column[Decimal | None]:
    if precision is None:
        sql_type = "numeric"
    elif scale is None:
        sql_type = f"numeric({precision})"
    else:
        sql_type = f"numeric({precision}, {scale})"
    return Column(sql_type, (Decimal, int), name)


def date(name: str | None = None) -> Column[_date | None]:
    return Column("date", (_date,), name)


def timestamp(name: str | None = None, *, tz: bool = False) -> Column[_datetime | None]:
    return Column("timestamptz" if tz else "timestamp", (_datetime,), name)

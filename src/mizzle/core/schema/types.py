"""Column types shared by every SQL dialect."""

from __future__ import annotations

from datetime import UTC
from datetime import date as _date
from datetime import datetime as _datetime
from decimal import Decimal
from typing import cast

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


def timestamp(
    name: str | None = None, *, tz: bool = False, precision: int | None = None
) -> Column[_datetime | None]:
    """``precision``: digits of fractional seconds kept, 0-6 (default 6). ``tz=True`` values
    are always read back as aware datetimes (in UTC)."""
    digits = f"({precision})" if precision is not None else ""
    if not tz:
        return Column(f"timestamp{digits}", (_datetime,), name)
    return Column(
        f"timestamptz{digits}", (_datetime,), name, encoder=_tz_encode, decoder=_tz_decode
    )


_MIN_UTC = _datetime.min.replace(tzinfo=UTC)
_MAX_UTC = _datetime.max.replace(tzinfo=UTC)


def _tz_decode(value: object) -> object:
    # asyncpg reads -infinity / infinity as naive datetime.min / max, even for timestamptz.
    stamp = cast(_datetime, value)
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _tz_encode(value: object) -> object:
    # ... and only writes the naive ones back as infinity, so they round-trip.
    if value in (_MAX_UTC, _MIN_UTC):
        return cast(_datetime, value).replace(tzinfo=None)
    return value

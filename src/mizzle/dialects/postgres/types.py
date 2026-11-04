"""PostgreSQL-only column types."""

from __future__ import annotations

import datetime as _dt
import enum
import ipaddress
import re
import struct
import uuid as _uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Protocol, cast, runtime_checkable

from asyncpg import (
    BitString,
    Box,
    Circle,
    Line,
    LineSegment,
    Path,
    Point,
    Polygon,
    Range,
)

from mizzle.core.errors import QueryError
from mizzle.core.naming import snake_case
from mizzle.core.schema.column import Column, ColumnBase
from mizzle.core.sql.nodes import Expr, Node, Param, TypedOperation

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


def time(
    name: str | None = None, *, tz: bool = False, precision: int | None = None
) -> Column[_dt.time | None]:
    """``precision``: digits of fractional seconds kept, 0-6 (default 6)."""
    digits = f"({precision})" if precision is not None else ""
    return Column(f"time{'tz' if tz else ''}{digits}", (_dt.time,), name)


def int4range(name: str | None = None) -> Column[Range[int] | None]:
    """A discrete ``integer`` range, ``[1,5)``. Values read and write as ``asyncpg.Range``."""
    return Column("int4range", (Range,), name)


def int8range(name: str | None = None) -> Column[Range[int] | None]:
    """A discrete ``bigint`` range. Values read and write as ``asyncpg.Range``."""
    return Column("int8range", (Range,), name)


def numrange(name: str | None = None) -> Column[Range[Decimal] | None]:
    """A continuous ``numeric`` range. Values read and write as ``asyncpg.Range``."""
    return Column("numrange", (Range,), name)


def tsrange(name: str | None = None) -> Column[Range[_dt.datetime] | None]:
    """A ``timestamp without time zone`` range. Values read and write as ``asyncpg.Range``."""
    return Column("tsrange", (Range,), name)


def tstzrange(name: str | None = None) -> Column[Range[_dt.datetime] | None]:
    """A ``timestamp with time zone`` range. Values read and write as ``asyncpg.Range``."""
    return Column("tstzrange", (Range,), name)


def daterange(name: str | None = None) -> Column[Range[_dt.date] | None]:
    """A ``date`` range. Values read and write as ``asyncpg.Range``."""
    return Column("daterange", (Range,), name)


def int4multirange(name: str | None = None) -> Column[list[Range[int]] | None]:
    """A set of disjoint ``int4range`` values, read and written as a list of ``Range``."""
    return Column("int4multirange", (list,), name)


def int8multirange(name: str | None = None) -> Column[list[Range[int]] | None]:
    """A set of disjoint ``int8range`` values, read and written as a list of ``Range``."""
    return Column("int8multirange", (list,), name)


def nummultirange(name: str | None = None) -> Column[list[Range[Decimal]] | None]:
    """A set of disjoint ``numrange`` values, read and written as a list of ``Range``."""
    return Column("nummultirange", (list,), name)


def tsmultirange(name: str | None = None) -> Column[list[Range[_dt.datetime]] | None]:
    """A set of disjoint ``tsrange`` values, read and written as a list of ``Range``."""
    return Column("tsmultirange", (list,), name)


def tstzmultirange(name: str | None = None) -> Column[list[Range[_dt.datetime]] | None]:
    """A set of disjoint ``tstzrange`` values, read and written as a list of ``Range``."""
    return Column("tstzmultirange", (list,), name)


def datemultirange(name: str | None = None) -> Column[list[Range[_dt.date]] | None]:
    """A set of disjoint ``daterange`` values, read and written as a list of ``Range``."""
    return Column("datemultirange", (list,), name)


def cidr(name: str | None = None) -> Column[IPAddress | None]:
    """An IPv4/IPv6 network, read and written as an ``ipaddress`` network object."""
    return Column("cidr", (ipaddress.IPv4Network, ipaddress.IPv6Network), name)


def macaddr(name: str | None = None) -> Column[str | None]:
    """A 6-byte MAC address (``08:00:2b:01:02:03``), read and written as text."""
    return Column("macaddr", (str,), name)


def macaddr8(name: str | None = None) -> Column[str | None]:
    """An 8-byte MAC address (``08:00:2b:01:02:03:04:05``), read and written as text."""
    return Column("macaddr8", (str,), name)


def bit(length: int | None = None, name: str | None = None) -> Column[BitString | None]:
    """A fixed-length bit string. ``bit()`` is ``bit(1)``; values read and write as
    ``asyncpg.BitString``."""
    return Column("bit" if length is None else f"bit({length})", (BitString,), name)


def varbit(length: int | None = None, name: str | None = None) -> Column[BitString | None]:
    """A variable-length bit string (``bit varying``). Values read and write as
    ``asyncpg.BitString``."""
    return Column("varbit" if length is None else f"varbit({length})", (BitString,), name)


def xml(name: str | None = None) -> Column[str | None]:
    """An XML document, read and written as text."""
    return Column("xml", (str,), name)


def money(name: str | None = None) -> Column[str | None]:
    """A currency amount (locale-formatted, e.g. ``$12.34``), read and written as text."""
    return Column("money", (str,), name)


def point(name: str | None = None) -> Column[Point | None]:
    """A geometric point. Values read and write as ``asyncpg.Point``."""
    return Column("point", (Point,), name)


def line(name: str | None = None) -> Column[Line | None]:
    """An infinite geometric line. Values read and write as ``asyncpg.Line``."""
    return Column("line", (Line,), name)


def lseg(name: str | None = None) -> Column[LineSegment | None]:
    """A finite line segment. Values read and write as ``asyncpg.LineSegment``."""
    return Column("lseg", (LineSegment,), name)


def box(name: str | None = None) -> Column[Box | None]:
    """A rectangular box. Values read and write as ``asyncpg.Box``."""
    return Column("box", (Box,), name)


def path(name: str | None = None) -> Column[Path | None]:
    """A geometric path. Values read and write as ``asyncpg.Path``."""
    return Column("path", (Path,), name)


def polygon(name: str | None = None) -> Column[Polygon | None]:
    """A closed geometric polygon. Values read and write as ``asyncpg.Polygon``."""
    return Column("polygon", (Polygon,), name)


def circle(name: str | None = None) -> Column[Circle | None]:
    """A geometric circle. Values read and write as ``asyncpg.Circle``."""
    return Column("circle", (Circle,), name)


def hstore(name: str | None = None) -> Column[str | None]:
    """An ``hstore`` key/value map (needs the ``hstore`` extension), read and written as
    text: ``'"a"=>"1"'``."""
    return Column("hstore", (str,), name)


def citext(name: str | None = None) -> Column[str | None]:
    """A case-insensitive string (needs the ``citext`` extension), read and written as text."""
    return Column("citext", (str,), name)


def ltree(name: str | None = None) -> Column[str | None]:
    """A label-tree path (needs the ``ltree`` extension), read and written as text."""
    return Column("ltree", (str,), name)


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


def tsvector(name: str | None = None) -> Column[str | None]:
    """Full-text search document, read and written as text (``'cat':1 'sat':2``).
    Usually generated: ``tsvector().generated_always_as(sql("to_tsvector('english', body)"))``."""
    return Column("tsvector", (str,), name)


def tsquery(name: str | None = None) -> Column[str | None]:
    """Full-text search query, read and written as text (``'cat' & 'sat'``)."""
    return Column("tsquery", (str,), name)


@runtime_checkable
class SupportsToList(Protocol):
    """A numpy array (or anything with ``.tolist()``): accepted as a vector value without
    mizzle depending on numpy."""

    def tolist(self) -> object: ...


def _vector_text(values: object) -> object:
    """``[1.0, 2.5]`` -> ``'[1,2.5]'``: pgvector's text form (asyncpg sends unknown types as
    text, so no codec or extension is needed on the connection)."""
    if isinstance(values, SupportsToList):
        values = values.tolist()
    try:
        items = [float(v) for v in cast(Iterable[float], values)]
    except (TypeError, ValueError) as exc:
        raise QueryError(f"vector values must be numbers; got {values!r}") from exc
    return "[" + ",".join(_number(v) for v in items) + "]"


def _number(value: float) -> str:
    return repr(value).removesuffix(".0")


def _vector_list(width: str) -> Callable[[object], object]:
    """Text -> floats. pgvector prints the shortest text that reads back as the same float4
    (``width`` "f") or float2 ("e"); as a Python float, round it to that width again."""

    def decode(text: object) -> object:
        body = str(text)[1:-1]
        values = [float(v) for v in body.split(",")] if body else []
        packing = f"{len(values)}{width}"
        return list(struct.unpack(packing, struct.pack(packing, *values)))

    return decode


_FLOAT4, _FLOAT2 = _vector_list("f"), _vector_list("e")
# What vector and halfvec columns accept: lists, tuples and numpy arrays.
_VECTOR_VALUES: tuple[type[object], ...] = (list, tuple, SupportsToList)


def vector(dimensions: int, name: str | None = None) -> Column[list[float] | None]:
    """A pgvector ``vector(n)`` (float4 values; needs the ``vector`` extension, which
    migrations create). Write a list, a tuple or a numpy array of floats; reads give lists."""
    return Column(
        f"vector({dimensions})", _VECTOR_VALUES, name, encoder=_vector_text, decoder=_FLOAT4
    )


def halfvec(dimensions: int, name: str | None = None) -> Column[list[float] | None]:
    """A pgvector ``halfvec(n)``: like ``vector`` with half-precision (float2) values."""
    return Column(
        f"halfvec({dimensions})", _VECTOR_VALUES, name, encoder=_vector_text, decoder=_FLOAT2
    )


def _sparse_text(dimensions: int) -> Callable[[object], object]:
    """``{0: 1.5, 4: 2.0}`` -> ``'{1:1.5,5:2}/6'``: pgvector's text form, whose indices
    start at 1 (mizzle's, like Python's and pgvector-python's, start at 0)."""

    def encode(value: object) -> object:
        items = cast(Mapping[object, object], value).items()
        parts: list[str] = []
        for index, number in sorted(items, key=lambda item: _sparse_index(item[0], dimensions)):
            if not isinstance(number, float | int) or isinstance(number, bool):
                raise QueryError(f"sparsevec values must be numbers; got {number!r}")
            parts.append(f"{cast(int, index) + 1}:{_number(float(number))}")
        return "{" + ",".join(parts) + f"}}/{dimensions}"

    return encode


def _sparse_index(index: object, dimensions: int) -> int:
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < dimensions:
        raise QueryError(f"sparsevec indices must be ints in 0..{dimensions - 1}; got {index!r}")
    return index


def _sparse_dict(text: object) -> object:
    """``'{1:1.5,5:2}/6'`` -> ``{0: 1.5, 4: 2.0}`` (values rounded to float4, like vector)."""
    body = str(text).rsplit("/", 1)[0][1:-1]
    pairs = [part.split(":") for part in body.split(",")] if body else []
    packing = f"{len(pairs)}f"
    values = cast(
        tuple[float, ...],
        struct.unpack(packing, struct.pack(packing, *(float(v) for _, v in pairs))),
    )
    return {int(i) - 1: v for (i, _), v in zip(pairs, values, strict=True)}


def sparsevec(dimensions: int, name: str | None = None) -> Column[dict[int, float] | None]:
    """A pgvector ``sparsevec(n)``: ``{index: value}`` with indices from 0 to n - 1 (float4
    values). Zeros are not stored, so ``{0: 0.0}`` reads back as ``{}``."""
    return Column(
        f"sparsevec({dimensions})",
        (dict,),
        name,
        encoder=_sparse_text(dimensions),
        decoder=_sparse_dict,
    )


type VectorOperand = (
    Expr[list[float]]
    | Expr[list[float] | None]
    | Expr[dict[int, float]]
    | Expr[dict[int, float] | None]
    | Sequence[float]
    | Mapping[int, float]
    | SupportsToList
)


def _distance(left: VectorOperand, op: str, right: VectorOperand) -> Expr[float]:
    def node(value: VectorOperand, other: VectorOperand) -> Node:
        if isinstance(value, Node):
            return value
        if isinstance(value, Mapping):
            # A sparse vector's text names its dimensions: take them from the column.
            dims = _SPARSE_DIMS.fullmatch(other.sql_type) if isinstance(other, ColumnBase) else None
            if dims is None:
                raise QueryError(
                    "a {index: value} operand needs a sparsevec column on the other side"
                )
            return Param(_sparse_text(int(dims.group(1)))(value))
        return Param(_vector_text(value))

    return TypedOperation[float](node(left, right), op, node(right, left))


_SPARSE_DIMS = re.compile(r"sparsevec\((\d+)\)")


def l2_distance(left: VectorOperand, right: VectorOperand) -> Expr[float]:
    """Euclidean distance, pgvector's ``<->``. Order by it (ascending) for nearest first."""
    return _distance(left, "<->", right)


def cosine_distance(left: VectorOperand, right: VectorOperand) -> Expr[float]:
    """1 - cosine similarity, pgvector's ``<=>``."""
    return _distance(left, "<=>", right)


def inner_product(left: VectorOperand, right: VectorOperand) -> Expr[float]:
    """The NEGATIVE inner product, pgvector's ``<#>`` (so ascending order is most similar
    first, and indexes apply). Negate it for the inner product itself."""
    return _distance(left, "<#>", right)


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
    column.enum_schema = item.enum_schema
    column.item = item
    return column


def _enum_value(value: object) -> object:
    # Only called with members of the column's enum. Enum.value is typed Any in typeshed.
    return cast(enum.Enum, value).value  # pyright: ignore[reportAny]


def pg_enum[E: enum.Enum](
    enum_cls: type[E],
    name: str | None = None,
    *,
    type_name: str | None = None,
    schema: str | None = None,
) -> Column[E | None]:
    """A PostgreSQL enum column. The DB type name defaults to the class name in snake_case;
    ``schema`` is the type's schema (default: the database's default schema)."""
    column: Column[E | None] = Column(
        type_name or snake_case(enum_cls.__name__),
        (enum_cls,),
        name,
        encoder=_enum_value,
        decoder=enum_cls,
    )
    column.enum = enum_cls
    column.enum_schema = schema
    return column


def pg_domain[T](
    type_name: str,
    py_type: type[T] | None = None,
    name: str | None = None,
    *,
    schema: str | None = None,
) -> Column[T | None]:
    """A column of a PostgreSQL domain. ``py_type`` is the Python type values decode to
    (``str`` for a text domain); declare the domain itself with ``mizzle.domain``. Give
    ``schema`` when the domain lives outside the database's default schema."""
    sql_type = type_name if schema in (None, "public") else f"{schema}.{type_name}"
    column: Column[T | None] = Column(
        sql_type, (py_type,) if py_type is not None else None, name, decoder=py_type
    )
    return column


def pg_composite[T](
    type_name: str,
    py_type: type[T] | None = None,
    name: str | None = None,
    *,
    schema: str | None = None,
) -> Column[T | None]:
    """A column of a PostgreSQL composite type; declare the type with ``mizzle.composite``.
    asyncpg reads a composite as an ``asyncpg.Record`` (fields by name) and writes a tuple of
    field values in order; pass ``py_type`` (``tuple`` or ``dict``, say) to decode to that
    instead. Give ``schema`` when the type lives outside the database's default schema."""
    sql_type = type_name if schema in (None, "public") else f"{schema}.{type_name}"
    column: Column[T | None] = Column(sql_type, (tuple,), name, decoder=py_type)
    return column

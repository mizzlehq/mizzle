"""PostgreSQL-only column types."""

from __future__ import annotations

import datetime as _dt
import enum
import ipaddress
import json as _jsonlib
import re
import struct
import uuid as _uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Never, Protocol, cast, overload, override, runtime_checkable

from asyncpg import BitString, Line, Point

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


# -- values nested in relational queries (C30) --------------------------------------
# JSON cannot carry these types: the compiler sends their text, and these turn it back
# into the value a plain select returns.


def _json[T](column: Column[T], decoder: Callable[[object], object]) -> Column[T]:
    column.json_decoder = decoder
    return column


_NUMBER = re.compile(r"[^\s(),<>\[\]{}]+")


def _numbers(value: object) -> list[float]:
    return [float(n) for n in cast("list[str]", _NUMBER.findall(str(value)))]


def _bits(value: object) -> BitString:
    text = str(value)
    return BitString.from_int(int(text, 2) if text else 0, length=len(text))


def cidr(name: str | None = None) -> Column[IPAddress | None]:
    """An IPv4/IPv6 network, read and written as an ``ipaddress`` network object."""
    return _json(
        Column("cidr", (ipaddress.IPv4Network, ipaddress.IPv6Network), name),
        lambda v: ipaddress.ip_network(str(v)),
    )


def macaddr(name: str | None = None) -> Column[str | None]:
    """A 6-byte MAC address (``08:00:2b:01:02:03``), read and written as text."""
    return Column("macaddr", (str,), name)


def macaddr8(name: str | None = None) -> Column[str | None]:
    """An 8-byte MAC address (``08:00:2b:01:02:03:04:05``), read and written as text."""
    return Column("macaddr8", (str,), name)


def bit(length: int | None = None, name: str | None = None) -> Column[BitString | None]:
    """A fixed-length bit string. ``bit()`` is ``bit(1)``; values read and write as
    ``asyncpg.BitString``."""
    return _json(Column(f"bit({length or 1})", (BitString,), name), _bits)


def varbit(length: int | None = None, name: str | None = None) -> Column[BitString | None]:
    """A variable-length bit string (``bit varying``). Values read and write as
    ``asyncpg.BitString``."""
    return _json(
        Column("varbit" if length is None else f"varbit({length})", (BitString,), name),
        _bits,
    )


def point(name: str | None = None) -> Column[Point | None]:
    """A geometric point. Values read and write as ``asyncpg.Point``."""
    return _json(Column("point", (Point,), name), lambda v: Point(*_numbers(v)))


def line(name: str | None = None) -> Column[Line | None]:
    """An infinite geometric line. Values read and write as ``asyncpg.Line``."""
    return _json(Column("line", (Line,), name), lambda v: Line(*_numbers(v)))


def inet(name: str | None = None) -> Column[IPAddress | None]:
    return Column("inet", _IP_TYPES, name)


class JsonText(str):
    """A JSON document already serialized: the driver's json/jsonb codec sends it as is."""

    __slots__: tuple[str, ...] = ()


def _json_documents(values: object) -> object:
    # A str is one array element to asyncpg; a list would be another array dimension (Q4-5).
    return [
        None if v is None else JsonText(_jsonlib.dumps(v)) for v in cast(Iterable[object], values)
    ]


class _JsonColumn[T](Column[T | None]):
    @override
    def array_column(self) -> ColumnBase:
        column = super().array_column()
        column.encoder = _json_documents
        return column


class jsonb[T](_JsonColumn[T]):
    """``jsonb[dict[str, int]]()``. ``T`` is a static hint only; it is not checked at runtime."""

    def __init__(self, name: str | None = None) -> None:
        super().__init__("jsonb", None, name)


class json[T](_JsonColumn[T]):
    """``json[list[str]]()``. ``T`` is a static hint only; it is not checked at runtime."""

    def __init__(self, name: str | None = None) -> None:
        super().__init__("json", None, name)


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
    """A pgvector ``vector(n)`` (float4 values; needs the ``vector`` extension: create it
    in a custom migration). Write a list, a tuple or a numpy array of floats; reads give
    lists."""
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
    column = cast(Column[list[T] | None], item.array_column())
    column.name = name
    return column


def _enum_encoder(enum_cls: type[enum.Enum]) -> Callable[[object], object]:
    """Encode a member as its label, ``str(member.value)`` (the label the snapshot creates).
    An IntEnum's value is an int, which a text enum parameter refused (B49). Anything else
    (a plain ``"admin"``) is a ``QueryError``, not a raw ``AttributeError`` from deep in the
    compiler (B15)."""

    def encode(value: object) -> object:
        if not isinstance(value, enum_cls):
            raise QueryError(
                f"{enum_cls.__name__} column expects a {enum_cls.__name__} member, got "
                f"{type(value).__name__}"
            )
        return str(cast(object, value.value))

    return encode


def _enum_decoder(enum_cls: type[enum.Enum]) -> Callable[[object], object]:
    """A label back to its member, also for an IntEnum, whose labels are ``"1"``, ``"2"``."""
    by_label: dict[str, enum.Enum] = {str(cast(object, m.value)): m for m in enum_cls}

    def decode(value: object) -> object:
        member = by_label.get(value) if isinstance(value, str) else None
        return member if member is not None else enum_cls(value)

    return decode


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
        encoder=_enum_encoder(enum_cls),
        decoder=_enum_decoder(enum_cls),
    )
    column.enum = enum_cls
    column.enum_schema = schema
    return column


class CustomColumn[T](Column[T]):
    """See ``custom``. A relational query nests its values as their JSON form."""


def _parse_json(value: object) -> object:
    # Nested as JSON text (to_json(col)::text), parsed here so numbers keep every digit:
    # a numeric domain's 1.10 is Decimal("1.10"), not a float (R8-1, Q9-2).
    return cast(object, _jsonlib.loads(cast(str, value), parse_float=Decimal))


@overload
def custom(sql_type: str, name: str | None = None) -> CustomColumn[object]: ...
# The value type: decode's return type, else encode's parameter type, else py_type (Q8-6).
# With decode, py_type does not take part: pyright would widen a disagreeing py_type and
# decode to a union (str | int); py_type must name decode's return type.
@overload
def custom[T, D](
    sql_type: str,
    name: str | None = None,
    *,
    py_type: type[object] | None = None,
    decode: Callable[[D], T],
    encode: Callable[[T], D] | None = None,
) -> CustomColumn[T | None]: ...
@overload
def custom[T](
    sql_type: str,
    name: str | None = None,
    *,
    py_type: type[T] | None = None,
    encode: Callable[[T], object],
) -> CustomColumn[T | None]: ...
@overload
def custom[T](
    sql_type: str, name: str | None = None, *, py_type: type[T]
) -> CustomColumn[T | None]: ...
def custom(
    sql_type: str,
    name: str | None = None,
    *,
    py_type: type[object] | None = None,
    decode: Callable[[Never], object] | None = None,
    encode: Callable[[Never], object] | None = None,
) -> ColumnBase:
    """A column of a type mizzle has no column type for, like Drizzle's ``customType``:
    ``sql_type`` is its DDL type, verbatim (``"citext"``, ``"int4range"``, ``"my_domain"``,
    ``"app.my_type"``). Values are the driver's (asyncpg's) for that type, both ways: a
    ``str`` for citext or tsvector, a ``Range`` for int4range, a ``Record`` for a composite.
    ``decode`` turns a value read into ``py_type``, ``encode`` one written into what the
    driver takes. A relational query nests it as its JSON form (``decode`` then gets that:
    a ``bool``, an ``int``, a ``Decimal`` for other numbers, a ``str``, a ``dict`` for a
    composite...).
    """
    column = CustomColumn[object](
        sql_type,
        (py_type or object,),
        name,
        encoder=cast(Callable[[object], object] | None, encode),
        decoder=cast(Callable[[object], object] | None, decode),
    )
    column.json_decoder = _parse_json
    return column

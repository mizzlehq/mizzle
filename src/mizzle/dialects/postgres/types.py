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
from typing import Protocol, Self, cast, override, runtime_checkable

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

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.naming import snake_case
from mizzle.core.query.base import Runner
from mizzle.core.schema import types as _core
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
# JSON cannot carry these types: the compiler sends a range as its parts and the others as
# their text, and these turn that back into the value a plain select returns.


def _json[T](column: Column[T], decoder: Callable[[object], object]) -> Column[T]:
    column.json_decoder = decoder
    return column


def _moment[D: _dt.date](kind: type[D]) -> Callable[[str], D]:
    def parse(text: str) -> D:
        if text in ("infinity", "-infinity"):  # as asyncpg reads them
            return cast(D, kind.max if text == "infinity" else kind.min)  # max is typed date
        return kind.fromisoformat(text)

    return parse


def _range_json[V: (int, Decimal, _dt.date)](
    bound: Callable[[str], V],
) -> Callable[[object], object]:
    def decode(value: object) -> object:
        lower, upper, lower_inc, upper_inc, empty = cast("list[object]", value)
        if empty:
            return Range[V](empty=True)
        return Range[V](
            None if lower is None else bound(str(lower)),
            None if upper is None else bound(str(upper)),
            lower_inc=bool(lower_inc),
            upper_inc=bool(upper_inc),
        )

    return decode


def _multirange_json[V: (int, Decimal, _dt.date)](
    bound: Callable[[str], V],
) -> Callable[[object], object]:
    one = _range_json(bound)
    return lambda value: [one(v) for v in cast("list[object]", value)]


_NUMBER = re.compile(r"[^\s(),<>\[\]{}]+")


def _numbers(value: object) -> list[float]:
    return [float(n) for n in cast("list[str]", _NUMBER.findall(str(value)))]


def _points(value: object) -> list[tuple[float, float]]:
    n = _numbers(value)
    return [(n[i], n[i + 1]) for i in range(0, len(n), 2)]


def _bits(value: object) -> BitString:
    text = str(value)
    return BitString.from_int(int(text, 2) if text else 0, length=len(text))


def _circle(value: object) -> Circle:
    x, y, radius = _numbers(value)
    return Circle(Point(x, y), radius)


def int4range(name: str | None = None) -> Column[Range[int] | None]:
    """A discrete ``integer`` range, ``[1,5)``. Values read and write as ``asyncpg.Range``."""
    return _json(Column("int4range", (Range,), name), _range_json(int))


def int8range(name: str | None = None) -> Column[Range[int] | None]:
    """A discrete ``bigint`` range. Values read and write as ``asyncpg.Range``."""
    return _json(Column("int8range", (Range,), name), _range_json(int))


def numrange(name: str | None = None) -> Column[Range[Decimal] | None]:
    """A continuous ``numeric`` range. Values read and write as ``asyncpg.Range``."""
    return _json(Column("numrange", (Range,), name), _range_json(Decimal))


def tsrange(name: str | None = None) -> Column[Range[_dt.datetime] | None]:
    """A ``timestamp without time zone`` range. Values read and write as ``asyncpg.Range``."""
    return _json(Column("tsrange", (Range,), name), _range_json(_moment(_dt.datetime)))


def tstzrange(name: str | None = None) -> Column[Range[_dt.datetime] | None]:
    """A ``timestamp with time zone`` range. Values read and write as ``asyncpg.Range``."""
    return _json(Column("tstzrange", (Range,), name), _range_json(_moment(_dt.datetime)))


def daterange(name: str | None = None) -> Column[Range[_dt.date] | None]:
    """A ``date`` range. Values read and write as ``asyncpg.Range``."""
    return _json(Column("daterange", (Range,), name), _range_json(_moment(_dt.date)))


def int4multirange(name: str | None = None) -> Column[list[Range[int]] | None]:
    """A set of disjoint ``int4range`` values, read and written as a list of ``Range``."""
    return _json(Column("int4multirange", (list,), name), _multirange_json(int))


def int8multirange(name: str | None = None) -> Column[list[Range[int]] | None]:
    """A set of disjoint ``int8range`` values, read and written as a list of ``Range``."""
    return _json(Column("int8multirange", (list,), name), _multirange_json(int))


def nummultirange(name: str | None = None) -> Column[list[Range[Decimal]] | None]:
    """A set of disjoint ``numrange`` values, read and written as a list of ``Range``."""
    return _json(Column("nummultirange", (list,), name), _multirange_json(Decimal))


def tsmultirange(name: str | None = None) -> Column[list[Range[_dt.datetime]] | None]:
    """A set of disjoint ``tsrange`` values, read and written as a list of ``Range``."""
    return _json(Column("tsmultirange", (list,), name), _multirange_json(_moment(_dt.datetime)))


def tstzmultirange(name: str | None = None) -> Column[list[Range[_dt.datetime]] | None]:
    """A set of disjoint ``tstzrange`` values, read and written as a list of ``Range``."""
    return _json(Column("tstzmultirange", (list,), name), _multirange_json(_moment(_dt.datetime)))


def datemultirange(name: str | None = None) -> Column[list[Range[_dt.date]] | None]:
    """A set of disjoint ``daterange`` values, read and written as a list of ``Range``."""
    return _json(Column("datemultirange", (list,), name), _multirange_json(_moment(_dt.date)))


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


def xml(name: str | None = None) -> Column[str | None]:
    """An XML document, read and written as text."""
    return Column("xml", (str,), name)


def money(name: str | None = None) -> Column[str | None]:
    """A currency amount (locale-formatted, e.g. ``$12.34``), read and written as text."""
    return Column("money", (str,), name)


def point(name: str | None = None) -> Column[Point | None]:
    """A geometric point. Values read and write as ``asyncpg.Point``."""
    return _json(Column("point", (Point,), name), lambda v: Point(*_numbers(v)))


def line(name: str | None = None) -> Column[Line | None]:
    """An infinite geometric line. Values read and write as ``asyncpg.Line``."""
    return _json(Column("line", (Line,), name), lambda v: Line(*_numbers(v)))


def lseg(name: str | None = None) -> Column[LineSegment | None]:
    """A finite line segment. Values read and write as ``asyncpg.LineSegment``."""
    return _json(Column("lseg", (LineSegment,), name), lambda v: LineSegment(*_points(v)))


def box(name: str | None = None) -> Column[Box | None]:
    """A rectangular box. Values read and write as ``asyncpg.Box``."""
    return _json(Column("box", (Box,), name), lambda v: Box(*_points(v)))


def path(name: str | None = None) -> Column[Path | None]:
    """A geometric path. Values read and write as ``asyncpg.Path``."""
    return _json(
        Column("path", (Path,), name),
        lambda v: Path(*_points(v), is_closed=str(v).startswith("(")),
    )


def polygon(name: str | None = None) -> Column[Polygon | None]:
    """A closed geometric polygon. Values read and write as ``asyncpg.Polygon``."""
    return _json(Column("polygon", (Polygon,), name), lambda v: Polygon(*_points(v)))


def circle(name: str | None = None) -> Column[Circle | None]:
    """A geometric circle. Values read and write as ``asyncpg.Circle``."""
    return _json(Column("circle", (Circle,), name), _circle)


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
    decoder: Callable[[object], object] | None = None
    if py_type is not None:
        convert = cast(Callable[[object], object], py_type)
        # asyncpg already reads a date/UUID/... domain as that type; date(date) fails (Q1).
        decoder = lambda v: v if isinstance(v, py_type) else convert(v)  # noqa: E731
    column: Column[T | None] = Column(
        sql_type, (py_type,) if py_type is not None else None, name, decoder=decoder
    )
    column.json_load = lambda runner: _json_type(runner, sql_type)  # its base type (Q4-6)
    return column


def _composite_json(py_type: type | None) -> Callable[[object], object]:
    """A composite nested in a relational row arrives as a dict of its fields (Q2, Q4-6):
    keep it by name, like asyncpg's Record, unless ``py_type`` wants positional values."""

    def parse(value: object) -> object:
        fields = cast(dict[str, object], value)
        if py_type is None or issubclass(py_type, Mapping):
            return fields
        return tuple(fields.values())

    return parse


def pg_composite[T](
    type_name: str,
    py_type: type[T] | None = None,
    name: str | None = None,
    *,
    schema: str | None = None,
) -> Column[T | None]:
    """A column of a PostgreSQL composite type; declare the type with ``mizzle.composite``.
    asyncpg reads a composite as an ``asyncpg.Record`` (fields by name) and writes a tuple of
    field values in order or a dict by field name; pass ``py_type`` (``tuple`` or ``dict``,
    say) to decode to that instead. Give ``schema`` when the type lives outside the
    database's default schema. In a relational (nested) row it is a dict by field name."""
    sql_type = type_name if schema in (None, "public") else f"{schema}.{type_name}"
    # dict too: the form py_type=dict reads back must be writable (Q9).
    column: Column[T | None] = Column(sql_type, (tuple, dict), name, decoder=py_type)
    column.json_load = lambda runner: _json_type(runner, sql_type)  # its field types (Q4-6)
    return _json(column, _composite_json(py_type))


# -- domains and composites nested in relational queries (Q4-6) ------------------------
# JSON carries a numeric as a float, a timestamp as a string...: a domain is nested like
# its base type and a composite field by field, both looked up in the database once.

_JSON_BASES: dict[str, Callable[[], ColumnBase]] = {
    "numeric": _core.numeric,
    "real": _core.real,
    "double precision": _core.double_precision,
    "date": _core.date,
    "timestamp without time zone": _core.timestamp,
    "timestamp with time zone": lambda: _core.timestamp(tz=True),
    "time without time zone": time,
    "time with time zone": lambda: time(tz=True),
    "interval": interval,
    "uuid": uuid,
    "bytea": bytea,
    "inet": inet,
    "cidr": cidr,
    "bit": bit,
    "bit varying": varbit,
    "point": point,
    "line": line,
    "lseg": lseg,
    "box": box,
    "path": path,
    "polygon": polygon,
    "circle": circle,
    "hstore": hstore,
    "int4range": int4range,
    "int8range": int8range,
    "numrange": numrange,
    "tsrange": tsrange,
    "tstzrange": tstzrange,
    "daterange": daterange,
    "int4multirange": int4multirange,
    "int8multirange": int8multirange,
    "nummultirange": nummultirange,
    "tsmultirange": tsmultirange,
    "tstzmultirange": tstzmultirange,
    "datemultirange": datemultirange,
}

_FIELDS = (
    "FROM pg_attribute a WHERE a.attrelid = t.typrelid AND a.attnum > 0"
    " AND NOT a.attisdropped ORDER BY a.attnum"
)
_LOOKUP = (
    "SELECT t.typtype::text, format_type(t.typbasetype, t.typtypmod),"
    f" ARRAY(SELECT a.attname::text {_FIELDS}),"
    f" ARRAY(SELECT format_type(a.atttypid, a.atttypmod) {_FIELDS}),"
    " (SELECT format_type(r.rngsubtype, NULL) FROM pg_range r"
    " WHERE t.oid IN (r.rngtypid, r.rngmultitypid))"
    " FROM pg_type t WHERE t.oid = to_regtype($1)"
)


class _Bound(Protocol):  # what asyncpg's Range takes: any subtype's value
    @override
    def __eq__(self, other: object, /) -> bool: ...
    def __lt__(self, other: Self, /) -> bool: ...
    def __gt__(self, other: Self, /) -> bool: ...


def _range_of_parts(value: object) -> object:
    """A user-defined range from its decoded [lower, upper, lower_inc, upper_inc, empty]."""
    lower, upper, lower_inc, upper_inc, empty = cast("list[_Bound | None]", value)
    if empty:
        return Range[_Bound](empty=True)
    return Range(lower, upper, lower_inc=bool(lower_inc), upper_inc=bool(upper_inc))


async def _json_type(runner: Runner, type_text: str) -> ColumnBase:
    """A column that nests and reads like ``type_text`` (as ``format_type`` prints it).
    Looked up once per database: the same name can be another type in another (Q4-17)."""
    if type_text.endswith("[]"):
        return (await _json_type(runner, type_text[:-2])).array_column()
    name = re.sub(r"\(.*?\)", "", type_text)  # numeric(10,2), timestamp(3) with time zone
    if (base := _JSON_BASES.get(name)) is not None:
        return base()
    if (known := runner.json_types.get(name)) is not None:
        return known
    rows = await runner.fetch(CompiledQuery(_LOOKUP, (name,), (None,)), read_only=True)
    column: ColumnBase = Column[object](name, None)  # anything else: its JSON is already its value
    if rows:
        kind, base_type, names, types, subtype = cast(
            tuple[str, str, list[str], list[str], str | None], tuple(rows[0])
        )
        if kind == "d":
            column = await _json_type(runner, base_type)
        elif kind == "c":
            fields = [(n, await _json_type(runner, t)) for n, t in zip(names, types, strict=True)]
            column.json_fields = tuple(fields)
        elif subtype is not None:  # a user-defined range or multirange (Q4-16, Q4-20)
            column = _json(Column[object](name, None), _range_of_parts)
            column.json_range = await _json_type(runner, subtype)
            if kind == "m":
                column = column.array_column()  # nests like an array of its ranges
    runner.json_types[name] = column
    return column

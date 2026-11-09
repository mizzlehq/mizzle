"""Pydantic models from tables (like drizzle-zod). Needs ``pip install "mizzle[pydantic]"``.

    UserOut = select_model(Users)                              # every column, as stored
    SignUp = insert_model(Users, include=["email", "name"])    # what a client may send
    UserPatch = update_model(Users, include=["name", "bio"])

    @app.post("/users", response_model=UserOut)
    async def create(body: SignUp):    # FastAPI validates the request
        [user] = await db.insert(Users).values(body).returning()
        return user                    # Users rows validate into UserOut directly

Security: a model accepts every field it contains, so a request can set any of them.
For models built from client input, list the allowed fields with ``include=`` (an
allowlist). Otherwise a client could, for example, send ``"role": "admin"``. Insert and
update models never contain database-generated columns (serial and identity ids,
generated columns), and update models never contain primary keys, unless you ``include``
them explicitly.

Models only carry the fields a client sent: ``values(model)`` / ``set(model)`` take the
fields set on the model (``model_dump(exclude_unset=True)``, with each value as the model
holds it), so omitted fields keep their database defaults.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal, TypeAliasType, cast

from asyncpg import BitString, Line, Point

try:
    import pydantic
    from pydantic_core import to_jsonable_python
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError('mizzle.pydantic needs pydantic: pip install "mizzle[pydantic]"') from exc

from mizzle.core.errors import SchemaError
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table
from mizzle.dialects.postgres.types import CustomColumn

type Mode = Literal["select", "insert", "update"]

# An array value: items, NULLs and, in a multi-dimensional array, lists of them (Q6-5).
type _Array[T] = list[T | _Array[T] | None]

_VARCHAR = re.compile(r"(?:varchar|character varying)\((\d+)\)")
_NUMERIC = re.compile(r"numeric\((\d+)(?:,\s*(\d+))?\)")
_VECTOR = re.compile(r"(?:vector|halfvec)\((\d+)\)")


def _nan_only(value: Decimal) -> Decimal:
    if not value.is_nan():
        raise ValueError("expected NaN")
    return value


_NAN = Annotated[Decimal, pydantic.Field(allow_inf_nan=True), pydantic.AfterValidator(_nan_only)]


def column_type(column: ColumnBase) -> object:
    """The Python type a column holds (without ``None``), with length/precision limits."""
    if column.item is not None:
        # Any PostgreSQL array can hold NULL (Q5-7), and have more dimensions (Q6-5).
        array: TypeAliasType = _Array
        return array[column_type(column.item)]
    if column.enum is not None:
        return column.enum
    if column.accepts is None:  # json / jsonb
        return Any
    if m := _VECTOR.fullmatch(column.sql_type):
        size = int(m.group(1))
        return Annotated[list[float], pydantic.Field(min_length=size, max_length=size)]
    if column.sql_type.startswith("sparsevec("):
        return dict[int, float]
    base = column.accepts[0]
    if base is str and (m := _VARCHAR.fullmatch(column.sql_type)):
        return Annotated[str, pydantic.Field(max_length=int(m.group(1)))]
    if base is Decimal and (m := _NUMERIC.fullmatch(column.sql_type)):
        digits, places = int(m.group(1)), int(m.group(2) or 0)
        fits = Annotated[Decimal, pydantic.Field(max_digits=digits, decimal_places=places)]
        return fits | _NAN  # NaN fits any numeric; Infinity only an unbounded one (Q4-13)
    if base is Decimal:
        return Annotated[Decimal, pydantic.Field(allow_inf_nan=True)]  # NaN, Infinity (Q4-13)
    if base is float:
        return float
    kinds = tuple(_pydantic_type(kind, column.json_decoder) for kind in column.accepts)
    # inet holds addresses, interfaces and networks of both IP versions (C35).
    union = kinds[0]
    for kind in kinds[1:]:
        union = cast(type[object], union) | cast(type[object], kind)
    return union


def _json_form(value: object) -> object:
    """A value as JSON, in the form its column's json_decoder reads back: the form a
    relational query nests it as (Q14). Lists cover arrays (Q4-9)."""
    match value:
        case BitString():
            return value.as_string().replace(" ", "")  # spaced every 4 bits (Q4-10)
        case list() | tuple():  # Point and Line are tuples
            return [_json_form(v) for v in cast(Sequence[object], value)]
        case _:
            return cast(object, to_jsonable_python(value))


# The JSON type each special value dumps to, which its field parses back (Q14, Q4-13).
_JSON_FORMS: dict[type, type] = {
    BitString: str,
    Point: list,
    Line: list,
}


def _pydantic_type(kind: type, parse: Callable[[object], object] | None) -> object:
    """``kind``, or an isinstance check when pydantic has no schema for it (asyncpg's
    Point, Line, BitString): such fields accept those objects as they are, and also
    their JSON form (``_json_form``) when the column can parse it."""
    try:
        pydantic.TypeAdapter(kind)
    except pydantic.PydanticSchemaGenerationError:
        form = _JSON_FORMS.get(kind) if parse is not None else None

        def check(value: object) -> object:
            if isinstance(value, kind):
                return value
            if form is not None and parse is not None and isinstance(value, form):
                try:
                    return parse(value)
                except (ValueError, TypeError, IndexError) as exc:
                    raise ValueError(f"invalid {kind.__qualname__}: {exc}") from exc
            raise ValueError(f"expected {kind.__module__}.{kind.__qualname__}")

        dump = pydantic.PlainSerializer(_json_form, when_used="json")
        return Annotated[object, pydantic.PlainValidator(check), dump]
    return kind


def _field(column: ColumnBase, mode: Mode, override: object) -> tuple[object, object]:
    item = column
    while item.item is not None:
        item = item.item
    untyped = isinstance(item, CustomColumn) and item.accepts == (object,)
    if untyped and mode == "select" and override is None:
        # Its values (asyncpg's Range, Box, Record...) may have no JSON form (Q8-4).
        raise SchemaError(
            f"{column.qualified_name}: select_model needs the Python type of a custom() "
            "column: give it py_type= (and decode=/encode= to convert the driver's "
            "values), or a field type with refine="
        )
    annotation = override if override is not None else column_type(column)
    if column.nullable:
        # Type forms (Annotated[...], list[...], Any, ...) support ``| None`` at runtime;
        # the cast only tells the checkers so.
        return cast(type[object], annotation) | None, (... if mode == "select" else None)
    if mode == "select":
        return annotation, ...
    if mode == "update":
        return annotation, None
    # insert: generated/defaulted columns may be left out; required ones must be given
    if column.required_on_insert:
        return annotation, ...
    return annotation, None


def _model(
    table: type[Table],
    mode: Mode,
    *,
    name: str | None,
    include: Sequence[str] | None,
    exclude: Sequence[str],
    refine: Mapping[str, object] | None,
) -> type[pydantic.BaseModel]:
    columns = table.__table__.columns
    refine = refine or {}
    named = [*(include or ()), *exclude, *refine]
    if unknown := [c for c in named if c not in columns]:
        raise SchemaError(f"{table.__name__} has no column(s) {unknown}")

    def wanted(attr: str, column: ColumnBase) -> bool:
        if attr in exclude:
            return False
        if include is not None:
            return attr in include
        if mode != "select" and column.generated:
            return False  # e.g. serial ids: assigned by the database, not by clients
        return not (mode == "update" and column.is_primary_key)

    fields: dict[str, tuple[object, object]] = {
        attr: _field(column, mode, refine.get(attr))
        for attr, column in columns.items()
        if wanted(attr, column)
    }
    suffix = {"select": "", "insert": "Insert", "update": "Update"}[mode]
    return pydantic.create_model(
        name or f"{table.__name__}{suffix}Model",
        # JSON that reads back: inf/NaN floats and non-UTF-8 bytes did not (Q4-13)
        __config__=pydantic.ConfigDict(
            from_attributes=True,
            extra="forbid",
            ser_json_inf_nan="strings",
            ser_json_bytes="base64",
            val_json_bytes="base64",
        ),
        # **kwargs are also checked against create_model's named options (__doc__, ...),
        # so only Any values fit; the definitions are (type form, default) pairs.
        **cast(dict[str, Any], fields),  # pyright: ignore[reportAny]
    )


def select_model(
    table: type[Table],
    *,
    name: str | None = None,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] = (),
    refine: Mapping[str, object] | None = None,
) -> type[pydantic.BaseModel]:
    """A model of a stored row. Validates table rows directly (``from_attributes``).
    Use ``exclude`` to keep secrets such as password hashes out of API responses."""
    return _model(table, "select", name=name, include=include, exclude=exclude, refine=refine)


def insert_model(
    table: type[Table],
    *,
    name: str | None = None,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] = (),
    refine: Mapping[str, object] | None = None,
) -> type[pydantic.BaseModel]:
    """A model for new rows. For client input, pass ``include=[...]`` (an allowlist).
    ``refine={"email": EmailStr}`` replaces a field's type."""
    return _model(table, "insert", name=name, include=include, exclude=exclude, refine=refine)


def update_model(
    table: type[Table],
    *,
    name: str | None = None,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] = (),
    refine: Mapping[str, object] | None = None,
) -> type[pydantic.BaseModel]:
    """A model for partial updates: every field optional, primary keys left out.
    For client input, pass ``include=[...]`` (an allowlist)."""
    return _model(table, "update", name=name, include=include, exclude=exclude, refine=refine)


__all__ = ["column_type", "insert_model", "select_model", "update_model"]

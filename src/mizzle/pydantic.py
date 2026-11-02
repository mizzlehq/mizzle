"""Pydantic models from tables (like drizzle-zod). Needs ``pip install "mizzle[pydantic]"``.

    UserOut = select_model(Users)     # every column, as stored
    UserIn = insert_model(Users)      # required only where the database needs a value
    UserPatch = update_model(Users)   # every column optional

    @app.post("/users", response_model=UserOut)
    async def create(body: UserIn):   # FastAPI validates the request with UserIn
        [user] = await db.insert(Users).values(body).returning()
        return user                   # Users rows validate into UserOut directly

Models only contain the fields a client sent: ``values(model)`` / ``set(model)`` use
``model_dump(exclude_unset=True)``, so omitted fields keep their database defaults.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal

try:
    import pydantic
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError('mizzle.pydantic needs pydantic: pip install "mizzle[pydantic]"') from exc

from mizzle.core.errors import SchemaError
from mizzle.core.schema.column import Column
from mizzle.core.schema.table import Table

type Mode = Literal["select", "insert", "update"]

_VARCHAR = re.compile(r"(?:varchar|character varying)\((\d+)\)")
_NUMERIC = re.compile(r"numeric\((\d+)(?:,\s*(\d+))?\)")


def column_type(column: Column[Any]) -> Any:
    """The Python type a column holds (without ``None``), with length/precision limits."""
    if column.item is not None:
        return list[column_type(column.item)]  # type: ignore[misc]
    if column.enum is not None:
        return column.enum
    if column.accepts is None:  # json / jsonb
        return Any
    base = column.accepts[0]
    if base is str and (m := _VARCHAR.fullmatch(column.sql_type)):
        return Annotated[str, pydantic.Field(max_length=int(m.group(1)))]
    if base is Decimal and (m := _NUMERIC.fullmatch(column.sql_type)):
        digits, places = int(m.group(1)), int(m.group(2) or 0)
        return Annotated[Decimal, pydantic.Field(max_digits=digits, decimal_places=places)]
    if base is float:
        return float
    return base


def _field(column: Column[Any], mode: Mode, override: Any) -> tuple[Any, Any]:
    annotation = override if override is not None else column_type(column)
    if mode == "select":
        return (annotation | None if column.nullable else annotation), ...
    if mode == "update":
        return (annotation | None if column.nullable else annotation), None
    # insert: generated/defaulted columns may be left out; required ones must be given
    if column.nullable:
        return annotation | None, None
    if column.required_on_insert:
        return annotation, ...
    return annotation, None


def _model(
    table: type[Table],
    mode: Mode,
    *,
    name: str | None,
    exclude: Sequence[str],
    refine: Mapping[str, Any] | None,
) -> type[pydantic.BaseModel]:
    columns = table.__table__.columns
    refine = refine or {}
    if unknown := [c for c in [*exclude, *refine] if c not in columns]:
        raise SchemaError(f"{table.__name__} has no column(s) {unknown}")
    fields: dict[str, Any] = {
        attr: _field(column, mode, refine.get(attr))
        for attr, column in columns.items()
        if attr not in exclude
    }
    suffix = {"select": "", "insert": "Insert", "update": "Update"}[mode]
    return pydantic.create_model(
        name or f"{table.__name__}{suffix}Model",
        __config__=pydantic.ConfigDict(from_attributes=True, extra="forbid"),
        **fields,
    )


def select_model(
    table: type[Table],
    *,
    name: str | None = None,
    exclude: Sequence[str] = (),
    refine: Mapping[str, Any] | None = None,
) -> type[pydantic.BaseModel]:
    """A model of a stored row. Validates table rows directly (``from_attributes``)."""
    return _model(table, "select", name=name, exclude=exclude, refine=refine)


def insert_model(
    table: type[Table],
    *,
    name: str | None = None,
    exclude: Sequence[str] = (),
    refine: Mapping[str, Any] | None = None,
) -> type[pydantic.BaseModel]:
    """A model for new rows. ``refine={"email": EmailStr}`` replaces a field's type."""
    return _model(table, "insert", name=name, exclude=exclude, refine=refine)


def update_model(
    table: type[Table],
    *,
    name: str | None = None,
    exclude: Sequence[str] = (),
    refine: Mapping[str, Any] | None = None,
) -> type[pydantic.BaseModel]:
    """A model for partial updates: every field optional."""
    return _model(table, "update", name=name, exclude=exclude, refine=refine)


__all__ = ["column_type", "insert_model", "select_model", "update_model"]

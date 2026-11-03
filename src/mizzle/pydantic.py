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

Models only carry the fields a client sent: ``values(model)`` / ``set(model)`` use
``model_dump(exclude_unset=True)``, so omitted fields keep their database defaults.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import GenericAlias
from typing import Annotated, Any, Literal, cast

try:
    import pydantic
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError('mizzle.pydantic needs pydantic: pip install "mizzle[pydantic]"') from exc

from mizzle.core.errors import SchemaError
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table

type Mode = Literal["select", "insert", "update"]

_VARCHAR = re.compile(r"(?:varchar|character varying)\((\d+)\)")
_NUMERIC = re.compile(r"numeric\((\d+)(?:,\s*(\d+))?\)")
_VECTOR = re.compile(r"(?:vector|halfvec)\((\d+)\)")


def column_type(column: ColumnBase) -> object:
    """The Python type a column holds (without ``None``), with length/precision limits."""
    if column.item is not None:
        return GenericAlias(list, (column_type(column.item),))  # list[item type]
    if column.enum is not None:
        return column.enum
    if column.accepts is None:  # json / jsonb
        return Any
    if m := _VECTOR.fullmatch(column.sql_type):
        size = int(m.group(1))
        return Annotated[list[float], pydantic.Field(min_length=size, max_length=size)]
    base = column.accepts[0]
    if base is str and (m := _VARCHAR.fullmatch(column.sql_type)):
        return Annotated[str, pydantic.Field(max_length=int(m.group(1)))]
    if base is Decimal and (m := _NUMERIC.fullmatch(column.sql_type)):
        digits, places = int(m.group(1)), int(m.group(2) or 0)
        return Annotated[Decimal, pydantic.Field(max_digits=digits, decimal_places=places)]
    if base is float:
        return float
    return base


def _field(column: ColumnBase, mode: Mode, override: object) -> tuple[object, object]:
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
        __config__=pydantic.ConfigDict(from_attributes=True, extra="forbid"),
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

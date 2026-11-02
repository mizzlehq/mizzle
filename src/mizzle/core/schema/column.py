"""Column: a typed table column. It is both a schema declaration and an SQL expression."""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Self, overload, override

from mizzle.core.errors import QueryError
from mizzle.core.naming import Casing, snake_case
from mizzle.core.sql.nodes import Condition, Expr, ExprBase, Node

if TYPE_CHECKING:
    from mizzle.core.schema.table import Table

type ReferentialAction = Literal["cascade", "restrict", "no action", "set null", "set default"]


class _Missing:
    """Sentinel for "no value given" (``None`` is a real value: SQL NULL)."""

    @override
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Final = _Missing()


@dataclass(frozen=True)
class Reference:
    target: Callable[[], ColumnBase]
    on_delete: ReferentialAction | None = None
    on_update: ReferentialAction | None = None


class ColumnBase(ExprBase):
    """Everything about a column except its value type. Internal code uses this non-generic
    base for columns of mixed types; user code sees ``Column[T]``."""

    sql_type: str
    accepts: tuple[type[object], ...] | None
    name: str | None
    nullable: bool
    generated: bool
    encoder: Callable[[object], object] | None
    decoder: Callable[[object], object] | None
    attr: str
    table: type[Table] | None
    enum: type[enum.Enum] | None
    item: ColumnBase | None
    is_primary_key: bool
    is_unique: bool
    unique_name: str | None
    default_value: object
    default_factory: Callable[[], object] | None
    reference: Reference | None
    checks: list[Condition]

    def __init__(
        self,
        sql_type: str,
        accepts: tuple[type[object], ...] | None,
        name: str | None = None,
        *,
        nullable: bool = True,
        generated: bool = False,
        encoder: Callable[[object], object] | None = None,
        decoder: Callable[[object], object] | None = None,
    ) -> None:
        self.sql_type = sql_type
        self.accepts = accepts
        self.name = name
        self.nullable = nullable
        self.generated = generated
        self.encoder = encoder
        self.decoder = decoder
        self.attr = ""
        self.table = None
        self.enum = None
        self.item = None  # element column of an array
        self.is_primary_key = False
        self.is_unique = False
        self.unique_name = None
        self.default_value = MISSING  # a value, an SQL expression, or MISSING
        self.default_factory = None
        self.reference = None
        self.checks = []

    @property
    def qualified_name(self) -> str:
        table = self.table.__name__ if self.table is not None else "?"
        return f"{table}.{self.attr}"

    @property
    def has_default(self) -> bool:
        return (
            self.generated or self.default_value is not MISSING or self.default_factory is not None
        )

    @property
    def required_on_insert(self) -> bool:
        return not self.nullable and not self.has_default

    def db_name(self, casing: Casing | None = None) -> str:
        if self.name is not None:
            return self.name
        return snake_case(self.attr) if casing == "snake_case" else self.attr

    def validate(self, value: object) -> None:
        """Raise QueryError if ``value`` cannot be stored in this column."""
        if isinstance(value, Node):
            return
        if value is None:
            if not self.nullable:
                raise QueryError(f"{self.qualified_name} is NOT NULL; got None")
            return
        if self.accepts is None:
            return
        # bool is a subclass of int; True must not slip into an integer column.
        bad_bool = isinstance(value, bool) and bool not in self.accepts
        if bad_bool or not isinstance(value, self.accepts):
            expected = " | ".join(t.__name__ for t in self.accepts)
            raise QueryError(
                f"{self.qualified_name} expects {expected}; got {type(value).__name__}"
            )

    def encode(self, value: object) -> object:
        return value if value is None or self.encoder is None else self.encoder(value)

    def decode(self, value: object) -> object:
        return value if value is None or self.decoder is None else self.decoder(value)

    @override
    def __repr__(self) -> str:
        return f"Column({self.qualified_name}: {self.sql_type})"

    # State changes live here (where the attributes are declared); Column[T] adds types.
    def _attach(self, owner: type[Table], name: str) -> None:
        self.attr = name
        self.table = owner

    def _set(
        self,
        *,
        nullable: bool | None = None,
        primary_key: bool | None = None,
        unique_name: str | None = None,
        unique: bool | None = None,
        default: object = MISSING,
        default_factory: Callable[[], object] | None = None,
        reference: Reference | None = None,
    ) -> None:
        if nullable is not None:
            self.nullable = nullable
        if primary_key is not None:
            self.is_primary_key = primary_key
        if unique is not None:
            self.is_unique = unique
            self.unique_name = unique_name
        if default is not MISSING:
            self.default_value = default
        if default_factory is not None:
            self.default_factory = default_factory
        if reference is not None:
            self.reference = reference


class Column[T](ColumnBase, Expr[T]):
    """``Users.id`` (class access) is the column; ``user.id`` (row access) is the value."""

    def __set_name__(self, owner: type[Table], name: str) -> None:
        self._attach(owner, name)

    @overload
    def __get__(self, obj: None, owner: type[object]) -> Self: ...
    @overload
    def __get__(self, obj: object, owner: type[object]) -> T: ...
    def __get__(self, obj: object | None, owner: type[object]) -> Self | T:
        # Only reached for instances when the row has no value: Column is a non-data
        # descriptor, so a loaded value in the instance __dict__ wins.
        if obj is None:
            return self
        raise AttributeError(f"{owner.__name__}.{self.attr} was not selected by this query")

    # -- declaration chain ------------------------------------------------------------

    @overload
    def not_null[U](self: Column[U | None]) -> Column[U]: ...
    @overload
    def not_null(self) -> Self: ...
    def not_null(self) -> ColumnBase:
        self._set(nullable=False)
        return self

    @overload
    def primary_key[U](self: Column[U | None]) -> Column[U]: ...
    @overload
    def primary_key(self) -> Self: ...
    def primary_key(self) -> ColumnBase:
        self._set(primary_key=True, nullable=False)
        return self

    def unique(self, name: str | None = None) -> Self:
        self._set(unique=True, unique_name=name)
        return self

    def default(self, value: T | ExprBase) -> Self:
        """A database-side default: a literal value or ``sql("now()")``."""
        self._set(default=value)
        return self

    def default_fn(self, factory: Callable[[], T]) -> Self:
        """A Python-side default, called once per inserted row."""
        self._set(default_factory=factory)
        return self

    def references(
        self,
        target: Callable[[], ColumnBase],
        *,
        on_delete: ReferentialAction | None = None,
        on_update: ReferentialAction | None = None,
    ) -> Self:
        self._set(reference=Reference(target, on_delete, on_update))
        return self

    def check(self, condition: Condition) -> Self:
        self.checks.append(condition)
        return self

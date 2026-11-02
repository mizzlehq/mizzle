"""Column: a typed table column. It is both a schema declaration and an SQL expression."""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Self, overload

from mizzle.core.errors import QueryError
from mizzle.core.naming import Casing, snake_case
from mizzle.core.sql.nodes import Expr, Node

if TYPE_CHECKING:
    from mizzle.core.schema.table import Table

type ReferentialAction = Literal["cascade", "restrict", "no action", "set null", "set default"]


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


@dataclass(frozen=True)
class Reference:
    target: Callable[[], Column[Any]]
    on_delete: ReferentialAction | None = None
    on_update: ReferentialAction | None = None


class Column[T](Expr[T]):
    """``Users.id`` (class access) is the column; ``user.id`` (row access) is the value."""

    def __init__(
        self,
        sql_type: str,
        accepts: tuple[type[Any], ...] | None,
        name: str | None = None,
        *,
        nullable: bool = True,
        generated: bool = False,
        encoder: Callable[[Any], Any] | None = None,
        decoder: Callable[[Any], Any] | None = None,
    ) -> None:
        self.sql_type = sql_type
        self.accepts = accepts
        self.name = name
        self.nullable = nullable
        self.generated = generated
        self.encoder = encoder
        self.decoder = decoder
        self.attr = ""
        self.table: type[Table] | None = None
        self.enum: type[enum.Enum] | None = None
        self.is_primary_key = False
        self.is_unique = False
        self.unique_name: str | None = None
        self.default_value: Any = MISSING
        self.default_factory: Callable[[], Any] | None = None
        self.reference: Reference | None = None
        self.checks: list[Expr[bool]] = []

    def __set_name__(self, owner: type[Table], name: str) -> None:
        self.attr = name
        self.table = owner

    @overload
    def __get__(self, obj: None, owner: type[Any]) -> Self: ...
    @overload
    def __get__(self, obj: object, owner: type[Any]) -> T: ...
    def __get__(self, obj: object | None, owner: type[Any]) -> Self | T:
        # Only reached for instances when the row has no value: Column is a non-data
        # descriptor, so a loaded value in the instance __dict__ wins.
        if obj is None:
            return self
        raise AttributeError(f"{owner.__name__}.{self.attr} was not selected by this query")

    def __repr__(self) -> str:
        return f"Column({self.qualified_name}: {self.sql_type})"

    # -- declaration chain ------------------------------------------------------------

    @overload
    def not_null[U](self: Column[U | None]) -> Column[U]: ...
    @overload
    def not_null(self) -> Self: ...
    def not_null(self) -> Any:
        self.nullable = False
        return self

    @overload
    def primary_key[U](self: Column[U | None]) -> Column[U]: ...
    @overload
    def primary_key(self) -> Self: ...
    def primary_key(self) -> Any:
        self.is_primary_key = True
        self.nullable = False
        return self

    def unique(self, name: str | None = None) -> Self:
        self.is_unique = True
        self.unique_name = name
        return self

    def default(self, value: T | Expr[Any]) -> Self:
        """A database-side default: a literal value or ``sql("now()")``."""
        self.default_value = value
        return self

    def default_fn(self, factory: Callable[[], T]) -> Self:
        """A Python-side default, called once per inserted row."""
        self.default_factory = factory
        return self

    def references(
        self,
        target: Callable[[], Column[Any]],
        *,
        on_delete: ReferentialAction | None = None,
        on_update: ReferentialAction | None = None,
    ) -> Self:
        self.reference = Reference(target, on_delete, on_update)
        return self

    def check(self, condition: Expr[bool]) -> Self:
        self.checks.append(condition)
        return self

    # -- runtime helpers --------------------------------------------------------------

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

    def encode(self, value: Any) -> Any:
        return value if value is None or self.encoder is None else self.encoder(value)

    def decode(self, value: Any) -> Any:
        return value if value is None or self.decoder is None else self.decoder(value)

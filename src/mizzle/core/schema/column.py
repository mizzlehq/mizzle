"""Column: a typed table column. It is both a schema declaration and an SQL expression."""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Self, overload, override

from mizzle.core.errors import QueryError
from mizzle.core.naming import Casing, snake_case
from mizzle.core.sql.nodes import Condition, Expr, ExprBase, Node, Raw

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


@dataclass(frozen=True)
class Identity:
    """``GENERATED {ALWAYS | BY DEFAULT} AS IDENTITY``; ``None`` options use the database's."""

    always: bool
    start: int | None = None
    increment: int | None = None


@dataclass(frozen=True)
class Computed:
    """``GENERATED ALWAYS AS (expression) STORED`` (or ``VIRTUAL``, PostgreSQL 18+)."""

    expression: ExprBase
    stored: bool = True


# The database's now(), for ``on_update_now()``.
NOW: Final = Raw(("now()",))


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
    identity: Identity | None
    computed: Computed | None
    # Written by update() (and upsert DO UPDATE) when the column is not set explicitly.
    on_update_value: Callable[[], object] | ExprBase | None

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
        self.identity = None
        self.computed = None
        self.on_update_value = None

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

    @property
    def read_only(self) -> bool:
        """Generated and ``GENERATED ALWAYS`` identity columns: the database writes them."""
        return self.computed is not None or (self.identity is not None and self.identity.always)

    def check_writable(self) -> None:
        """Raise QueryError if insert/update may not set this column."""
        if self.computed is not None:
            raise QueryError(
                f"{self.qualified_name} is a generated column; the database computes it, "
                "so it cannot be written"
            )
        if self.identity is not None and self.identity.always:
            raise QueryError(
                f"{self.qualified_name} is GENERATED ALWAYS AS IDENTITY; the database assigns "
                "it. Use generated_by_default_as_identity() to allow explicit values"
            )

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
        identity: Identity | None = None,
        computed: Computed | None = None,
        on_update: Callable[[], object] | ExprBase | None = None,
    ) -> None:
        if identity is not None:
            self.identity = identity
            self.nullable = False
            self.generated = True
        if computed is not None:
            self.computed = computed
            self.generated = True
        if on_update is not None:
            self.on_update_value = on_update
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

    @overload
    def generated_always_as_identity[U](
        self: Column[U | None], *, start: int | None = None, increment: int | None = None
    ) -> Column[U]: ...
    @overload
    def generated_always_as_identity(
        self, *, start: int | None = None, increment: int | None = None
    ) -> Self: ...
    def generated_always_as_identity(
        self, *, start: int | None = None, increment: int | None = None
    ) -> ColumnBase:
        """An identity column the database always assigns; inserts cannot set it."""
        self._set(identity=Identity(True, start, increment))
        return self

    @overload
    def generated_by_default_as_identity[U](
        self: Column[U | None], *, start: int | None = None, increment: int | None = None
    ) -> Column[U]: ...
    @overload
    def generated_by_default_as_identity(
        self, *, start: int | None = None, increment: int | None = None
    ) -> Self: ...
    def generated_by_default_as_identity(
        self, *, start: int | None = None, increment: int | None = None
    ) -> ColumnBase:
        """An identity column the database assigns unless an insert gives a value."""
        self._set(identity=Identity(False, start, increment))
        return self

    def generated_always_as(self, expression: ExprBase, *, stored: bool = True) -> Self:
        """A column computed from others: ``sql('price * qty')``. Never written by inserts or
        updates. ``stored=False`` makes it ``VIRTUAL`` (PostgreSQL 18+)."""
        self._set(computed=Computed(expression, stored))
        return self

    def on_update(self, fn: Callable[[], T]) -> Self:
        """Call ``fn`` for the value on every ``update()`` (and upsert ``DO UPDATE``) that
        does not set this column itself."""
        self._set(on_update=fn)
        return self

    def on_update_now(self) -> Self:
        """Set the column to the database's ``now()`` on every update that does not set it."""
        self._set(on_update=NOW)
        return self

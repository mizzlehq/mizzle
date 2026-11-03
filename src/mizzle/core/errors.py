"""Every error mizzle raises. Catch ``MizzleError`` to catch them all."""

from __future__ import annotations


class MizzleError(Exception):
    """Base class of all mizzle errors."""


class SchemaError(MizzleError):
    """A table definition is invalid."""


class QueryError(MizzleError):
    """A query cannot be built or compiled."""


class RelationNotLoaded(MizzleError):
    """A relation was accessed that the query did not load."""


class DatabaseError(MizzleError):
    """The database rejected a statement. Messages never include parameter values."""

    def __init__(
        self,
        message: str,
        *,
        sql: str | None = None,
        original: BaseException | None = None,
        constraint: str | None = None,
        column: str | None = None,
    ) -> None:
        super().__init__(message if sql is None else f"{message}\nSQL: {sql}")
        self.sql: str | None = sql
        self.original: BaseException | None = original
        self.constraint: str | None = constraint
        self.column: str | None = column

    @property
    def sqlstate(self) -> str | None:
        """The server's SQLSTATE code (e.g. ``"42501"`` when RLS or a grant denies access)."""
        code = getattr(self.original, "sqlstate", None)
        return code if isinstance(code, str) else None


class UniqueViolation(DatabaseError):
    pass


class ForeignKeyViolation(DatabaseError):
    """A missing referenced row, or a delete/update blocked by a foreign key (NO ACTION
    or RESTRICT)."""


class NotNullViolation(DatabaseError):
    pass


class CheckViolation(DatabaseError):
    pass


class SerializationFailure(DatabaseError):
    """The transaction conflicted with another one. Safe to retry."""


class ConnectionError(DatabaseError):
    pass


class QueryTimeout(DatabaseError):
    """The statement ran longer than its timeout and was cancelled."""


class ListenerOverflow(MizzleError):
    """A ``listen()`` consumer fell ``max_queue`` notifications behind (``overflow="raise"``);
    later notifications were not kept."""


class MigrationError(MizzleError):
    pass


class MigrationLockTimeout(MigrationError):
    pass


class MigrationTampered(MigrationError):
    pass


class DataLossBlocked(MigrationError):
    pass

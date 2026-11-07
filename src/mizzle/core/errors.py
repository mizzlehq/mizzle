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
        table: str | None = None,
    ) -> None:
        super().__init__(message if sql is None else f"{message}\nSQL: {sql}")
        self.sql: str | None = sql
        self.original: BaseException | None = original
        self.constraint: str | None = constraint
        self.column: str | None = column
        self.table: str | None = table

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
    """A connection could not be opened or was lost.

    ``retryable`` is False when trying again cannot help (bad credentials, unknown database,
    an unsupported feature), so a caller's retry loop can fail fast instead of waiting.
    ``lost`` is False when no connection was opened (the pool could not hand one out):
    the pooled connections are then fine and are kept."""

    retryable: bool = True
    lost: bool = True


class QueryTimeout(DatabaseError):
    """The statement ran longer than its timeout and was cancelled."""


class AfterCommitError(MizzleError):
    """An ``after_commit`` hook raised. The transaction **did** commit: the data is saved.

    Raised instead of the hook's own error so a caller does not mistake it for a failed
    write and run the transaction again. The hook's error is the cause."""


class AfterRollbackError(MizzleError):
    """An ``after_rollback`` hook raised while unwinding another error. The original error is
    the cause, and is what the caller sees; this only exists so the hook's failure is not
    lost (it is attached as the context of the original)."""


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

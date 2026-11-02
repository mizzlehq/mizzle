from mizzle.core.errors import (
    DatabaseError,
    MigrationError,
    MigrationTampered,
    MizzleError,
    UniqueViolation,
)


def test_hierarchy() -> None:
    assert issubclass(UniqueViolation, DatabaseError)
    assert issubclass(DatabaseError, MizzleError)
    assert issubclass(MigrationTampered, MigrationError)


def test_database_error_keeps_sql_and_original() -> None:
    original = ValueError("boom")
    err = UniqueViolation(
        "duplicate key", sql="INSERT INTO x VALUES ($1)", original=original, constraint="x_key"
    )
    assert "duplicate key" in str(err)
    assert "INSERT INTO x VALUES ($1)" in str(err)
    assert err.original is original
    assert err.constraint == "x_key"

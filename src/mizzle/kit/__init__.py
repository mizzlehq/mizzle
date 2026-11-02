"""Migration kit. Programmatic use: ``await mizzle.kit.migrate(db, "migrations")``."""

from __future__ import annotations

from pathlib import Path

from mizzle.db import Database
from mizzle.kit.journal import MigrationsDir
from mizzle.kit.migrate import run_migrations


async def migrate(
    db: Database,
    path: str | Path = "migrations",
    *,
    schema: str = "public",
    lock_timeout: float = 60.0,
) -> list[str]:
    """Apply pending migrations (e.g. at app startup). Safe to call from many processes at
    once: an advisory lock makes each migration run exactly once."""
    directory = MigrationsDir(Path(path), db.dialect.name)
    return await run_migrations(
        db.driver, db.dialect, directory, schema=schema, lock_timeout=lock_timeout
    )


__all__ = ["migrate"]

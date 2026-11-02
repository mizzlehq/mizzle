"""Apply pending migrations safely: locked, transactional, tamper-checked."""

from __future__ import annotations

import logging
from collections.abc import Callable

from mizzle.core.errors import MigrationError, MigrationTampered
from mizzle.core.steps import Statement
from mizzle.dialects.base import Connection, Dialect, Driver
from mizzle.kit.journal import Migration, MigrationsDir

log = logging.getLogger("mizzle")


async def apply_statements(
    conn: Connection, statements: list[Statement], after: tuple[str, list[object]] | None = None
) -> None:
    """Run statements; ``after`` (e.g. the history record) runs only once all succeeded.

    Fully transactional lists run in one transaction (all or nothing). Otherwise consecutive
    transactional statements are grouped and the others run alone.
    """
    if all(s.transactional for s in statements):
        async with conn.transaction():
            for s in statements:
                await conn.execute(s.sql, [])
            if after is not None:
                await conn.execute(*after)
        return
    batch: list[Statement] = []

    async def flush() -> None:
        if batch:
            async with conn.transaction():
                for s in batch:
                    await conn.execute(s.sql, [])
            batch.clear()

    for s in statements:
        if s.transactional:
            batch.append(s)
        else:
            await flush()
            await conn.execute(s.sql, [])
    await flush()
    if after is not None:
        await conn.execute(*after)


async def run_migrations(
    driver: Driver,
    dialect: Dialect,
    directory: MigrationsDir,
    *,
    schema: str = "public",
    lock_timeout: float = 60.0,
    on_apply: Callable[[Migration], None] | None = None,
) -> list[str]:
    """Apply every pending migration in order. Returns the names that were applied."""
    migrations = directory.migrations()
    async with driver.acquire() as conn:
        await dialect.acquire_migration_lock(conn, lock_timeout)
        try:
            await dialect.ensure_migrations_table(conn, schema)
            applied = await dialect.applied_migrations(conn, schema)
            known = {m.name for m in migrations}
            if missing := [name for name in applied if name not in known]:
                raise MigrationError(f"applied migrations are missing on disk: {missing}")
            last_applied = max((m.index for m in migrations if m.name in applied), default=0)
            done: list[str] = []
            for migration in migrations:
                hash = migration.hash()
                if migration.name in applied:
                    if applied[migration.name] != hash:
                        raise MigrationTampered(
                            f"{migration.name}/migration.sql changed after it was applied; "
                            "restore it and put the change in a new migration"
                        )
                    continue
                if migration.index < last_applied:
                    raise MigrationError(
                        f"{migration.name} is not applied but a later migration is; "
                        "this usually comes from merging branches. Run `mizzle check`."
                    )
                if on_apply is not None:
                    on_apply(migration)
                record = dialect.record_migration(schema, migration.name, hash)
                await apply_statements(conn, migration.statements(), record)
                done.append(migration.name)
            return done
        finally:
            try:
                await dialect.release_migration_lock(conn)
            except Exception:  # the connection may be broken; the lock dies with it
                log.warning("could not release the migration lock", exc_info=True)

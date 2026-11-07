"""Apply pending migrations safely: locked, transactional, tamper-checked."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence

from mizzle.core.errors import MigrationError, MigrationTampered
from mizzle.core.steps import Statement
from mizzle.dialects.base import Connection, Dialect, Driver
from mizzle.kit.journal import Migration, MigrationsDir
from mizzle.kit.sqltext import transaction_control

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


def resolve_target(migrations: list[Migration], target: str) -> int:
    """The index of the migration named by ``target``: an exact folder name or an exact
    number. A loose prefix or a wrong name is refused, so a typo cannot pick a neighbour."""
    for migration in migrations:
        if migration.name == target:
            return migration.index
    if target.isdigit():
        for migration in migrations:
            if migration.index == int(target):
                return migration.index
    raise MigrationError(f"no migration matches --to {target!r}")


def superseded_names(migrations: list[Migration]) -> set[str]:
    """Every name a squashed migration replaces, so it is not reported as missing on disk."""
    return {old for migration in migrations for old in migration.supersedes}


def history_complete(names: Sequence[str], applied: Mapping[str, str]) -> bool:
    """Whether the database applied the whole history ``names`` (a squash's supersedes).

    An earlier squash in the list (a ``0001_`` name after the first) stands for every name
    before it, so a database that ran that squash, or the history it replaced, counts.
    Checking only the last name adopted a database with a gap (B57).
    """
    complete = True
    for position, name in enumerate(names):
        if position > 0 and name.startswith("0001_"):
            complete = complete or name in applied
        else:
            complete = complete and name in applied
    return complete


def squash_adopted(migration: Migration, applied: Mapping[str, str]) -> bool:
    """A squash is in effect when the database applied the whole history it replaced."""
    return bool(migration.supersedes) and history_complete(migration.supersedes, applied)


def refuse_partial_squash(migrations: list[Migration], applied: Mapping[str, str]) -> None:
    """Fail when the database applied only part of the history a squash replaced: it can
    neither adopt the squash nor run it (that would recreate what is already there)."""
    for migration in migrations:
        if migration.name in applied or squash_adopted(migration, applied):
            continue
        if done := [name for name in migration.supersedes if name in applied]:
            raise MigrationError(partial_squash_message(migration.name, migration.supersedes, done))


def partial_squash_message(name: str, supersedes: Sequence[str], done: Sequence[str]) -> str:
    missing = [n for n in supersedes if n not in done]
    return (
        f"the database applied only part of the history {name} replaced "
        f"({', '.join(done)}, but not {', '.join(missing)}), so it can neither adopt nor "
        "run it. Check out the commit before the squash and run `mizzle migrate` there "
        "first, or reset this database."
    )


def pending_migrations(
    migrations: list[Migration], applied: Mapping[str, str], target: str | None = None
) -> list[Migration]:
    """The migrations ``migrate`` would run, after the same checks ``migrate`` makes: an
    applied migration was edited, applied migrations are missing on disk, or a pending
    migration is older than an applied one."""
    target_index = resolve_target(migrations, target) if target is not None else None
    known = {migration.name for migration in migrations} | superseded_names(migrations)
    if missing := [name for name in applied if name not in known]:
        raise MigrationError(f"applied migrations are missing on disk: {missing}")
    refuse_partial_squash(migrations, applied)
    last_applied = max((m.index for m in migrations if m.name in applied), default=0)
    pending: list[Migration] = []
    for migration in migrations:
        if migration.name in applied:
            if applied[migration.name] != migration.hash():
                raise MigrationTampered(
                    f"{migration.name}/migration.sql changed after it was applied; "
                    "restore it and put the change in a new migration"
                )
            continue
        if squash_adopted(migration, applied):
            continue  # already in effect: the history it replaced is applied
        if migration.index < last_applied:
            raise MigrationError(
                f"{migration.name} is not applied but a later migration is; "
                "this usually comes from merging branches. Run `mizzle check`."
            )
        if target_index is not None and migration.index > target_index:
            continue
        if word := transaction_control(migration.sql()):
            # It would commit part of the migration and then record it as applied; reset
            # refuses such a history too (C28).
            raise MigrationError(
                f"{migration.name} contains {word}: each migration runs in one transaction, "
                "so leave transaction control out (use -- mizzle:no-transaction for a "
                "statement that cannot run in a transaction)"
            )
        pending.append(migration)
    return pending


async def run_migrations(
    driver: Driver,
    dialect: Dialect,
    directory: MigrationsDir,
    *,
    schema: str = "public",
    lock_timeout: float = 60.0,
    target: str | None = None,
    on_apply: Callable[[Migration], None] | None = None,
) -> list[str]:
    """Apply every pending migration in order, or only those up to ``target``. Returns the
    names that were applied."""
    migrations = directory.migrations()
    async with driver.acquire() as conn:
        await dialect.acquire_migration_lock(conn, lock_timeout)
        try:
            await dialect.ensure_migrations_table(conn, schema)
            applied = await dialect.applied_migrations(conn, schema)
            # A squashed migration whose replaced names are all applied is already in
            # effect: replace those names with the squashed one in the history.
            for migration in migrations:
                if migration.name not in applied and squash_adopted(migration, applied):
                    for old in migration.supersedes:
                        await conn.execute(*dialect.forget_migration(schema, old))
                        applied.pop(old, None)
                    record = dialect.record_migration(schema, migration.name, migration.hash())
                    await conn.execute(*record)
                    applied[migration.name] = migration.hash()
            done: list[str] = []
            for migration in pending_migrations(migrations, applied, target):
                if on_apply is not None:
                    on_apply(migration)
                record = dialect.record_migration(schema, migration.name, migration.hash())
                await apply_statements(conn, migration.statements(), record)
                done.append(migration.name)
            return done
        finally:
            try:
                await dialect.release_migration_lock(conn)
            except Exception:  # the connection may be broken; the lock dies with it
                log.warning("could not release the migration lock", exc_info=True)

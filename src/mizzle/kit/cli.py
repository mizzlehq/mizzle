"""The ``mizzle`` command line: generate, migrate, push, pull, check, drop, status,
typegen, studio."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib
import json
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse

from mizzle.core.errors import DatabaseError, DataLossBlocked, MigrationError, MizzleError
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.steps import Step
from mizzle.dialects.base import Dialect, Driver
from mizzle.kit.codegen import render_schema_module, render_types_module
from mizzle.kit.config import Config, load_config
from mizzle.kit.diff import RenameKind, RenameResolver, StrictRenames, diff, no_renames
from mizzle.kit.init import init_project
from mizzle.kit.journal import (
    JournalEntry,
    Migration,
    MigrationsDir,
    format_sql,
    parse_sql,
    slugify,
)
from mizzle.kit.migrate import (
    apply_statements,
    history_complete,
    partial_squash_message,
    pending_migrations,
    refuse_partial_squash,
    resolve_target,
    run_migrations,
    squash_adopted,
    superseded_names,
)
from mizzle.kit.snapshot import build_snapshot
from mizzle.kit.sqltext import transaction_control

log = logging.getLogger("mizzle")


class Args(Protocol):
    """Parsed arguments; each subcommand sets only its own options (see ``_parser``)."""

    command: str
    config: Path | None
    func: Callable[[Config, Args], int]
    name: str | None
    custom: bool
    rename: list[str] | None
    no_renames: bool
    allow_data_loss: bool
    lock_timeout: float
    force: bool
    database: str | None
    update: bool
    dry_run: bool
    sql: bool
    json: bool
    check: bool
    fix: bool
    template: str | None
    to: str | None
    out: str | None
    port: int
    host: str
    read_only: bool
    no_open: bool
    auth: str | None
    token: str | None
    download_ui: bool
    ui_path: str | None
    schema: str
    url_env: str
    pyproject: bool


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(prompt: str) -> str:
    """``input()``, where Ctrl-D is an empty answer rather than an EOFError traceback (B60)."""
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        return ""


def _confirm(question: str) -> bool:
    return _ask(f"{question} [y/N] ").lower() in ("y", "yes")


def _ask_renames(
    kind: RenameKind, scope: str, removed: list[str], added: list[str]
) -> dict[str, str]:
    result: dict[str, str] = {}
    left = list(removed)
    for new in added:
        if not left:
            break
        options = ", ".join(left)
        what = "a move" if scope == "*" else "a rename"  # "*": names are "schema.table"
        answer = _ask(
            f"{kind} {new if scope == '*' else f'{scope}.{new}'} is new. Is it {what} of one "
            f"of: {options}? "
            "Type the old name, or press enter to create it: "
        )
        if answer:
            if answer not in left:
                raise MigrationError(f"{answer!r} is not one of {left}")
            result[answer] = new
            left.remove(answer)
    return result


def _resolver(args: Args, dialect: Dialect) -> RenameResolver:
    if args.no_renames:
        return no_renames
    renames: dict[str, str] = {}
    for item in args.rename or []:
        old, sep, new = item.partition("=")
        if not sep:
            raise MigrationError(f"--rename expects old=new, got {item!r}")
        renames[old] = new
    ask = _ask_renames if _interactive() else None
    return StrictRenames(renames, dialect.default_schema, ask)


def _guard_data_loss(steps: Sequence[Step], allow: bool) -> None:
    risky = [s for s in steps if s.data_loss]
    if not risky or allow:
        return
    listing = "\n".join(f"  - {s.describe()}" for s in risky)
    message = f"these changes can lose data:\n{listing}"
    if _interactive():
        if _confirm(f"Warning: {message}\nContinue?"):
            return
        raise DataLossBlocked("cancelled")
    raise DataLossBlocked(f"{message}\nrerun with --allow-data-loss if this is intended")


async def _with_driver[R](config: Config, fn: Callable[[Driver, Dialect], Awaitable[R]]) -> R:
    dialect = config.dialect()
    url = config.url()
    try:
        driver = await dialect.connect(url, pool_size=1, statement_cache_size=100)
    except MizzleError:
        raise
    except Exception as exc:  # a malformed URL is a user error, not a traceback
        raise MigrationError(f"cannot connect to the database: {exc}") from exc
    try:
        return await fn(driver, dialect)
    finally:
        await driver.close()


def _schemas(config: Config, *snapshots: Snapshot) -> list[str]:
    schemas = list(config.schema_filter)
    for snapshot in snapshots:
        schemas += [s for s in snapshot["schemas"] if s not in schemas]
    return schemas


def _types_path(config: Config) -> Path:
    module = importlib.import_module(config.schema[0])
    if module.__file__ is None:
        raise MigrationError(f"cannot locate the file of {config.schema[0]}")
    path = Path(module.__file__)
    return path.with_name(f"{path.stem}_types.py")


# -- commands -------------------------------------------------------------------------


def cmd_generate(config: Config, args: Args) -> int:
    dialect = config.dialect()
    tables = config.load_tables()
    directory = MigrationsDir(config.out, dialect.name)
    previous = directory.latest_snapshot()
    prev_id = previous["id"] if directory.migrations() else None
    current = build_snapshot(
        tables, dialect, config.casing, prev_id=prev_id, objects=config.load_objects()
    )
    if args.template and not args.custom:
        raise MigrationError("--template only applies to --custom migrations")
    if args.custom:
        if args.template:
            template = Path(args.template)
            try:
                header = template.read_text(encoding="utf-8")
            except OSError as exc:
                raise MigrationError(f"cannot read --template {template}: {exc}") from exc
        else:
            header = CUSTOM_HEADER
        if args.sql:  # --sql writes nothing, not even a --custom folder
            print(header.rstrip())
            return 0
        migration = directory.write(args.name or "custom", [], current, header, custom=True)
        print(f"Created {migration.sql_path}")
        return 0
    steps = diff(previous, current, _resolver(args, dialect))
    if args.sql:
        if not steps:
            print("No schema changes.")
            return 0
        for statement in dialect.render(steps):
            print(statement.sql + ";")
        return 0
    if not steps:
        print("No schema changes.")
        return 0
    _guard_data_loss(steps, args.allow_data_loss)
    header = "".join(f"-- {s.describe()}\n" for s in steps)
    name = args.name or steps[0].describe()
    migration = directory.write(name, dialect.render(steps), current, header)
    print(f"Created {migration.sql_path} ({len(steps)} changes)")
    types_path = _types_path(config)
    if types_path.exists():
        types_path.write_text(render_types_module(tables), encoding="utf-8")
        print(f"Updated {types_path}")
    return 0


def cmd_migrate(config: Config, args: Args) -> int:
    directory = MigrationsDir(config.out, config.dialect_name)

    if args.dry_run:
        return _migrate_dry_run(config, args, directory)

    async def run(driver: Driver, dialect: Dialect) -> list[str]:
        return await run_migrations(
            driver,
            dialect,
            directory,
            schema=config.migrations_schema,
            lock_timeout=args.lock_timeout,
            target=args.to,
            on_apply=lambda m: print(f"Applying {m.name}"),
        )

    applied = asyncio.run(_with_driver(config, run))
    if applied:
        print(f"Applied {len(applied)} migration(s).")
        return 0
    if args.to is not None:
        note = asyncio.run(_target_note(config, directory, args.to))
        if note is not None:
            print(note)
            return 0
    print("Database is up to date.")
    return 0


async def _target_note(config: Config, directory: MigrationsDir, target: str) -> str | None:
    """Say that ``--to`` names a migration the database has already applied, instead of the
    bare "up to date" that hides the migrations after it."""

    async def run(driver: Driver, dialect: Dialect) -> str | None:
        migrations = directory.migrations()
        target_index = resolve_target(migrations, target)
        target_migration = next(m for m in migrations if m.index == target_index)
        async with driver.acquire() as conn:
            try:
                applied = await dialect.applied_migrations(conn, config.migrations_schema)
            except DatabaseError as exc:
                if exc.sqlstate in ("42P01", "3F000"):  # undefined table / schema
                    applied = {}
                else:
                    raise
        last = max((m.index for m in migrations if m.name in applied), default=0)
        if target_index > last:
            return None
        ahead = f"; the database is ahead of it ({last:04d})" if target_index < last else ""
        return f"Target {target_migration.name} is already applied{ahead}."

    return await _with_driver(config, run)


def _migrate_dry_run(config: Config, args: Args, directory: MigrationsDir) -> int:
    """Print the SQL ``migrate`` would run, without touching the database. It makes the same
    tamper, missing-on-disk and ordering checks, so a run that would fail fails here too."""

    async def plan(driver: Driver, dialect: Dialect) -> list[Migration]:
        async with driver.acquire() as conn:
            try:
                applied = await dialect.applied_migrations(conn, config.migrations_schema)
            except DatabaseError as exc:
                if exc.sqlstate in ("42P01", "3F000"):  # undefined table / schema
                    applied = {}
                else:
                    raise
        return pending_migrations(directory.migrations(), applied, args.to)

    pending = asyncio.run(_with_driver(config, plan))
    if not pending:
        print("Database is up to date.")
        return 0
    for migration in pending:
        print(f"-- {migration.name}")
        print(migration.sql().rstrip())
    print(f"\n{len(pending)} pending migration(s); nothing was applied (--dry-run).")
    return 0


def _url_target(url: str) -> tuple[str, str]:
    """``(host[:port], database)`` from the URL, without any password."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:  # its message names the bad part, never the password
        raise MigrationError(f"the database URL is malformed: {exc}") from None
    if not parsed.scheme:
        raise MigrationError("the database URL must be a postgresql:// URL")
    host = parsed.hostname or "localhost"
    if port:
        host = f"{host}:{port}"
    return host, parsed.path.lstrip("/")


def _managed_only(live: Snapshot, managed: Snapshot) -> Snapshot:
    """The part of ``live`` that mizzle manages: objects named in ``managed``. Reset drops
    this, never an unmanaged table, view, extension or function in the same schema."""
    out = copy.deepcopy(live)
    tables = live.get("tables", {})
    out["tables"] = {k: v for k, v in tables.items() if k in managed.get("tables", {})}
    views = live.get("views", {})
    out["views"] = {k: v for k, v in views.items() if k in managed.get("views", {})}
    enums = live.get("enums", {})
    out["enums"] = {k: v for k, v in enums.items() if k in managed.get("enums", {})}
    domains = live.get("domains", {})
    out["domains"] = {k: v for k, v in domains.items() if k in managed.get("domains", {})}
    composites = live.get("composites", {})
    out["composites"] = {k: v for k, v in composites.items() if k in managed.get("composites", {})}
    functions = live.get("functions", {})
    out["functions"] = {k: v for k, v in functions.items() if k in managed.get("functions", {})}
    extensions = live.get("extensions", {})
    out["extensions"] = {k: v for k, v in extensions.items() if k in managed.get("extensions", {})}
    return out


def cmd_reset(config: Config, args: Args) -> int:
    """Drop every mizzle-managed object and re-run every migration from scratch."""
    directory = MigrationsDir(config.out, config.dialect_name)
    migrations = directory.migrations()
    if not migrations:
        print("No migrations to run.")
        return 0
    host, database = _url_target(config.url())
    if not database:
        # The driver would connect to ?dbname=, PGDATABASE or the user name instead, so the
        # name the user confirms could differ from the database dropped (B56).
        raise MigrationError(
            "reset needs the database URL to name the database (postgresql://host/NAME), "
            "so the name you confirm is the database it drops"
        )
    if args.database is not None and args.database != database:
        raise MigrationError(
            f"--database {args.database!r} does not match the URL database {database!r}"
        )
    # Reset re-runs every migration on top of dropping only what the latest snapshot holds.
    # In one transaction a failure rolls everything back; a statement that cannot run in a
    # transaction rules that out, and then hand-written SQL (which may create objects reset
    # does not drop, so it fails the second time) could leave the database half reset.
    hand = _hand_written(directory)
    for migration in migrations:
        if word := transaction_control(migration.sql()):
            # Every migration runs inside reset's transaction; a COMMIT in one ended it, so
            # the drops stayed while reset reported a rollback (B52).
            raise MigrationError(
                f"{migration.name} contains {word}: mizzle runs each migration in a "
                "transaction, and a statement that ends it would make reset impossible to "
                "roll back. Take the transaction control out of the migration"
            )
    # The migrations before the first one that cannot run in a transaction re-run in the
    # same transaction as the drops; the rest run after it commits (B51).
    first = next(
        (i for i, m in enumerate(migrations) if not all(s.transactional for s in m.statements())),
        len(migrations),
    )
    one_transaction = first == len(migrations)
    if hand and not one_transaction:
        raise MigrationError(
            f"reset cannot safely re-run the hand-written SQL in {', '.join(hand)}: it may "
            "create objects reset does not drop, and a migration that cannot run in a "
            "transaction means a failure could not be rolled back. Drop and recreate the "
            "database instead, then run `mizzle migrate`"
        )
    if args.force:
        if args.database is None:
            raise MigrationError(
                f"reset drops every managed object on {host}/{database}; "
                f"rerun with --force --database {database!r} to confirm"
            )
    elif not _interactive():
        raise MigrationError(
            f"reset drops every managed object on {host}/{database}; "
            f"pass --force --database {database!r} to confirm"
        )
    if not one_transaction:
        print(
            f"Note: {migrations[first].name} cannot run in a transaction, so a failure from "
            "there on cannot be rolled back; reset then stops and says what it applied."
        )
    if (
        not args.force
        and _ask(
            f"Drop every mizzle-managed object on {host}/{database} and re-run all "
            f"{len(migrations)} migration(s)? Type the database name to confirm: "
        )
        != database
    ):
        print("Cancelled: that is not the database name.")
        return 1

    async def run(driver: Driver, dialect: Dialect) -> int:
        latest = directory.latest_snapshot()
        async with driver.acquire() as conn:
            await dialect.acquire_migration_lock(conn, args.lock_timeout)
            try:
                schemas = _schemas(config, latest)
                live = await dialect.introspect(conn, schemas, list(latest.get("roles", {})))
                managed_live = _managed_only(live, latest)
                steps = diff(
                    managed_live,
                    empty_snapshot(config.dialect_name),
                    no_renames,
                    compare_expressions=False,
                )

                async def rerun(migration: Migration) -> None:
                    record = dialect.record_migration(
                        config.migrations_schema, migration.name, migration.hash()
                    )
                    await apply_statements(conn, migration.statements(), record)

                try:
                    async with conn.transaction():  # all drops, or none
                        for statement in dialect.render(steps):
                            await conn.execute(statement.sql, [])
                        # The history table is not in the schema diff; always clear it so
                        # every migration runs again even when its schema is not managed.
                        await dialect.drop_migrations_table(conn, config.migrations_schema)
                        await dialect.ensure_migrations_table(conn, config.migrations_schema)
                        for migration in migrations[:first]:
                            await rerun(migration)
                except DatabaseError as exc:
                    hint = (
                        f" Hand-written SQL in {', '.join(hand)} may create objects reset "
                        "does not drop; drop them by hand, or recreate the database."
                        if hand
                        else ""
                    )
                    raise MigrationError(
                        f"reset failed and was rolled back; the database is unchanged: {exc}{hint}"
                    ) from exc
                for migration in migrations[:first]:  # printed once they are committed
                    print(f"Applied {migration.name}")
                for done, migration in enumerate(migrations[first:], first):
                    try:
                        await rerun(migration)
                    except DatabaseError as exc:
                        raise MigrationError(
                            f"reset dropped the managed objects and re-ran {done} "
                            f"migration(s), then {migration.name} failed: {exc}. The database "
                            "is partly reset: a statement that cannot run in a transaction "
                            "cannot be rolled back. Fix the cause, then run `mizzle migrate` "
                            "to apply the rest"
                        ) from exc
                    print(f"Applied {migration.name}")
                return len(migrations)
            finally:
                try:
                    await dialect.release_migration_lock(conn)
                except Exception:  # the connection may be broken; the lock dies with it
                    log.warning("could not release the migration lock", exc_info=True)

    applied = asyncio.run(_with_driver(config, run))
    print(f"Reset: applied {applied} migration(s).")
    return 0


def cmd_squash(config: Config, args: Args) -> int:
    """Collapse the migration history into one migration that builds the latest schema."""
    dialect = config.dialect()
    directory = MigrationsDir(config.out, config.dialect_name)
    migrations = directory.migrations()
    if not migrations:
        print("No migrations to squash.")
        return 0
    if len(migrations) == 1 and migrations[0].supersedes:
        print("Nothing to squash: the history is already one squashed migration.")
        return 0
    if unlisted := directory.unlisted():
        # A folder from a merged branch: squash must not delete SQL it does not know (B54).
        raise MigrationError(
            f"{', '.join(unlisted)} is not in the journal; run `mizzle check --fix` (or "
            "remove the folder) before squashing"
        )
    if hand := _hand_written(directory):
        raise MigrationError(
            f"squash would lose hand-written SQL in {', '.join(hand)} (a custom migration, "
            "or SQL edited after generate); take it out, squash, then add it to the squashed "
            "migration by hand"
        )
    # Every name any database may have applied, so each history can adopt the squash.
    supersedes = [name for m in migrations for name in (*m.supersedes, m.name)]
    base = slugify(args.name or "squash")[:40]
    slug, n = base, 1
    while f"0001_{slug}" in supersedes:  # a reused name would read as an edited migration
        n += 1
        slug = f"{base}_{n}"
    try:
        applied = _applied_now(config)
    except MizzleError:  # squash works offline; the warning below is best effort
        applied = None
    done = [name for name in supersedes if applied and name in applied]
    if applied is not None and done and not history_complete(supersedes, applied):
        # Squashing anyway left advice that could only be followed after a git restore (B63).
        raise MigrationError(
            "refusing to squash: "
            + partial_squash_message("the squash", supersedes, done)
            + " Run `mizzle migrate` first"
        )
    latest = directory.latest_snapshot()
    steps = diff(empty_snapshot(config.dialect_name), latest, no_renames, compare_expressions=False)
    statements = dialect.render(steps)
    latest["prev_id"] = None
    directory.clear()
    migration = directory.write(slug, statements, latest, supersedes=supersedes)
    print(f"Squashed {len(migrations)} migration(s) into {migration.name}.")
    print("A database that applied the old history treats it as applied; a fresh one runs it.")
    return 0


def cmd_seed(config: Config, _args: Args) -> int:
    """Run every ``*.sql`` file in the seed directory, in name order."""
    folder = config.seed if config.seed.is_absolute() else config.root / config.seed
    scripts = sorted(folder.glob("*.sql")) if folder.is_dir() else []
    if not scripts:
        print(f"No seed scripts in {folder}.")
        return 0

    async def run(driver: Driver, _dialect: Dialect) -> int:
        async with driver.acquire() as conn:
            for script in scripts:
                print(f"Seeding {script.name}")
                await apply_statements(conn, parse_sql(script.read_text(encoding="utf-8")))
        return len(scripts)

    count = asyncio.run(_with_driver(config, run))
    print(f"Ran {count} seed script(s).")
    return 0


def cmd_push(config: Config, args: Args) -> int:
    tables, objects = config.load_tables(), config.load_objects()

    async def run(driver: Driver, dialect: Dialect) -> int:
        desired = build_snapshot(tables, dialect, config.casing, objects=objects)
        async with driver.acquire() as conn:
            live = await dialect.introspect(
                conn, _schemas(config, desired), list(desired.get("roles", {}))
            )
            # View queries and expressions as the database prints them, to compare.
            desired = await dialect.normalize_snapshot(conn, desired)
            steps = diff(live, desired, _resolver(args, dialect), compare_expressions=False)
            if not steps:
                print("Database is up to date.")
                return 0
            statements = dialect.render(steps)
            for statement in statements:
                print(statement.sql + ";")
            if args.dry_run:
                return 0
            _guard_data_loss(steps, args.allow_data_loss)
            if not args.force:
                if not _interactive():
                    raise MigrationError("push changes the database directly; pass --force")
                if not _confirm("Apply these changes?"):
                    return 1
            await apply_statements(conn, statements)
        print(f"Applied {len(steps)} change(s).")
        return 0

    return asyncio.run(_with_driver(config, run))


def cmd_pull(config: Config, args: Args) -> int:
    # A new project gets the pulled tables as its schema module. An existing schema module
    # is never overwritten (the result goes next to it, to compare and copy from), unless
    # --update: another app owns the schema and this one follows it.
    schema_file = config.schema_file()
    if args.out:
        out = Path(args.out)
    elif schema_file.exists() and not args.update:
        out = schema_file.with_name("pulled_schema.py")
    else:
        out = schema_file
    if out.exists() and not (args.force or args.update):
        raise MigrationError(f"{out} exists; pass --force to overwrite it")

    async def run(driver: Driver, dialect: Dialect) -> Snapshot:
        async with driver.acquire() as conn:
            return await dialect.introspect(conn, config.schema_filter)

    snapshot = asyncio.run(_with_driver(config, run))
    directory = MigrationsDir(config.out, config.dialect_name)
    if args.update and directory.migrations():
        previous = directory.latest_snapshot()
        steps = diff(previous, snapshot, no_renames, compare_expressions=False)
        if not steps:
            print("Up to date with the database.")
            return 0
        snapshot["id"], snapshot["prev_id"] = str(uuid.uuid4()), previous["id"]
        sql = format_sql(config.dialect().render(steps))
        header = PULLED_HEADER + "".join(
            f"-- {line}\n" if line else "--\n" for line in sql.splitlines()
        )
        migration = directory.write(args.name or "pull", [], snapshot, header)
        print(f"Recorded {migration.path} ({len(steps)} changes, already in the database)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_schema_module(snapshot), encoding="utf-8")
    print(f"Wrote {out} ({len(snapshot['tables'])} tables)")
    if not directory.migrations():
        header = "-- Baseline pulled from an existing database. Nothing to run.\n"
        migration = directory.write("pull", [], snapshot, header)
        print(f"Created baseline {migration.path}")
    return 0


PULLED_HEADER = (
    "-- Pulled from the database: another app already applied these changes.\n"
    "-- Kept as a record only; every statement is commented out, so migrate runs nothing.\n"
)


CUSTOM_HEADER = "-- Custom migration. Separate statements with: -- mizzle:breakpoint\n"
_CUSTOM_HASH = hashlib.sha256(format_sql([], CUSTOM_HEADER).encode()).hexdigest()


def _edited(entry: JournalEntry, migration: Migration) -> bool:
    """A generated migration whose SQL changed after generate. A --custom one is meant to be
    written after generate (an old journal marks it only by its empty-template hash)."""
    return not migration.custom and entry["hash"] not in (migration.hash(), _CUSTOM_HASH)


def _hand_written(directory: MigrationsDir) -> list[str]:
    """Migrations holding SQL a person wrote: custom ones, and generated ones edited later."""
    return [
        m.name
        for entry, m in zip(directory.journal()["entries"], directory.migrations(), strict=True)
        if _edited(entry, m) or ((m.custom or entry["hash"] == _CUSTOM_HASH) and m.statements())
    ]


def cmd_check(config: Config, args: Args) -> int:
    directory = MigrationsDir(config.out, config.dialect_name)
    if args.fix:
        fixes = directory.repair()
        for fix in fixes:
            print(f"fixed: {fix}")
        if not fixes:
            print("Nothing to fix.")
    problems: list[str] = []
    warnings: list[str] = []
    journal = directory.journal()
    names = [e["name"] for e in journal["entries"]]
    prev_id: str | None = None
    for entry, migration in zip(journal["entries"], directory.migrations(), strict=True):
        if not migration.sql_path.exists() or not migration.snapshot_path.exists():
            problems.append(f"{migration.name}: migration.sql or snapshot.json is missing")
            continue
        if _edited(entry, migration):
            warnings.append(f"{migration.name}: migration.sql was edited after generate")
        snapshot = migration.snapshot()
        if snapshot["prev_id"] != prev_id:
            problems.append(
                f"{migration.name}: snapshot does not follow the previous migration "
                "(two branches generated migrations from the same parent?)"
            )
        prev_id = snapshot["id"]
    if config.out.exists():
        for folder in sorted(p for p in config.out.iterdir() if p.is_dir() and p.name != "meta"):
            if folder.name not in names:
                problems.append(f"{folder.name}: folder is not in meta/journal.json")
    for w in warnings:
        print(f"warning: {w}")
    for p in problems:
        print(f"error: {p}")
    if problems:
        return 1
    print(f"{len(names)} migration(s) OK.")
    return 0


def _applied_now(config: Config) -> dict[str, str] | None:
    """The applied migrations, or ``None`` when there is no database URL to ask."""
    try:
        config.url()
    except MigrationError:
        return None

    async def load(driver: Driver, dialect: Dialect) -> dict[str, str]:
        # Read only: no migrations table (or schema) yet means nothing is applied.
        async with driver.acquire() as conn:
            try:
                return await dialect.applied_migrations(conn, config.migrations_schema)
            except DatabaseError as exc:
                if exc.sqlstate in ("42P01", "3F000"):  # undefined table / schema
                    return {}
                raise

    return asyncio.run(_with_driver(config, load))


def cmd_status(config: Config, args: Args) -> int:
    directory = MigrationsDir(config.out, config.dialect_name)
    migrations = directory.migrations()
    applied = _applied_now(config)
    if applied is not None:
        refuse_partial_squash(migrations, applied)
    superseded = superseded_names(migrations)

    rows: list[dict[str, object]] = []
    counts = {"applied": 0, "pending": 0, "changed": 0, "unknown": 0}
    for migration in migrations:
        if applied is None:
            state = "unknown"
        elif migration.name in applied:
            state = "changed" if applied[migration.name] != migration.hash() else "applied"
        elif squash_adopted(migration, applied):
            state = "applied"  # a squash whose replaced history is applied
        else:
            state = "pending"
        counts[state] += 1
        rows.append({"name": migration.name, "state": state})
    known = {m.name for m in migrations} | superseded
    missing = [name for name in (applied or {}) if name not in known]

    if args.json:
        print(
            json.dumps(
                {
                    "migrations": rows,
                    "applied": counts["applied"],
                    "pending": counts["pending"],
                    "missing": missing,
                }
            )
        )
    elif not migrations:
        print("No migrations.")
    else:
        width = max(len(m.name) for m in migrations)
        labels = {
            "unknown": "unknown (set the database URL to compare)",
            "pending": "pending",
            "changed": "applied (changed after it was applied)",
            "applied": "applied",
        }
        for row in rows:
            print(f"  {row['name']:<{width}}  {labels[cast(str, row['state'])]}")
        if applied is None:
            print(f"\n{len(migrations)} migration(s). Set ${config.url_env} to compare.")
        else:
            for name in missing:
                print(f"  {name:<{width}}  applied but missing on disk")
            print(f"\n{counts['applied']} applied, {counts['pending']} pending.")
    # An applied migration edited on disk is a failure, exactly as `migrate` treats it.
    failed = applied is None or counts["pending"] or counts["changed"] or missing
    if args.check and failed:
        return 1
    return 0


def cmd_info(config: Config, args: Args) -> int:
    directory = MigrationsDir(config.out, config.dialect_name)
    migrations = directory.migrations()
    print("mizzle")
    print(f"  dialect:     {config.dialect_name}")
    print(f"  config:      {config.root}")
    print(f"  schema:      {', '.join(config.schema)} ({config.schema_file()})")
    print(f"  migrations:  {config.out} ({len(migrations)} on disk)")
    try:
        config.url()
    except MigrationError as exc:
        print(f"  database:    {exc}")
        print(f"  applied:     unknown (set ${config.url_env})")
        return 1 if args.command == "doctor" else 0

    async def inspect(driver: Driver, dialect: Dialect) -> tuple[str, dict[str, str], list[Step]]:
        async with driver.acquire() as conn:
            [row] = await conn.fetch_dicts("SELECT current_setting('server_version') AS v", [])
            latest = directory.latest_snapshot()
            try:
                applied = await dialect.applied_migrations(conn, config.migrations_schema)
            except DatabaseError as exc:
                if exc.sqlstate in ("42P01", "3F000"):  # undefined table / schema
                    applied = {}
                else:
                    raise
            live = await dialect.introspect(
                conn, _schemas(config, latest), list(latest.get("roles", {}))
            )
            desired = await dialect.normalize_snapshot(conn, latest)
            drift = diff(live, desired, no_renames, compare_expressions=False)
            return str(row["v"]), applied, drift

    version, applied, drift = asyncio.run(_with_driver(config, inspect))
    superseded = superseded_names(migrations)
    known = {m.name for m in migrations} | superseded
    missing = [name for name in applied if name not in known]
    changed = [m.name for m in migrations if m.name in applied and applied[m.name] != m.hash()]

    def in_effect(migration: Migration) -> bool:
        return migration.name in applied or squash_adopted(migration, applied)

    pending = sum(1 for m in migrations if not in_effect(m))
    try:
        refuse_partial_squash(migrations, applied)
        partial = None
    except MigrationError as exc:  # migrate refuses this state; say so here too (B62)
        partial = str(exc)
    print(f"  database:    PostgreSQL {version}")
    print(f"  applied:     {len(migrations) - pending} applied, {pending} pending")
    if changed:
        print(f"  changed:     {len(changed)} applied migration(s) edited after they were applied")
    for name in missing:
        print(f"  missing:     {name} is applied but not on disk")
    if partial:
        print(f"  squash:      {partial}")
    if drift:
        print(f"  drift:       {len(drift)} change(s) between the database and the migrations")
        for step in drift[:10]:
            print(f"    - {step.describe()}")
    else:
        print("  drift:       none")
    if args.command == "doctor":
        return 1 if pending or drift or changed or missing or partial else 0
    return 0


def cmd_drop(config: Config, args: Args) -> int:
    directory = MigrationsDir(config.out, config.dialect_name)
    migrations = directory.migrations()
    if not migrations:
        raise MigrationError("there are no migrations to drop")
    latest = migrations[-1]
    try:
        config.url()
        has_url = True
    except MigrationError:
        has_url = False
    if has_url:

        async def applied(driver: Driver, dialect: Dialect) -> dict[str, str]:
            async with driver.acquire() as conn:
                await dialect.ensure_migrations_table(conn, config.migrations_schema)
                return await dialect.applied_migrations(conn, config.migrations_schema)

        if latest.name in asyncio.run(_with_driver(config, applied)):
            raise MigrationError(f"{latest.name} is already applied; write a new migration instead")
    elif not args.force:
        raise MigrationError(
            f"cannot verify {latest.name} is unapplied: set {config.url_env} or pass --force"
        )
    directory.drop_latest()
    print(f"Dropped {latest.name}")
    return 0


def cmd_typegen(config: Config, args: Args) -> int:
    tables = config.load_tables()
    path = Path(args.out) if args.out else _types_path(config)
    path.write_text(render_types_module(tables), encoding="utf-8")
    print(f"Wrote {path}")
    return 0


def cmd_init(args: Args) -> int:
    root = Path.cwd()
    for line in init_project(
        root,
        schema_file=Path(args.schema),
        out=args.out or "migrations",
        url_env=args.url_env,
        in_pyproject=args.pyproject,
        force=args.force,
    ):
        print(f"  {line}")
    print(
        f"\nNext:\n  export {args.url_env}=postgresql://user:password@localhost:5432/db\n"
        "  mizzle generate   # write the first migration from your schema\n"
        "  mizzle migrate    # apply it"
    )
    return 0


def cmd_studio(config: Config, args: Args) -> int:
    if args.download_ui:
        # Prefetch for offline use: this needs no Studio extra, only the standard library.
        from mizzle.studio.assets import ui_path

        print(f"Studio UI at {ui_path(args.ui_path)}")
        return 0
    try:
        from mizzle.studio.server import serve
    except ImportError as exc:
        raise MigrationError("Studio needs extra packages: pip install 'mizzle[studio]'") from exc
    serve(
        config,
        port=args.port,
        read_only=args.read_only,
        open_browser=not args.no_open,
        ui_dir=args.ui_path,
        host=args.host,
        token=args.token,
        auth=args.auth,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mizzle", description="mizzle migration kit")
    parser.add_argument("--config", type=Path, help="mizzle.toml or pyproject.toml to use")
    sub = parser.add_subparsers(dest="command", required=True)

    def renames(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--rename",
            action="append",
            metavar="OLD=NEW",
            help="table rename (users=people) or column rename (users.name=full_name)",
        )
        p.add_argument("--no-renames", action="store_true", help="treat changes as drop + create")
        p.add_argument("--allow-data-loss", action="store_true")

    p = sub.add_parser("init", help="set up mizzle in this project")
    p.add_argument("--schema", default="db/schema.py", help="schema module file to create")
    p.add_argument("--out", default="migrations", help="migrations folder")
    p.add_argument("--url-env", default="DATABASE_URL", help="env var with the database URL")
    p.add_argument("--pyproject", action="store_true", help="use [tool.mizzle] in pyproject.toml")
    p.add_argument("--force", action="store_true", help="replace an existing mizzle.toml")
    p.set_defaults(func=None)

    p = sub.add_parser("generate", help="write a migration from schema changes")
    p.add_argument("--name")
    p.add_argument("--custom", action="store_true", help="empty migration for hand-written SQL")
    p.add_argument(
        "--sql", action="store_true", help="print the SQL instead of writing a migration"
    )
    p.add_argument("--template", help="file whose contents seed a --custom migration")
    renames(p)
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("migrate", help="apply pending migrations")
    p.add_argument("--lock-timeout", type=float, default=60.0)
    p.add_argument("--dry-run", action="store_true", help="print the SQL, apply nothing")
    p.add_argument("--to", help="apply only up to this migration (exact name or number)")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("reset", help="drop the schema and re-run every migration")
    p.add_argument("--force", action="store_true", help="do not ask (needs --database to confirm)")
    p.add_argument("--database", help="the database name, to confirm a destructive reset")
    p.add_argument("--lock-timeout", type=float, default=60.0)
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("squash", help="collapse the history into one migration")
    p.add_argument("--name", help="name for the squashed migration")
    p.set_defaults(func=cmd_squash)

    p = sub.add_parser("seed", help="run the seed scripts (seeds/*.sql)")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("push", help="sync the schema to the database directly (development)")
    p.add_argument("--force", action="store_true", help="do not ask before applying")
    p.add_argument("--dry-run", action="store_true", help="only print the SQL")
    renames(p)
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("pull", help="write a schema module from an existing database")
    p.add_argument(
        "--out",
        help="file to write (default: your schema module if it does not exist yet, "
        "otherwise pulled_schema.py next to it)",
    )
    p.add_argument("--force", action="store_true", help="overwrite the output file")
    p.add_argument(
        "--update",
        action="store_true",
        help="the schema is owned by another app: rewrite your schema module and record "
        "the database's changes as a migration that runs nothing",
    )
    p.add_argument("--name", help="name of the recorded migration (with --update)")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("check", help="validate the migrations folder")
    p.add_argument("--fix", action="store_true", help="repair journal and prev_id inconsistencies")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("status", help="show which migrations are applied and which are pending")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--check", action="store_true", help="exit 1 when migrations are pending")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("info", help="environment, connectivity, pending count and drift")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("doctor", help="like info, but exit 1 when something is wrong")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("drop", help="delete the latest (unapplied) migration")
    p.add_argument("--force", action="store_true", help="drop without checking the database")
    p.set_defaults(func=cmd_drop)

    p = sub.add_parser("typegen", help="write row/insert/update TypedDicts")
    p.add_argument("--out")
    p.set_defaults(func=cmd_typegen)

    p = sub.add_parser("studio", help="browse and edit data in the browser")
    p.add_argument("--port", type=int, default=4983)
    p.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.add_argument(
        "--auth",
        metavar="USER:PASSWORD",
        help="also require HTTP Basic auth (needed to bind a non-loopback host)",
    )
    p.add_argument(
        "--token",
        help="bearer token (default: a random one, or $MIZZLE_STUDIO_TOKEN)",
    )
    p.add_argument(
        "--download-ui",
        action="store_true",
        help="download and verify the Studio UI, then exit (prefetch for offline use)",
    )
    p.add_argument(
        "--ui-path",
        help="folder with a built Studio UI (offline use; default: download on first run)",
    )
    p.set_defaults(func=cmd_studio)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # argparse sets these attributes dynamically; _parser defines them to match Args.
    args = cast(Args, cast(object, _parser().parse_args(argv)))
    try:
        if args.command == "init":  # runs before any config exists
            return cmd_init(args)
        config = load_config(args.config)
        return args.func(config, args)
    except MizzleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

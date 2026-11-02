"""The ``mizzle`` command line: generate, migrate, push, pull, check, drop, typegen, studio."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

from mizzle.core.errors import DataLossBlocked, MigrationError, MizzleError
from mizzle.core.snapshot import Snapshot
from mizzle.core.steps import Step
from mizzle.dialects.base import Dialect, Driver
from mizzle.kit.codegen import render_schema_module, render_types_module
from mizzle.kit.config import Config, load_config
from mizzle.kit.diff import RenameKind, RenameResolver, StrictRenames, diff, no_renames
from mizzle.kit.journal import MigrationsDir
from mizzle.kit.migrate import apply_statements, run_migrations
from mizzle.kit.snapshot import build_snapshot


class Args(Protocol):
    """Parsed arguments; each subcommand sets only its own options (see ``_parser``)."""

    config: Path | None
    func: Callable[[Config, Args], int]
    name: str | None
    custom: bool
    rename: list[str] | None
    no_renames: bool
    allow_data_loss: bool
    lock_timeout: float
    force: bool
    dry_run: bool
    out: str | None
    port: int
    read_only: bool
    no_open: bool


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _ask_renames(
    kind: RenameKind, scope: str, removed: list[str], added: list[str]
) -> dict[str, str]:
    result: dict[str, str] = {}
    left = list(removed)
    for new in added:
        if not left:
            break
        options = ", ".join(left)
        answer = input(
            f"{kind} {scope}.{new} is new. Is it a rename of one of: {options}? "
            "Type the old name, or press enter to create it: "
        ).strip()
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
    driver = await dialect.connect(config.url(), pool_size=1, statement_cache_size=100)
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
    current = build_snapshot(tables, dialect, config.casing, prev_id=prev_id)
    if args.custom:
        header = "-- Custom migration. Separate statements with: -- mizzle:breakpoint\n"
        migration = directory.write(args.name or "custom", [], current, header)
        print(f"Created {migration.sql_path}")
        return 0
    steps = diff(previous, current, _resolver(args, dialect))
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

    async def run(driver: Driver, dialect: Dialect) -> list[str]:
        return await run_migrations(
            driver,
            dialect,
            directory,
            schema=config.migrations_schema,
            lock_timeout=args.lock_timeout,
            on_apply=lambda m: print(f"Applying {m.name}"),
        )

    applied = asyncio.run(_with_driver(config, run))
    print(f"Applied {len(applied)} migration(s)." if applied else "Database is up to date.")
    return 0


def cmd_push(config: Config, args: Args) -> int:
    tables = config.load_tables()

    async def run(driver: Driver, dialect: Dialect) -> int:
        desired = build_snapshot(tables, dialect, config.casing)
        async with driver.acquire() as conn:
            live = await dialect.introspect(conn, _schemas(config, desired))
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
    out = Path(args.out) if args.out else config.root / "pulled_schema.py"
    if out.exists() and not args.force:
        raise MigrationError(f"{out} exists; pass --force to overwrite it")

    async def run(driver: Driver, dialect: Dialect) -> Snapshot:
        async with driver.acquire() as conn:
            return await dialect.introspect(conn, config.schema_filter)

    snapshot = asyncio.run(_with_driver(config, run))
    out.write_text(render_schema_module(snapshot), encoding="utf-8")
    print(f"Wrote {out} ({len(snapshot['tables'])} tables)")
    directory = MigrationsDir(config.out, config.dialect_name)
    if not directory.migrations():
        header = "-- Baseline pulled from an existing database. Nothing to run.\n"
        migration = directory.write("pull", [], snapshot, header)
        print(f"Created baseline {migration.path}")
    return 0


def cmd_check(config: Config, _args: Args) -> int:
    directory = MigrationsDir(config.out, config.dialect_name)
    problems: list[str] = []
    warnings: list[str] = []
    journal = directory.journal()
    names = [e["name"] for e in journal["entries"]]
    prev_id: str | None = None
    for entry, migration in zip(journal["entries"], directory.migrations(), strict=True):
        if not migration.sql_path.exists() or not migration.snapshot_path.exists():
            problems.append(f"{migration.name}: migration.sql or snapshot.json is missing")
            continue
        if migration.hash() != entry["hash"]:
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


def cmd_studio(config: Config, args: Args) -> int:
    try:
        from mizzle.studio.server import serve
    except ImportError as exc:
        raise MigrationError("Studio needs extra packages: pip install 'mizzle[studio]'") from exc
    serve(config, port=args.port, read_only=args.read_only, open_browser=not args.no_open)
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

    p = sub.add_parser("generate", help="write a migration from schema changes")
    p.add_argument("--name")
    p.add_argument("--custom", action="store_true", help="empty migration for hand-written SQL")
    renames(p)
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("migrate", help="apply pending migrations")
    p.add_argument("--lock-timeout", type=float, default=60.0)
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("push", help="sync the schema to the database directly (development)")
    p.add_argument("--force", action="store_true", help="do not ask before applying")
    p.add_argument("--dry-run", action="store_true", help="only print the SQL")
    renames(p)
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("pull", help="write a schema module from an existing database")
    p.add_argument("--out")
    p.add_argument("--force", action="store_true", help="overwrite the output file")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("check", help="validate the migrations folder")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("drop", help="delete the latest (unapplied) migration")
    p.add_argument("--force", action="store_true", help="drop without checking the database")
    p.set_defaults(func=cmd_drop)

    p = sub.add_parser("typegen", help="write insert/update TypedDicts")
    p.add_argument("--out")
    p.set_defaults(func=cmd_typegen)

    p = sub.add_parser("studio", help="browse and edit data in the browser")
    p.add_argument("--port", type=int, default=4983)
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.set_defaults(func=cmd_studio)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # argparse sets these attributes dynamically; _parser defines them to match Args.
    args = cast(Args, cast(object, _parser().parse_args(argv)))
    try:
        config = load_config(args.config)
        return args.func(config, args)
    except MizzleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

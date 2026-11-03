# CLI

Run `mizzle <command> --help` for all options. Every command reads `mizzle.toml`, or
`[tool.mizzle]` in `pyproject.toml`; `--config` picks another file.

| Command | What it does |
|---|---|
| `mizzle init` | Set up mizzle in this project. |
| `mizzle generate` | Write a migration from schema changes. |
| `mizzle migrate` | Apply pending migrations. |
| `mizzle push` | Sync the schema to the database directly (development). |
| `mizzle pull` | Write a schema module from an existing database. |
| `mizzle check` | Validate the migrations folder. |
| `mizzle drop` | Delete the latest (unapplied) migration. |
| `mizzle typegen` | Write insert/update TypedDicts. |
| `mizzle studio` | Browse and edit data in the browser. |

## generate

```bash
mizzle generate --name add_bio
mizzle generate --rename users=people             # a table rename, not drop + create
mizzle generate --rename users.name=full_name     # a column rename
mizzle generate --no-renames                      # treat changes as drop + create
mizzle generate --allow-data-loss                 # allow drops and narrowing types
mizzle generate --custom --name backfill          # empty migration for your own SQL
```

When a column or table is gone and a new one appears, mizzle asks if it is a rename (on a
terminal), or stops and tells you which `--rename` to pass (in CI).

In a `--custom` migration, separate statements with `-- mizzle:breakpoint`.

## migrate

```bash
mizzle migrate
mizzle migrate --lock-timeout 5   # seconds to wait for another server's migration (default 60)
```

Each migration runs in one transaction. Many app servers can run `migrate` at the same time:
an advisory lock lets one apply and the others wait. A server that waits longer than
`--lock-timeout` stops with `MigrationLockTimeout`.

## push

```bash
mizzle push --dry-run   # only print the SQL
mizzle push --force     # do not ask before applying
```

For development databases. In production, use `generate` and `migrate`.

## pull

```bash
mizzle pull                       # writes your schema module, or pulled_schema.py
mizzle pull --out db/legacy.py --force
```

Reads tables, enums, views, relations, row-level security and triggers from the database.

## check

Fails when the journal and the migration folders disagree. Warns when a `migration.sql`
was edited after `generate` (custom migrations are expected to be edited). Run it in CI.

## studio

```bash
mizzle studio --read-only --port 4983
```

The UI downloads on first run (verified by SHA-256). `--ui-path` uses a local build
instead, for offline machines.

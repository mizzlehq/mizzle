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
| `mizzle status` | Show which migrations are applied and which are pending. |
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
mizzle pull --update              # another app owns the schema: follow it
```

Reads tables, enums, views, relations, row-level security and triggers from the database.
`--update` rewrites your schema module and records the database's changes as a migration
that runs nothing ([Following a schema another app owns](guide/migrations.md#following-a-schema-another-app-owns)).

## check

Fails when the journal and the migration folders disagree. Warns when a `migration.sql`
was edited after `generate` (custom migrations are expected to be edited). Run it in CI.

## status

```bash
mizzle status
```

Lists every migration with its state: `applied`, `pending`, `applied (changed after it was
applied)`, or a name the database has but the folder does not. Set the database URL to
compare with the database; without it, the state is `unknown`.

```text
  0001_create_users   applied
  0002_add_posts      pending

1 applied, 1 pending.
```

## studio

```bash
mizzle studio --read-only --port 4983
```

The UI downloads on first run (verified by SHA-256). `--ui-path` uses a local build
instead, for offline machines. To prefetch the UI ahead of time (for example while
building an image), run `mizzle studio --download-ui`; it downloads and verifies the UI,
prints where it landed, and exits without starting the server.

Studio binds `127.0.0.1` and requires a bearer token (printed in the URL it opens), so only
someone at the machine can reach it. To use it remotely, bind another host and set credentials:

```bash
mizzle studio --host 0.0.0.0 --auth admin:$(cat /run/secrets/studio-password)
```

Binding a non-loopback host without `--auth` (or `--token` / `MIZZLE_STUDIO_TOKEN`) is
refused. Basic auth is over HTTP, so put it behind TLS (a reverse proxy) when the network is
not trusted.

In the browser, a table's **Structure** tab can create and alter tables and columns: add a
table, add or drop a column, change a column's type, nullability or default, and drop a
table. Studio never runs free-form SQL for this — it builds a small allow-listed operation,
generates the DDL with mizzle's own renderer, and shows it on a review screen before you
apply it. Column types and default expressions are checked, and every name is quoted.
`--read-only` still lets you review the generated SQL; it only blocks applying it.


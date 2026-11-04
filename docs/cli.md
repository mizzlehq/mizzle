# CLI

Run `mizzle <command> --help` for all options. Every command reads `mizzle.toml`, or
`[tool.mizzle]` in `pyproject.toml`; `--config` picks another file.

| Command | What it does |
|---|---|
| `mizzle init` | Set up mizzle in this project. |
| `mizzle generate` | Write a migration from schema changes. |
| `mizzle migrate` | Apply pending migrations. |
| `mizzle reset` | Drop every mizzle-managed object and re-run all migrations. |
| `mizzle squash` | Collapse the migration history into one migration. |
| `mizzle seed` | Run the seed scripts (`seeds/*.sql`) in order. |
| `mizzle push` | Sync the schema to the database directly (development). |
| `mizzle pull` | Write a schema module from an existing database. |
| `mizzle check` | Validate the migrations folder. |
| `mizzle status` | Show which migrations are applied and which are pending. |
| `mizzle info` | Environment, connectivity, pending count and schema drift. |
| `mizzle doctor` | Like `info`, but exits 1 when something is wrong (use in CI). |
| `mizzle drop` | Delete the latest (unapplied) migration. |
| `mizzle typegen` | Write row/insert/update TypedDicts. |
| `mizzle studio` | Browse and edit data in the browser. |

## generate

```bash
mizzle generate --name add_bio
mizzle generate --rename users=people             # a table rename, not drop + create
mizzle generate --rename users.name=full_name     # a column rename
mizzle generate --no-renames                      # treat changes as drop + create
mizzle generate --allow-data-loss                 # allow drops and narrowing types
mizzle generate --custom --name backfill          # empty migration for your own SQL
mizzle generate --sql                             # print the SQL, write nothing
mizzle generate --custom --name backfill --template header.sql
mizzle generate --custom --name backfill --sql    # print the header only, write nothing
```

`--template <file>` seeds the body of a `--custom` migration with that file's contents. With
`--sql`, nothing is written to disk (not even a `--custom` folder) and the SQL is printed.

When a column or table is gone and a new one appears, mizzle asks if it is a rename (on a
terminal), or stops and tells you which `--rename` to pass (in CI).

In a `--custom` migration, separate statements with `-- mizzle:breakpoint`.

## migrate

```bash
mizzle migrate
mizzle migrate --lock-timeout 5   # seconds to wait for another server's migration (default 60)
mizzle migrate --dry-run          # run every check, print the SQL, apply nothing
mizzle migrate --to 0003_add_posts
```

`--dry-run` performs the same checks as a real run (an edited applied migration, migrations
missing from disk after a squash, a pending migration older than an applied one) and exits
non-zero on the same failures; it only skips sending the statements.

`--to <name>` stops after that migration. The target is matched by exact folder name or by
exact number, never by prefix: `--to 3` means migration 0003, not `0031_...`. When the
target was already applied, `migrate --to` says so instead of claiming everything is
up to date.

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

```bash
mizzle check
mizzle check --fix   # repair journal and prev_id inconsistencies
```

Fails when the journal and the migration folders disagree. Warns when a `migration.sql`
was edited after `generate` (custom migrations are expected to be edited). Run it in CI.

`--fix` rewrites the journal where it can: a missing `prev_id`, a duplicate number it can
resolve unambiguously. It never touches an entry that may already be applied, refuses
ambiguous branch cases instead of guessing, and prints every change it makes.

## status

```bash
mizzle status
mizzle status --json    # machine-readable
mizzle status --check   # exit 1 when a migration is pending (for deploy gates)
```

Lists every migration with its state: `applied`, `pending`, `applied (changed after it was
applied)`, or a name the database has but the folder does not. Set the database URL to
compare with the database; without it, the state is `unknown`, and the counts only include
migrations whose state is known.

`--check` exits 1 when anything is pending, and also when an applied migration was edited
after it was applied. `--json` prints
`{"migrations": [...], "applied": n, "pending": n, "missing": n}`.

```text
  0001_create_users   applied
  0002_add_posts      pending

1 applied, 1 pending.
```

## reset

```bash
mizzle reset --database shop --force
```

Drops every object mizzle manages (from the latest snapshot) and re-runs all migrations.
Only mizzle-managed objects are dropped: unmanaged tables in the same schema, installed
extensions and `public`'s owner and grants survive. The migrations table is always cleared,
so every migration runs again even when its schema is outside `schema_filter`.

`reset` is destructive, so it asks first and shows the host and database. `--force` skips
the prompt but still requires `--database <name>` matching the database in the URL, so
`DATABASE_URL=<production> mizzle reset --force` cannot wipe production by accident. It
takes the same advisory lock as `migrate`, so a concurrent `migrate` waits instead of
failing.

## squash

```bash
mizzle squash --name initial
```

Replaces the applied history with one migration holding the latest snapshot. It refuses to
run when the range contains custom (hand-written) migrations, because their SQL would be
lost; move that SQL into the squashed migration by hand first.

Databases that already applied the old migrations are not broken: the squashed migration
records the names it `supersedes`, and `migrate` treats it as applied for them.

## seed

```bash
mizzle seed
```

Runs `seeds/*.sql` in filename order. Each file commits on its own, so a failure in the
second file keeps the first file's rows; re-running is not idempotent. This is a script
runner, not a data generator — write the SQL you want.

## info and doctor

```bash
mizzle info
mizzle doctor   # exit 1 when something is wrong
```

Both print the environment, whether the database is reachable, how many migrations are
pending and whether the schema has drifted. `doctor` exits non-zero when a migration is
pending, an applied migration was edited, or the schema drifts; use it as a CI gate.

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
# The password comes from the environment, not argv: `ps` shows command lines.
export MIZZLE_STUDIO_AUTH="admin:$(cat /run/secrets/studio-password)"
mizzle studio --host 0.0.0.0
```

Binding a non-loopback host without `--auth` / `MIZZLE_STUDIO_AUTH` (or
`--token` / `MIZZLE_STUDIO_TOKEN`) is refused. A token used for a remote bind must be at
least 32 characters; omit `--token` to have one generated.

Failed logins are throttled: after 10 wrong credentials within a minute the server answers
`429` with `Retry-After` and refuses every attempt for 30 seconds, including a correct one.
A successful login clears the count. Basic auth is over HTTP, so put
it behind TLS (a reverse proxy) when the network is not trusted.

Behind that proxy, set `MIZZLE_STUDIO_ORIGIN` to the public origin (comma-separated for
several) so Studio's cross-site request check accepts the browser's `Origin`:

```bash
export MIZZLE_STUDIO_ORIGIN="https://studio.example.com"
```

Without it, a request that carries an `Origin` header is refused, which fails closed.

Studio refuses cross-site requests: a state-changing call must be `Content-Type:
application/json` and must not come from another site (`Sec-Fetch-Site` / `Origin`). This
matters because a browser attaches cached Basic credentials to a form post from any page.

Read-only mode blocks writes and also refuses statements that could leave the read-only
transaction or act on the server: `BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT`, `SET`/`RESET`,
`COPY`, `DO`, `NOTIFY`, `pg_read_file`, `pg_terminate_backend` and similar. That blocklist
cannot cover everything a superuser can do, so a read-only Studio started on a superuser
connection prints a warning: use a non-superuser role.

CSV export prefixes a cell starting with `=`, `+`, `-` or `@` with an apostrophe, so a
spreadsheet treats it as text rather than a formula. JSON export is unaltered.

In the browser, a table's **Structure** tab can create and alter tables and columns: add a
table, add or drop a column, change a column's type, nullability or default, and drop a
table. Studio never runs free-form SQL for this — it builds a small allow-listed operation,
generates the DDL with mizzle's own renderer, and shows it on a review screen before you
apply it. Column types and default expressions are checked, and every name is quoted.
`--read-only` still lets you review the generated SQL; it only blocks applying it.


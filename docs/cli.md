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
mizzle generate --rename app.users=archive.people  # a move to another schema (and rename)
mizzle generate --no-renames                      # treat changes as drop + create
mizzle generate --allow-data-loss                 # allow drops and narrowing types
mizzle generate --custom --name backfill          # empty migration for your own SQL
mizzle generate --sql                             # print the SQL, write nothing
mizzle generate --custom --name backfill --template header.sql
mizzle generate --custom --name backfill --sql    # print the header only, write nothing
```

`--template <file>` seeds the body of a `--custom` migration with that file's contents. With
`--sql`, nothing is written to disk (not even a `--custom` folder) and the SQL is printed.

When a column or table is gone and a new one appears, also in another schema, mizzle asks
if it is a rename (on a terminal), or stops and tells you which `--rename` to pass (in CI).
A `--rename` that matches nothing is an error, so a typo cannot turn into a drop.

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
target is already applied, `migrate --to` says so instead of claiming everything is
up to date (later migrations may still be pending).

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

`--fix` rewrites the journal where it can: it adds a migration folder the journal is
missing, drops an entry whose folder is gone, and re-links a `prev_id` when the migration
it was generated on has the same schema as the one now before it (for example a custom
migration merged in between). It prints every change it makes, and refuses instead of
guessing:

- two folders with the same number: rename one first;
- a migration generated on another branch, whose snapshot lacks the changes now before it:
  re-linking it would make the next `generate` repeat those changes, so delete it and run
  `generate` again on the merged history;
- it never refreshes a recorded hash, so a `migration.sql` edited after `generate` keeps
  its warning (and `squash` keeps refusing it);
- a `snapshot.json` that is missing: restore it from version control first.

A folder it adds whose snapshot does not change the schema (a `--custom` migration from
another branch) is recorded as custom, so `squash` keeps its SQL.

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
`{"migrations": [...], "applied": n, "pending": n, "missing": [names]}`, where `missing`
lists the applied migrations that are not on disk.

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

`reset` is destructive, so it shows the host and database and asks you to type the
database name. The URL must name the database (`postgresql://host/NAME`). Without a terminal (CI) it refuses unless you pass `--force`. `--force`
skips the prompt but still requires `--database <name>` matching the database in the URL,
so `DATABASE_URL=<production> mizzle reset --force` cannot wipe production by accident. It
takes the same advisory lock as `migrate`, so a concurrent `migrate` waits instead of
failing.

The drops, the history reset and the re-run of the migrations happen in one transaction:
if anything fails, for example an unmanaged view that depends on a managed table, or a
custom migration whose SQL creates an object reset does not drop, everything is rolled back
and the database is unchanged. When a migration cannot run in a transaction (adding an enum
value), the transaction ends just before it, and the rest run after. `reset` says so before
it asks. A failure from there on cannot be rolled back: `reset` stops, says how many
migrations it re-ran, and you fix the cause and run `mizzle migrate`. With hand-written SQL
in the history, `reset` refuses that case up front; drop and recreate the database instead.

A migration that contains `BEGIN`, `COMMIT`, `ROLLBACK` or other transaction control is
refused: mizzle runs each migration in a transaction, and such a statement would end it.

## squash

```bash
mizzle squash --name initial
```

Replaces the applied history with one migration holding the latest snapshot. It refuses to
run when the history holds hand-written SQL, because that SQL would be lost: a custom
migration with statements, or a generated `migration.sql` edited after `generate` (the
edit `check` warns about). Take that SQL out, squash, then add it to the squashed migration
by hand.

Databases that already applied the old migrations are not broken: the squashed migration
records every name it `supersedes`, including those an earlier squash replaced, and
`migrate` treats it as applied for a database that applied that whole history. Each squash
gets a name no earlier migration used (`0001_squash`, then `0001_squash_2`, ...).

A database that applied only part of the history (say 0001 of 2, or 0001 and 0003 but not
0002) can neither adopt the squash nor run it: `squash` refuses when the database URL is
set, `migrate`, `migrate --dry-run` and `status` refuse with an error, and `info` and
`doctor` report it. Migrate it to the end of the old history first.

`squash` also refuses while a migration folder is missing from the journal (a merged
branch): run `mizzle check --fix` first, so its SQL is not deleted.

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
`--token` / `MIZZLE_STUDIO_TOKEN`) is refused. A token from `--token` or
`MIZZLE_STUDIO_TOKEN` must have at least 32 non-space characters, on any host and also when
`--auth` is set (the bearer token is accepted alongside Basic auth); omit it to have one
generated. The Basic auth password must have at least 12 non-space characters.

Failed logins are throttled per client address: after 10 different wrong credentials within
a minute, that client gets `429` with `Retry-After` for every API request, a correct one
included, until the oldest failure is a minute old. Only new guesses count: a request with no
credential, or the same wrong one again (an old tab after a Studio restart), does not.
Requests a browser marks as coming from another site are refused before the login check and
never count, so a web page you visit cannot lock out a local Studio either.

The client address is the TCP peer. On a loopback bind every local client shares
`127.0.0.1`, so another local user who guesses wrong can make you wait a minute. Behind a
reverse proxy, set `FORWARDED_ALLOW_IPS` to the proxy's address (`127.0.0.1` for a proxy on
the same host): its `X-Forwarded-For` then gives each client its own address. Without it,
`X-Forwarded-For` is ignored and every client shares the proxy's budget. Basic auth is over
HTTP, so put it behind TLS (a reverse proxy) when the network is not trusted.

A browser request that carries an `Origin` header must come from the page Studio served: on
a remote bind, its origin must match the `Host` the request was sent to. If the proxy changes
the `Host` header, set `MIZZLE_STUDIO_ORIGIN` to the public origin (comma-separated for
several):

```bash
export MIZZLE_STUDIO_ORIGIN="https://studio.example.com"
```

Studio refuses cross-site requests: a state-changing call must be `Content-Type:
application/json` and must not come from another site (`Sec-Fetch-Site` / `Origin`). This
matters because a browser attaches cached Basic credentials to a form post from any page.

`--read-only` needs a non-privileged database role. Read-only mode runs SQL in a
`READ ONLY` transaction, and that stops writes to tables but not what a privileged role can
do on the server (read files, run programs, signal other backends). So read-only Studio
refuses to start when the login role, or the role it switches to, is a superuser, has
`REPLICATION`, or is a member of `pg_read_server_files`, `pg_write_server_files`,
`pg_execute_server_program`, `pg_signal_backend` or `pg_maintain`, or has a direct
`EXECUTE` grant on a server-side function such as `pg_read_file`. Create a plain role for
it, used by nothing else: any role may cancel or terminate the sessions of its own role, so
a role the application also uses lets read-only Studio stop the application's sessions. For example:

```sql
CREATE ROLE studio_ro LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA public TO studio_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO studio_ro;
```

As defence in depth, read-only mode also refuses statements that could leave the read-only
transaction (`BEGIN`/`START`/`COMMIT`/`ROLLBACK`/`SAVEPOINT`/`PREPARE`, `SET`/`RESET`, ...),
statements that rewrite or lock tables (`CLUSTER`, `REINDEX`, `ANALYZE`, `VACUUM`) and a list of
server-side functions (`pg_read_file`, `pg_notify`, replication slot functions, ...). That
list is not the security control and cannot be complete; the role is.

The SQL console keeps a query history and saved queries in `.mizzle/studio.json` in the
project folder. The history holds the SQL as you typed it, so a statement with a password
lands there: the file is readable only by you, and `.mizzle/` ignores itself in git. Clear
the history from the console, or delete the file.

CSV export prefixes a text cell starting with `=`, `+`, `-` or `@` (also after leading
whitespace) with an apostrophe, so a spreadsheet treats it as text rather than a formula.
Numbers, intervals, dates and times are left as they are. JSON export is unaltered.

In the browser, a table's **Structure** tab can create and alter tables and columns: add a
table, add or drop a column, change a column's type, nullability or default, and drop a
table. Studio never runs free-form SQL for this — it builds a small allow-listed operation,
generates the DDL with mizzle's own renderer, and shows it on a review screen before you
apply it. Column types and default expressions are checked, and every name is quoted.
`--read-only` still lets you review the generated SQL; it only blocks applying it.


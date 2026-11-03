# Changelog

## 0.2.0

**Read replicas**

- `Database.connect(url, replicas=[...])` opens a pool to each read replica. Plain selects
  and relational queries go to the replicas in turn. Writes, `FOR UPDATE`, raw `execute()`,
  transactions and migrations always use the primary.
- `db.primary` sends a query to the primary, for reading your own writes while replicas lag:
  `await db.primary.select().from_(Users)`.
- If a replica cannot give a connection, the read goes to the primary.
- `FOR UPDATE` anywhere in a query (also in a CTE or subquery) sends it to the primary.
  Raw `sql(...)` inside a select still reads from a replica: use `db.primary` for functions
  that write, such as `nextval()`.

**Pool settings**

- `pool_min_size` (default 1) and `max_idle` (seconds before an unused connection closes,
  default 300) join `pool_size`.

**Query timeouts**

- `query_timeout=` on `Database.connect` cancels any statement that runs longer.
- `db.with_timeout(seconds)` changes it for some queries, also inside a transaction.
- A timeout raises the new `QueryTimeout` error. Postgres `statement_timeout` raises it too.

**Retries**

- `await db.retry(fn, attempts=3)` runs `fn` again on `SerializationFailure` (serialization
  conflicts and deadlocks), with backoff. `fn` should open its own transaction.

**Speed**

- Faster row decoding, query building and bulk inserts. Against Tortoise ORM, mizzle is
  1.2–2.3x faster across the benchmark in `benchmarks/bench.py`.
- Nested relational queries without a `limit` use simpler SQL.
- `reset_connections="auto"` (opt-in) skips the session reset after queries mizzle built
  itself: up to ~40% faster small queries. Keep the default `"always"` if triggers or
  functions change session settings.

**Lost connections**

- When the server closes connections (restart, failover, idle timeout), mizzle replaces the
  whole pool. A plain read is retried once on a fresh connection, so the app sees no error.
  Writes and raw SQL are never retried; they raise `ConnectionError`.
- asyncpg's low-level connection errors (`InterfaceError`, `InternalClientError`) now raise
  `ConnectionError` too.

**`mizzle pull`**

- Writes relation fields: `one()` for each foreign key (`userId` -> `user`) and the reverse
  `many()` (`comments`), or `one()` when the key is unique. Two keys to the same table, and
  keys to the table itself, get clear names (`notificationsBySender`) and `relation_name`.
  Names follow the table's camelCase or snake_case style.
- Enum class names keep their capitals (`NotificationStatus`), and literal enum defaults
  become members (`.default(NotificationStatus.UNREAD)`).
- `config()` is typed `list[Constraint]` (`Constraint` is now exported), not `list[Any]`.
- Columns of unknown types no longer generate a file that fails to import.
- Keeps fractional-second precision: `timestamp(3)` (what Prisma creates) becomes
  `timestamp(precision=3)`. Before, the next `generate` wanted to change those columns.
  `timestamp()` and `time()` take a new `precision=` argument.

**Fixes**

- `real` columns in nested relational rows came back slightly changed (75005392.0 as
  75005390.0).
- `interval` values in nested relational rows could be off by a microsecond, and intervals
  with months or years did not match a plain select.
- A subquery inside a relational query's `where` used the outer table's columns, so it could
  return wrong rows.
- Floats in nested relational rows depended on the server's `extra_float_digits`, and
  `date.min`/`date.max` (`-infinity`/`infinity`) failed to decode there.
- Returning a connection to the pool after a server restart could raise even though the
  statement had finished; it is now logged instead.

**Migrations**

- `on_delete="no action"`, `.unique()` on a primary key column, and columns of a composite
  primary key no longer produce the same migration again and again.
- Long generated constraint names keep their `_key`/`_fkey`/`_pkey` suffix, so two of them
  can no longer clash. Existing names longer than 63 bytes get a one-time `RENAME CONSTRAINT`.
- List defaults on array columns render valid SQL.
- Type changes work when the old default cannot be cast (the default is dropped and set
  again), when a dropped table's foreign keys pointed at the changed column, and when a key
  that a foreign key depends on is replaced.
- Adding two or more enum values before existing ones works.
- Date and time literal defaults no longer produce a migration every time: `datetime` with a
  time zone (PostgreSQL prints it in the session time zone) and any value with fractional
  seconds (`.500000` is printed as `.5`).

**Tested in CI**

- A real streaming read replica, and PgBouncer in transaction mode.
- Random values for every column type through select, returning, update and nested rows.
- Random filters, orderings, limits and aggregates, checked against a Python model of SQL.
- Cancellation, timeouts, overload, killed connections, deadlocks, serialization conflicts
  and racing upserts, with a check that no connection leaks.

## 0.1.1

- PyPI page: the README now shows the logo and its links work (they used repo-relative paths).
- A simpler README; development and release notes moved to `CONTRIBUTING.md`.

## 0.1.0

First release.

**ORM**

- Tables as Python classes (`@final class Users(Table)`), with typed rows for pyright and
  mypy and no code generation: `user.email` is a `str`, `user.bio` is `str | None`.
- SQL-like queries: `select`, `insert`, `update`, `delete`, joins (typed `left_join`), CTEs,
  subqueries, upserts, `returning()`, `for_update()` and prepared queries with
  `placeholder()`.
- Relational queries: `db.query(Users).find_many(with_={"posts": True})` loads nested rows in
  one SQL statement, with `columns=`, `where`, `order_by` and `limit` at every level.
- Raw SQL with `sql(...)`, including t-strings on Python 3.14; values are always bind
  parameters.
- Transactions with savepoints, isolation levels and safe concurrent use.
- Safe by default: `update`/`delete` need `.where()` or `.all_rows()`, and error messages and
  tracebacks never include row values.
- PostgreSQL types: enums, arrays, `jsonb`, `uuid`, `inet`, `numeric`, intervals and more.
- `mizzle.pydantic`: Pydantic models from tables, with `include=` allowlists for safe
  request bodies.

**CLI**

- `init`, `generate`, `migrate`, `push`, `pull`, `check`, `drop`, `typegen` and `studio`.
- Migrations are diffed from your schema, ask about renames, guard against data loss, and
  run locked and transactional, so many app servers can start at once.
- Config in `mizzle.toml` or `[tool.mizzle]`; the database URL comes from the environment
  or a `.env` file.

**Studio**

- Browse, filter and edit data in the browser; run SQL; inspect table structure.
- Runs on localhost with a one-time token; `--read-only` mode.
- Its UI downloads on first run (about 250 KB) into your virtual environment, verified by
  SHA-256.

Supports Python 3.12–3.14 and PostgreSQL 14–18.

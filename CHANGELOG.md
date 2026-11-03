# Changelog

## 0.2.0

**Read replicas**

- `Database.connect(url, replicas=[...])` opens a pool to each read replica. Plain selects
  and relational queries go to the replicas in turn. Writes, `FOR UPDATE`, raw `execute()`,
  transactions and migrations always use the primary.
- `db.primary` sends a query to the primary, for reading your own writes while replicas lag:
  `await db.primary.select().from_(Users)`.
- If a replica cannot give a connection, the read goes to the primary.

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

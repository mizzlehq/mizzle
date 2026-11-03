# Changelog

## 0.2.0

**Upgrading from 0.1**

- Generated constraint names longer than 63 bytes are now shortened with a hash of the
  full name and keep their `_key`/`_fkey`/`_pkey` suffix. The first `generate` after
  upgrading renames those constraints (nothing is dropped).
- Functions in snapshots are keyed by name and arguments (`public.add(a integer)`), so
  overloads are separate functions. Older snapshots are read as before: nothing changes.
- A recreated view now gets two more statements, which keep its privileges, owner and
  comment.
- `push` and `generate` now see non-btree indexes (GIN, GiST, HNSW...). One that exists in
  the database but not in the schema is dropped, like btree indexes always were.

**Queries**

- Arithmetic on number columns: `db.update(Items).set(stock=Items.stock - 1)`.
- `case_().when(cond, value).else_(default)`, `coalesce`, `nullif`, `greatest`, `least`,
  `cast(expr, text())`.
- Functions: `lower`, `upper`, `trim`, `length`, `concat`, `abs_`, `round_`, `now`,
  `current_date`, `date_trunc`, `extract`, and `func[T]("name", ...)` for any other
  function (the name is checked, values are always bound).
- `q1.union(q2)`, `union_all`, `intersect`, `intersect_all`, `except_`, `except_all`.
- Window functions: `row_number()`, `rank()`, `dense_rank()`, `lag`, `lead`,
  `first_value`... and aggregates with `.over(partition_by=..., order_by=..., frame=...)`.
- Table aliases for self-joins: `manager = alias(Employees, "manager")`.
- Full-text search: `to_tsvector`, `websearch_to_tsquery` (and friends), `ts_rank`,
  `matches(vector, query)`.
- `not_like`, `not_ilike`, `regex`, `not_regex`, `similar_to`, `not_similar_to`.
- Date and time arithmetic: `Events.at + timedelta(days=1)`, `Events.at - Events.started`
  (an interval), `date - date` (days). Typed: the result is `Expr[datetime]`,
  `Expr[timedelta]` or `Expr[int]`.
- `+` on text joins with `||`: `Users.first + " " + Users.last` (NULL gives NULL).
- Named windows: `q.window("w", partition_by=..., order_by=...)` and `.over("w")`.
- `ts_rank(v, q, weights=[...], normalization=n)` and `ts_rank_cd(...)`.
- Subqueries in FROM and JOIN: `sub = db.select({...}).from_(T).subquery("x")`, then
  `sub.c("n", int)`. Their columns (and CTE columns) decode like the inner column, so an
  enum comes back as the enum.
- `db.query(alias(Users, "u")).find_many(...)`.
- `order_by` after `union`/`intersect`/`except` takes expressions, not only columns.
- `db.insert(T).values(...).overriding_system_value()` (and `overriding_user_value()`) to
  set a `GENERATED ALWAYS` identity column.

**Columns**

- Identity columns: `integer().generated_always_as_identity()`,
  `.generated_by_default_as_identity(start=, increment=)`.
- Generated columns: `.generated_always_as(sql("..."))`, stored or virtual (PostgreSQL 18).
- `.on_update(fn)` and `.on_update_now()`: set on every `update()` and upsert, like
  Prisma's `@updatedAt`.
- pgvector: `vector(1536)`, `halfvec(n)`, `sparsevec(n)`, `l2_distance`, `cosine_distance`,
  `inner_product`; migrations create the extension.
- `tsvector()` and `tsquery()` columns.
- `timestamp()` and `time()` take `precision=`.
- `index(...).using("gin" | "hnsw" | ..., opclass)` and `.with_(...)` storage options.

**Database objects in migrations**

- Extensions: `extension("pg_trgm")`.
- Views and materialized views (`db.refresh(View)`), queryable like tables.
- Row-level security: `rls()` and `policy(...)` in `config()`.
- Roles: `role("admin", create_role=True)`; mizzle never drops roles it does not manage.
- Functions and triggers: `function(...)`, `trigger(...)`.
- Function overloads: functions with the same name and other `args` are separate
  functions.
- Function options: `strict=`, `leakproof=`, `parallel="safe"`, `cost=` and
  `settings={"search_path": "public, pg_temp"}`. SQL-standard bodies (`RETURN a + b`,
  `BEGIN ATOMIC ... END`) are written as is, without quotes.
- Triggers: `update_of=[Items.price]` (`UPDATE OF price`), transition tables
  (`new_table="added"`, `old_table=`), and `INSTEAD OF` triggers on views
  (`trigger(..., on=SomeView, timing="instead of")`).
- Grants: `grant(["select", "insert"], to=[reader])` in a table's or view's `config()`.
  Migrations grant what is declared and revoke what was declared before. `push` never
  revokes from roles the schema does not name. When a view is dropped and created again,
  its privileges, owner and comment are kept, also grants made outside the schema.
- Enums in another schema: `pg_enum(Mood, schema="app")`.
- Identity options: `min_value=`, `max_value=`, `cache=` and `cycle=`, next to `start=` and
  `increment=`.
- Index operator classes per key: `.using("btree", [None, "text_pattern_ops"])`.
- `mizzle.supabase`: Supabase roles, `auth_uid()`, `auth_jwt()`, `auth_role()`, and
  `crud_policy(...)` like Drizzle's.

**Bulk and streaming**

- `async for row in query.stream(batch_size=1000)` reads any number of rows with little
  memory (selects and relational queries).
- `db.copy_into(Users, rows)` loads rows with COPY (~3x faster than INSERT);
  `db.copy_from(query, "out.csv")` exports.
- `db.insert_many(Users, rows)` splits big inserts into batches in one transaction.
- `db.listen("channel")` and `db.notify("channel", payload)`.
- `listen(..., reconnect=True)` listens again after a lost connection and yields a
  `Reconnected` event first, so the gap is never silent.
- `listen(..., max_queue=n, overflow=...)`: `"raise"` (`ListenerOverflow`), `"drop_oldest"`
  or `"block"` (stop reading until the consumer catches up; nothing is lost).
- Through a connection pooler `listen()` raises `DatabaseError`: PgBouncer in transaction
  or statement mode drops notifications. If the pooler runs in session mode, pass
  `listen(..., pooler_session_mode=True)`; listen() then checks that a test notification
  arrives.
- `insert_many(..., on_conflict=db.insert(T).on_conflict_do_update(...))` upserts in
  batches: the template `Insert` gives the conflict clause.

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
- Writes grants, function overloads and options, SQL-standard bodies, trigger columns,
  transition tables and view triggers, enums in other schemas, identity options and
  operator classes per key.
- Keeps fractional-second precision: `timestamp(3)` (what Prisma creates) becomes
  `timestamp(precision=3)`. Before, the next `generate` wanted to change those columns.
  `timestamp()` and `time()` take a new `precision=` argument.

**Fixes**

- `mizzle check` no longer warns that a `--custom` migration was "edited after generate":
  those are written by hand on purpose.

- A delete blocked by an `on_delete="restrict"` foreign key raised a plain `DatabaseError`;
  it is now a `ForeignKeyViolation`. `DatabaseError.sqlstate` gives the server's code (for
  example `"42501"` when row-level security or a grant denies access).
- `Expr` and `Condition` are exported, for typing your own helpers.

- `copy_from` wrote floats rounded to 15 digits; it now writes them exactly. `$1` inside
  quoted text or comments of a raw query is no longer replaced, and `timeout` covers the
  whole export.
- `timestamptz` `infinity` and `-infinity` come back with a time zone, like other values.
- Vectors accept any object with `tolist()` (numpy arrays).
- `SET EXPRESSION` on PostgreSQL before 17, and VIRTUAL columns before 18, raise an error
  that names the needed version.
- `insert_many()` and `copy_into()` with a value for a `GENERATED ALWAYS` identity column
  say how to set it.

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
- Moving a table to another schema is `ALTER TABLE ... SET SCHEMA` (its data stays), when
  the name and columns match: `--rename app.users=archive.users`, or answer the prompt.
  `--no-renames` drops and creates it.
- Making a column generated, switching it between STORED and VIRTUAL, or making a VIRTUAL
  column a plain one drops the column and adds it again (`--allow-data-loss`). Its indexes
  and constraints, and other tables' foreign keys to it, are created again (also a foreign
  key renamed in the same migration).
- A new generated expression uses `SET EXPRESSION` on PostgreSQL 17+. Older servers drop the
  column and add it again in the same statement, and keep its indexes, constraints and
  foreign keys.
- Widening a type no longer asks for `--allow-data-loss`: `varchar(40)` to `varchar(64)`,
  `varchar` or `text`, `numeric(10,2)` to `numeric(12,2)`, `smallint` to `integer` to
  `bigint`, `real` to `double precision`. Narrowing still asks.
- A table replaced by a view of the same name is dropped before the view is created.
- A table dropped while another one takes over its index names (a rename with
  `--no-renames`) is dropped first.
- Defaults that are SQL keywords (`current_user`, `current_timestamp(3)`, `localtime`...)
  no longer make `push` change them on every run.
- Naming an index's default operator class (`text_ops`) no longer produces a migration
  every time.
- Two long generated names with the same first 63 bytes no longer get the same name.
- Changing the type of a renamed column that a trigger uses drops and creates the trigger
  around the change.
- Date and time literal defaults no longer produce a migration every time: `datetime` with a
  time zone (PostgreSQL prints it in the session time zone) and any value with fractional
  seconds (`.500000` is printed as `.5`).

**Tested in CI**

- A real streaming read replica, and PgBouncer in transaction and session mode.
- `examples/shop`: a FastAPI marketplace that uses every 0.2 feature, run on the primary,
  the replica, PgBouncer and pgvector, and through a primary restart under load.
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

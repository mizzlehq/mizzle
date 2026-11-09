# Production

```python
db = await Database.connect(
    "postgresql://...",
    schema=[Users],
    replicas=["postgresql://replica-1/...", "postgresql://replica-2/..."],
    pool_size=20,
    query_timeout=5,  # seconds; slow queries raise QueryTimeout
)

await db.select().from_(Users)  # reads go to the replicas, in turn
await db.primary.select().from_(Users)  # read your own write right away
await db.retry(transfer)  # re-run a transaction on a serialization conflict
```

- Writes, transactions and `FOR UPDATE` always use the primary. A replica that goes down
  while the app runs is skipped; every replica must be reachable when `connect()` runs.
- A lost connection is replaced. A read that fails because the connection dropped is tried
  once more.
- Database errors map to mizzle errors (`UniqueViolation`, `ForeignKeyViolation`,
  `NotNullViolation`, `QueryTimeout`, `ConnectionError`...). `DatabaseError.sqlstate` holds
  the PostgreSQL code. Error messages never include your parameter values.

## Connection reset

When a connection goes back to the pool, mizzle resets its session state (`RESET ALL`,
`UNLISTEN *`, advisory locks, cursors, temp tables) only after a statement that may have changed it: raw
`sql(...)`, a function you name with `func()`, or direct connection use. After
the statements mizzle builds itself it skips the reset, which saves a round trip per query
(2x on a real network). Open transactions are always rolled back.

mizzle cannot see inside the database: if a trigger or function changes the session itself
(`SET` or `set_config(..., false)` without `LOCAL`, a session advisory lock, a temp table),
reset every time:

```python
db = await Database.connect(url, reset_connections="always")
```

## PgBouncer

- **Transaction mode:** pass `statement_cache_size=0`.
- **Session mode:** works as is.

## Schemas other roles can write to

mizzle's own SQL (studio, migrations, introspection) calls PostgreSQL's built-in functions
as `pg_catalog.<name>`, so a function another role creates in `public` cannot stand in for
them. Your queries, and the asyncpg driver's own type lookups, use the search path as
written. Do not let untrusted roles create objects in a schema on the app's or the
migrator's search path: this is the default from PostgreSQL 15; on older servers run
`REVOKE CREATE ON SCHEMA public FROM PUBLIC`.

## Memory

On glibc, a busy process can keep freed memory, so its RSS grows slowly and then levels
off. This is not a leak. To cap it, set `MALLOC_ARENA_MAX=2` or use jemalloc.

## What CI tests

On every change: PostgreSQL 14 to 18, Python 3.12 to 3.14, a real streaming replica,
PgBouncer in transaction and session mode, a TLS-only server with unusual settings, server
restarts, pgvector, and `examples/shop` under load.

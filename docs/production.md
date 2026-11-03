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

- Writes, transactions and `FOR UPDATE` always use the primary. A replica that is down is
  skipped.
- A lost connection is replaced. A read that fails because the connection dropped is tried
  once more.
- Database errors map to mizzle errors (`UniqueViolation`, `ForeignKeyViolation`,
  `NotNullViolation`, `QueryTimeout`, `ConnectionError`...). `DatabaseError.sqlstate` holds
  the PostgreSQL code. Error messages never include your parameter values.

## PgBouncer

- **Transaction mode:** pass `statement_cache_size=0`. `db.listen()` refuses to run here,
  since notifications would be lost: connect `listen()` directly to PostgreSQL.
- **Session mode:** works as is. `db.listen(..., pooler_session_mode=True)` is allowed.

## Memory

On glibc, a busy process can keep freed memory, so its RSS grows slowly and then levels
off. This is not a leak. To cap it, set `MALLOC_ARENA_MAX=2` or use jemalloc.

## What CI tests

On every change: PostgreSQL 14 to 18, Python 3.12 to 3.14, a real streaming replica,
PgBouncer in transaction and session mode, a TLS-only server with unusual settings, server
restarts, pgvector, and `examples/shop` under load.

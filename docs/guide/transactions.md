# Transactions and errors

The examples use these two tables:

```python
@final
class Accounts(Table, name="txdoc_accounts"):
    id = serial().primary_key()
    owner = text().not_null().unique()
    balance = integer().not_null().default(0)
    entries = many(lambda: Entries)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [check("txdoc_balance_not_negative", sql("balance >= 0"))]


@final
class Entries(Table, name="txdoc_entries"):
    id = serial().primary_key()
    account_id = integer().not_null().references(lambda: Accounts.id)
    amount = integer().not_null()
    account = one(
        lambda: Accounts, fields=lambda: [Entries.account_id], references=lambda: [Accounts.id]
    )
```

## db.transaction()

```python
async with db.transaction() as tx:
    await tx.update(Accounts).set(balance=Accounts.balance - 10).where(eq(Accounts.id, ann.id))
    await tx.update(Accounts).set(balance=Accounts.balance + 10).where(eq(Accounts.id, bob.id))
# committed here; an exception in the block rolls both updates back
```

`tx` is a `Session`: it has the same query methods as `db` (`select`, `insert`, `query`,
`execute`...), all on one connection. Queries on `db` inside the block run outside the
transaction, on other connections.

Statements on one `tx` never overlap, so `asyncio.gather` on it is safe; they run one
after another. While a savepoint is open, only the code inside it (and tasks it starts) may
use the transaction: a statement from another task would be undone with the savepoint, so
it raises `QueryError` instead.

```python
async with db.transaction() as tx:
    accounts, entries = await asyncio.gather(
        tx.select().from_(Accounts), tx.select().from_(Entries)
    )
```

## Savepoints

`tx.transaction()` inside a transaction is a `SAVEPOINT`. An error rolls back to it, and
the outer transaction goes on:

```python
async with db.transaction() as tx:
    await tx.insert(Entries).values(account_id=ann.id, amount=-500)
    try:
        async with tx.transaction() as sp:  # SAVEPOINT
            await sp.update(Accounts).set(balance=Accounts.balance - 500).where(
                eq(Accounts.id, ann.id)
            )
    except CheckViolation:
        pass  # only the savepoint rolled back; the entry is still there
```

### Side effects after commit

`transaction(after_commit=..., after_rollback=...)` runs a callback outside the transaction,
so a write that did not commit does not send an email or enqueue a job:

```python
async with db.transaction(after_commit=notify_warehouse) as tx:
    await tx.insert(Orders).values(...)
```

Two rules keep hook errors from confusing the outcome:

- In a **savepoint**, `after_commit` is queued, not run: releasing a savepoint is not a
  commit, and the outer transaction can still roll back. Queued hooks run after the
  outermost `COMMIT`, and are dropped if either level rolls back. In the same way, a
  savepoint's `after_rollback` fires if an enclosing savepoint or the outer transaction
  rolls back even though the savepoint itself was released. Rollback drops only the hooks
  of the savepoint that failed, not those of siblings that already released.
- An `after_commit` hook that raises raises `AfterCommitError`. The data **is** committed:
  the message says so, so a retry does not insert it twice. Every queued hook still runs;
  one failure does not skip the rest. The first failure is the error's `__cause__`; later
  ones are only logged. An `after_rollback` hook that raises does not hide the error that
  caused the rollback: the original error is raised. When it had no visible context, its
  `__context__` is an `AfterRollbackError` whose `__cause__` is the hook's failure (with its
  traceback); otherwise a note on the original names the failure.
- A cancel or timeout that arrives after `COMMIT`, while the connection goes back to the
  pool, still propagates, but the `after_commit` hooks run, the `after_rollback` hooks do
  not, and a note on the error says the transaction was committed. Do not retry it.
- Savepoints on one transaction run one at a time, from the task that opened them. A
  savepoint started from another task while one is open raises `QueryError`.
- A statement that fails aborts the whole transaction in PostgreSQL, even if you catch the
  error. Leaving the block then raises `DatabaseError` ("aborted the transaction"), the
  `after_rollback` hooks run, and nothing is committed. To go on after an expected failure,
  run the statement in a savepoint (`tx.transaction()`).

## Isolation levels and read-only

```python
async with db.transaction("serializable") as tx:
    total = await tx.select(Accounts.balance).from_(Accounts)
async with db.transaction("repeatable read", readonly=True) as tx:
    report = await tx.select().from_(Accounts)
```

The levels are `"read committed"` (PostgreSQL's default), `"repeatable read"` and
`"serializable"`. They apply to the outermost transaction only: passing one to a nested
`transaction()` raises `QueryError`. A write in a `readonly=True` transaction raises
`DatabaseError` with `sqlstate` `"25006"`.

## Settings and roles for one transaction

mizzle has no special API for this; run the SQL with `tx.execute()`. `SET LOCAL` and
`set_config(..., true)` end with the transaction, so they never leak to the next user of
the pooled connection:

```python
async with db.transaction() as tx:
    await tx.execute(sql("SET LOCAL lock_timeout = '2s'"))
    await tx.execute(sql("SELECT set_config('app.user_id', {}, true)", "42"))
    rows = await tx.execute(sql("SELECT current_setting('app.user_id') AS uid"))
# both settings end with the transaction
```

`SET LOCAL ROLE some_role` works the same way. That is how row-level security policies
see the current user; see [Migrations](migrations.md#row-level-security-and-policies).

## Retrying serialization failures

Under `"serializable"` or `"repeatable read"`, PostgreSQL aborts a transaction that
conflicts with another one. A deadlock aborts one of the two. Both raise
`SerializationFailure`, and the fix is to run the whole transaction again:

```python
async def transfer() -> None:
    async with db.transaction("serializable") as tx:
        [src] = await tx.select().from_(Accounts).where(eq(Accounts.id, ann.id))
        await tx.update(Accounts).set(balance=src.balance - 1).where(eq(Accounts.id, ann.id))
        await tx.update(Accounts).set(balance=Accounts.balance + 1).where(
            eq(Accounts.id, bob.id)
        )

await db.retry(transfer)  # up to 3 attempts on SerializationFailure
await db.retry(transfer, attempts=5, backoff=0.1)
```

`retry` waits `backoff` seconds (doubled each attempt, with jitter) and raises the last
`SerializationFailure` when attempts run out. Other errors are raised at once. The
function must open its own transaction: calling `retry` inside one raises `QueryError`.

## Row locks

```python
async with db.transaction() as tx:
    [acct] = await tx.select().from_(Accounts).where(eq(Accounts.id, ann.id)).for_update()
    await tx.update(Accounts).set(balance=acct.balance - 5).where(eq(Accounts.id, acct.id))

# a job queue: each worker takes rows no other worker has locked
async with db.transaction() as tx:
    batch = await (
        tx.select().from_(Accounts).order_by(Accounts.id).limit(10).for_update(skip_locked=True)
    )
```

`for_update(nowait=True)` fails at once when a row is locked, instead of waiting:

```python
await tx.select().from_(Accounts).where(eq(Accounts.id, ann.id)).for_update(
    nowait=True
)
```

It raises `DatabaseError` with `sqlstate` `"55P03"`. `for_update(of=[Accounts])` locks
only that table's rows in a join. Locks are held until the transaction ends, so
`for_update()` belongs inside `db.transaction()`. A query with `FOR UPDATE` always runs on
the primary, never on a replica.

## Timeouts

`query_timeout=` on `Database.connect` sets a limit for every statement
([Production](../production.md)). `with_timeout()` changes it for some queries, also on a
transaction:

```python
slow = db.with_timeout(0.5)
try:
    await slow.execute(sql("SELECT pg_sleep(2)"))
except QueryTimeout:
    pass
```

The statement is cancelled on the server. `with_timeout(None)` goes back to the pool's
default. PostgreSQL's own `statement_timeout` (for example `SET LOCAL statement_timeout`)
raises `QueryTimeout` too.

## Errors

Every mizzle error is a `MizzleError`:

```python
try:
    await db.insert(Accounts).values(owner="ann")
except MizzleError:
    pass
```

Errors the database raises are `DatabaseError`s. They carry the SQL (`exc.sql`), the
driver's exception (`exc.original`), and `exc.sqlstate`, PostgreSQL's
[error code](https://www.postgresql.org/docs/current/errcodes-appendix.html). Constraint
errors also name the constraint or column:

```python
try:
    await db.insert(Accounts).values(owner="ann")
except UniqueViolation as exc:
    print(exc.constraint)  # txdoc_accounts_owner_key
```

Messages never include your values: the server's detail line
(`Key (owner)=(ann) already exists`) is left out.

| Error | Raised when |
|---|---|
| `DatabaseError` | The database rejected a statement. The base of the errors below; other codes (permission denied `42501`, lock not available `55P03`...) raise it as is. |
| `UniqueViolation` | A unique constraint or index (`23505`). `exc.constraint` names it. |
| `ForeignKeyViolation` | A referenced row is missing, or a delete or update is blocked by a foreign key (`NO ACTION`, `RESTRICT`). `exc.constraint`. |
| `NotNullViolation` | A `NOT NULL` column got `NULL`. `exc.column` names it. |
| `CheckViolation` | A `CHECK` constraint failed. `exc.constraint`. |
| `SerializationFailure` | A serialization conflict (`40001`) or a deadlock (`40P01`). Safe to retry: use `db.retry`. |
| `QueryTimeout` | The statement ran longer than its timeout and was cancelled. |
| `ConnectionError` | No connection could be made or taken from the pool, or the connection was lost. A plain read is retried once on a new connection first; writes and raw SQL are never retried. |
| `QueryError` | A query cannot be built: `update`/`delete` without `.where()` (use `.all_rows()` to mean every row), `retry()` or `listen()` inside a transaction, isolation on a nested transaction, `batch_size < 1`... |
| `RelationNotLoaded` | You read a relation the query did not load: add it to `with_`. |
| `SchemaError` | A table definition is invalid, for example a table class with no columns. Raised when the class is defined. |
| `ListenerOverflow` | A `listen()` consumer fell `max_queue` notifications behind. See [Bulk and streaming](bulk.md#listen-and-notify). |
| `MigrationError` | The migration kit cannot go on: applied migrations missing on disk, a migration out of order, `push` without `--force` off a terminal... |
| `MigrationLockTimeout` | `migrate` waited longer than `--lock-timeout` for another run's lock. |
| `MigrationTampered` | A `migration.sql` changed after it was applied. |
| `DataLossBlocked` | `generate` or `push` would lose data and `--allow-data-loss` was not passed. |

Because `ConnectionError` shares its name with Python's built-in, import it from mizzle
explicitly: `from mizzle import ConnectionError`.

```python
try:
    await db.delete(Accounts)  # no .where()
except QueryError:
    pass
await db.update(Entries).set(amount=0).all_rows()  # every row, on purpose
```

```python
[entry] = await db.query(Entries).find_many()
try:
    print(entry.account)
except RelationNotLoaded:
    pass  # Entries.account was not loaded; add 'account' to with_
[entry] = await db.query(Entries).find_many(with_={"account": True})
if entry.account is not None:  # one() is Accounts | None
    print(entry.account.owner)
```

```python
try:
    await Database.connect("postgresql://mizzle:mizzle@localhost:1/mizzle")
except ConnectionError:
    pass
```

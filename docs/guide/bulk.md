# Bulk, COPY and LISTEN

Loading many rows, exporting them, reading big results without holding them in memory,
and notifications with LISTEN / NOTIFY. The examples use a `Users` table (`id`, `email` unique, `name`) and a
`Posts` table (`id`, `author_id`, `body`) with `Users.posts = many(...)`.

## insert_many

```python
rows = [{"email": f"user{i}@example.com", "name": f"User {i}"} for i in range(10_000)]
count = await db.insert_many(Users, rows)  # 10000
```

`insert_many` splits the rows into as few `INSERT` statements as PostgreSQL's bind
parameter limit allows, all in one transaction: every row is stored, or none. Rows are
checked like `insert().values()`. It returns the row count, or the rows with
`returning=True`:

```python
more = [{"email": f"new{i}@example.com", "name": "New"} for i in range(1_000)]
await db.insert_many(Users, more, batch_size=500)  # at most 500 rows per INSERT
saved = await db.insert_many(Users, [{"email": "x@example.com", "name": "X"}], returning=True)
print(saved[0].id)  # list[Users]
```

### Upserts in batches

`on_conflict` takes an insert without values; every batch gets its conflict clause:

```python
upsert = db.insert(Users).on_conflict_do_update(
    target=Users.email, set_={"name": excluded(Users.name)}
)
changed = [{"email": "user1@example.com", "name": "Renamed"}]
await db.insert_many(Users, changed, on_conflict=upsert)

skip = db.insert(Users).on_conflict_do_nothing(Users.email)
added = await db.insert_many(Users, rows, on_conflict=skip)  # 0: all exist already
```

Rows skipped by `DO NOTHING` are not counted or returned. With `DO UPDATE`, two rows with
the same key in one batch fail: PostgreSQL cannot update a row twice in one statement.

## copy_into

`COPY` is the fastest way to load many rows, about 3 times faster than
`insert_many`. Rows are dicts or models, checked like `insert().values()`, and enum, json
and other values are converted per column:

```python
loaded = await db.copy_into(Posts, ({"author_id": 1, "body": f"post {i}"} for i in range(5000)))
```

Every row sets the same columns (those the first row sets, or `columns=[...]`); the others
get their database default. All rows or none are stored, and rows are sent as they are read,
so a generator never sits in memory. `copy_into` has no `on_conflict`: use `insert_many`
for upserts. A table with a `custom()` or vector column is loaded with `insert_many`
instead, because COPY sends binary values those types may not have.

## copy_to

Export a query with `COPY ... TO STDOUT`, as CSV (default), text or binary:

```python
query = db.select(Users.id, Users.email).from_(Users).order_by(Users.id)
exported = await db.copy_to(query, "users.csv", header=True)  # a file path
await db.copy_to(query, buffer, format="binary")  # a binary file


async def write(chunk: bytes) -> None:  # or an async function
    await response.send(chunk)


await db.copy_to(sql("SELECT id, body FROM posts"), write, format="text")
```

It returns the row count. Plain selects read from a replica, like other reads. An error of
the output (a full disk) is raised as it is.

COPY takes no bind parameters, so mizzle writes the query's values into its text. They
show in `pg_stat_activity` and in server logs: keep secrets out of `copy_to()` filters.
With values, the SQL may not hold comments, dollar quotes, backslashes or unclosed quotes
(a raw `sql()` that does raises `QueryError`).

## stream()

Awaiting a query loads every row into memory at once. `stream()` reads from a server-side
cursor, `batch_size` rows at a time (default 1000):

```python
async for user in db.select().from_(Users).order_by(Users.id).stream(batch_size=1000):
    print(user.email)
```

Relational queries stream too, nested rows included:

```python
query = db.query(Users).find_many(with_={"posts": True})
async for user in query.stream(batch_size=200):
    seen += len(user.posts)
```

A stream holds a connection and a transaction (cursors only live inside one) until the
loop ends. Inside `db.transaction()` it uses that transaction's connection. If you break
out early, wrap it in `contextlib.aclosing()` so the connection goes back at once instead
of when the generator is garbage collected:

```python
async with aclosing(db.select().from_(Users).stream()) as users:
    async for user in users:
        if user.email.startswith("new"):
            break  # the cursor and its connection are released here
```

## LISTEN and NOTIFY

```python
async with db.listen("orders", decode=json.loads) as events:
    async for event in events:
        if isinstance(event, Reconnected):
            await reload_orders()  # notifications sent while it was down were missed
        else:
            await handle(event.data)  # event.channel, event.payload, event.pid
```

```python
await db.notify("orders", {"id": 7, "status": "paid"})  # a dict is sent as JSON
async with db.transaction() as tx:
    await tx.notify("orders", "last")  # sent when the transaction commits
```

- `listen()` uses a connection of its own, never one from the pool.
- A lost connection is opened again on its own, then `Reconnected` is yielded: reload
  whatever the missed notifications would have told you.
- `decode=` turns each payload into `event.data` (`json.loads`,
  `Order.model_validate_json`); without it `data` is the payload. A `decode` that raises
  fails that step only; the next notification still arrives.
- More than `max_queue` (10 000) unread notifications raise `ListenerOverflow`, after the
  queued ones: none are dropped without an error.
- After `keepalive` (30) seconds with no notification, it checks the connection, so a
  dropped network is noticed too.

PgBouncer in transaction mode drops notifications. Through a pooler, `listen()` checks
that a test notification arrives, and raises if it does not. Point it straight at
PostgreSQL with `url=`:

```python
async with db.listen("orders", url="postgresql://app@db-primary/app") as events: ...
```

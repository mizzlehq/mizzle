# Bulk and streaming

Loading many rows, exporting them, reading big results without holding them in memory,
and LISTEN/NOTIFY. The examples use a `Users` table (`id`, `email` unique, `name`) and a
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

`COPY ... FROM STDIN` is about three times faster than `INSERT` for big loads:

```python
authors = await db.select(Users.id).from_(Users).order_by(asc(Users.id)).limit(100)
posts = ({"author_id": uid, "body": f"post {n}"} for (uid,) in authors for n in range(50))
loaded = await db.copy_into(Posts, posts)  # 5000
```

`rows` can be any iterable, a generator too. Every row must set the same columns: by
default the ones the first row sets. `columns=` names them; columns left out get their
database default:

```python
await db.copy_into(
    Posts, [{"author_id": u1.id, "body": "hi"}], columns=[Posts.author_id, Posts.body]
)
```

All rows or none are stored. `copy_into` has no `on_conflict`: use `insert_many` for
upserts. Tables with `vector` or `tsvector` columns are loaded with `insert_many` instead,
since those types have no binary COPY format.

## copy_from

`COPY (query) TO STDOUT` exports a select, a relational query or raw `sql(...)`:

```python
query = db.select(Users.id, Users.email).from_(Users).order_by(Users.id)
exported = await db.copy_from(query, "users.csv", header=True)  # a path
```

The output is a file path, a binary file, or an async function that gets each chunk (for
example, to stream a download):

```python
buffer = io.BytesIO()
await db.copy_from(query, buffer, format="binary")  # a binary file

chunks: list[bytes] = []

async def write(chunk: bytes) -> None:  # or an async function, e.g. a response body
    chunks.append(chunk)

await db.copy_from(sql("SELECT id, body FROM bulkdoc_posts"), write, format="text")
```

`format` is `"csv"` (default), `"text"` or `"binary"`; `header=True` adds a header line
(csv). It returns the row count. Selects without `FOR UPDATE` read from a replica when you
have one, like other reads.

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
async def consume() -> None:
    async with db.listen("orders") as events:
        async for event in events:
            print(event.channel, event.payload, event.pid)
            if event.payload == "last":
                break

task = asyncio.create_task(consume())
await asyncio.sleep(0.1)  # (let the listener start)
await db.notify("orders", "order 1 paid")
async with db.transaction() as tx:
    await tx.notify("orders", "last")  # sent when the transaction commits
```

`db.listen(*channels)` holds one pooled connection until the block exits and yields
`Notification(channel, payload, pid)`. `db.notify(channel, payload)` runs `pg_notify`
with the payload as a bind parameter. A trigger can send them too: `PERFORM pg_notify(...)`
in a [function](migrations.md#functions). Call `listen()` on `db`, not on a transaction:
there it raises `QueryError`.

### Lost connections

By default, a lost connection makes the loop raise `ConnectionError`; notifications sent
meanwhile are gone. With `reconnect=True`, `listen()` LISTENs again on a new connection,
retrying until the database is back, and yields a `Reconnected` event first, so the gap
is never silent:

```python
async with db.listen("orders", reconnect=True) as events:
    async for event in events:
        if isinstance(event, Reconnected):
            await reload()  # notifications sent meanwhile were missed
            continue
        await handle(event)
```

### Slow consumers

By default the queue of unread notifications has no bound. `max_queue=n` sets one, and
`overflow=` says what happens when it is full:

- `"raise"` (default): after the queued notifications, the loop raises
  `ListenerOverflow`. Later ones were not kept.
- `"drop_oldest"`: the oldest is discarded, with a log warning.
- `"block"`: mizzle stops reading the connection until you catch up. PostgreSQL holds
  the rest, so nothing is lost. The queue can briefly go over `max_queue` by what was
  already in one network read.

```python
try:
    async with db.listen("orders", max_queue=100) as events:
        async for event in events:
            received.append(event.payload)
except ListenerOverflow:
    ...  # 100 queued ones were read; later ones were not kept
```

### Connection poolers

PgBouncer in transaction or statement mode drops notifications, so `listen()` through a
pooler raises `DatabaseError`. Connect the listener directly to PostgreSQL (a second
`Database.connect` with the direct URL). If the pooler runs in session mode, say so:

```python
async with db.listen("orders", pooler_session_mode=True) as events:
    await db.notify("orders", "through the pooler")
    async for event in events:
        print(event.payload)
        break
```

`listen()` then checks that a test notification arrives before it returns. See
[Production](../production.md#pgbouncer).

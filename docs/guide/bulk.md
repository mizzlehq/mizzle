# Bulk and streaming

Loading many rows and reading big results without holding them in memory. The examples use a `Users` table (`id`, `email` unique, `name`) and a
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

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/mizzlehq/mizzle/main/assets/logo-dark.png">
    <img alt="mizzle" src="https://raw.githubusercontent.com/mizzlehq/mizzle/main/assets/logo.png" width="300">
  </picture>
</p>

<h3 align="center">Fully typed async ORM for Python and PostgreSQL</h3>

<p align="center">
  <a href="https://github.com/mizzlehq/mizzle">GitHub</a> •
  <a href="https://pypi.org/project/mizzle/">PyPI</a> •
  <a href="https://github.com/mizzlehq/mizzle/tree/main/examples/social">Example app</a> •
  <a href="https://github.com/mizzlehq/mizzle/releases">Releases</a>
</p>

<br>

## What's mizzle?

mizzle is an ORM for people who like SQL. You describe tables as Python classes and write
queries that read like the SQL they produce. Every row comes back typed, so your editor and
type checker know `user.email` is a `str` — no code generation, no plugins.

It is async from the ground up, built on [asyncpg](https://github.com/MagicStack/asyncpg),
with one runtime dependency. Values are always sent as bind parameters, `update` and
`delete` refuse to touch a whole table by accident, and database errors never leak your
data into logs.

Need nested data? Relational queries load a user with their posts and comments in a single
SQL statement, typed all the way down.

## Ecosystem

**mizzle CLI** generates SQL migrations from your schema, applies them safely (even with
many app servers starting at once), pushes changes straight to a dev database, and pulls an
existing database into Python code.

**mizzle studio** opens your database in the browser to browse, filter and edit data.

## Quick look

```bash
pip install mizzle
mizzle init        # creates mizzle.toml, db/schema.py and migrations/
```

```python
from typing import final

from mizzle import Database, Table, eq, sql
from mizzle.pg import serial, text, timestamp, varchar


@final
class Users(Table, name="users"):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    bio = text()
    created_at = timestamp(tz=True).not_null().default(sql("now()"))


async with await Database.connect("postgresql://...", schema=[Users]) as db:
    [ann] = await db.insert(Users).values(email="ann@example.com").returning()
    users = await db.select().from_(Users).where(eq(Users.id, ann.id))  # list[Users]
```

```bash
mizzle generate    # write a migration from your schema
mizzle migrate     # apply it
mizzle studio      # browse the data
```

## In production

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

Writes, transactions and `FOR UPDATE` always use the primary. Behind PgBouncer in
transaction mode, pass `statement_cache_size=0`. CI tests a real streaming replica and
PgBouncer on every change.

## Learn more

The [example app](https://github.com/mizzlehq/mizzle/tree/main/examples/social) is a
complete FastAPI service built with mizzle: schema, migrations, relational queries,
Pydantic models, seed data and tests. Full documentation is on the way.

## License

[MIT](https://github.com/mizzlehq/mizzle/blob/main/LICENSE)

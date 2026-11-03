# Getting started

## Install

```bash
pip install mizzle               # or: uv add mizzle
pip install "mizzle[pydantic]"   # Pydantic models from your tables
pip install "mizzle[studio]"     # mizzle studio, the data browser
```

mizzle needs Python 3.12+ and PostgreSQL 14+.

## Set up a project

```bash
mizzle init
```

This writes `mizzle.toml`, `db/schema.py` and a `migrations/` folder:

```toml
# Module(s) with your Table classes.
schema = "db.schema"
# Migrations folder. Commit it.
out = "migrations"
# Environment variable holding the database URL (never put the URL in this file).
url_env = "DATABASE_URL"
```

## Describe tables

A table is a class. Columns come from `mizzle.pg`. Relations (`one`, `many`) are for
relational queries; they add no columns.

```python
from typing import final

from mizzle import Table, many, one, sql
from mizzle.pg import integer, serial, text, timestamp, varchar


@final
class Users(Table, name="users"):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    created_at = timestamp(tz=True).not_null().default(sql("now()"))
    posts = many(lambda: Posts)


@final
class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    body = text().not_null()
    author = one(lambda: Users, fields=lambda: [Posts.author_id], references=lambda: [Users.id])
```

## Create the tables

```bash
export DATABASE_URL=postgresql://user:password@localhost:5432/app
mizzle generate --name init   # writes migrations/0001_init/migration.sql
mizzle migrate                # applies it
```

Read the SQL file before you commit it. On a dev database, `mizzle push` applies the
schema directly, with no migration file.

## Query

```python
from mizzle import Database, desc, eq

async with await Database.connect(url, schema=[Users, Posts]) as db:
    [ann] = await db.insert(Users).values(email="ann@example.com").returning()
    await db.insert(Posts).values(author_id=ann.id, body="hello")

    # select ... from ... where: a list of typed rows
    users = await db.select().from_(Users).where(eq(Users.email, "ann@example.com"))

    # update and delete need a where (no whole-table writes by accident)
    await db.update(Users).set(email="ann@example.org").where(eq(Users.id, ann.id))

    # relational query: posts with their author, in one SQL statement
    posts = await db.query(Posts).find_many(
        with_={"author": True}, order_by=desc(Posts.id), limit=20
    )
    print(posts[0].author.email)

    # transactions: commit at the end of the block, roll back on an exception
    async with db.transaction() as tx:
        await tx.delete(Posts).where(eq(Posts.author_id, ann.id))
```

Every row is typed: your editor knows `posts[0].author.email` is a `str`.

## Next

- [CLI](cli.md): every `mizzle` command.
- [Production](production.md): pools, replicas, timeouts, PgBouncer.
- [Changelog](changelog.md): examples for every feature.
- [examples/social](https://github.com/mizzlehq/mizzle/tree/main/examples/social): a full
  FastAPI app. [examples/shop](https://github.com/mizzlehq/mizzle/tree/main/examples/shop)
  uses every 0.2 feature.

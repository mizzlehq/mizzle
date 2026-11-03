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

## Benchmarks

Operations per second, higher is better: the same queries on the same PostgreSQL 18 database,
median of 5 rounds ([benchmarks/bench.py](https://github.com/mizzlehq/mizzle/blob/main/benchmarks/bench.py)).

**A busy app:** 50 concurrent clients, 8 connections per library, and 1 ms of network
latency each way (2.2 ms round trip, like a database on another server):

| | mizzle | Tortoise | SQLAlchemy ORM | SQLAlchemy Core | Piccolo | Django |
|---|---:|---:|---:|---:|---:|---:|
| insert 1 row | **3,040** | 1,587 | 935 | 921 | 1,577 | 427 |
| bulk insert 1000 rows | 448 | 180 | 223 | **462** | 76 | 111 |
| get by primary key | **3,078** | 1,566 | 942 | 952 | 1,565 | 413 |
| select 1000 rows | **728** | 362 | 268 | 680\* | 331 | 239 |
| update by primary key | **3,035** | 1,566 | 935 | 923 | 1,573 | 426 |
| 100 users with their posts | **1,358** | 392 | 245 | 549 | 369 | 110 |

**One client, database on the same machine** (0.04 ms round trip, 4 connections):

| | mizzle | Tortoise | SQLAlchemy ORM | SQLAlchemy Core | Piccolo | Django |
|---|---:|---:|---:|---:|---:|---:|
| insert 1 row | **9,831** | 6,207 | 2,226 | 2,827 | 5,651 | 4,588 |
| bulk insert 1000 rows | **215** | 120 | 156 | 194 | 66 | 132 |
| get by primary key | **10,112** | 4,516 | 2,623 | 3,182 | 4,706 | 3,467 |
| select 1000 rows | **898** | 356 | 300 | 846\* | 390 | 336 |
| update by primary key | **10,516** | 4,234 | 2,085 | 2,779 | 5,345 | 4,061 |
| 100 users with their posts | **655** | 336 | 234 | 472 | 320 | 218 |

\* SQLAlchemy Core returns plain tuples; the ORMs build full row objects.

mizzle loads nested rows in one statement, so it pays one round trip where the others pay
two. Raw asyncpg, the driver under mizzle, is the upper limit: on single-row work mizzle
reaches 97% of it over the network and 67–84% on the same machine. Python 3.14, mizzle 0.2.3, Tortoise 1.1.9,
SQLAlchemy 2.1.6, Piccolo 1.39.0, Django 6.1.3 (its async ORM runs queries in one thread, on
psycopg). The script's docstring explains each library's setup and the latency proxy. Run
it yourself:

```bash
docker compose up -d
uv run --with tortoise-orm --with "sqlalchemy[asyncio]" --with piccolo --with django \
  --with "psycopg[binary,pool]" python benchmarks/bench.py
BENCH_CLIENTS=50 BENCH_POOL=8 uv run ...  # the concurrent run
```

## What else is in the box

Joins, subqueries, CTEs, window functions, `UNION`, `CASE` and typed SQL functions;
identity, generated and auto-updated columns; pgvector and full-text search; views, row-level
security, roles, functions and triggers in migrations; streaming, `COPY`, `LISTEN`/`NOTIFY`;
read replicas, timeouts and retries. See the
[changelog](https://github.com/mizzlehq/mizzle/blob/main/CHANGELOG.md) for examples.

## Learn more

The [example app](https://github.com/mizzlehq/mizzle/tree/main/examples/social) is a
complete FastAPI service built with mizzle: schema, migrations, relational queries,
Pydantic models, seed data and tests. Full documentation is on the way.

## License

[MIT](https://github.com/mizzlehq/mizzle/blob/main/LICENSE)

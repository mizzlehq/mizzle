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
| insert 1 row | **3,060** | 1,565 | 922 | 907 | 1,552 | 423 |
| bulk insert 1000 rows | 328 | 180 | 224 | **456** | 75 | 115 |
| get by primary key | **3,074** | 1,558 | 937 | 955 | 1,563 | 412 |
| select 1000 rows | **1,726** | 297 | 263 | 677\* | 415 | 233 |
| update by primary key | **3,041** | 1,563 | 916 | 911 | 1,570 | 424 |
| 100 users with their posts | **1,327** | 384 | 238 | 540 | 373 | 106 |

**One client, database on the same machine** (0.05 ms round trip, 4 connections):

| | mizzle | Tortoise | SQLAlchemy ORM | SQLAlchemy Core | Piccolo | Django |
|---|---:|---:|---:|---:|---:|---:|
| insert 1 row | **12,859** | 6,586 | 2,356 | 2,849 | 4,924 | 4,680 |
| bulk insert 1000 rows | **214** | 125 | 165 | 207 | 62 | 140 |
| get by primary key | **10,178** | 4,900 | 2,733 | 3,177 | 4,733 | 3,555 |
| select 1000 rows | **1,072** | 319 | 311 | 837\* | 382 | 377 |
| update by primary key | **9,278** | 4,301 | 2,127 | 2,771 | 5,168 | 3,972 |
| 100 users with their posts | **650** | 320 | 219 | 453 | 302 | 212 |

\* SQLAlchemy Core returns plain tuples; the ORMs build full row objects.

mizzle loads nested rows in one statement, so it pays one round trip where the others pay
two (over the network that beats even hand-written asyncpg doing two queries). Against raw
asyncpg, the driver under mizzle, it reaches 97–99% over the network (bulk insert: 54%) and
71–93% on single rows on the same machine. Python 3.14, mizzle 0.2.4, Tortoise 1.1.9,
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
security, policies and roles in migrations (custom migrations for the rest); streaming, `COPY`, `LISTEN`/`NOTIFY`;
read replicas, timeouts and retries. See the
[changelog](https://github.com/mizzlehq/mizzle/blob/main/CHANGELOG.md) for examples.

## Learn more

The [example app](https://github.com/mizzlehq/mizzle/tree/main/examples/social) is a
complete FastAPI service built with mizzle: schema, migrations, relational queries,
Pydantic models, seed data and tests. Full documentation is at
[mizzle.readthedocs.io](https://mizzle.readthedocs.io), with
[llms.txt](https://mizzle.readthedocs.io/llms.txt) for LLM tools.

## License

[MIT](https://github.com/mizzlehq/mizzle/blob/main/LICENSE)

"""mizzle vs other Python PostgreSQL libraries on the same queries and the same database.

    docker compose up -d
    uv run --with tortoise-orm --with "sqlalchemy[asyncio]" --with piccolo \\
        --with django --with "psycopg[binary,pool]" python benchmarks/bench.py

BENCH_LIBS=sqlalchemy,django limits the run to those libraries (any of tortoise, sqlalchemy
for both ORM and Core, piccolo, django); asyncpg and mizzle always run, as they are the
baseline of the comparison table.

Uses its own UNLOGGED tables (bench_users, bench_posts): without the write-ahead log, writes
measure the library rather than disk flushes. Each scenario runs once per library to warm
up (and checks that it read or changed the expected number of rows), then ROUNDS times with
the libraries taking turns; the table shows the median round, in operations per second.

Columns: asyncpg with its default pool (resets each connection on release, like mizzle's
reset_connections="always") and without that reset (like mizzle's default, "auto", after
the statements it builds). Compare mizzle with the asyncpg column that uses the same setting.

Every library gets a pool of at most 4 connections; the scenarios are sequential, so only
one is in use at a time. How each library is used:

- tortoise: model API (create, bulk_create, filter().first(), prefetch_related).
- sqlalchemy: 2.x ORM, AsyncSession over asyncpg (pool_size=4, max_overflow=0), one
  session per operation as in a web request; writes in session.begin(). Bulk insert is the
  documented "ORM bulk INSERT" fast path, session.execute(insert(User), [dicts]); nested is
  selectinload. SQLAlchemy always opens a transaction, so every operation also sends BEGIN
  and COMMIT/ROLLBACK, which the autocommitting libraries do not.
- sqlalchemy core: Core select/insert/update on an AsyncEngine (same pool settings), one
  engine.connect()/begin() per operation; rows are Row tuples, Core's natural result.
  Nested is two queries (users, then posts with author_id IN (...)) grouped in Python.
- piccolo: objects() queries returning Table instances, asyncpg pool (max_size=4). Piccolo
  has no reverse-FK eager load for one-to-many, so nested is the same two queries, grouped.
- django: async ORM (acreate, abulk_create, aget, aupdate, async for, prefetch_related) on
  psycopg 3 with its pool (max_size=4). Django's async ORM runs the sync ORM in a worker
  thread (sync_to_async), so each query also pays a thread hop, and all queries share the
  one connection of that thread.

SQLModel is not listed separately: it is the SQLAlchemy ORM with pydantic models on top.

Concurrency: BENCH_CLIENTS=50 BENCH_POOL=8 runs each scenario as 50 clients at once, as a
busy web app does (row-by-row scenarios split their operations, each client on its own
rows; the others run once per client).

Network latency: on one machine a round trip is ~0.1 ms; to another server it is ~1-2 ms,
which every library pays alike. Toxiproxy adds it in front of the same database:

    docker run -d --name mizzle-toxiproxy --network host ghcr.io/shopify/toxiproxy:2.12.0
    curl -X POST localhost:8474/proxies \
        -d '{"name":"pg","listen":"127.0.0.1:54340","upstream":"127.0.0.1:54329"}'
    for s in upstream downstream; do curl -X POST localhost:8474/proxies/pg/toxics \
        -d '{"type":"latency","stream":"'$s'","attributes":{"latency":1}}'; done
    MIZZLE_TEST_URL=postgresql://mizzle:mizzle@127.0.0.1:54340/mizzle uv run ...

The output prints the measured round trip.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import final
from urllib.parse import urlsplit

import asyncpg
import django
import sqlalchemy as sa
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connections
from django.db import models as dj
from piccolo.columns import ForeignKey, Text, Varchar
from piccolo.engine.postgres import PostgresEngine
from piccolo.table import Table as PTable
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload
from tortoise import Tortoise, fields
from tortoise.models import Model

from mizzle import Database, Table, eq, many, one
from mizzle.pg import integer, serial, text, varchar

URL = os.environ.get("MIZZLE_TEST_URL", "postgresql://mizzle:mizzle@localhost:54329/mizzle")
LIBS = {lib for lib in os.environ.get("BENCH_LIBS", "").split(",") if lib}
ROUNDS = 5
USERS, POSTS_PER_USER = 1000, 5
# BENCH_CLIENTS=50 runs every scenario as 50 concurrent clients; the row-by-row scenarios
# split their 500/1000 operations between them, each client on its own rows.
CLIENTS = int(os.environ.get("BENCH_CLIENTS", "1"))
POOL = int(os.environ.get("BENCH_POOL", "4"))
INSERTS, GETS, UPDATES = 500 // CLIENTS, 1000 // CLIENTS, 500 // CLIENTS


def ids(client: int, n: int) -> range:
    """The primary keys client number ``client`` works on: n of them, its own."""
    return range(1 + client * n, 1 + (client + 1) * n)


DDL = """
DROP TABLE IF EXISTS bench_posts, bench_users;
CREATE UNLOGGED TABLE bench_users (id serial PRIMARY KEY, email varchar(255) NOT NULL, bio text);
CREATE UNLOGGED TABLE bench_posts (
    id serial PRIMARY KEY,
    author_id integer NOT NULL REFERENCES bench_users(id) ON DELETE CASCADE,
    title text NOT NULL
);
CREATE INDEX ON bench_posts (author_id);
"""
SEED = f"""
INSERT INTO bench_users (email, bio)
    SELECT 'user' || i || '@example.com', 'bio ' || i FROM generate_series(1, {USERS}) i;
INSERT INTO bench_posts (author_id, title)
    SELECT u, 'post ' || p
    FROM generate_series(1, {USERS}) u, generate_series(1, {POSTS_PER_USER}) p;
"""
# Per scenario, checked after warm-up: a count query and its expected value, or (None, n)
# when the scenario's own return value (rows or posts it loaded) must be n.
CHECKS: list[tuple[str | None, int]] = [
    ("SELECT count(*) FROM bench_users", USERS + INSERTS * CLIENTS),
    ("SELECT count(*) FROM bench_users", USERS + 1000 * CLIENTS),
    (None, GETS),
    (None, 1000),
    ("SELECT count(*) FROM bench_users WHERE bio = 'changed'", UPDATES * CLIENTS),
    (None, 100 * POSTS_PER_USER),
]


# -- mizzle schema ---------------------------------------------------------------------


@final
class Users(Table, name="bench_users"):
    id = serial().primary_key()
    email = varchar(255).not_null()
    bio = text()
    posts = many(lambda: Posts)


@final
class Posts(Table, name="bench_posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id)
    title = text().not_null()
    author = one(lambda: Users, fields=lambda: [Posts.author_id], references=lambda: [Users.id])


# -- Tortoise models -------------------------------------------------------------------


class TUser(Model):
    id = fields.IntField(primary_key=True)
    email = fields.CharField(max_length=255)
    bio = fields.TextField(null=True)

    class Meta:
        table = "bench_users"


class TPost(Model):
    id = fields.IntField(primary_key=True)
    author: fields.ForeignKeyRelation[TUser] = fields.ForeignKeyField(
        "models.TUser", related_name="posts", source_field="author_id"
    )
    title = fields.TextField()

    class Meta:
        table = "bench_posts"


# -- SQLAlchemy ORM models and Core tables ---------------------------------------------


class SABase(DeclarativeBase):
    pass


class SAUser(SABase):
    __tablename__ = "bench_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(sa.String(255))
    bio: Mapped[str | None] = mapped_column(sa.Text)
    posts: Mapped[list[SAPost]] = relationship(back_populates="author")


class SAPost(SABase):
    __tablename__ = "bench_posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(sa.ForeignKey("bench_users.id"))
    title: Mapped[str]
    author: Mapped[SAUser] = relationship(back_populates="posts")


core_meta = sa.MetaData()
core_users = sa.Table(
    "bench_users",
    core_meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("email", sa.String(255), nullable=False),
    sa.Column("bio", sa.Text),
)
core_posts = sa.Table(
    "bench_posts",
    core_meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("author_id", sa.ForeignKey("bench_users.id"), nullable=False),
    sa.Column("title", sa.Text, nullable=False),
)


# -- Piccolo tables --------------------------------------------------------------------

piccolo_db = PostgresEngine(config={"dsn": URL})


class PUser(PTable, tablename="bench_users", db=piccolo_db):
    email = Varchar(length=255)
    bio = Text(null=True)


class PPost(PTable, tablename="bench_posts", db=piccolo_db):
    author = ForeignKey(references=PUser, db_column_name="author_id")
    title = Text()


# -- Django models (settings configured in-script, no project) -------------------------

_url = urlsplit(URL)
settings.configure(
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _url.path.lstrip("/"),
            "USER": _url.username,
            "PASSWORD": _url.password,
            "HOST": _url.hostname,
            "PORT": _url.port,
            "OPTIONS": {"pool": {"min_size": 1, "max_size": POOL}},
        }
    },
    INSTALLED_APPS=[],
    USE_TZ=True,
)
django.setup()


class DUser(dj.Model):
    id = dj.AutoField(primary_key=True)
    email = dj.CharField(max_length=255)
    bio = dj.TextField(null=True)

    class Meta:
        app_label = "bench"
        db_table = "bench_users"


class DPost(dj.Model):
    id = dj.AutoField(primary_key=True)
    author = dj.ForeignKey(DUser, on_delete=dj.CASCADE, related_name="posts", db_column="author_id")
    title = dj.TextField()

    class Meta:
        app_label = "bench"
        db_table = "bench_posts"


# -- scenarios -------------------------------------------------------------------------

type Op = Callable[[int], Awaitable[int | None]]  # argument: the client number
type Scenario = tuple[str, int, Op]  # name, operations per round, run one round
type Ops = tuple[Op, Op, Op, Op, Op, Op]
# insert one x500, bulk insert, get by pk x1000, select 1000, update x500, nested

# Name and operations per round (all clients together).
NAMES = [
    ("insert 1 row (x500)", INSERTS * CLIENTS),
    ("bulk insert 1000 rows", CLIENTS),
    ("get by primary key (x1000)", GETS * CLIENTS),
    ("select 1000 rows", CLIENTS),
    ("update by primary key (x500)", UPDATES * CLIENTS),
    ("100 users with posts", CLIENTS),
]


def scenarios(ops: Ops) -> list[Scenario]:
    return [(name, n, op) for (name, n), op in zip(NAMES, ops, strict=True)]


def mizzle_scenarios(db: Database) -> list[Scenario]:
    async def insert_one(c: int) -> None:
        for i in range(INSERTS):
            await db.insert(Users).values(email=f"m{i}@x.com", bio="hi")

    async def insert_bulk(c: int) -> None:
        await db.insert(Users).values([{"email": f"b{i}@x.com", "bio": "hi"} for i in range(1000)])

    async def get_by_pk(c: int) -> int:
        found = 0
        for i in ids(c, GETS):
            found += len(await db.select().from_(Users).where(eq(Users.id, i)))
        return found

    async def select_1000(c: int) -> int:
        return len(await db.select().from_(Users).limit(1000))

    async def update_one(c: int) -> None:
        for i in ids(c, UPDATES):
            await db.update(Users).set(bio="changed").where(eq(Users.id, i))

    async def nested(c: int) -> int:
        users = await db.query(Users).find_many(with_={"posts": True}, limit=100)
        return sum(len(u.posts) for u in users)

    return scenarios((insert_one, insert_bulk, get_by_pk, select_1000, update_one, nested))


def tortoise_scenarios() -> list[Scenario]:
    async def insert_one(c: int) -> None:
        for i in range(INSERTS):
            await TUser.create(email=f"t{i}@x.com", bio="hi")

    async def insert_bulk(c: int) -> None:
        await TUser.bulk_create([TUser(email=f"b{i}@x.com", bio="hi") for i in range(1000)])

    async def get_by_pk(c: int) -> int:
        found = 0
        for i in ids(c, GETS):
            found += await TUser.filter(id=i).first() is not None
        return found

    async def select_1000(c: int) -> int:
        return len(await TUser.all().limit(1000))

    async def update_one(c: int) -> None:
        for i in ids(c, UPDATES):
            await TUser.filter(id=i).update(bio="changed")

    async def nested(c: int) -> int:
        users = await TUser.all().limit(100).prefetch_related("posts")
        return sum(len(u.posts) for u in users)

    return scenarios((insert_one, insert_bulk, get_by_pk, select_1000, update_one, nested))


def sqlalchemy_scenarios(engine: AsyncEngine) -> list[Scenario]:
    session = async_sessionmaker(engine, expire_on_commit=False)

    async def insert_one(c: int) -> None:
        for i in range(INSERTS):
            async with session.begin() as s:
                s.add(SAUser(email=f"s{i}@x.com", bio="hi"))

    async def insert_bulk(c: int) -> None:
        async with session.begin() as s:
            await s.execute(
                sa.insert(SAUser), [{"email": f"b{i}@x.com", "bio": "hi"} for i in range(1000)]
            )

    async def get_by_pk(c: int) -> int:
        found = 0
        for i in ids(c, GETS):
            async with session() as s:
                found += await s.get(SAUser, i) is not None
        return found

    async def select_1000(c: int) -> int:
        async with session() as s:
            return len((await s.scalars(sa.select(SAUser).limit(1000))).all())

    async def update_one(c: int) -> None:
        for i in ids(c, UPDATES):
            async with session.begin() as s:
                await s.execute(sa.update(SAUser).where(SAUser.id == i).values(bio="changed"))

    async def nested(c: int) -> int:
        async with session() as s:
            query = sa.select(SAUser).limit(100).options(selectinload(SAUser.posts))
            users = (await s.scalars(query)).all()
            return sum(len(u.posts) for u in users)

    return scenarios((insert_one, insert_bulk, get_by_pk, select_1000, update_one, nested))


def core_scenarios(engine: AsyncEngine) -> list[Scenario]:
    users, posts = core_users, core_posts

    async def insert_one(c: int) -> None:
        for i in range(INSERTS):
            async with engine.begin() as conn:
                await conn.execute(sa.insert(users).values(email=f"c{i}@x.com", bio="hi"))

    async def insert_bulk(c: int) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                sa.insert(users), [{"email": f"b{i}@x.com", "bio": "hi"} for i in range(1000)]
            )

    async def get_by_pk(c: int) -> int:
        found = 0
        for i in ids(c, GETS):
            async with engine.connect() as conn:
                result = await conn.execute(sa.select(users).where(users.c.id == i))
                found += result.first() is not None
        return found

    async def select_1000(c: int) -> int:
        async with engine.connect() as conn:
            return len((await conn.execute(sa.select(users).limit(1000))).all())

    async def update_one(c: int) -> None:
        for i in ids(c, UPDATES):
            async with engine.begin() as conn:
                await conn.execute(sa.update(users).where(users.c.id == i).values(bio="changed"))

    async def nested(c: int) -> int:
        async with engine.connect() as conn:
            rows = (await conn.execute(sa.select(users).limit(100))).all()
            by_user: dict[int, list[sa.Row[tuple[int, int, str]]]] = {r.id: [] for r in rows}
            query = sa.select(posts).where(posts.c.author_id.in_(list(by_user)))
            for post in await conn.execute(query):
                by_user[post.author_id].append(post)
        return sum(len(p) for p in by_user.values())

    return scenarios((insert_one, insert_bulk, get_by_pk, select_1000, update_one, nested))


def piccolo_scenarios() -> list[Scenario]:
    async def insert_one(c: int) -> None:
        for i in range(INSERTS):
            await PUser.insert(PUser(email=f"p{i}@x.com", bio="hi"))

    async def insert_bulk(c: int) -> None:
        await PUser.insert(*[PUser(email=f"b{i}@x.com", bio="hi") for i in range(1000)])

    async def get_by_pk(c: int) -> int:
        found = 0
        for i in ids(c, GETS):
            found += await PUser.objects().get(PUser.id == i) is not None
        return found

    async def select_1000(c: int) -> int:
        return len(await PUser.objects().limit(1000))

    async def update_one(c: int) -> None:
        for i in ids(c, UPDATES):
            await PUser.update({PUser.bio: "changed"}).where(PUser.id == i)

    async def nested(c: int) -> int:
        users = await PUser.objects().limit(100)
        by_user: dict[int, list[PPost]] = {u.id: [] for u in users}
        for post in await PPost.objects().where(PPost.author.is_in(list(by_user))):
            by_user[post.author].append(post)
        return sum(len(p) for p in by_user.values())

    return scenarios((insert_one, insert_bulk, get_by_pk, select_1000, update_one, nested))


def django_scenarios() -> list[Scenario]:
    async def insert_one(c: int) -> None:
        for i in range(INSERTS):
            await DUser.objects.acreate(email=f"d{i}@x.com", bio="hi")

    async def insert_bulk(c: int) -> None:
        await DUser.objects.abulk_create(
            [DUser(email=f"b{i}@x.com", bio="hi") for i in range(1000)]
        )

    async def get_by_pk(c: int) -> int:
        found = 0
        for i in ids(c, GETS):
            found += await DUser.objects.aget(pk=i) is not None
        return found

    async def select_1000(c: int) -> int:
        return len([u async for u in DUser.objects.all()[:1000]])

    async def update_one(c: int) -> None:
        for i in ids(c, UPDATES):
            await DUser.objects.filter(pk=i).aupdate(bio="changed")

    async def nested(c: int) -> int:
        users = [u async for u in DUser.objects.prefetch_related("posts")[:100]]
        return sum(len(u.posts.all()) for u in users)

    return scenarios((insert_one, insert_bulk, get_by_pk, select_1000, update_one, nested))


def asyncpg_scenarios(pool: asyncpg.Pool) -> list[Scenario]:
    async def insert_one(c: int) -> None:
        for i in range(INSERTS):
            await pool.execute(
                "INSERT INTO bench_users (email, bio) VALUES ($1, $2)", f"a{i}@x.com", "hi"
            )

    async def insert_bulk(c: int) -> None:
        await pool.executemany(
            "INSERT INTO bench_users (email, bio) VALUES ($1, $2)",
            [(f"b{i}@x.com", "hi") for i in range(1000)],
        )

    async def get_by_pk(c: int) -> int:
        found = 0
        for i in ids(c, GETS):
            row = await pool.fetchrow("SELECT id, email, bio FROM bench_users WHERE id = $1", i)
            found += row is not None
        return found

    async def select_1000(c: int) -> int:
        return len(await pool.fetch("SELECT id, email, bio FROM bench_users LIMIT 1000"))

    async def update_one(c: int) -> None:
        for i in ids(c, UPDATES):
            await pool.execute("UPDATE bench_users SET bio = $1 WHERE id = $2", "changed", i)

    async def nested(c: int) -> int:
        users = await pool.fetch("SELECT id, email, bio FROM bench_users LIMIT 100")
        posts = await pool.fetch(
            "SELECT id, author_id, title FROM bench_posts WHERE author_id = any($1::int[])",
            [u["id"] for u in users],
        )
        return len(posts)

    return scenarios((insert_one, insert_bulk, get_by_pk, select_1000, update_one, nested))


async def reset(pool: asyncpg.Pool) -> None:
    await pool.execute(DDL)
    await pool.execute(SEED)


async def check(pool: asyncpg.Pool, lib: str, index: int, results: list[int | None]) -> None:
    """Fail unless the warm-up run read or changed as many rows as every library must."""
    sql, expected = CHECKS[index]
    for got in [await pool.fetchval(sql)] if sql else results:
        if got != expected:
            raise SystemExit(f"{lib}, {NAMES[index][0]}: got {got}, expected {expected}")


async def run(op: Op) -> list[int | None]:
    """One round: every client at once."""
    return await asyncio.gather(*(op(c) for c in range(CLIENTS)))


async def measure(
    pool: asyncpg.Pool, libs: dict[str, list[Scenario]]
) -> dict[str, dict[str, float]]:
    """Libraries take turns inside every round, so disk and cache warm-up hit all alike."""
    times: dict[str, dict[str, list[float]]] = {lib: {} for lib in libs}
    for index, (name, _) in enumerate(NAMES):
        for lib, lib_scenarios in libs.items():  # warm up: statement caches, pool connections
            await reset(pool)
            await check(pool, lib, index, await run(lib_scenarios[index][2]))
        for round_ in range(ROUNDS):
            order = list(libs)
            order = order[round_ % len(order) :] + order[: round_ % len(order)]
            for lib in order:
                await reset(pool)
                start = time.perf_counter()
                await run(libs[lib][index][2])
                times[lib].setdefault(name, []).append(time.perf_counter() - start)
    return {
        lib: {name: n / statistics.median(times[lib][name]) for name, n in NAMES} for lib in libs
    }


async def round_trip(pool: asyncpg.Pool) -> float:
    """Milliseconds per SELECT 1: shows the network latency the run had."""
    async with pool.acquire() as conn:
        for _ in range(20):
            await conn.fetchval("SELECT 1")
        start = time.perf_counter()
        for _ in range(200):
            await conn.fetchval("SELECT 1")
    return (time.perf_counter() - start) / 200 * 1000


async def _no_reset(_conn: asyncpg.Connection) -> None:
    pass


def wanted(lib: str) -> bool:
    return not LIBS or lib in LIBS


def sa_engine() -> AsyncEngine:
    return create_async_engine(
        URL.replace("postgresql://", "postgresql+asyncpg://", 1),
        pool_size=POOL,
        max_overflow=0,
    )


async def main() -> None:
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=POOL)
    # Without the per-release session reset: the fastest raw asyncpg setup, to compare
    # with mizzle's reset_connections="auto".
    fast_pool = await asyncpg.create_pool(URL, min_size=1, max_size=POOL, reset=_no_reset)
    await reset(pool)
    db = await Database.connect(URL, schema=[Users, Posts], pool_size=POOL)
    db_always = await Database.connect(
        URL, schema=[Users, Posts], pool_size=POOL, reset_connections="always"
    )
    libs = {
        "asyncpg": asyncpg_scenarios(pool),
        "asyncpg no-reset": asyncpg_scenarios(fast_pool),
        "mizzle": mizzle_scenarios(db),
        "mizzle always": mizzle_scenarios(db_always),
    }
    if wanted("tortoise"):
        await Tortoise.init(
            db_url=URL.replace("postgresql://", "asyncpg://", 1) + f"?minsize=1&maxsize={POOL}",
            modules={"models": ["__main__"]},
        )
        libs["tortoise"] = tortoise_scenarios()
    sa_orm, sa_core = sa_engine(), sa_engine()
    if wanted("sqlalchemy"):
        libs["sqlalchemy"] = sqlalchemy_scenarios(sa_orm)
        libs["sqlalchemy core"] = core_scenarios(sa_core)
    if wanted("piccolo"):
        await piccolo_db.start_connection_pool(min_size=1, max_size=POOL)
        libs["piccolo"] = piccolo_scenarios()
    if wanted("django"):
        libs["django"] = django_scenarios()

    rtt = await round_trip(pool)
    results = await measure(pool, libs)

    if wanted("tortoise"):
        await Tortoise.close_connections()
    if wanted("piccolo"):
        await piccolo_db.close_connection_pool()
    if wanted("django"):
        await sync_to_async(connections.close_all)()
    await sa_orm.dispose()
    await sa_core.dispose()
    await db.close()
    await db_always.close()
    await fast_pool.close()
    await pool.execute("DROP TABLE bench_posts, bench_users")
    await pool.close()
    report(results, rtt)


def report(results: dict[str, dict[str, float]], rtt: float) -> None:
    libs = list(results)
    w = 17
    print(f"{'ops/sec (higher is better)':30}" + "".join(f"{lib:>{w}}" for lib in libs))
    for name, _ in NAMES:
        print(f"{name:30}" + "".join(f"{results[lib][name]:>{w},.0f}" for lib in libs))
    print()
    others = libs[4:]
    print("Same-setting comparisons (mizzle / other):")
    header = ["vs asyncpg no-reset", "always vs asyncpg"] + [f"vs {lib}" for lib in others]
    print(f"  {'':30}" + "".join(f"{h:>{w + 3}}" for h in header))
    for name, _ in NAMES:
        r = {lib: results[lib][name] for lib in libs}
        ratios = [r["mizzle"] / r["asyncpg no-reset"], r["mizzle always"] / r["asyncpg"]]
        ratios += [r["mizzle"] / r[lib] for lib in others]
        print(f"  {name:30}" + "".join(f"{x:>{w + 2}.2f}x" for x in ratios))
    print()
    clients = "one client" if CLIENTS == 1 else f"{CLIENTS} concurrent clients"
    print(f"Pool: at most {POOL} connections per library; {clients}.")
    print(f"Round trip: {rtt:.2f} ms per SELECT 1.")
    if "sqlalchemy" in results:
        print("sqlalchemy: every operation runs in a transaction (extra BEGIN/COMMIT).")
    if "django" in results:
        print("django: the async ORM runs the sync ORM in a worker thread (sync_to_async).")


if __name__ == "__main__":
    asyncio.run(main())

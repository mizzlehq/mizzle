"""mizzle vs Tortoise ORM vs raw asyncpg on the same queries and the same database.

    docker compose up -d
    uv run --with tortoise-orm python benchmarks/bench.py

Uses its own UNLOGGED tables (bench_users, bench_posts): without the write-ahead log, writes
measure the library rather than disk flushes. Each scenario runs once per library to warm
up, then ROUNDS times with the libraries taking turns; the table shows the median round,
in operations per second.

Columns: asyncpg with its default pool (resets each connection on release, like mizzle's
default) and without that reset (like mizzle's reset_connections="auto"). Compare mizzle
with the asyncpg column that uses the same setting.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import final

import asyncpg
from tortoise import Tortoise, fields
from tortoise.models import Model

from mizzle import Database, Table, eq, many, one
from mizzle.pg import integer, serial, text, varchar

URL = os.environ.get("MIZZLE_TEST_URL", "postgresql://mizzle:mizzle@localhost:54329/mizzle")
ROUNDS = 5
USERS, POSTS_PER_USER = 1000, 5

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


# -- scenarios -------------------------------------------------------------------------

type Op = Callable[[], Awaitable[object]]
type Scenario = tuple[str, int, Op]  # name, operations per round, run one round


def mizzle_scenarios(db: Database) -> list[Scenario]:
    async def insert_one() -> None:
        for i in range(500):
            await db.insert(Users).values(email=f"m{i}@x.com", bio="hi")

    async def insert_bulk() -> None:
        await db.insert(Users).values([{"email": f"b{i}@x.com", "bio": "hi"} for i in range(1000)])

    async def get_by_pk() -> None:
        for i in range(1, 1001):
            await db.select().from_(Users).where(eq(Users.id, i))

    async def select_1000() -> None:
        await db.select().from_(Users).limit(1000)

    async def update_one() -> None:
        for i in range(1, 501):
            await db.update(Users).set(bio="changed").where(eq(Users.id, i))

    async def nested() -> None:
        await db.query(Users).find_many(with_={"posts": True}, limit=100)

    return [
        ("insert 1 row (x500)", 500, insert_one),
        ("bulk insert 1000 rows", 1, insert_bulk),
        ("get by primary key (x1000)", 1000, get_by_pk),
        ("select 1000 rows", 1, select_1000),
        ("update by primary key (x500)", 500, update_one),
        ("100 users with posts", 1, nested),
    ]


def tortoise_scenarios() -> list[Scenario]:
    async def insert_one() -> None:
        for i in range(500):
            await TUser.create(email=f"t{i}@x.com", bio="hi")

    async def insert_bulk() -> None:
        await TUser.bulk_create([TUser(email=f"b{i}@x.com", bio="hi") for i in range(1000)])

    async def get_by_pk() -> None:
        for i in range(1, 1001):
            await TUser.filter(id=i).first()

    async def select_1000() -> None:
        await TUser.all().limit(1000)

    async def update_one() -> None:
        for i in range(1, 501):
            await TUser.filter(id=i).update(bio="changed")

    async def nested() -> None:
        await TUser.all().limit(100).prefetch_related("posts")

    return [
        ("insert 1 row (x500)", 500, insert_one),
        ("bulk insert 1000 rows", 1, insert_bulk),
        ("get by primary key (x1000)", 1000, get_by_pk),
        ("select 1000 rows", 1, select_1000),
        ("update by primary key (x500)", 500, update_one),
        ("100 users with posts", 1, nested),
    ]


def asyncpg_scenarios(pool: asyncpg.Pool) -> list[Scenario]:
    async def insert_one() -> None:
        for i in range(500):
            await pool.execute(
                "INSERT INTO bench_users (email, bio) VALUES ($1, $2)", f"a{i}@x.com", "hi"
            )

    async def insert_bulk() -> None:
        await pool.executemany(
            "INSERT INTO bench_users (email, bio) VALUES ($1, $2)",
            [(f"b{i}@x.com", "hi") for i in range(1000)],
        )

    async def get_by_pk() -> None:
        for i in range(1, 1001):
            await pool.fetchrow("SELECT id, email, bio FROM bench_users WHERE id = $1", i)

    async def select_1000() -> None:
        await pool.fetch("SELECT id, email, bio FROM bench_users LIMIT 1000")

    async def update_one() -> None:
        for i in range(1, 501):
            await pool.execute("UPDATE bench_users SET bio = $1 WHERE id = $2", "changed", i)

    async def nested() -> None:
        users = await pool.fetch("SELECT id, email, bio FROM bench_users LIMIT 100")
        await pool.fetch(
            "SELECT id, author_id, title FROM bench_posts WHERE author_id = any($1::int[])",
            [u["id"] for u in users],
        )

    return [
        ("insert 1 row (x500)", 500, insert_one),
        ("bulk insert 1000 rows", 1, insert_bulk),
        ("get by primary key (x1000)", 1000, get_by_pk),
        ("select 1000 rows", 1, select_1000),
        ("update by primary key (x500)", 500, update_one),
        ("100 users with posts", 1, nested),
    ]


async def reset(pool: asyncpg.Pool) -> None:
    await pool.execute(DDL)
    await pool.execute(SEED)


async def measure(
    pool: asyncpg.Pool, libs: dict[str, list[Scenario]]
) -> dict[str, dict[str, float]]:
    """Libraries take turns inside every round, so disk and cache warm-up hit all alike."""
    times: dict[str, dict[str, list[float]]] = {lib: {} for lib in libs}
    names = [name for name, _, _ in next(iter(libs.values()))]
    for index, name in enumerate(names):
        for scenarios in libs.values():  # warm up: statement caches, pool connections
            await reset(pool)
            await scenarios[index][2]()
        for round_ in range(ROUNDS):
            order = list(libs)
            order = order[round_ % len(order) :] + order[: round_ % len(order)]
            for lib in order:
                await reset(pool)
                start = time.perf_counter()
                await libs[lib][index][2]()
                times[lib].setdefault(name, []).append(time.perf_counter() - start)
    return {
        lib: {
            name: scenarios[i][1] / statistics.median(times[lib][name])
            for i, (name, _, _) in enumerate(scenarios)
        }
        for lib, scenarios in libs.items()
    }


async def _no_reset(_conn: asyncpg.Connection) -> None:
    pass


async def main() -> None:
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    # Without the per-release session reset: the fastest raw asyncpg setup, to compare
    # with mizzle's reset_connections="auto".
    fast_pool = await asyncpg.create_pool(URL, min_size=1, max_size=4, reset=_no_reset)
    await reset(pool)
    db = await Database.connect(URL, schema=[Users, Posts], pool_size=4)
    db_auto = await Database.connect(
        URL, schema=[Users, Posts], pool_size=4, reset_connections="auto"
    )
    await Tortoise.init(
        db_url=URL.replace("postgresql://", "asyncpg://", 1), modules={"models": ["__main__"]}
    )
    results = await measure(
        pool,
        {
            "asyncpg": asyncpg_scenarios(pool),
            "asyncpg no-reset": asyncpg_scenarios(fast_pool),
            "mizzle": mizzle_scenarios(db),
            "mizzle auto": mizzle_scenarios(db_auto),
            "tortoise": tortoise_scenarios(),
        },
    )
    await Tortoise.close_connections()
    await db.close()
    await db_auto.close()
    await fast_pool.close()
    await pool.execute("DROP TABLE bench_posts, bench_users")
    await pool.close()

    libs = list(results)
    names = list(results["mizzle"])
    print(f"{'ops/sec (higher is better)':30}" + "".join(f"{lib:>18}" for lib in libs))
    for name in names:
        row = "".join(f"{results[lib][name]:>18,.0f}" for lib in libs)
        print(f"{name:30}{row}")
    print()
    print("Same-setting comparisons (mizzle / other):")
    print(f"  {'':30}{'vs asyncpg':>14}{'auto vs no-reset':>18}{'vs tortoise':>14}")
    for name in names:
        r = {lib: results[lib][name] for lib in libs}
        print(
            f"  {name:30}{r['mizzle'] / r['asyncpg']:>13.2f}x"
            f"{r['mizzle auto'] / r['asyncpg no-reset']:>17.2f}x"
            f"{r['mizzle'] / r['tortoise']:>13.2f}x"
        )


if __name__ == "__main__":
    asyncio.run(main())

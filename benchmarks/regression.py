"""A small mizzle-vs-asyncpg regression gate, made to run in CI.

``bench.py`` is the full comparison table across libraries and is run by hand. This measures
one thing instead: mizzle's overhead over the raw asyncpg driver, on this machine, in this
process. It reports operations/second for both and fails when mizzle falls below a *fraction*
of asyncpg. A relative gate needs no machine-specific baseline and is stable enough for CI,
while still catching a gross regression (an accidental extra round trip or a lost cache).

    docker compose up -d
    uv run python benchmarks/regression.py

Set ``MIZZLE_TEST_URL`` to point at another database. The table it creates is UNLOGGED and
dropped at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from typing import final

import asyncpg

from mizzle import Database, Table, eq
from mizzle.pg import serial, text, varchar

URL = os.environ.get("MIZZLE_TEST_URL", "postgresql://mizzle:mizzle@localhost:54329/mizzle")

DDL = """
DROP TABLE IF EXISTS bench_regression_users;
CREATE UNLOGGED TABLE bench_regression_users (
    id serial PRIMARY KEY,
    email varchar(255) NOT NULL,
    bio text
);
"""


@final
class RegUser(Table, name="bench_regression_users"):
    id = serial().primary_key()
    email = varchar(255).not_null()
    bio = text()


async def reset(pool: asyncpg.Pool, rows: int) -> None:
    await pool.execute(DDL)
    await pool.execute(
        "INSERT INTO bench_regression_users (email, bio)"
        " SELECT 's' || i || '@example.com', 'bio' FROM generate_series(1, $1) i",
        rows,
    )


async def timed(op: Callable[[], Awaitable[object]]) -> float:
    start = time.perf_counter()
    await op()
    return time.perf_counter() - start


def rate(times: list[float], operations: int) -> float:
    return operations / statistics.median(times)


# Each scenario: name, operations per round, the asyncpg op, the mizzle op.
asyncpg_op = Callable[[asyncpg.Pool], Awaitable[object]]
mizzle_op = Callable[[Database], Awaitable[object]]


def scenarios(rows: int) -> list[tuple[str, int, asyncpg_op, mizzle_op]]:
    async def apg_insert(pool: asyncpg.Pool) -> None:
        for i in range(rows):
            await pool.execute(
                "INSERT INTO bench_regression_users (email, bio) VALUES ($1, $2)",
                f"a{i}@example.com",
                "hi",
            )

    async def mz_insert(db: Database) -> None:
        for i in range(rows):
            await db.insert(RegUser).values(email=f"m{i}@example.com", bio="hi")

    async def apg_get(pool: asyncpg.Pool) -> None:
        for i in range(1, rows + 1):
            await pool.fetchrow(
                "SELECT id, email, bio FROM bench_regression_users WHERE id = $1", i
            )

    async def mz_get(db: Database) -> None:
        for i in range(1, rows + 1):
            await db.select().from_(RegUser).where(eq(RegUser.id, i))

    async def apg_update(pool: asyncpg.Pool) -> None:
        for i in range(1, rows + 1):
            await pool.execute(
                "UPDATE bench_regression_users SET bio = $1 WHERE id = $2", "changed", i
            )

    async def mz_update(db: Database) -> None:
        for i in range(1, rows + 1):
            await db.update(RegUser).set(bio="changed").where(eq(RegUser.id, i))

    async def apg_scan(pool: asyncpg.Pool) -> None:
        await pool.fetch(f"SELECT id, email, bio FROM bench_regression_users LIMIT {rows}")

    async def mz_scan(db: Database) -> None:
        await db.select().from_(RegUser).limit(rows)

    return [
        ("insert 1 row", rows, apg_insert, mz_insert),
        ("get by primary key", rows, apg_get, mz_get),
        ("update by primary key", rows, apg_update, mz_update),
        ("select many rows", 1, apg_scan, mz_scan),
    ]


async def measure(
    pool: asyncpg.Pool, db: Database, rows: int, rounds: int
) -> dict[str, tuple[float, float]]:
    results: dict[str, tuple[float, float]] = {}
    win = 17
    print(f"{'ops/sec (higher is better)':24}{'asyncpg':>{win}}{'mizzle':>{win}}{'ratio':>{win}}")
    for name, operations, apg_op, mz_op in scenarios(rows):
        apg_times: list[float] = []
        mz_times: list[float] = []
        # One warm-up round each: statement cache and pool connections.
        await reset(pool, rows)
        await apg_op(pool)
        await reset(pool, rows)
        await mz_op(db)
        for _ in range(rounds):
            await reset(pool, rows)
            apg_times.append(await timed(lambda op=apg_op: op(pool)))
            await reset(pool, rows)
            mz_times.append(await timed(lambda op=mz_op: op(db)))
        apg, mz = rate(apg_times, operations), rate(mz_times, operations)
        results[name] = (apg, mz)
        print(f"{name:24}{apg:>{win},.0f}{mz:>{win},.0f}{mz / apg:>{win}.2f}x")
    return results


# Floors per scenario, as a fraction of asyncpg's ops/sec. Measured on a developer machine
# at 1.39x (insert), 1.34x (get), 1.19x (update) and 0.75x (select many); each floor is about
# 85% of that, which leaves room for CI noise but not for an extra round trip per statement
# (which drops the single-row scenarios to 0.38-0.55x). --min-ratio overrides every floor.
FLOORS = {
    "insert 1 row": 1.18,
    "get by primary key": 1.14,
    "update by primary key": 1.01,
    "select many rows": 0.64,
}


async def run(min_ratio: float | None, rows: int, rounds: int) -> int:
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    db = await Database.connect(URL, schema=[RegUser], pool_size=4)
    try:
        async with pool.acquire() as conn:
            for _ in range(20):
                await conn.fetchval("SELECT 1")
            start = time.perf_counter()
            for _ in range(200):
                await conn.fetchval("SELECT 1")
            rtt = (time.perf_counter() - start) / 200 * 1000
        results = await measure(pool, db, rows, rounds)
    finally:
        await db.close()
        await pool.execute("DROP TABLE IF EXISTS bench_regression_users")
        await pool.close()

    print(f"\nRound trip: {rtt:.2f} ms per SELECT 1.")
    failures: list[str] = []
    for name, (apg, mz) in results.items():
        measured = mz / apg
        floor = min_ratio if min_ratio is not None else FLOORS[name]
        verdict = "ok" if measured >= floor else "FAIL"
        print(f"  {name:24}{measured:5.2f}x  (floor {floor:.2f}x)  {verdict}")
        if measured < floor:
            failures.append(f"{name!r} at {measured:.2f}x, below its floor {floor:.2f}x")
    if failures:
        print(
            "\nFAIL: " + "; ".join(failures) + ".\n"
            "A per-scenario floor near the measured ratio is what catches a regression: the "
            "old single 0.25x gate passed even with two extra round trips per statement.",
            file=sys.stderr,
        )
        return 1
    print("OK: every scenario is at or above its floor.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-ratio",
        type=float,
        default=None,
        help="fail when mizzle is below this fraction of asyncpg's ops/sec for every "
        "scenario (default: a per-scenario floor, see FLOORS)",
    )
    parser.add_argument("--rows", type=int, default=300, help="rows per round (default: 300)")
    parser.add_argument("--rounds", type=int, default=5, help="timed rounds per scenario")
    args = parser.parse_args()
    return asyncio.run(run(args.min_ratio, args.rows, args.rounds))


if __name__ == "__main__":
    raise SystemExit(main())

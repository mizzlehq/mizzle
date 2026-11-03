"""Load, restart and soak tests against the app served by uvicorn (4 worker processes, all
migrating at startup) over real HTTP.

    test_load       50 concurrent clients, ~3000 mixed requests
    test_restart    the same while the primary restarts (SHOP_RESTART_CMD)
    test_soak       SHOP_SOAK_SECONDS of mixed load; worker memory and connections stay flat

After each run the data must add up: stock never negative, every order total is the sum of
its lines, units sold plus units in stock equal the starting stock, no connection leaked.
"""

import asyncio
import collections
import os
import random
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import (
    admin_connect,
    drop_database,
    recreate_database,
    wait_for_replica,
    with_database,
)

ROOT = Path(__file__).resolve().parent.parent
DB_NAME = "shop_load"
WORKERS = 4
POOL_SIZE = 8
PRODUCTS = 20
STOCK = {"main": 40, "backup": 20}


@dataclass
class Server:
    base: str
    process: subprocess.Popen[bytes]
    url: str  # direct to the primary, for checks
    log: Path

    def worker_pids(self) -> list[int]:
        # Workers are spawned from several threads: read every thread's children.
        tasks = Path(f"/proc/{self.process.pid}/task").iterdir()
        pids = [int(p) for t in tasks for p in (t / "children").read_text().split()]
        return [
            p for p in pids if b"resource_tracker" not in Path(f"/proc/{p}/cmdline").read_bytes()
        ]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def rss_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


@pytest.fixture(scope="module")
async def server(
    admin_url: str, tmp_path_factory: pytest.TempPathFactory
) -> AsyncGenerator[Server]:
    await recreate_database(admin_url, DB_NAME)
    direct = with_database(admin_url, DB_NAME, application_name=DB_NAME)
    replica = os.environ.get("SHOP_REPLICA_URL")
    bouncer = os.environ.get("SHOP_BOUNCER_URL")
    if replica:
        await wait_for_replica(direct, with_database(replica, DB_NAME))
    env = {
        **os.environ,
        "DATABASE_URL": direct,
        "MIGRATE_ON_START": "1",  # all workers at once: the advisory lock serializes them
        "POOL_SIZE": str(POOL_SIZE),
        "REPLICA_URLS": with_database(replica, DB_NAME, application_name=DB_NAME)
        if replica
        else "",
    }
    if bouncer:
        migrate = subprocess.run(
            [sys.executable, "-m", "mizzle", "migrate"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert migrate.returncode == 0, migrate.stderr
        env |= {
            "DATABASE_URL": with_database(bouncer, DB_NAME, application_name=DB_NAME),
            "MIGRATE_ON_START": "0",
            "STATEMENT_CACHE_SIZE": "0",
        }
    port = free_port()
    log = tmp_path_factory.mktemp("uvicorn") / "server.log"
    with log.open("wb") as out:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "shop.app:create_app",
                "--factory",
                "--port",
                str(port),
                "--workers",
                str(WORKERS),
                "--log-level",
                "warning",
                "--timeout-keep-alive",  # no idle-close race with the clients' keep-alive
                "60",
            ],
            cwd=ROOT,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base) as c:
            for _ in range(300):
                try:
                    if (await c.get("/admin/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert process.poll() is None, log.read_text()
                await asyncio.sleep(0.1)
        yield Server(base, process, direct, log)
    finally:
        process.terminate()
        process.wait(30)
        await drop_database(admin_url, DB_NAME)


@dataclass
class Shop:
    seller: str
    store_id: int
    slug: str
    products: list[int]
    customers: list[str]


async def seed(c: httpx.AsyncClient, name: str, server: Server) -> Shop:
    async def post(url: str, body: object, user: str | None = None) -> Any:
        r = await c.post(url, json=body, headers={"X-User-Id": user} if user else {})
        assert r.status_code == 201, r.text
        return r.json()

    seller = (await post("/users", {"email": f"{name}@load.io", "name": name, "role": "seller"}))[
        "id"
    ]
    replica = os.environ.get("SHOP_REPLICA_URL")
    if replica:  # the app looks users up on the replica
        await wait_for_replica(server.url, with_database(replica, DB_NAME))
    store = await post("/stores", {"slug": name, "name": name}, seller)
    products = [
        (
            await post(
                f"/stores/{store['id']}/products",
                {"sku": f"w{i}", "name": f"widget {i}", "price": f"{1 + i}.25", "stock": STOCK},
                seller,
            )
        )["id"]
        for i in range(PRODUCTS)
    ]
    customers = [
        (await post("/users", {"email": f"{name}-c{i}@load.io", "name": f"c{i}"}))["id"]
        for i in range(50)
    ]
    if replica:
        await wait_for_replica(server.url, with_database(replica, DB_NAME))
    return Shop(seller, store["id"], name, products, customers)


@dataclass
class Tally:
    codes: collections.Counter[tuple[str, int]] = field(
        default_factory=collections.Counter[tuple[str, int]]
    )
    errors: list[str] = field(default_factory=list[str])
    created: int = 0


EXPECTED = {
    "product": {200},
    "search": {200},
    "store": {200},
    "order": {201, 409},
    "pay": {200, 409},
    "cancel": {200, 409},
    "review": {200},
    "orders": {200},
}


async def client_loop(
    c: httpx.AsyncClient,
    shop: Shop,
    user: str,
    n: int | None,
    tally: Tally,
    rng: random.Random,
    until: Callable[[], bool],
) -> None:
    headers = {"X-User-Id": user}
    mine: list[int] = []
    i = 0
    while (n is None or i < n) and not until():
        i += 1
        roll = rng.random()
        try:
            if roll < 0.25:
                kind, r = "product", await c.get(f"/products/{rng.choice(shop.products)}")
            elif roll < 0.35:
                kind, r = (
                    "search",
                    await c.get("/products/search", params={"q": "widget", "limit": 5}),
                )
            elif roll < 0.45:
                kind, r = "store", await c.get(f"/stores/{shop.slug}")
            elif roll < 0.75:
                items = [
                    {"product_id": p, "quantity": rng.randint(1, 3)}
                    for p in rng.sample(shop.products, rng.randint(1, 3))
                ]
                kind = "order"
                r = await c.post(
                    "/orders", json={"store_id": shop.store_id, "items": items}, headers=headers
                )
                if r.status_code == 201:
                    mine.append(r.json()["order"]["id"])
                    tally.created += 1
            elif roll < 0.85 and mine:
                kind, r = "pay", await c.post(f"/orders/{rng.choice(mine)}/pay", headers=headers)
            elif roll < 0.90 and mine:
                kind, r = (
                    "cancel",
                    await c.post(f"/orders/{rng.choice(mine)}/cancel", headers=headers),
                )
            elif roll < 0.95:
                body = {"rating": rng.randint(1, 5), "body": "ok"}
                kind, r = (
                    "review",
                    await c.put(
                        f"/products/{rng.choice(shop.products)}/review", json=body, headers=headers
                    ),
                )
            else:
                kind, r = "orders", await c.get("/orders", headers=headers)
        except httpx.TransportError as exc:
            tally.errors.append(f"transport: {exc!r}")
            continue
        tally.codes[(kind, r.status_code)] += 1
        if r.status_code not in EXPECTED[kind]:
            tally.errors.append(f"{kind} {r.status_code} {r.text[:200]}")


async def run_load(
    server: Server,
    shop: Shop,
    clients: int,
    requests_each: int | None,
    until: Callable[[], bool] = lambda: False,
    seed_value: int = 1,
) -> Tally:
    tally = Tally()
    limits = httpx.Limits(max_connections=clients, max_keepalive_connections=clients)
    async with httpx.AsyncClient(base_url=server.base, limits=limits, timeout=30) as c:
        await asyncio.gather(
            *(
                client_loop(
                    c,
                    shop,
                    shop.customers[i % len(shop.customers)],
                    requests_each,
                    tally,
                    random.Random(seed_value * 1000 + i),
                    until,
                )
                for i in range(clients)
            )
        )
    return tally


async def check_invariants(server: Server, shop: Shop, created: int | None) -> None:
    conn = await admin_connect(server.url)
    try:
        lowest = await conn.fetchval("SELECT min(quantity) FROM inventory")
        assert isinstance(lowest, int) and lowest >= 0
        bad_totals = await conn.fetchval(
            "SELECT count(*) FROM orders o WHERE total <> "
            "(SELECT coalesce(sum(line_total), 0) FROM order_items i WHERE i.order_id = o.id)"
        )
        assert bad_totals == 0
        rows = await conn.fetch(
            "SELECT p.id,"
            " (SELECT sum(quantity) FROM inventory i WHERE i.product_id = p.id) AS stock,"
            " (SELECT coalesce(sum(oi.quantity), 0) FROM order_items oi"
            "  JOIN orders o ON o.id = oi.order_id"
            "  WHERE oi.product_id = p.id AND o.status <> 'cancelled') AS sold"
            " FROM products p WHERE p.store_id = $1",
            shop.store_id,
        )
        start = sum(STOCK.values())
        assert [(r["id"], r["stock"] + r["sold"]) for r in rows] == [(r["id"], start) for r in rows]
        orders = await conn.fetchval(
            "SELECT count(*) FROM orders WHERE store_id = $1", shop.store_id
        )
        if created is not None:
            assert orders == created
        audited = await conn.fetchval(
            "SELECT count(*) FROM shop_audit.audit_log"
            " WHERE table_name = 'orders' AND action = 'insert'"
            " AND (data ->> 'store_id')::int = $1",
            shop.store_id,
        )
        assert audited == orders
    finally:
        await conn.close()


async def connections(server: Server) -> tuple[int, int]:
    """(open, busy) connections of the app on the primary."""
    conn = await admin_connect(server.url)
    try:
        row = await conn.fetchrow(
            "SELECT count(*) AS n, count(*) FILTER (WHERE state <> 'idle') AS busy"
            " FROM pg_stat_activity WHERE application_name = $1",
            DB_NAME,
        )
        assert row is not None
        return int(row["n"]), int(row["busy"])
    finally:
        await conn.close()


async def assert_no_leaks(server: Server) -> None:
    await asyncio.sleep(1)
    n, busy = await connections(server)
    behind_bouncer = bool(os.environ.get("SHOP_BOUNCER_URL"))
    assert busy == 0, f"{busy} connections still busy"
    if not behind_bouncer:  # PgBouncer owns the server connections
        assert n <= WORKERS * POOL_SIZE, n


def report(tally: Tally, seconds: float) -> str:
    total = sum(tally.codes.values())
    codes = dict(sorted(tally.codes.items()))
    return f"{total} requests in {seconds:.1f}s ({total / seconds:.0f}/s): {codes}"


async def test_load(server: Server) -> None:
    async with httpx.AsyncClient(base_url=server.base, timeout=30) as c:
        shop = await seed(c, f"load-{random.randrange(10**6)}", server)
    started = time.monotonic()
    tally = await run_load(server, shop, clients=50, requests_each=60)
    print(report(tally, time.monotonic() - started))
    assert tally.errors == [], tally.errors[:10]
    assert sum(tally.codes.values()) == 3000
    await check_invariants(server, shop, tally.created)
    await assert_no_leaks(server)
    assert server.process.poll() is None
    assert "Traceback" not in server.log.read_text()


async def test_restart(server: Server) -> None:
    command = os.environ.get("SHOP_RESTART_CMD")
    if not command:
        pytest.skip("set SHOP_RESTART_CMD to the command that restarts the server under test")
    async with httpx.AsyncClient(base_url=server.base, timeout=30) as c:
        shop = await seed(c, f"restart-{random.randrange(10**6)}", server)
    stop = asyncio.Event()
    load = asyncio.create_task(
        run_load(server, shop, clients=30, requests_each=None, until=stop.is_set, seed_value=2)
    )
    await asyncio.sleep(2)
    restarted = await asyncio.create_subprocess_shell(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    assert await restarted.wait() == 0
    await asyncio.sleep(5)  # load continues against the restarted server
    stop.set()
    during = await load
    print(report(during, 7))
    # While the server is down, requests fail fast with 503 (lost connection). Behind
    # PgBouncer they wait in its queue instead, until the query timeout (504).
    allowed = {"503", "504"} if os.environ.get("SHOP_BOUNCER_URL") else {"503"}
    unexpected = [e for e in during.errors if e.split()[1] not in allowed and "transport" not in e]
    assert unexpected == [], unexpected[:10]

    # Afterwards, everything works again without an app restart.
    after = await run_load(server, shop, clients=20, requests_each=20, seed_value=3)
    assert after.errors == [], after.errors[:10]
    await check_invariants(server, shop, None)
    await assert_no_leaks(server)


async def test_soak(server: Server) -> None:
    seconds = float(os.environ.get("SHOP_SOAK_SECONDS", "0"))
    if not seconds:
        pytest.skip("set SHOP_SOAK_SECONDS (e.g. 600) for the soak run")
    async with httpx.AsyncClient(base_url=server.base, timeout=30) as c:
        shop = await seed(c, f"soak-{random.randrange(10**6)}", server)
    deadline = time.monotonic() + seconds
    samples: list[tuple[float, int, int]] = []  # (time, total worker RSS kB, connections)

    async def sample() -> None:
        while time.monotonic() < deadline:
            rss = sum(rss_kb(pid) for pid in server.worker_pids())
            n, _ = await connections(server)
            samples.append((time.monotonic(), rss, n))
            await asyncio.sleep(10)

    started = time.monotonic()
    tally, *_ = await asyncio.gather(
        run_load(
            server,
            shop,
            clients=20,
            requests_each=None,
            until=lambda: time.monotonic() >= deadline,
            seed_value=4,
        ),
        sample(),
    )
    print(report(tally, time.monotonic() - started))
    for t, rss, n in samples:
        print(f"t={t - started:6.0f}s rss={rss / 1024:7.1f} MiB connections={n}")
    assert tally.errors == [], tally.errors[:10]
    await check_invariants(server, shop, None)
    await assert_no_leaks(server)
    # Memory: after warm-up (first quarter), RSS grows less than 15% + 20 MiB.
    warm = [rss for t, rss, _ in samples if t - started >= seconds / 4]
    assert warm[-1] <= warm[0] * 1.15 + 20 * 1024, (warm[0], warm[-1])
    assert max(n for _, _, n in samples) <= WORKERS * POOL_SIZE

"""Streaming, insert_many, COPY and LISTEN/NOTIFY through PgBouncer and the TLS-only "odd"
server. Skipped unless the tests/infra/compose.yml variables are set."""

import asyncio
import io
from collections.abc import AsyncGenerator
from typing import Any, final

import pytest

from mizzle import Database, DatabaseError, Notification, Table, gt, sql
from mizzle.pg import integer, jsonb, serial, text
from tests.integration.test_infra import env
from tests.integration.test_stress import assert_no_leak


@final
class IoItems(Table, name="infra_io_items"):
    id = serial().primary_key()
    n = integer().not_null()
    label = text().not_null()
    doc = jsonb[Any]()


DDL = """
DROP TABLE IF EXISTS infra_io_items;
CREATE TABLE infra_io_items (
    id serial PRIMARY KEY, n integer NOT NULL, label text NOT NULL, doc jsonb
);
"""


async def setup(url: str) -> None:
    async with await Database.connect(url) as db, db.driver.acquire() as conn:
        await conn.execute(DDL, [])


async def teardown(url: str) -> None:
    async with await Database.connect(url) as db, db.driver.acquire() as conn:
        await conn.execute("DROP TABLE infra_io_items", [])


async def exercise(db: Database) -> None:
    rows = [{"n": i, "label": f"row {i}", "doc": {"i": i, "s": "é"}} for i in range(3000)]
    assert await db.insert_many(IoItems, rows) == 3000
    assert await db.insert_many(IoItems, rows, batch_size=1000) == 3000
    streamed = [r async for r in db.select().from_(IoItems).order_by(IoItems.id).stream(500)]
    assert [(r.n, r.doc) for r in streamed] == [(r["n"], r["doc"]) for r in rows * 2]
    async with db.transaction() as tx:
        async for row in tx.select().from_(IoItems).where(gt(IoItems.n, 2990)).stream(3):
            await tx.update(IoItems).set(label="seen").where(gt(IoItems.id, row.id - 1))
    # COPY is one statement: fine in transaction mode too.
    assert await db.copy_into(IoItems, rows) == 3000
    out = io.BytesIO()
    assert await db.copy_to(db.select(IoItems.n).from_(IoItems).where(gt(IoItems.n, 2998)), out)
    assert out.getvalue().split() == [b"2999"] * 3
    assert_no_leak(db)


async def receives(db: Database, channel: str, url: str | None = None) -> str:
    async with db.listen(channel, url=url) as events:
        await db.notify(channel, "über")
        async with asyncio.timeout(5):
            event = await anext(events)
    assert isinstance(event, Notification)
    return event.payload


@pytest.fixture
async def bouncer_url() -> AsyncGenerator[str]:
    url = env("MIZZLE_PGBOUNCER_URL")
    primary = env("MIZZLE_PRIMARY_URL")
    await setup(primary)
    yield url
    await teardown(primary)


async def test_through_pgbouncer(bouncer_url: str) -> None:
    # Cursors live inside one transaction: fine in transaction mode.
    async with await Database.connect(
        bouncer_url, schema=[IoItems], statement_cache_size=0, pool_size=3
    ) as db:
        await exercise(db)


@pytest.fixture
async def odd_url() -> AsyncGenerator[str]:
    url = env("MIZZLE_ODD_URL")
    await setup(url)
    yield url
    await teardown(url)


async def test_on_the_odd_tls_server(odd_url: str) -> None:
    async with await Database.connect(
        odd_url, schema=[IoItems], pool_size=3, reset_connections="auto"
    ) as db:
        await exercise(db)
        assert_no_leak(db)


async def test_listen_through_pgbouncer_in_transaction_mode(bouncer_url: str) -> None:
    # LISTEN registers on whichever server session ran it, which goes back to PgBouncer's
    # pool: notifications never arrive. listen() checks with a test one and refuses.
    async with await Database.connect(bouncer_url, statement_cache_size=0) as db:
        with pytest.raises(DatabaseError, match="transaction mode"):
            async with db.listen("bounced"):
                pass
        assert await db.execute(sql("select 1 as one")) == [{"one": 1}]  # pool still fine
        # Past the pooler it works.
        assert await receives(db, "bounced", url=env("MIZZLE_PRIMARY_URL")) == "über"


async def test_listen_through_pgbouncer_in_session_mode() -> None:
    async with await Database.connect(env("MIZZLE_PGBOUNCER_SESSION_URL")) as db:
        assert await receives(db, "pooled") == "über"


async def test_listen_on_the_odd_tls_server(odd_url: str) -> None:
    async with await Database.connect(odd_url) as db:
        assert await receives(db, "odd") == "über"

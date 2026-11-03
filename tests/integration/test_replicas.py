from collections.abc import AsyncGenerator

import asyncpg
import pytest

from mizzle import Database, QueryTimeout, eq, sql
from mizzle.dialects.postgres.driver import PostgresDriver
from tests.models import Posts, Users

# The "replica" is the same database with search_path=replica, where users holds other rows.
REPLICA_DDL = """
DROP SCHEMA IF EXISTS replica CASCADE;
CREATE SCHEMA replica;
CREATE TABLE replica.users (LIKE public.users INCLUDING ALL);
INSERT INTO replica.users (email) VALUES ('replica@example.com');
"""


@pytest.fixture
async def db(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    async with driver.acquire() as conn:
        await conn.execute(REPLICA_DDL, [])
    replica = pg_url + ("&" if "?" in pg_url else "?") + "search_path=replica"
    async with await Database.connect(
        pg_url, schema=[Users, Posts], replicas=[replica]
    ) as database:
        yield database
    async with driver.acquire() as conn:
        await conn.execute("DROP SCHEMA replica CASCADE", [])


async def emails(rows: list[Users]) -> list[str]:
    return [row.email for row in rows]


async def test_reads_go_to_replicas_and_writes_to_primary(db: Database) -> None:
    await db.insert(Users).values(email="primary@example.com")
    assert await emails(await db.select().from_(Users)) == ["replica@example.com"]
    assert await emails(await db.query(Users).find_many()) == ["replica@example.com"]
    assert await emails(await db.primary.select().from_(Users)) == ["primary@example.com"]
    assert await emails(await db.select().from_(Users).for_update()) == ["primary@example.com"]
    [row] = await db.execute(sql("select email from users"))
    assert row["email"] == "primary@example.com"
    async with db.transaction() as tx:
        assert await emails(await tx.select().from_(Users)) == ["primary@example.com"]


async def test_query_timeout(pg_url: str, driver: PostgresDriver) -> None:
    async with await Database.connect(pg_url, query_timeout=0.2, pool_size=1) as db:
        with pytest.raises(QueryTimeout):
            await db.execute(sql("select pg_sleep(2)"))
        # The pool's only connection still works after the cancel.
        assert await db.execute(sql("select 1 as one")) == [{"one": 1}]
        await db.with_timeout(5).execute(sql("select pg_sleep(0.3)"))
        async with db.transaction() as tx:
            with pytest.raises(QueryTimeout):
                await tx.with_timeout(0.1).execute(sql("select pg_sleep(2)"))


async def test_server_statement_timeout_is_query_timeout(db: Database) -> None:
    async with db.transaction() as tx:
        await tx.execute(sql("set local statement_timeout = 50"))
        with pytest.raises(QueryTimeout):
            await tx.execute(sql("select pg_sleep(2)"))


async def test_pool_settings(pg_url: str, driver: PostgresDriver) -> None:
    async with await Database.connect(pg_url, pool_size=3, pool_min_size=2, max_idle=1) as db:
        assert await db.execute(sql("select 1 as one")) == [{"one": 1}]


async def test_auto_reset_only_after_raw_sql(
    pg_url: str, driver: PostgresDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets: list[str] = []
    original = asyncpg.Connection.get_reset_query

    def counting(self: asyncpg.Connection) -> str:
        resets.append("reset")
        return original(self)

    monkeypatch.setattr(asyncpg.Connection, "get_reset_query", counting)
    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=1, reset_connections="auto"
    ) as db:
        await db.insert(Users).values(email="a@example.com")
        await db.select().from_(Users).where(eq(Users.email, "a@example.com"))
        async with db.transaction() as tx:
            await tx.update(Users).set(bio="x").where(eq(Users.id, 1))
        assert resets == []  # builder statements cannot change session state

        await db.execute(sql("set application_name = 'leaked'"))
        assert resets == ["reset"]
        # Same (only) connection: the setting did not survive the release.
        [row] = await db.execute(sql("show application_name"))
        assert row["application_name"] != "leaked"

        resets.clear()
        await db.select().from_(Users).where(sql("{} = 1", Users.id))  # raw fragment
        assert resets == ["reset"]


async def test_default_always_resets(
    pg_url: str, driver: PostgresDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets: list[str] = []
    original = asyncpg.Connection.get_reset_query

    def counting(self: asyncpg.Connection) -> str:
        resets.append("reset")
        return original(self)

    monkeypatch.setattr(asyncpg.Connection, "get_reset_query", counting)
    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        await db.select().from_(Users)
    assert resets == ["reset"]


async def test_unreachable_replica_falls_back_to_primary(
    pg_url: str, driver: PostgresDriver
) -> None:
    # pool_min_size=0 opens no connection up front, so the dead replica fails on acquire.
    dead = "postgresql://mizzle:mizzle@127.0.0.1:1/mizzle"
    async with await Database.connect(
        pg_url, schema=[Users, Posts], replicas=[dead], pool_min_size=0
    ) as db:
        await db.insert(Users).values(email="a@example.com")
        assert [u.email for u in await db.select().from_(Users)] == ["a@example.com"]

"""Against real infrastructure: a streaming read replica and PgBouncer (transaction mode).

Start it with ``docker compose -f tests/infra/compose.yml up -d --wait``; see that file for
the environment variables. Skipped when they are not set.
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import final

import pytest

from mizzle import Database, DatabaseError, QueryTimeout, Table, eq, many, one, placeholder, sql
from mizzle.pg import integer, serial, text


@final
class Notes(Table, name="infra_notes"):
    id = serial().primary_key()
    body = text().not_null()
    tags = many(lambda: Tags)


@final
class Tags(Table, name="infra_tags"):
    id = serial().primary_key()
    note_id = integer().not_null().references(lambda: Notes.id, on_delete="cascade")
    name = text().not_null()
    note = one(lambda: Notes, fields=lambda: [Tags.note_id], references=lambda: [Notes.id])


DDL = """
DROP TABLE IF EXISTS infra_tags, infra_notes;
CREATE TABLE infra_notes (id serial PRIMARY KEY, body text NOT NULL);
CREATE TABLE infra_tags (
    id serial PRIMARY KEY,
    note_id integer NOT NULL REFERENCES infra_notes(id) ON DELETE CASCADE,
    name text NOT NULL
);
"""


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"set {name} (see tests/infra/compose.yml)")
    return value


@pytest.fixture
async def primary_url() -> AsyncGenerator[str]:
    url = env("MIZZLE_PRIMARY_URL")
    async with await Database.connect(url) as db, db.driver.acquire() as conn:
        await conn.execute(DDL, [])
    yield url


async def wait_replicated(db: Database, body: str) -> None:
    for _ in range(100):
        try:
            if await db.select().from_(Notes).where(eq(Notes.body, body)):
                return
        except DatabaseError as exc:  # the fixture's CREATE TABLE has not replicated yet
            if exc.sqlstate != "42P01":
                raise
        await asyncio.sleep(0.05)
    raise AssertionError("row never reached the replica")


async def test_real_replica_routing(primary_url: str) -> None:
    replica_url = env("MIZZLE_REPLICA_URL")
    in_recovery = {"standby": sql("pg_is_in_recovery()")}
    async with await Database.connect(
        primary_url, schema=[Notes, Tags], replicas=[replica_url]
    ) as db:
        # A write through db must reach the primary: the replica is read-only.
        [note] = await db.insert(Notes).values(body="hello").returning()
        await db.insert(Tags).values(note_id=note.id, name="a")
        await wait_replicated(db, "hello")

        [row] = await db.select(in_recovery).from_(Notes).limit(1)
        assert row["standby"] is True  # plain reads: replica
        [row] = await db.primary.select(in_recovery).from_(Notes).limit(1)
        assert row["standby"] is False
        [row] = await db.select(in_recovery).from_(Notes).limit(1).for_update()
        assert row["standby"] is False  # row locks: primary
        async with db.transaction() as tx:
            [row] = await tx.select(in_recovery).from_(Notes).limit(1)
            assert row["standby"] is False

        [loaded] = await db.query(Notes).find_many(with_={"tags": True})
        assert [t.name for t in loaded.tags] == ["a"]


async def test_through_pgbouncer_transaction_mode(primary_url: str) -> None:
    url = env("MIZZLE_PGBOUNCER_URL")
    async with await Database.connect(
        url, schema=[Notes, Tags], statement_cache_size=0, pool_size=8
    ) as db:
        [note] = await db.insert(Notes).values(body="bouncer").returning()
        await db.insert(Tags).values([{"note_id": note.id, "name": n} for n in "abc"])
        by_id = db.select().from_(Notes).where(eq(Notes.id, placeholder("id"))).prepare()
        assert [n.body for n in await by_id.execute(id=note.id)] == ["bouncer"]
        [loaded] = await db.query(Notes).find_many(with_={"tags": True})
        assert sorted(t.name for t in loaded.tags) == ["a", "b", "c"]

        async with db.transaction("serializable") as tx:
            await tx.update(Notes).set(body="changed").where(eq(Notes.id, note.id))
            async with tx.transaction():  # savepoint
                await tx.insert(Tags).values(note_id=note.id, name="d")
        assert len(await db.select().from_(Tags)) == 4

        # Many queries at once over few server connections.
        results = await asyncio.gather(
            *(db.select().from_(Notes).where(eq(Notes.id, note.id)) for _ in range(50))
        )
        assert all(r[0].body == "changed" for r in results)

        with pytest.raises(QueryTimeout):
            await db.with_timeout(0.1).execute(sql("select pg_sleep(2)"))
        assert await db.execute(sql("select 1 as one")) == [{"one": 1}]


async def test_auto_reset_through_pgbouncer(primary_url: str) -> None:
    url = env("MIZZLE_PGBOUNCER_URL")
    async with await Database.connect(
        url, schema=[Notes, Tags], statement_cache_size=0, reset_connections="auto"
    ) as db:
        await db.insert(Notes).values(body="auto")
        assert [n.body for n in await db.select().from_(Notes)] == ["auto"]

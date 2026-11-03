"""What a production server looks like: TLS only, unusual server settings, restarts, and big
data.

The TLS and restart tests need ``docker compose -f tests/infra/compose.yml up -d --wait``
(see that file for the environment variables) and are skipped without them. The settings and
big-data tests also run against ``MIZZLE_TEST_URL``, each in a schema of its own.
"""

import asyncio
import datetime as dt
import gc
import os
import tracemalloc
from collections.abc import AsyncGenerator, Generator
from decimal import Decimal
from pathlib import Path
from typing import final
from urllib.parse import quote

import asyncpg
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from mizzle import ConnectionError, Database, QueryError, Table, eq, many, one, sql
from mizzle.pg import bytea, integer, jsonb, serial, text
from tests.integration.test_fuzz_types import DDL as FUZZ_DDL
from tests.integration.test_fuzz_types import Owners, Things, check, thing_values
from tests.integration.test_infra import DDL as NOTES_DDL
from tests.integration.test_infra import Notes, Tags, env
from tests.integration.test_stress import assert_no_leak, pool_of

ROOT = Path(__file__).parents[2]


def with_params(url: str, **params: str) -> str:
    """Add query parameters; asyncpg sends unknown ones as session settings."""
    query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
    return url + ("&" if "?" in url else "?") + query


async def run_ddl(url: str, ddl: str) -> None:
    async with await Database.connect(url, pool_size=1) as db, db.driver.acquire() as conn:
        await conn.execute(ddl, [])


async def odd_database(name: str) -> str:
    """A database of our own on the TLS-only "odd" server (its defaults apply to it)."""
    base = env("MIZZLE_ODD_URL")
    async with await Database.connect(base, pool_size=1) as db:
        if not await db.execute(sql("select 1 from pg_database where datname = {}", name)):
            await db.execute(sql(f"create database {name}"))
    head, _, tail = base.partition("?")
    return head.rsplit("/", 1)[0] + f"/{name}" + (f"?{tail}" if tail else "")


# -- 1. TLS -----------------------------------------------------------------------------


async def test_tls_only_server() -> None:
    url = await odd_database("mizzle_odd")
    assert "sslmode=require" in url
    await run_ddl(url, NOTES_DDL)
    async with await Database.connect(url, schema=[Notes, Tags], pool_size=4) as db:
        [row] = await db.execute(sql("select ssl from pg_stat_ssl where pid = pg_backend_pid()"))
        assert row["ssl"] is True

        [note] = await db.insert(Notes).values(body="secure").returning()
        await db.insert(Tags).values([{"note_id": note.id, "name": n} for n in "xyz"])
        [loaded] = await db.query(Notes).find_many(with_={"tags": True})
        assert sorted(t.name for t in loaded.tags) == ["x", "y", "z"]
        async with db.transaction("serializable") as tx:
            await tx.update(Notes).set(body="changed").where(eq(Notes.id, note.id))
            async with tx.transaction():
                await tx.insert(Tags).values(note_id=note.id, name="w")
        with pytest.raises(RuntimeError):
            async with db.transaction() as tx:
                await tx.delete(Tags).all_rows()
                raise RuntimeError
        assert len(await db.select().from_(Tags)) == 4
        [again] = await db.select().from_(Notes)
        assert again.body == "changed"
        results = await asyncio.gather(*(db.select().from_(Notes) for _ in range(20)))
        assert all(r[0].body == "changed" for r in results)
        assert_no_leak(db)

    plain = url.replace("sslmode=require", "sslmode=disable")
    with pytest.raises(ConnectionError, match="no encryption"):
        await Database.connect(plain, pool_size=1)


# -- 2. Unusual server and session settings ---------------------------------------------

SESSION_SETTINGS = [
    # +05:45, LMT +05:41:16 before 1920
    {"TimeZone": "Asia/Kathmandu", "DateStyle": "German", "IntervalStyle": "postgres_verbose"},
    # -03:30 / -02:30 with DST, LMT -03:30:52
    {"TimeZone": "America/St_Johns", "DateStyle": "SQL, MDY", "IntervalStyle": "sql_standard"},
    {"TimeZone": "Pacific/Chatham", "DateStyle": "Postgres, YMD", "IntervalStyle": "iso_8601"},
    {"extra_float_digits": "0", "TimeZone": "Pacific/Kiritimati"},  # +14:00
]


def targets() -> list[str]:
    return ["odd", *(f"session-{i}" for i in range(len(SESSION_SETTINGS)))]


async def target_url(target: str) -> str:
    if target == "odd":
        return await odd_database("mizzle_odd")
    base = os.environ.get("MIZZLE_TEST_URL")
    if not base:
        pytest.skip("set MIZZLE_TEST_URL")
    await run_ddl(base, "CREATE SCHEMA IF NOT EXISTS prod_env")
    params = SESSION_SETTINGS[int(target.removeprefix("session-"))]
    return with_params(base, search_path="prod_env", **params)


@pytest.fixture(scope="module", params=targets())
def settings_runner(request: pytest.FixtureRequest) -> Generator[tuple[asyncio.Runner, Database]]:
    target: str = request.param
    with asyncio.Runner() as loop:
        url = loop.run(target_url(target))
        loop.run(run_ddl(url, FUZZ_DDL))
        db = loop.run(Database.connect(url, schema=[Owners, Things], pool_size=2))
        yield loop, db
        loop.run(db.close())
        drop = "DROP TABLE fuzz_things, fuzz_owners; DROP TYPE fuzz_mood"
        loop.run(run_ddl(url, drop))


chatham = dt.timezone(dt.timedelta(hours=12, minutes=45))
tricky = {
    "day": dt.date(1, 1, 1),
    "at": dt.datetime(1900, 1, 1, 0, 0, 0, 1),
    "at_tz": dt.datetime(1890, 6, 1, 12, 0, tzinfo=chatham),  # LMT in every zone above
    "clock": dt.time(23, 59, 59, 999999),
    "clock_tz": dt.time(1, 2, 3, 4, tzinfo=chatham),
    "span": -dt.timedelta(days=400, microseconds=1),
    "f8": 0.1 + 0.2,
    "f4": 1.1,
    "money": Decimal("-12345.67890123"),
}


@settings(
    max_examples=int(os.environ.get("MIZZLE_FUZZ_EXAMPLES", "100")),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
# Also a regression: date / timestamp min and max are -infinity / infinity in nested JSON.
@example(rows=[tricky, {"at_tz": dt.datetime(2026, 4, 5, 2, 45, tzinfo=chatham)}])
@example(rows=[{"day": dt.date.max, "at": dt.datetime.min}, {"at": dt.datetime.max}])
@given(rows=st.lists(thing_values, min_size=1, max_size=4))
def test_every_type_round_trips_under_unusual_settings(
    settings_runner: tuple[asyncio.Runner, Database], rows: list[dict[str, object]]
) -> None:
    loop, db = settings_runner

    async def scenario() -> None:
        [owner] = await db.insert(Owners).values({}).returning()
        inserted = (
            await db.insert(Things)
            .values([{**row, "owner_id": owner.id} for row in rows])
            .returning()
        )
        for sent, got in zip(rows, inserted, strict=True):
            check(sent, got, "RETURNING")
        selected = await db.select().from_(Things).where(eq(Things.owner_id, owner.id))
        selected.sort(key=lambda t: t.id)
        for sent, got in zip(rows, selected, strict=True):
            check(sent, got, "SELECT")
        [loaded] = await db.query(Owners).find_many(
            where=eq(Owners.id, owner.id), with_={"things": {"order_by": Things.id}}
        )
        for sent, got in zip(rows, loaded.things, strict=True):
            check(sent, got, "relational JSON")
        [thing] = await db.query(Things).find_many(
            where=eq(Things.id, selected[0].id), with_={"owner": True}
        )
        assert thing.owner is not None and thing.owner.id == owner.id
        await db.delete(Owners).where(eq(Owners.id, owner.id))

    loop.run(scenario())


# -- 3. A real server restart -----------------------------------------------------------


async def restart(command: str) -> None:
    proc = await asyncio.create_subprocess_shell(command, cwd=ROOT)
    assert await proc.wait() == 0, f"{command!r} failed"


async def wait_up(url: str) -> None:
    """Until a brand-new connection works (outside of any pool under test)."""
    async with asyncio.timeout(60):
        while True:
            try:
                conn = await asyncpg.connect(url)
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
                await asyncio.sleep(0.2)
                continue
            await conn.close()
            return


async def load(db: Database, stop: asyncio.Event, errors: list[BaseException]) -> int:
    """Reads and writes until ``stop``; returns how many succeeded."""
    done = 0
    while not stop.is_set():
        try:
            await db.insert(Notes).values(body="load")
            await db.select().from_(Notes).limit(5)
            await db.query(Notes).find_many(limit=2, with_={"tags": True})
            await db.select({"slept": sql("pg_sleep(0.2)")}).from_(Notes).limit(1)
            done += 1
        except Exception as exc:
            errors.append(exc)
            await asyncio.sleep(0.05)
    return done


async def write_after_restart(db: Database) -> None:
    """A write may fail once with ConnectionError (its connection died); then it works."""
    try:
        await db.insert(Notes).values(body="after")
    except ConnectionError:
        await db.insert(Notes).values(body="after")


async def test_server_restart() -> None:
    command = env("MIZZLE_RESTART_CMD")
    url = await odd_database("mizzle_restart")
    await run_ddl(url, NOTES_DDL)
    async with await Database.connect(
        url, schema=[Notes, Tags], pool_size=4, pool_min_size=4
    ) as db:
        await asyncio.gather(*(db.execute(sql("select pg_sleep(0.05)")) for _ in range(4)))
        assert pool_of(db).get_size() == 4

        # Restart under load: the only error anyone sees is ConnectionError.
        stop, errors = asyncio.Event(), list[BaseException]()
        workers = [asyncio.create_task(load(db, stop, errors)) for _ in range(4)]
        await asyncio.sleep(0.3)
        await restart(command)
        await wait_up(url)
        await asyncio.sleep(0.5)
        stop.set()
        assert sum(await asyncio.gather(*workers)) > 0
        assert all(isinstance(e, ConnectionError) for e in errors), errors
        assert_no_leak(db)

        # Restart while idle: every pooled connection is dead, yet reads just work.
        await restart(command)
        await wait_up(url)
        results = await asyncio.gather(
            *(db.select().from_(Notes).limit(1) for _ in range(8)),
            db.query(Notes).find_many(limit=1, with_={"tags": True}),
        )
        assert all(len(r) == 1 for r in results)
        assert_no_leak(db)

        # Restart, then write first.
        await restart(command)
        await wait_up(url)
        await write_after_restart(db)
        async with db.transaction() as tx:
            await tx.insert(Notes).values(body="tx")
        assert_no_leak(db)


async def test_primary_restart_with_replica_and_pgbouncer() -> None:
    command = env("MIZZLE_PRIMARY_RESTART_CMD")
    primary_url = env("MIZZLE_PRIMARY_URL")
    replica_url = env("MIZZLE_REPLICA_URL")
    bouncer_url = env("MIZZLE_PGBOUNCER_URL")
    await run_ddl(primary_url, NOTES_DDL)
    async with (
        await Database.connect(
            primary_url, schema=[Notes, Tags], replicas=[replica_url], pool_size=4
        ) as db,
        await Database.connect(
            bouncer_url, schema=[Notes, Tags], statement_cache_size=0, pool_size=4
        ) as bouncer,
    ):
        await db.insert(Notes).values(body="before")
        await bouncer.insert(Notes).values(body="before")
        for _ in range(100):  # wait for the replica to have the table and rows
            try:
                if len(await db.select().from_(Notes)) == 2:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.05)

        await restart(command)
        # The replica keeps serving reads while the primary is down.
        assert len(await db.select().from_(Notes)) == 2
        await wait_up(primary_url)

        assert len(await db.primary.select().from_(Notes)) == 2
        await write_after_restart(db)
        # PgBouncer reconnects to the primary on its own; its clients may see one error.
        for attempt in range(20):
            try:
                await bouncer.insert(Notes).values(body="bouncer")
                break
            except ConnectionError:
                assert attempt < 19
                await asyncio.sleep(0.25)
        assert len(await bouncer.select().from_(Notes)) == 4
        assert_no_leak(db)
        assert_no_leak(bouncer)


# -- 4. Big data -------------------------------------------------------------------------


@final
class Parents(Table, name="big_parents"):
    id = serial().primary_key()
    name = text()
    items = many(lambda: Items)


@final
class Items(Table, name="big_items"):
    id = serial().primary_key()
    parent_id = integer().not_null().references(lambda: Parents.id)
    body = text()
    blob = bytea()
    doc = jsonb[dict[str, list[int]]]()
    parent = one(lambda: Parents, fields=lambda: [Items.parent_id], references=lambda: [Parents.id])


BIG_DDL = """
CREATE SCHEMA IF NOT EXISTS prod_big;
DROP TABLE IF EXISTS big_items, big_parents;
CREATE TABLE big_parents (id serial PRIMARY KEY, name text);
CREATE TABLE big_items (
    id serial PRIMARY KEY,
    parent_id integer NOT NULL REFERENCES big_parents(id),
    body text, blob bytea, doc jsonb
);
"""


@pytest.fixture
async def big(pg_url: str) -> AsyncGenerator[Database]:
    await run_ddl(pg_url, "CREATE SCHEMA IF NOT EXISTS prod_big")
    url = with_params(pg_url, search_path="prod_big")
    await run_ddl(url, BIG_DDL)
    async with await Database.connect(url, schema=[Parents, Items], pool_size=4) as db:
        yield db
        assert_no_leak(db)
    await run_ddl(pg_url, "DROP SCHEMA prod_big CASCADE")


async def test_huge_single_values(big: Database) -> None:
    body = "é🙂x" * (10 * 2**20 // 7)  # ~10 MB of UTF-8
    blob = os.urandom(10 * 2**20)
    doc = {f"k{i}": list(range(i % 50)) for i in range(40_000)}  # ~5 MB of JSON
    [parent] = await big.insert(Parents).values(name="p").returning()
    [item] = (
        await big.insert(Items)
        .values(parent_id=parent.id, body=body, blob=blob, doc=doc)
        .returning()
    )
    assert (item.body, item.blob, item.doc) == (body, blob, doc)
    [got] = await big.select().from_(Items)
    assert (got.body, got.blob, got.doc) == (body, blob, doc)
    [loaded] = await big.query(Parents).find_many(with_={"items": True})
    [nested] = loaded.items
    assert (nested.body, nested.blob, nested.doc) == (body, blob, doc)
    [child] = await big.query(Items).find_many(with_={"parent": True})
    assert child.parent is not None and child.parent.name == "p"


async def test_200k_rows(big: Database) -> None:
    await big.execute(
        sql("insert into big_parents (name) select 'p' || g from generate_series(1, 10) g")
    )
    await big.execute(
        sql(
            "insert into big_items (parent_id, body) "
            "select 1 + g % 10, 'row ' || g from generate_series(1, 200000) g"
        )
    )
    rows = await big.select().from_(Items)
    assert len(rows) == 200_000
    assert {r.body for r in rows} == {f"row {g}" for g in range(1, 200_001)}
    parents = await big.query(Parents).find_many(
        order_by=Parents.id, with_={"items": {"order_by": Items.id}}
    )
    assert [len(p.items) for p in parents] == [20_000] * 10
    assert parents[0].items[0].body == "row 10"
    assert all(i.parent_id == p.id for p in parents for i in p.items)


async def test_parameter_limit(big: Database) -> None:
    exact = [{"name": f"n{i}"} for i in range(32767)]  # one parameter per row
    assert len(await big.insert(Parents).values(exact).returning()) == 32767
    with pytest.raises(QueryError, match=r"32768 parameters.*32767"):
        await big.insert(Parents).values([*exact, {"name": "one too many"}])
    [count] = await big.execute(sql("select count(*) as n from big_parents"))
    assert count["n"] == 32767


async def test_no_memory_leak_over_many_queries(big: Database) -> None:
    [parent] = await big.insert(Parents).values(name="p").returning()

    async def mixed(i: int) -> None:
        [item] = await big.insert(Items).values(parent_id=parent.id, body=f"b{i}").returning()
        await big.select().from_(Items).where(eq(Items.id, item.id))
        await big.update(Items).set(body="u").where(eq(Items.id, item.id))
        await big.query(Parents).find_many(
            where=eq(Parents.id, parent.id), with_={"items": {"limit": 3}}
        )
        async with big.transaction() as tx:
            await tx.delete(Items).where(eq(Items.id, item.id))

    async def batch(start: int) -> None:
        await asyncio.gather(*(mixed(i) for i in range(start, start + 50)))

    for start in range(0, 500, 50):  # warm up caches and the pool
        await batch(start)
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for start in range(0, 4000, 50):  # 4000 x 5 = 20k queries
            await batch(start)
        gc.collect()
        grown = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert grown < 2 * 2**20, f"memory grew by {grown / 2**20:.1f} MB over 20k queries"

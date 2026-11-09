"""The runtime under the app: replicas, lost connections, session resets, bulk inserts,
streaming, transactions and the less common query features, on the app's own database."""

import asyncio
import contextlib
import datetime
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import final

import asyncpg
import pytest
from shop.models import ProductIn, ProductPatch, UserOut
from shop.schema import TABLES, Inventory, OrderItems, Orders, Products, Stores, UserRole, Users

from mizzle import (
    ConnectionError,
    Database,
    DatabaseError,
    ForeignKeyViolation,
    QueryError,
    QueryTimeout,
    SerializationFailure,
    UniqueViolation,
    abs_,
    alias,
    and_,
    cast,
    coalesce,
    current_date,
    eq,
    extract,
    func,
    greatest,
    in_,
    least,
    length,
    lt,
    nullif,
    placeholder,
    sql,
    trim,
    upper,
)
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.pg import text
from tests.conftest import DB_NAME, Sync, admin_connect, with_database

RECOVERY = func[bool]("pg_is_in_recovery")


def pool_of(db: Database) -> asyncpg.Pool:
    driver = db.driver
    assert isinstance(driver, PostgresDriver)
    return driver._pool


def assert_no_leak(db: Database) -> None:
    pool = pool_of(db)
    assert pool.get_idle_size() == pool.get_size(), "a connection was not returned to the pool"


@pytest.fixture
async def seed(db: Database, sync: Sync) -> AsyncGenerator[tuple[Users, Stores, list[Products]]]:
    t = uuid.uuid4().hex[:8]
    [user] = (
        await db.insert(Users)
        .values(email=f"rt-{t}@x.io", name="rt", role=UserRole.SELLER)
        .returning()
    )
    [store] = (
        await db.insert(Stores).values(owner_id=user.id, slug=f"rt-{t}", name="rt").returning()
    )
    products = await (
        db.insert(Products)
        .values(
            [
                {"store_id": store.id, "sku": f"s{i}", "name": f"P{i}", "price": Decimal(i)}
                for i in range(1, 31)
            ]
        )
        .returning()
    )
    await sync()
    yield user, store, products
    await db.delete(Orders).where(eq(Orders.store_id, store.id))  # items RESTRICT products
    await db.delete(Stores).where(eq(Stores.id, store.id))


# -- replicas ---------------------------------------------------------------------------


async def test_reads_go_to_the_replica(db: Database, replica_url: str | None, seed: object) -> None:
    del seed
    if replica_url is None:
        pytest.skip("set SHOP_REPLICA_URL")
    one = db.select(RECOVERY).from_(Users).limit(1)
    assert await one == [(True,)]  # a hot standby
    assert await db.primary.select(RECOVERY).from_(Users).limit(1) == [(False,)]
    assert await one.for_update() == [(False,)]  # locking reads use the primary
    async with db.transaction() as tx:
        assert await tx.select(RECOVERY).from_(Users).limit(1) == [(False,)]
    rows = await db.execute(sql("SELECT pg_is_in_recovery() AS r"))  # raw: primary
    assert rows == [{"r": False}]


@final
class Proxy:
    """A TCP proxy to stand in for a replica that goes away."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.writers: list[asyncio.StreamWriter] = []
        self.server: asyncio.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers += [writer, up_writer]

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except OSError:
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        for w in self.writers:
            w.close()
        await self.server.wait_closed()


async def test_replica_down_falls_back_to_primary(
    app_url: str, replica_url: str | None, seed: tuple[Users, Stores, list[Products]]
) -> None:
    if replica_url is None:
        pytest.skip("set SHOP_REPLICA_URL")
    port = int(replica_url.split("@")[1].split("/")[0].split(":")[1])
    proxy = Proxy(port)
    local = await proxy.start()
    proxied = replica_url.replace(f":{port}/", f":{local}/")
    async with await Database.connect(
        app_url, schema=TABLES, replicas=[proxied], pool_size=3
    ) as db:
        q = (
            db.select(RECOVERY, Products.name)
            .from_(Products)
            .where(eq(Products.store_id, seed[1].id))
        )
        assert {r for (r, _) in await q} == {True}
        await proxy.stop()  # the replica is gone, its pooled connections die
        rows = await q
        assert {r for (r, _) in rows} == {False} and len(rows) == 30
        assert (
            len(await db.query(Products).find_many(where=eq(Products.store_id, seed[1].id))) == 30
        )
        assert_no_leak(db)


# -- lost connections, session reset ---------------------------------------------------


async def terminate_app_backends(admin_url: str) -> int:
    admin = await admin_connect(admin_url)
    try:
        return len(
            await admin.fetch(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND backend_type = 'client backend'"
                " AND pid <> pg_backend_pid()",
                DB_NAME,
            )
        )
    finally:
        await admin.close()


async def test_reads_survive_killed_connections(
    db: Database, admin_url: str, seed: tuple[Users, Stores, list[Products]]
) -> None:
    store = seed[1]
    q = db.primary.select(Products.id).from_(Products).where(eq(Products.store_id, store.id))
    await asyncio.gather(*(q for _ in range(6)))  # several pooled connections
    assert await terminate_app_backends(admin_url) >= 1
    assert len(await q) == 30  # retried once on a fresh connection: no error
    assert await terminate_app_backends(admin_url) >= 1
    with contextlib.suppress(ConnectionError):  # writes are never retried
        await db.update(Products).set(active=True).where(eq(Products.store_id, store.id))
    assert await db.update(Products).set(active=True).where(eq(Products.store_id, store.id)) == 30
    assert_no_leak(db)


async def test_session_state_never_leaks(
    db: Database, seed: tuple[Users, Stores, list[Products]]
) -> None:
    """reset_connections="auto": SET LOCAL ends with the transaction; raw SQL is reset."""
    user = seed[0]

    async def as_user() -> None:
        async with db.transaction() as tx:
            await tx.execute(
                sql("SELECT set_config('request.jwt.claims', {}, true)", f'{{"sub": "{user.id}"}}')
            )
            await tx.execute(sql("SET LOCAL ROLE shop_authenticated"))
            [row] = await tx.execute(sql("SELECT current_user AS u, auth.uid() AS uid"))
            assert row == {"u": "shop_authenticated", "uid": user.id}

    await asyncio.gather(*(as_user() for _ in range(20)))
    await db.primary.execute(sql("SET search_path = nowhere"))  # raw: reset afterwards
    check = sql(
        "SELECT current_user AS u, current_setting('request.jwt.claims', true) AS c,"
        " current_setting('search_path') AS p"
    )
    rows = await asyncio.gather(*(db.primary.execute(check) for _ in range(20)))
    assert {(r[0]["u"], r[0]["c"] or "", r[0]["p"]) for r in rows} == {
        ("mizzle", "", '"$user", public')
    }
    assert len(await db.primary.select(Products.id).from_(Products).limit(1)) == 1
    assert_no_leak(db)


# -- bulk I/O and streaming ---------------------------------------------------------------


async def test_stream(db: Database, seed: tuple[Users, Stores, list[Products]]) -> None:
    store = seed[1]
    q = (
        db.select(Products.sku, Products.price)
        .from_(Products)
        .where(eq(Products.store_id, store.id))
        .order_by(Products.id)
    )
    streamed = [row async for row in q.stream(batch_size=7)]
    assert streamed == await q and len(streamed) == 30
    rel = db.query(Products).find_many(
        where=eq(Products.store_id, store.id), order_by=Products.id, with_={"store": True}
    )
    names = [p.store.slug async for p in rel.stream(batch_size=4) if p.store is not None]
    assert names == [store.slug] * 30


async def test_insert_many_batches(
    db: Database, seed: tuple[Users, Stores, list[Products]]
) -> None:
    store = seed[1]
    rows = [
        {"store_id": store.id, "sku": f"m{i}", "name": "m", "price": Decimal("0.5")}
        for i in range(700)
    ]
    assert await db.insert_many(Products, rows, batch_size=100) == 700
    with pytest.raises(UniqueViolation):  # all or nothing
        await db.insert_many(Products, [{**rows[0], "sku": "fresh"}, rows[0]], batch_size=1)
    [(n,)] = (
        await db.primary.select(func[int]("count", Products.id))
        .from_(Products)
        .where(eq(Products.sku, "fresh"))
    )
    assert n == 0


# -- transactions -------------------------------------------------------------------------


async def test_savepoints_and_rollback(db: Database) -> None:
    t = uuid.uuid4().hex[:8]
    a, b, c = (f"{x}-{t}@x.io" for x in "abc")
    async with db.transaction() as tx:
        await tx.insert(Users).values(email=a, name="a")
        with pytest.raises(UniqueViolation):
            async with tx.transaction() as sp:
                await sp.insert(Users).values(email=a.upper(), name="dup")
        await tx.insert(Users).values(email=b, name="b")
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.insert(Users).values(email=c, name="c")
            raise RuntimeError
    found = await db.primary.select(Users.email).from_(Users).where(in_(Users.email, [a, b, c]))
    assert sorted(e for (e,) in found) == [a, b]
    with pytest.raises(DatabaseError, match="read-only"):
        async with db.transaction(readonly=True) as tx:
            await tx.insert(Users).values(email=f"ro-{t}@x.io", name="x")
    with pytest.raises(QueryError, match="outermost"):
        async with db.transaction() as tx, tx.transaction("serializable"):
            pass


async def test_serializable_retry(db: Database, seed: tuple[Users, Stores, list[Products]]) -> None:
    pid = seed[2][0].id
    await db.insert(Inventory).values(product_id=pid, warehouse="ser", quantity=0)
    attempts = 0
    gate = asyncio.Barrier(2)

    async def bump() -> None:
        nonlocal attempts
        attempts += 1
        async with db.transaction("serializable") as tx:
            [(q,)] = (
                await tx.select(Inventory.quantity)
                .from_(Inventory)
                .where(and_(eq(Inventory.product_id, pid), eq(Inventory.warehouse, "ser")))
            )
            if attempts <= 2:
                await gate.wait()  # both read before either writes
            await (
                tx.update(Inventory)
                .set(quantity=q + 1)
                .where(and_(eq(Inventory.product_id, pid), eq(Inventory.warehouse, "ser")))
            )

    await asyncio.gather(db.retry(bump), db.retry(bump))
    assert attempts == 3  # one conflict, retried once
    [(q,)] = (
        await db.primary.select(Inventory.quantity)
        .from_(Inventory)
        .where(and_(eq(Inventory.product_id, pid), eq(Inventory.warehouse, "ser")))
    )
    assert q == 2
    with pytest.raises(SerializationFailure):

        async def always() -> None:
            raise SerializationFailure("x")

        await db.retry(always, attempts=2, backoff=0.001)


async def test_timeouts(db: Database) -> None:
    with pytest.raises(QueryTimeout):
        await db.with_timeout(0.1).execute(sql("SELECT pg_sleep(2)"))
    with pytest.raises(QueryTimeout):
        async with db.transaction() as tx:
            await tx.with_timeout(0.1).execute(sql("SELECT pg_sleep(2)"))
    assert await db.with_timeout(2).execute(sql("SELECT 1 AS one")) == [{"one": 1}]
    assert_no_leak(db)


# -- identity, generated columns, prepared statements, functions ---------------------------


async def test_identity_and_generated(
    db: Database, seed: tuple[Users, Stores, list[Products]]
) -> None:
    user, store, products = seed
    big = 10**12 + int(uuid.uuid4().int % 10**6)
    with pytest.raises(QueryError, match="overriding_system_value"):
        await db.insert(Orders).values(id=big, user_id=user.id, store_id=store.id)
    [order] = (
        await db.insert(Orders)
        .values(id=big, user_id=user.id, store_id=store.id)
        .overriding_system_value()
        .returning()
    )
    assert order.id == big and order.total == Decimal("0.00")
    with pytest.raises(QueryError, match="generated"):
        await db.insert(OrderItems).values(
            order_id=big,
            line=1,
            product_id=products[0].id,
            quantity=1,
            unit_price=Decimal(1),
            line_total=Decimal(5),
        )
    [item] = (
        await db.insert(OrderItems)
        .values(
            order_id=big, line=1, product_id=products[2].id, quantity=4, unit_price=Decimal("2.25")
        )
        .returning()
    )
    assert item.line_total == Decimal("9.00")
    [item] = (
        await db.update(OrderItems)
        .set(quantity=OrderItems.quantity * 2)
        .where(eq(OrderItems.order_id, big))
        .returning()
    )
    assert (item.quantity, item.line_total) == (8, Decimal("18.00"))


async def test_prepared_statements(
    db: Database, seed: tuple[Users, Stores, list[Products]]
) -> None:
    store = seed[1]
    by_sku = (
        db.select(Products.price)
        .from_(Products)
        .where(and_(eq(Products.store_id, store.id), eq(Products.sku, placeholder("sku"))))
        .prepare()
    )
    results = await asyncio.gather(*(by_sku.execute(sku=f"s{i}") for i in range(1, 31)))
    assert [r[0][0] for r in results] == [Decimal(i) for i in range(1, 31)]
    with pytest.raises(QueryError):
        await by_sku.execute()


async def test_functions_and_relational_alias(
    db: Database, seed: tuple[Users, Stores, list[Products]]
) -> None:
    user, store, _ = seed
    p = Products
    [row] = await (
        db.select(
            greatest(p.price, Decimal(5)),
            least(p.price, Decimal(5)),
            nullif(p.sku, "s3"),
            abs_(p.price - 10),
            length(p.name),
            upper(trim(cast(sql("{}", "  hi  "), text().not_null()))),
            coalesce(p.details, "none"),
            extract("year", current_date()),
            lt(p.created_at - datetime.timedelta(days=1), p.created_at),
        )
        .from_(p)
        .where(and_(eq(p.store_id, store.id), eq(p.sku, "s3")))
    )
    year = datetime.datetime.now(datetime.UTC).year
    assert row[:7] == (Decimal(5), Decimal(3), None, Decimal(7), 2, "HI", "none")
    assert row[7] in (year, year + 1) and row[8] is True

    u = alias(Users, "u")
    found = await db.primary.query(u).find_many(
        where=eq(u.id, user.id),
        with_={"stores": {"with_": {"products": {"limit": 2, "order_by": Products.id}}}},
    )
    assert [(s.slug, [x.sku for x in s.products]) for x in found for s in x.stores] == [
        (store.slug, ["s1", "s2"])
    ]


def test_pydantic_models(seed: tuple[Users, Stores, list[Products]]) -> None:
    user = seed[0]
    out = UserOut.model_validate(user)
    assert out.model_dump()["email"] == user.email
    assert "id" not in ProductPatch.model_fields  # update models never contain the key
    assert "search" not in ProductIn.model_fields  # generated: never client input
    with pytest.raises(ValueError, match="decimal"):
        ProductIn.model_validate({"sku": "x", "name": "x", "price": "1.234"})
    with pytest.raises(ValueError):
        ProductIn.model_validate(
            {"sku": "x", "name": "x", "price": "123456789.00"}
        )  # numeric(10, 2)


async def test_url_helper() -> None:
    assert (
        with_database("postgresql://u:p@h:1/db?sslmode=require", "x", a="b")
        == "postgresql://u:p@h:1/x?sslmode=require&a=b"
    )


async def test_restrict_is_a_foreign_key_violation(
    db: Database, seed: tuple[Users, Stores, list[Products]]
) -> None:
    user, store, products = seed
    [order] = await db.insert(Orders).values(user_id=user.id, store_id=store.id).returning()
    await db.insert(OrderItems).values(
        order_id=order.id, line=1, product_id=products[0].id, quantity=1, unit_price=Decimal(1)
    )
    with pytest.raises(ForeignKeyViolation) as caught:  # order_items RESTRICTs products
        await db.delete(Products).where(eq(Products.id, products[0].id))
    assert (caught.value.sqlstate, caught.value.constraint) == (
        "23001",
        "order_items_product_id_fkey",
    )

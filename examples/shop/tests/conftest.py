"""Fixtures: a fresh ``shop_app`` database on the server under test, the app on top of it
(migrated at startup, like production) and an HTTP client.

    SHOP_URL          admin URL of the server under test (any database on it), required
    SHOP_REPLICA_URL  admin URL of its streaming replica (optional): reads go there
    SHOP_BOUNCER_URL  a PgBouncer (transaction mode) URL that routes to shop_app (optional)

See README.md for the commands per server.
"""

import asyncio
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from shop.app import create_app, get_db

from mizzle import Database

DB_NAME = "shop_app"
APP_NAME = "shop_app"
ROLE_SQL = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'shop_authenticated') THEN
    CREATE ROLE shop_authenticated NOLOGIN;
  END IF;
EXCEPTION WHEN unique_violation OR duplicate_object THEN NULL;  -- created concurrently
END $$;
"""


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"set {name} to run the shop tests (see README.md)")
    return value


def with_database(url: str, name: str, **params: str) -> str:
    """``url`` pointing at database ``name``, with extra query parameters."""
    parts = urlsplit(url)
    query = "&".join(
        [*([parts.query] if parts.query else []), *(f"{k}={v}" for k, v in params.items())]
    )
    return urlunsplit(parts._replace(path=f"/{name}", query=query))


async def admin_connect(url: str) -> asyncpg.Connection:
    ssl = "require" if "sslmode=require" in url else None
    return await asyncpg.connect(url.split("?")[0], ssl=ssl)


async def recreate_database(admin_url: str, name: str) -> None:
    admin = await admin_connect(admin_url)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{name}"')
        await admin.execute(ROLE_SQL)
    finally:
        await admin.close()


async def drop_database(admin_url: str, name: str) -> None:
    admin = await admin_connect(admin_url)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


async def wait_for_replica(primary_url: str, replica_url: str) -> None:
    """Until the replica replayed everything the primary wrote so far."""
    primary = await admin_connect(primary_url)
    replica: asyncpg.Connection | None = None
    try:
        lsn = await primary.fetchval("SELECT pg_current_wal_lsn()")
        async with asyncio.timeout(30):
            while True:
                try:
                    if replica is None:
                        replica = await admin_connect(replica_url)
                    if await replica.fetchval("SELECT pg_last_wal_replay_lsn() >= $1", lsn):
                        return
                except (OSError, asyncpg.PostgresError):
                    replica = None  # e.g. the database is not there yet
                await asyncio.sleep(0.02)
    finally:
        await primary.close()
        if replica is not None:
            await replica.close()


type Sync = Callable[[], Awaitable[None]]


@pytest.fixture(scope="session")
def admin_url() -> str:
    return env("SHOP_URL")


@pytest.fixture(scope="session")
def app_url(admin_url: str) -> str:
    return with_database(admin_url, DB_NAME, application_name=APP_NAME)


@pytest.fixture(scope="session")
def replica_url() -> str | None:
    url = os.environ.get("SHOP_REPLICA_URL")
    return with_database(url, DB_NAME, application_name=APP_NAME) if url else None


@pytest.fixture(scope="session")
async def sync(app_url: str, replica_url: str | None) -> Sync:
    async def wait() -> None:
        if replica_url is not None:
            await wait_for_replica(app_url, replica_url)

    return wait


@pytest.fixture(scope="session")
async def app(
    admin_url: str, app_url: str, replica_url: str | None, sync: Sync
) -> AsyncGenerator[FastAPI]:
    await recreate_database(admin_url, DB_NAME)
    await sync()  # the replica has the new database too
    bouncer = os.environ.get("SHOP_BOUNCER_URL")
    if bouncer:
        # Migrations need a real session (advisory lock): run them directly first.
        direct = create_app(app_url, replicas=[], migrate_on_start=True)
        async with direct.router.lifespan_context(direct):
            pass
        application = create_app(
            with_database(bouncer, DB_NAME, application_name=APP_NAME),
            replicas=[],
            migrate_on_start=False,
            statement_cache_size=0,
        )
    else:
        replicas = [replica_url] if replica_url else []
        application = create_app(app_url, replicas=replicas, migrate_on_start=True, pool_size=8)
    async with application.router.lifespan_context(application):
        await sync()
        yield application
    await drop_database(admin_url, DB_NAME)


@pytest.fixture(scope="session")
def db(app: FastAPI) -> Database:
    del app
    return get_db()


@pytest.fixture(scope="session")
async def client(app: FastAPI, sync: Sync) -> AsyncGenerator[httpx.AsyncClient]:
    async def after_write(response: httpx.Response) -> None:
        if response.request.method != "GET":
            await sync()  # read-your-writes in tests even when reads go to a replica

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", event_hooks={"response": [after_write]}
    ) as c:
        yield c

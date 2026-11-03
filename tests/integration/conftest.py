import os
from collections.abc import AsyncGenerator

import pytest

from mizzle import Database
from mizzle.dialects.postgres.driver import PostgresDriver
from tests.models import Posts, Users

# Until the migration kit exists, tests create the schema with plain DDL.
DDL = """
DROP SCHEMA IF EXISTS replica CASCADE;  -- left by test_replicas if it fails
DROP TABLE IF EXISTS posts, users CASCADE;
DROP TYPE IF EXISTS role;
CREATE TYPE role AS ENUM ('admin', 'user');
CREATE TABLE users (
    id serial PRIMARY KEY,
    email varchar(255) NOT NULL UNIQUE,
    bio text,
    role role NOT NULL DEFAULT 'user',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE posts (
    id serial PRIMARY KEY,
    author_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title text NOT NULL CHECK (title <> ''),
    meta jsonb
);
"""


@pytest.fixture
def pg_url() -> str:
    url = os.environ.get("MIZZLE_TEST_URL")
    if not url:
        pytest.skip("set MIZZLE_TEST_URL to run integration tests (see docker-compose.yml)")
    return url


@pytest.fixture
async def driver(pg_url: str) -> AsyncGenerator[PostgresDriver]:
    drv = await PostgresDriver.connect(pg_url, pool_size=4)
    async with drv.acquire() as conn:
        await conn.execute(DDL, [])
    yield drv
    await drv.close()


@pytest.fixture
async def db(driver: PostgresDriver, pg_url: str) -> AsyncGenerator[Database]:
    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=4) as database:
        yield database

"""MERGE (QB14) against a live database."""

import pytest

from mizzle import Database, eq, is_not_null, lt
from mizzle.core.errors import DatabaseError
from mizzle.dialects.postgres.driver import PostgresDriver
from tests.models import Posts, Role, Users


async def seed(db: Database) -> None:
    await db.insert(Users).values(
        [
            {"email": "ann@x", "bio": "cats", "role": Role.ADMIN},
            {"email": "bob@y", "bio": None},
            {"email": "cid@x", "bio": "dogs"},
        ]
    )
    await db.insert(Posts).values(
        [
            {"author_id": 1, "title": "cat"},
            {"author_id": 3, "title": "bird"},
        ]
    )


async def _version(driver: PostgresDriver) -> int:
    async with driver.acquire() as conn:
        [row] = await conn.fetch("SHOW server_version_num", [])
        return int(str(row[0]))


async def _needs_17(driver: PostgresDriver) -> None:
    if await _version(driver) < 170000:
        pytest.skip("MERGE ... RETURNING / NOT MATCHED BY SOURCE need PostgreSQL 17")


async def _needs_15(driver: PostgresDriver) -> None:
    if await _version(driver) < 150000:
        pytest.skip("MERGE needs PostgreSQL 15")


async def test_merge_upsert(db: Database, driver: PostgresDriver) -> None:
    await _needs_15(driver)
    await seed(db)
    source = db.select(Users.id, Users.email).from_(Users).subquery("src")
    sid, email = source.c("id", int), source.c("email", str)
    affected = await (
        db.merge(Posts)
        .using(source)
        .on(eq(Posts.author_id, sid))
        .when_matched_then_update({"title": email})
        .when_not_matched_then_insert({"author_id": sid, "title": email})
    )
    assert affected == 3
    rows = await db.select(Posts.author_id, Posts.title).from_(Posts).order_by(Posts.author_id)
    assert rows == [(1, "ann@x"), (2, "bob@y"), (3, "cid@x")]


async def test_merge_delete_matched(db: Database, driver: PostgresDriver) -> None:
    await _needs_15(driver)
    await seed(db)
    affected = await (
        db.merge(Posts)
        .using(Users)
        .on(eq(Posts.author_id, Users.id))
        .when_matched_then_delete(where=is_not_null(Users.bio))
    )
    assert affected == 2
    assert await db.select(Posts.id).from_(Posts) == []


async def test_merge_not_matched_by_source(db: Database, driver: PostgresDriver) -> None:
    await _needs_17(driver)
    await seed(db)
    source = db.select(Users.id).from_(Users).where(lt(Users.id, 3)).subquery("src")
    affected = await (
        db.merge(Posts)
        .using(source)
        .on(eq(Posts.author_id, source.c("id", int)))
        .when_not_matched_by_source_then_delete()
    )
    assert affected == 1  # the post by author 3 has no source row
    assert await db.select(Posts.author_id).from_(Posts) == [(1,)]


async def test_merge_returning(db: Database, driver: PostgresDriver) -> None:
    await _needs_17(driver)
    await seed(db)
    source = db.select(Users.id).from_(Users).where(eq(Users.id, 2)).subquery("src")
    sid = source.c("id", int)
    rows = await (
        db.merge(Posts)
        .using(source)
        .on(eq(Posts.author_id, sid))
        .when_not_matched_then_insert({"author_id": sid, "title": "new"})
        .returning(Posts.author_id, Posts.title)
    )
    assert rows == [(2, "new")]


async def test_merge_17_syntax_on_an_older_server_names_the_version(
    db: Database, driver: PostgresDriver
) -> None:
    # Q3: where the syntax is unavailable the error says which version is needed, rather
    # than a bare "syntax error at or near ...".
    version = await _version(driver)
    if version >= 170000:
        pytest.skip("this server supports MERGE ... RETURNING")
    # On 14 the statement stops at MERGE itself; on 15/16 it stops at RETURNING.
    expected = "needs PostgreSQL 17" if version >= 150000 else "needs PostgreSQL 15"
    await seed(db)
    source = db.select(Users.id).from_(Users).subquery("src")
    query = (
        db.merge(Posts)
        .using(source)
        .on(eq(Posts.author_id, source.c("id", int)))
        .when_matched_then_update({"title": "x"})
        .returning(Posts.id)
    )
    with pytest.raises(DatabaseError, match=expected):
        await query

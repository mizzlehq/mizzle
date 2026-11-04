"""MERGE (QB14) against a live database."""

from mizzle import Database, eq, is_not_null, lt
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


async def test_merge_upsert(db: Database) -> None:
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


async def test_merge_delete_matched(db: Database) -> None:
    await seed(db)
    affected = await (
        db.merge(Posts)
        .using(Users)
        .on(eq(Posts.author_id, Users.id))
        .when_matched_then_delete(where=is_not_null(Users.bio))
    )
    assert affected == 2
    assert await db.select(Posts.id).from_(Posts) == []


async def test_merge_not_matched_by_source(db: Database) -> None:
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


async def test_merge_returning(db: Database) -> None:
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

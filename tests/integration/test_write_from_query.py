"""Writing rows from a query (INSERT ... SELECT, UPDATE ... FROM, DELETE ... USING), the
single-row helpers, DISTINCT ON, row-lock strengths and ON CONFLICT ON CONSTRAINT, live."""

import pytest

from mizzle import Database, QueryError, desc, eq
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
            {"author_id": 1, "title": "dog"},
            {"author_id": 3, "title": "bird"},
        ]
    )


async def test_insert_from_select(db: Database) -> None:
    await seed(db)
    source = db.select(Posts.author_id, Posts.title).from_(Posts).where(eq(Posts.id, 1))
    inserted = await db.insert(Posts).from_select([Posts.author_id, Posts.title], source)
    assert inserted == 1
    titles = await db.select(Posts.title).from_(Posts).order_by(Posts.id)
    assert titles == [("cat",), ("dog",), ("bird",), ("cat",)]


async def test_insert_from_select_returning(db: Database) -> None:
    await seed(db)
    source = db.select(Posts.author_id, Posts.title).from_(Posts).where(eq(Posts.id, 2))
    [row] = await db.insert(Posts).from_select([Posts.author_id, Posts.title], source).returning()
    assert (row.author_id, row.title) == (1, "dog")
    assert row.id == 4


async def test_insert_from_select_single_column(db: Database) -> None:
    await seed(db)
    # from_select() takes one column directly, not only a list. The row already exists,
    # so DO NOTHING keeps the copy a no-op.
    n = (
        await db.insert(Users)
        .from_select(Users.email, db.select(Users.email).from_(Users).where(eq(Users.id, 1)))
        .on_conflict_do_nothing()
    )
    assert n == 0


async def test_update_from(db: Database) -> None:
    await seed(db)
    updated = (
        await db.update(Posts)
        .set(title=Users.bio)
        .from_(Users)
        .where(eq(Posts.author_id, Users.id))
    )
    assert updated == 3
    titles = await db.select(Posts.title).from_(Posts).order_by(Posts.id)
    assert titles == [("cats",), ("cats",), ("dogs",)]


async def test_update_from_with_filter(db: Database) -> None:
    await seed(db)
    updated = (
        await db.update(Posts)
        .set(title=Users.email)
        .from_(Users)
        .where(eq(Posts.author_id, Users.id))
        .where(eq(Users.email, "ann@x"))
    )
    assert updated == 2
    titles = await db.select(Posts.title).from_(Posts).order_by(Posts.id)
    assert titles == [("ann@x",), ("ann@x",), ("bird",)]


async def test_delete_using(db: Database) -> None:
    await seed(db)
    deleted = (
        await db.delete(Posts)
        .using(Users)
        .where(eq(Posts.author_id, Users.id))
        .where(eq(Users.email, "ann@x"))
    )
    assert deleted == 2
    titles = await db.select(Posts.title).from_(Posts).order_by(Posts.id)
    assert titles == [("bird",)]


async def test_distinct_on(db: Database) -> None:
    await seed(db)
    rows = (
        await db.select(Posts.author_id, Posts.title)
        .from_(Posts)
        .distinct_on(Posts.author_id)
        .order_by(Posts.author_id, desc(Posts.id))
    )
    assert rows == [(1, "dog"), (3, "bird")]


async def test_first_one_scalar(db: Database) -> None:
    await seed(db)
    query = db.select(Users.email).from_(Users).order_by(Users.id)
    assert await query.first() == ("ann@x",)
    assert await query.scalar() == "ann@x"
    assert await query.where(eq(Users.id, 999)).first() is None
    assert await query.where(eq(Users.id, 1)).one() == ("ann@x",)
    with pytest.raises(QueryError, match="more than one row"):
        await query.one()


async def test_row_lock_strengths(db: Database) -> None:
    await seed(db)
    shared = await db.select(Users.id).from_(Users).where(eq(Users.id, 1)).for_share()
    assert shared == [(1,)]
    key = await db.select(Users.id).from_(Users).where(eq(Users.id, 2)).for_key_share()
    assert key == [(2,)]
    nokey = await db.select(Users.id).from_(Users).where(eq(Users.id, 3)).for_no_key_update()
    assert nokey == [(3,)]


async def test_on_conflict_on_constraint(db: Database) -> None:
    await seed(db)
    n = (
        await db.insert(Users)
        .values(email="ann@x", bio="new")
        .on_conflict_do_update(constraint="users_email_key", set_={"bio": "updated"})
    )
    assert n == 1
    assert await db.select(Users.bio).from_(Users).where(eq(Users.email, "ann@x")) == [("updated",)]
    skipped = (
        await db.insert(Users)
        .values(email="ann@x", bio="again")
        .on_conflict_do_nothing(constraint="users_email_key")
    )
    assert skipped == 0
    assert await db.select(Users.bio).from_(Users).where(eq(Users.email, "ann@x")) == [("updated",)]

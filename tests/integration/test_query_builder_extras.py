"""LATERAL joins, GROUP BY ROLLUP/CUBE/GROUPING SETS, TABLESAMPLE, recursive CTEs and
data-modifying CTEs, against a live database."""

from typing import cast

from mizzle import Database, count, desc, eq, gt
from tests.models import Posts, Role, Users


def total(rows: list[dict[str, object]]) -> list[int]:
    return sorted(cast(int, row["n"]) for row in rows)


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


async def test_left_join_lateral(db: Database) -> None:
    await seed(db)
    top = (
        db.select({"author": Posts.author_id, "n": count()})
        .from_(Posts)
        .group_by(Posts.author_id)
        .subquery("top")
    )
    author, n = top.c("author", int), top.c("n", int)
    rows = (
        await db.select(Users.email, n)
        .from_(Users)
        .left_join_lateral(top, eq(author, Users.id))
        .order_by(Users.id)
    )
    assert rows == [("ann@x", 2), ("bob@y", None), ("cid@x", 1)]


async def test_join_lateral_correlated(db: Database) -> None:
    await seed(db)
    # The subquery refers to the outer posts row, so it must be LATERAL.
    author = db.select(Users.email).from_(Users).where(eq(Users.id, Posts.author_id)).subquery("a")
    rows = (
        await db.select(Posts.title, author.c("email", str))
        .from_(Posts)
        .cross_join_lateral(author)
        .order_by(Posts.id)
    )
    assert rows == [
        ("cat", "ann@x"),
        ("dog", "ann@x"),
        ("bird", "cid@x"),
    ]


async def test_rollup_and_cube(db: Database) -> None:
    await seed(db)
    rolled = await db.select({"n": count()}).from_(Users).rollup(Users.role)
    assert total(rolled) == [1, 2, 3]
    cubed = await db.select({"n": count()}).from_(Users).cube(Users.bio)
    assert total(cubed) == [1, 1, 1, 3]


async def test_grouping_sets(db: Database) -> None:
    await seed(db)
    rows = await db.select({"n": count()}).from_(Users).grouping_sets([Users.role], [])
    assert total(rows) == [1, 2, 3]


async def test_tablesample(db: Database) -> None:
    await seed(db)
    rows = await db.select(Users.id).from_(Users).tablesample("SYSTEM", 100, seed=1)
    assert sorted(i for (i,) in rows) == [1, 2, 3]


async def test_recursive_cte(db: Database) -> None:
    await seed(db)
    seed_cte = db.select({"n": Users.id}).from_(Users).where(eq(Users.id, 3)).cte("walk")
    n = seed_cte.c("n", int)
    step = db.select({"n": n - 1}).from_(seed_cte).where(gt(n, 1))
    tree = (
        db.select({"n": Users.id})
        .from_(Users)
        .where(eq(Users.id, 3))
        .union_all(step)
        .cte("walk", recursive=True)
    )
    rows = (
        await db.with_(tree).select(tree.c("n", int)).from_(tree).order_by(desc(tree.c("n", int)))
    )
    assert rows == [(3,), (2,), (1,)]


async def test_data_modifying_cte(db: Database) -> None:
    await seed(db)
    moved = db.delete(Posts).where(eq(Posts.author_id, 1)).returning().cte("moved")
    rows = (
        await db.with_(moved).select(moved.c("id", int)).from_(moved).order_by(moved.c("id", int))
    )
    assert rows == [(1,), (2,)]
    remaining = await db.select(Posts.id).from_(Posts).order_by(Posts.id)
    assert remaining == [(3,)]

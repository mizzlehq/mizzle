"""Results of: SIMILAR TO, date/time arithmetic, text +, named windows, ts_rank weights,
subqueries in FROM, relational queries on an alias, ORDER BY expressions after UNION,
raw SQL reused next to different types, and OVERRIDING ... VALUE."""

import datetime
from typing import final

import pytest

from mizzle import (
    Database,
    QueryError,
    Table,
    abs_,
    alias,
    and_,
    asc,
    cast,
    count,
    desc,
    eq,
    first_value,
    func,
    lag,
    lower,
    not_similar_to,
    row_number,
    similar_to,
    sql,
    sum_,
    to_tsquery,
    to_tsvector,
    ts_rank,
    ts_rank_cd,
)
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.pg import date, integer, text
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
            {"author_id": 1, "title": "cat cat dog"},
            {"author_id": 1, "title": "dog"},
            {"author_id": 3, "title": "cat and a long text before dog"},
        ]
    )


async def test_similar_to(db: Database) -> None:
    await seed(db)
    q = db.select(Users.email).from_(Users).order_by(Users.id)
    assert await q.where(similar_to(Users.email, "(a|c)%@x")) == [("ann@x",), ("cid@x",)]
    assert await q.where(not_similar_to(Users.email, "%x")) == [("bob@y",)]
    assert await q.where(similar_to(Users.bio, "%a%")) == [("ann@x",)]  # NULL never matches


async def test_date_time_arithmetic(db: Database) -> None:
    await seed(db)
    stamp = datetime.datetime(2026, 3, 1, 12, tzinfo=datetime.UTC)
    await db.update(Users).set(created_at=stamp).all_rows()
    day = datetime.timedelta(days=1, hours=2)
    [row] = await (
        db.select(
            Users.created_at + day,
            Users.created_at - day,
            Users.created_at - stamp,
            Users.created_at - (Users.created_at - day),
            cast(Users.created_at, date().not_null()) - datetime.date(2026, 2, 1),
        )
        .from_(Users)
        .where(eq(Users.id, 1))
    )
    assert row == (stamp + day, stamp - day, datetime.timedelta(0), day, 28)
    # Interval arithmetic, and a Python timedelta on the left.
    elapsed = Users.created_at - datetime.datetime(2026, 2, 28, 12, tzinfo=datetime.UTC)
    [row2] = await (
        db.select(
            day + Users.created_at,
            day + cast(Users.created_at, date().not_null()),
            elapsed + day,
            elapsed - day,
            elapsed + elapsed,
            day + elapsed,
        )
        .from_(Users)
        .where(eq(Users.id, 1))
    )
    one = datetime.timedelta(days=1)  # 2026-03-01 12:00 - 2026-02-28 12:00
    midnight = datetime.datetime(2026, 3, 2, 2)  # 2026-03-01 + 1 day 2 h, without tz
    assert row2 == (stamp + day, midnight, one + day, one - day, one + one, day + one)


async def test_text_concatenation(db: Database) -> None:
    await seed(db)
    rows = await (
        db.select(Users.email + ": " + Users.bio, "<" + lower(Users.email) + ">")
        .from_(Users)
        .order_by(Users.id)
    )
    assert rows == [("ann@x: cats", "<ann@x>"), (None, "<bob@y>"), ("cid@x: dogs", "<cid@x>")]


async def test_named_windows(db: Database) -> None:
    await seed(db)
    rows = await (
        db.select(
            Posts.id,
            row_number().over("w"),
            lag(Posts.id).over("w"),
            first_value(Posts.id).over("w"),
            sum_(Posts.id).over("all"),
        )
        .from_(Posts)
        .window("w", partition_by=Posts.author_id, order_by=desc(Posts.id))
        .window("all")
        .order_by(Posts.id)
    )
    assert rows == [(1, 2, 2, 2, 6), (2, 1, None, 2, 6), (3, 1, None, 3, 6)]


async def test_ts_rank_weights_and_normalization(db: Database) -> None:
    await seed(db)
    vector, query = to_tsvector(Posts.title, config="simple"), to_tsquery("cat & dog")
    weights = [0.1, 0.2, 0.4, 0.9]
    got = await (
        db.select(
            ts_rank(vector, query, weights=weights, normalization=1),
            ts_rank_cd(vector, query, weights=weights),
            ts_rank_cd(vector, query, normalization=2),
        )
        .from_(Posts)
        .where(eq(Posts.id, 3))
    )
    v = "to_tsvector('simple', title)"
    want = await db.execute(
        sql(
            f"SELECT ts_rank('{{0.1,0.2,0.4,0.9}}', {v}, to_tsquery('cat & dog'), 1) AS a, "
            f"ts_rank_cd('{{0.1,0.2,0.4,0.9}}', {v}, to_tsquery('cat & dog')) AS b, "
            f"ts_rank_cd({v}, to_tsquery('cat & dog'), 2) AS c FROM posts WHERE id = 3"
        )
    )
    assert got == [tuple(want[0].values())]
    assert got[0][1] != got[0][2]


async def test_subquery_in_from_and_join(db: Database) -> None:
    await seed(db)
    counts = (
        db.select({"author": Posts.author_id, "n": count()})
        .from_(Posts)
        .group_by(Posts.author_id)
        .subquery("counts")
    )
    author, n = counts.c("author", int), counts.c("n", int)
    rows = await (
        db.select(Users.email, n)
        .from_(Users)
        .left_join(counts, eq(author, Users.id))
        .order_by(Users.id)
    )
    assert rows == [("ann@x", 2), ("bob@y", None), ("cid@x", 1)]
    # Selecting from it; values decode like the inner column (here into the enum).
    roles = db.select(Users.id, Users.role).from_(Users).where(eq(Users.id, 1)).subquery("r")
    [(role,)] = await db.select(roles.c("role", Role)).from_(roles)
    assert role is Role.ADMIN
    # Nested: a subquery of a subquery, with a filter on the outer level.
    outer = db.select({"k": n}).from_(counts).where(eq(n, 2)).subquery("o")
    assert await db.select(outer.c("k", int)).from_(outer) == [(2,)]


async def test_relational_query_on_an_alias(db: Database) -> None:
    await seed(db)
    u = alias(Users, "u")
    found = await db.query(u).find_many(
        where=eq(u.role, Role.USER), order_by=desc(u.id), with_={"posts": True}
    )
    assert [(type(x), x.id, len(x.posts)) for x in found] == [(Users, 3, 1), (Users, 2, 0)]
    first = await db.query(u).find_first(columns=[u.email], where=eq(Users.id, 1))
    assert first is not None and first.email == "ann@x"


async def test_order_by_expression_after_union(db: Database) -> None:
    await seed(db)
    users, posts = db.select(Users.id).from_(Users), db.select(Posts.id).from_(Posts)
    q = users.union_all(posts).order_by(asc(abs_(Users.id - 2)), desc(Users.id)).limit(4)
    assert [i for (i,) in await q] == [2, 2, 3, 3]
    cte = db.select({"k": Users.id}).from_(Users).cte("c")
    k = cte.c("k", int)
    other = alias(Posts, "p")
    q2 = (
        db.with_(cte)
        .select(k)
        .from_(cte)
        .union(db.select(other.id).from_(other))
        .order_by(desc(k * -1))
    )
    assert [i for (i,) in await q2] == [1, 2, 3]
    q3 = db.with_(cte).select(k).from_(cte).except_(db.select(other.id).from_(other)).order_by(k)
    assert await q3 == []


async def test_raw_sql_reused_next_to_different_types(db: Database) -> None:
    await seed(db)
    value = sql("{}", "ann@x")
    # email is varchar, bio is text: the same parameter would get two types.
    q = db.select(Users.id).from_(Users).where(and_(eq(Users.email, value), eq(Users.bio, value)))
    assert await q == []
    # NULL next to an integer and next to text (and cast to both).
    nothing = sql("{}", None)
    q3 = db.select(cast(nothing, integer()), cast(nothing, text())).from_(Users)
    assert await q3.where(and_(eq(Users.id, nothing), eq(Users.bio, nothing))) == []
    assert await q3.where(eq(Users.id, 1)) == [(None, None)]
    one = sql("{}", 1)
    q2 = db.select(Posts.id).from_(Posts).where(and_(eq(Posts.author_id, one), eq(Posts.id, one)))
    assert await q2 == [(1,)]


@final
class Tickets(Table, name="tickets"):
    id = integer().primary_key().generated_always_as_identity()
    label = text()


async def test_overriding_identity_values(db: Database, driver: PostgresDriver) -> None:
    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS tickets; CREATE TABLE tickets "
            "(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, label text)",
            [],
        )
    try:
        with pytest.raises(QueryError, match="overriding_system_value"):
            await db.insert(Tickets).values(id=10, label="x")
        # The bulk paths cannot override: the error says what to use instead.
        for bulk in (db.insert_many, db.copy_into):
            with pytest.raises(QueryError, match=r"overriding_system_value\(\), or make"):
                await bulk(Tickets, [{"id": 5, "label": "x"}])
        await db.insert(Tickets).values(id=10, label="a").overriding_system_value()
        await db.insert(Tickets).values([{"id": 99, "label": "b"}]).overriding_user_value()
        await db.insert(Tickets).values(label="c")
        rows = await db.select(Tickets.id, Tickets.label).from_(Tickets).order_by(Tickets.label)
        assert rows == [(10, "a"), (1, "b"), (2, "c")]
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP TABLE tickets", [])


async def test_cross_join(db: Database) -> None:
    await seed(db)
    rows = await (
        db.select(Users.email, Posts.id).from_(Users).cross_join(Posts).order_by(Users.id, Posts.id)
    )
    users = await db.select(Users.email).from_(Users).order_by(Users.id)
    posts = await db.select(Posts.id).from_(Posts).order_by(Posts.id)
    assert rows == [(u, p) for (u,) in users for (p,) in posts]
    assert len(rows) == 9
    # A nullable column against a non-null one: types check, and the SQL runs.
    assert await db.select(Users.id).from_(Users).where(eq(Users.bio, Users.email)) == []


async def test_auto_reset_after_a_user_named_function(db: Database, pg_url: str) -> None:
    # func("set_config", ..., false) changes the session; mizzle cannot know that of a
    # function you name, so reset_connections="auto" resets the connection after it.
    await seed(db)
    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=1, reset_connections="auto"
    ) as auto:
        leak = func[str]("set_config", "application_name", "leaked", False)
        assert await auto.select(leak).from_(Users).limit(1) == [("leaked",)]
        [(name,)] = (
            await auto.select(func[str]("current_setting", "application_name"))
            .from_(Users)
            .limit(1)
        )
        assert name != "leaked"


async def test_a_select_with_a_data_modifying_cte_runs_on_the_primary(db: Database) -> None:
    # Q1: the DELETE inside the WITH actually runs, and the outer SELECT returns its rows.
    await seed(db)
    moved = db.delete(Posts).where(eq(Posts.author_id, 1)).returning(Posts.id).cte("moved")
    # The main query reads the pre-delete snapshot (PostgreSQL semantics); selecting from
    # the CTE returns the rows the DELETE removed.
    deleted = db.with_(moved).select(moved.c("id", int)).from_(moved)
    assert sorted(await deleted) == [(1,), (2,)]
    assert await db.select(count(Posts.id)).from_(Posts) == [(1,)]  # one post (author 3) left


async def test_ctes_run_on_insert_update_and_delete(db: Database) -> None:
    # Q2: with_() is emitted before every write, not only SELECT.
    await seed(db)
    keep = db.select(Users.id).from_(Users).cte("keep")
    updated = db.with_(keep).update(Posts).set({"title": "renamed"}).where(eq(Posts.author_id, 1))
    assert await updated == 2
    assert await db.select(Posts.title).from_(Posts).where(eq(Posts.title, "renamed")) == [
        ("renamed",),
        ("renamed",),
    ]
    deleted = db.with_(keep).delete(Posts).where(eq(Posts.author_id, 3))
    assert await deleted == 1


async def test_relational_query_keeps_the_with_clause(db: Database) -> None:
    # B9: query() dropped the CTEs, so a data-modifying CTE never ran (silently).
    await seed(db)
    moved = db.delete(Posts).where(eq(Posts.author_id, 3)).returning(Posts.id).cte("moved")
    rows = await db.with_(moved).query(Users).find_many(order_by=Users.id)
    assert [u.email for u in rows] == ["ann@x", "bob@y", "cid@x"]
    # The DELETE inside the WITH ran.
    assert await db.select(count(Posts.id)).from_(Posts) == [(2,)]
    found = await db.with_(moved).query(Users).find_first(where=eq(Users.id, 1))
    assert found is not None and found.email == "ann@x"


async def test_lateral_join_without_a_condition(db: Database) -> None:
    # T6: ON true is written for you, so no sql("true") is needed (and the query stays
    # session-safe).
    await seed(db)
    # ON true pairs every outer row with the subquery's rows, so scope the subquery.
    posts = db.select(Posts.id).from_(Posts).where(eq(Posts.author_id, 1)).subquery("p")
    rows = await (
        db.select(Users.email, posts.c("id", int))
        .from_(Users)
        .join_lateral(posts)
        .order_by(Users.id, posts.c("id", int))
    )
    assert rows == [
        ("ann@x", 1),
        ("ann@x", 2),
        ("bob@y", 1),
        ("bob@y", 2),
        ("cid@x", 1),
        ("cid@x", 2),
    ]


async def test_one_keeps_a_tighter_user_limit(db: Database) -> None:
    # T4: one() must not rewrite a limit the caller already set to be exact.
    await seed(db)
    row = await db.select(Users.email).from_(Users).order_by(Users.email).limit(1).one()
    assert row == ("ann@x",)


async def test_as_rows_names_a_projection(db: Database) -> None:
    from typing import TypedDict

    class UserRow(TypedDict):
        id: int
        email: str

    await seed(db)
    rows = (
        await db.select({"id": Users.id, "email": Users.email})
        .from_(Users)
        .as_rows(UserRow)
        .order_by(Users.id)
    )
    assert rows[0] == {"id": 1, "email": "ann@x"}
    with pytest.raises(QueryError, match="projection has"):
        db.select({"id": Users.id}).from_(Users).as_rows(UserRow)

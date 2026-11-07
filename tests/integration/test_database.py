import asyncio
import datetime
import traceback

import pytest

from mizzle import (
    CheckViolation,
    ConnectionError,
    Database,
    DatabaseError,
    ForeignKeyViolation,
    MizzleError,
    NotNullViolation,
    QueryError,
    QueryEvent,
    UniqueViolation,
    count,
    desc,
    eq,
    excluded,
    nullable,
    placeholder,
    sql,
)
from tests.models import Posts, Role, Users


async def add_user(db: Database, email: str, role: Role = Role.USER) -> Users:
    [user] = await db.insert(Users).values(email=email, role=role).returning()
    return user


async def test_insert_returning_and_select(db: Database) -> None:
    user = await add_user(db, "ann@x.io", Role.ADMIN)
    assert user.id > 0
    assert user.role is Role.ADMIN
    assert isinstance(user.created_at, datetime.datetime)
    assert await db.select().from_(Users).where(eq(Users.id, user.id)) == [user]


async def test_rowcounts(db: Database) -> None:
    assert await db.insert(Users).values([{"email": "a"}, {"email": "b"}, {"email": "c"}]) == 3
    assert await db.update(Users).set(bio="hi").where(eq(Users.email, "a")) == 1
    assert await db.delete(Users).all_rows() == 3


async def test_columns_mapping_and_aggregates(db: Database) -> None:
    ann = await add_user(db, "ann")
    await db.insert(Posts).values([{"author_id": ann.id, "title": t} for t in ("a", "b")])
    rows = (
        await db.select({"author": Posts.author_id, "n": count()})
        .from_(Posts)
        .group_by(Posts.author_id)
    )
    assert rows == [{"author": ann.id, "n": 2}]
    titles = await db.select(Posts.title).from_(Posts).order_by(desc(Posts.title))
    assert titles == [("b",), ("a",)]


async def test_left_join_gives_none_for_missing(db: Database) -> None:
    ann = await add_user(db, "ann")
    bob = await add_user(db, "bob")
    await db.insert(Posts).values(author_id=ann.id, title="hello")
    rows = await (
        db.select(Users, Posts)
        .from_(Users)
        .left_join(Posts, eq(Posts.author_id, Users.id))
        .order_by(Users.id)
    )
    assert [(u.email, p.title if p else None) for u, p in rows] == [("ann", "hello"), ("bob", None)]
    assert rows[1][0] == bob


async def test_nullable_left_join_columns(db: Database) -> None:
    ann = await add_user(db, "ann")
    await add_user(db, "bob")
    await db.insert(Posts).values(author_id=ann.id, title="hello")
    rows = await (
        db.select(Users.email, nullable(Posts.title))
        .from_(Users)
        .left_join(Posts, eq(Posts.author_id, Users.id))
        .order_by(Users.email)
    )
    assert rows == [("ann", "hello"), ("bob", None)]


async def test_jsonb_roundtrip(db: Database) -> None:
    ann = await add_user(db, "ann")
    [post] = (
        await db.insert(Posts).values(author_id=ann.id, title="t", meta={"k": [1, 2]}).returning()
    )
    assert post.meta == {"k": [1, 2]}


async def test_upsert(db: Database) -> None:
    await add_user(db, "ann")
    [user] = await (
        db.insert(Users)
        .values(email="ann", bio="new")
        .on_conflict_do_update(target=Users.email, set_={"bio": excluded(Users.bio)})
        .returning()
    )
    assert user.bio == "new"
    assert await db.insert(Users).values(email="ann").on_conflict_do_nothing() == 0


async def test_cte_and_subquery(db: Database) -> None:
    await add_user(db, "ann", Role.ADMIN)
    await add_user(db, "bob")
    admins = (
        db.select(Users.id, Users.email)
        .from_(Users)
        .where(eq(Users.role, Role.ADMIN))
        .cte("admins")
    )
    assert await db.with_(admins).select(admins.c("email", str)).from_(admins) == [("ann",)]


async def test_prepared_query(db: Database) -> None:
    await add_user(db, "ann", Role.ADMIN)
    await add_user(db, "bob")
    q = db.select(Users.email).from_(Users).where(eq(Users.role, placeholder("role"))).prepare()
    assert await q.execute(role=Role.ADMIN) == [("ann",)]
    assert await q.execute(role=Role.USER) == [("bob",)]


async def test_raw_execute(db: Database) -> None:
    await add_user(db, "ann")
    rows = await db.execute(sql("select email, {}::int as n from users", 7))
    assert rows == [{"email": "ann", "n": 7}]


async def test_explain(db: Database) -> None:
    plan = await db.explain(db.select().from_(Users).where(eq(Users.email, "ann")))
    assert plan and "QUERY PLAN" in plan[0]
    analyzed = await db.explain(db.select().from_(Users), analyze=True)
    assert analyzed and "QUERY PLAN" in analyzed[0]


async def test_server_data_error_hides_value(db: Database) -> None:
    with pytest.raises(DatabaseError, match="value hidden") as info:
        await db.execute(sql("select {}::text::role", "secret-x"))
    assert "secret-x" not in str(info.value)


async def test_bad_param_type_error_hides_value(db: Database) -> None:
    with pytest.raises(DatabaseError, match="wrong type") as info:
        await db.execute(sql("select {}::int", "secret-value"))
    assert "secret-value" not in str(info.value)


# -- transactions ---------------------------------------------------------------------


async def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.insert(Users).values(email="ann")
            raise RuntimeError("boom")
    assert await db.select(Users.id).from_(Users) == []


async def test_savepoint_rolls_back_inner_only(db: Database) -> None:
    async with db.transaction() as tx:
        await tx.insert(Users).values(email="outer")
        with pytest.raises(RuntimeError):
            async with tx.transaction() as inner:
                await inner.insert(Users).values(email="inner")
                raise RuntimeError("boom")
    assert await db.select(Users.email).from_(Users) == [("outer",)]


async def test_isolation_only_on_outer(db: Database) -> None:
    async with db.transaction(isolation="serializable") as tx:
        with pytest.raises(QueryError, match="outermost"):
            async with tx.transaction(isolation="serializable"):
                pass


async def test_gather_inside_transaction(db: Database) -> None:
    async with db.transaction() as tx:
        await asyncio.gather(*(tx.insert(Users).values(email=f"u{i}") for i in range(10)))
    [(n,)] = await db.select(count()).from_(Users)
    assert n == 10


async def test_lock_rows_in_transaction(db: Database) -> None:
    await add_user(db, "ann")
    async with db.transaction() as tx:
        rows = await tx.select().from_(Users).for_update(skip_locked=True)
        assert len(rows) == 1


# -- errors ---------------------------------------------------------------------------


async def test_unique_violation_hides_values(db: Database) -> None:
    secret = "-".join(["very", "secret", "email"])  # not spelled out in source lines
    await add_user(db, secret)
    with pytest.raises(UniqueViolation) as info:
        await add_user(db, secret)
    assert info.value.constraint == "users_email_key"
    assert info.value.table == "users"
    assert secret not in str(info.value)
    # Logged tracebacks must not print the chained driver error (its DETAIL has the value).
    assert secret not in "".join(traceback.format_exception(info.value))
    assert info.value.original is not None  # still available on purpose


async def test_other_violations(db: Database) -> None:
    with pytest.raises(ForeignKeyViolation):
        await db.insert(Posts).values(author_id=999, title="x")
    ann = await add_user(db, "ann")
    with pytest.raises(CheckViolation) as info:
        await db.insert(Posts).values(author_id=ann.id, title="")
    assert info.value.table == "posts"
    with pytest.raises(NotNullViolation) as info:
        await db.execute(sql("insert into users (email) values (null)"))
    assert info.value.column == "email"
    assert info.value.table == "users"
    assert info.value.sqlstate == "23502"


async def test_restrict_foreign_key_is_a_foreign_key_violation(db: Database) -> None:
    # PostgreSQL 18 reports RESTRICT as SQLSTATE 23001, older ones as 23503: both are
    # ForeignKeyViolation.
    with pytest.raises(ForeignKeyViolation) as info:
        async with db.transaction() as tx:  # rolled back by the error
            await tx.execute(sql("create table fk_parent (id int primary key)"))
            await tx.execute(
                sql("create table fk_child (p int references fk_parent on delete restrict)")
            )
            await tx.execute(sql("insert into fk_parent values (1)"))
            await tx.execute(sql("insert into fk_child values (1)"))
            await tx.execute(sql("delete from fk_parent"))
    assert info.value.sqlstate in ("23001", "23503")


async def test_logger_hides_params_by_default(pg_url: str, driver: object) -> None:
    events: list[QueryEvent] = []
    async with await Database.connect(pg_url, logger=events.append) as db:
        await db.select(Users.id).from_(Users).where(eq(Users.email, "x"))
    assert events[0].params is None
    assert events[0].sql.startswith("SELECT")
    assert events[0].error is None
    async with await Database.connect(pg_url, logger=events.append, log_params=True) as db:
        await db.select(Users.id).from_(Users).where(eq(Users.email, "x"))
    assert events[-1].params == ("x",)


async def test_bad_logger_does_not_break_queries(pg_url: str, driver: object) -> None:
    def bad(event: QueryEvent) -> None:
        raise ValueError("logger bug")

    async with await Database.connect(pg_url, logger=bad) as db:
        assert await db.select(Users.id).from_(Users) == []


async def test_tags_reach_the_logger(pg_url: str, driver: object) -> None:
    events: list[QueryEvent] = []
    async with await Database.connect(pg_url, logger=events.append) as db:
        tagged = db.tag(request_id="r1").tag(route="/users")
        await tagged.select(Users.id).from_(Users)
        await db.select(Users.id).from_(Users)  # the original session has no tags
    assert events[-2].tags == {"request_id": "r1", "route": "/users"}
    assert events[-1].tags == {}


async def test_connection_errors() -> None:
    with pytest.raises(ConnectionError):
        await Database.connect("postgresql://nobody:pw@127.0.0.1:1/none")
    with pytest.raises(MizzleError, match="unsupported"):
        await Database.connect("mysql://x")
    with pytest.raises(MizzleError, match="not connected"):
        await Database().select().from_(Users)


async def test_misuse_is_not_a_lost_connection(db: Database) -> None:
    # A wrong argument count is a DatabaseError, not ConnectionError: no pool reset or retry.
    with pytest.raises(DatabaseError) as info:
        async with db.driver.acquire() as conn:
            await conn.fetch("select $1::int", [])
    assert not isinstance(info.value, ConnectionError)
    assert "expects 1 argument" in str(info.value)

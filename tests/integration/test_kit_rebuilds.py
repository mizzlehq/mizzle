"""Re-created views, rebuilt columns and unmanaged references keep what they had (K7)."""

import asyncio
import enum
import uuid
from typing import cast, final, override

import asyncpg
import pytest

from mizzle import Constraint, Database, QueryBuilder, Table, View, check, index, policy, rls, sql
from mizzle.core.errors import DatabaseError
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import Statement
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import integer, pg_enum, serial, text, varchar
from tests.integration.test_kit_cli import kit_url
from tests.integration.test_kit_unmanaged import (
    Items,
    apply,
    dialect,
    execute,
    fetch_one,
    items_view,
    live,
    migrate,
)

__all__ = ["kit_url"]  # fixture


async def test_recreated_view_keeps_column_comments_defaults_and_grantors(
    kit_url: str, pg_url: str
) -> None:
    # K7-1: column comments and defaults went, and a grant made by a grantee with grant
    # option came back as the owner's, so revoking that grantee no longer cascaded.
    a, b = (f"mizzle_k7_{n}_{uuid.uuid4().hex[:6]}" for n in "ab")
    v1 = build_snapshot([Items, items_view("")], dialect)
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    await execute(pg_url, f"CREATE ROLE {a}; CREATE ROLE {b}")
    try:
        await execute(
            kit_url,
            "COMMENT ON COLUMN v_items.name IS 'the name'; "
            "ALTER VIEW v_items ALTER COLUMN name SET DEFAULT 'x'; "
            f"GRANT SELECT ON v_items TO {a} WITH GRANT OPTION; "
            f"GRANT UPDATE (name) ON v_items TO {a} WITH GRANT OPTION; "
            f"SET ROLE {a}; GRANT SELECT ON v_items TO {b}; GRANT UPDATE (name) ON v_items TO {b}",
        )
        await migrate(kit_url, v1, build_snapshot([Items, items_view(" WHERE id > 0")], dialect))
        row = await fetch_one(
            kit_url,
            "SELECT col_description('v_items'::regclass, 2) AS comment, "
            "(SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef "
            "WHERE adrelid = 'v_items'::regclass) AS default, "
            "(SELECT relacl::text FROM pg_class WHERE oid = 'v_items'::regclass) AS acl, "
            "(SELECT attacl::text FROM pg_attribute WHERE attrelid = 'v_items'::regclass "
            "AND attname = 'name') AS column_acl",
        )
        assert row["comment"] == "the name" and row["default"] == "'x'::text"
        assert f"{b}=r/{a}" in row["acl"] and f"{b}=w/{a}" in row["column_acl"]
    finally:
        await execute(kit_url, f"DROP OWNED BY {a}, {b}")
        await execute(pg_url, f"DROP ROLE {a}, {b}")


async def test_recreating_a_view_refuses_a_grantor_it_cannot_set_role_to(
    kit_url: str, pg_url: str
) -> None:
    # K8-4: the check asked for membership, but PostgreSQL 16+ allows SET ROLE only with
    # the SET option: it failed with "permission denied to set role" instead.
    row = await fetch_one(pg_url, "SELECT current_setting('server_version_num')::int AS v")
    if row["v"] < 160000:
        pytest.skip("membership without SET is PostgreSQL 16+")
    a, m = (f"mizzle_k8_{n}_{uuid.uuid4().hex[:6]}" for n in "am")
    v1 = build_snapshot([Items, items_view("")], dialect)
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    await execute(pg_url, f"CREATE ROLE {a}; CREATE ROLE {m} LOGIN PASSWORD 'pw'")
    try:
        await execute(
            kit_url,
            f"GRANT CREATE ON SCHEMA public TO {m}; ALTER TABLE items OWNER TO {m}; "
            f"ALTER VIEW v_items OWNER TO {m}; SET ROLE {m}; "
            f"GRANT SELECT ON v_items TO {a} WITH GRANT OPTION; RESET ROLE; "
            f"SET ROLE {a}; GRANT SELECT ON v_items TO PUBLIC; RESET ROLE; "
            f"GRANT {a} TO {m} WITH INHERIT FALSE, SET FALSE",
        )
        base, _, database = kit_url.rpartition("/")
        host = base.rpartition("@")[2]
        with pytest.raises(DatabaseError, match="needs a custom migration"):
            await migrate(
                f"postgresql://{m}:pw@{host}/{database}",
                v1,
                build_snapshot([Items, items_view(" WHERE id > 0")], dialect),
            )
    finally:
        await execute(kit_url, f"DROP OWNED BY {a}, {m}")
        await execute(pg_url, f"DROP ROLE {a}, {m}")


async def test_a_foreign_key_to_an_unmanaged_table_is_not_read(kit_url: str) -> None:
    # K7-2: the FK to an extension's table was read, pull wrote no code for it, and the
    # next generate dropped it.
    await execute(
        kit_url,
        "CREATE EXTENSION pg_trgm; CREATE TABLE ext_t (id int PRIMARY KEY); "
        "ALTER EXTENSION pg_trgm ADD TABLE ext_t; CREATE SCHEMA other; "
        "CREATE TABLE other.o (id int PRIMARY KEY); CREATE TABLE places (id int PRIMARY KEY, "
        "e int REFERENCES ext_t, o int REFERENCES other.o)",
    )
    snapshot = await live(kit_url, ["public", "other"])
    assert list(snapshot["tables"]["public.places"]["foreign_keys"]) == ["places_o_fkey"]
    driver = await PostgresDriver.connect(kit_url, pool_size=1)
    try:
        async with driver.acquire() as conn:
            skipped = await dialect.skipped_objects(conn, ["public", "other"])
    finally:
        await driver.close()
    assert "foreign key places_e_fkey on places (it references ext_t)" in skipped


@pytest.mark.parametrize("reader", [False, True])
async def test_a_policy_naming_its_own_table_is_normalized(kit_url: str, reader: bool) -> None:
    # K7-3: `refs.t` failed on the temporary table, so the text stayed as written and
    # differed from the database's forever. K8-1: it was then created on the real table,
    # which queued an ACCESS EXCLUSIVE lock behind an open reader (and blocked every read).
    @final
    class Totals(Table, name="totals"):
        id = serial().primary_key()
        total = integer()

    @final
    class Refs(Table, name="refs"):
        id = serial().primary_key()
        t = integer()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            using = sql("exists (select 1 from totals i where i.total = refs.t)")
            return [rls(), policy("by_total", using=using)]

    v1 = build_snapshot([Totals, Refs], dialect)
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    other = await asyncpg.connect(kit_url)
    driver = await PostgresDriver.connect(kit_url, pool_size=1)
    try:
        if reader:
            await other.execute("BEGIN; SELECT * FROM refs")
        async with driver.acquire() as conn:
            normalized = await asyncio.wait_for(dialect.normalize_snapshot(conn, v1), 10)
    finally:
        await other.close()
        await driver.close()
    current = await live(kit_url)
    assert diff(current, normalized, compare_expressions=False, normalized=True) == []


def owned(with_policies: bool) -> Snapshot:
    @final
    class Owned(Table, name="owned", schema="app"):
        id = serial().primary_key()
        owner = text()
        x = integer()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            if not with_policies:
                return []
            return [
                rls(),
                policy("own", using=sql("app.owned.owner = current_user")),
                policy("row", using=sql("app.mine(owned)")),
                check("pos", sql("owned.x > 0")),  # K9-5
                check("qualified", sql("app.owned.x < 10")),
            ]

    return build_snapshot([Owned], dialect)


@pytest.mark.parametrize("reader", [False, True])
async def test_a_policy_or_check_naming_its_table_by_schema_or_row_type_is_normalized(
    kit_url: str, reader: bool
) -> None:
    # K9-1: `app.owned.owner` and `mine(owned)` failed on both temporary tables, so the text
    # stayed as written and push altered the policy every time. They are made on the real
    # table only when its lock is free right now: with a reader in, never wait (K8-1).
    # K9-5: a CHECK naming its own table was dropped and added again every push.
    await execute(kit_url, "CREATE SCHEMA app")
    v0, v1 = owned(False), owned(True)
    await migrate(kit_url, empty_snapshot("postgresql"), v0)
    await execute(
        kit_url,
        "CREATE FUNCTION app.mine(app.owned) RETURNS boolean LANGUAGE sql "
        "AS 'SELECT $1.owner = current_user'",
    )
    await migrate(kit_url, v0, v1)
    other = await asyncpg.connect(kit_url)
    driver = await PostgresDriver.connect(kit_url, pool_size=1)
    try:
        if reader:
            await other.execute("BEGIN; SELECT * FROM app.owned")
        async with driver.acquire() as conn:
            started = asyncio.get_running_loop().time()
            normalized = await asyncio.wait_for(dialect.normalize_snapshot(conn, v1), 10)
            elapsed = asyncio.get_running_loop().time() - started
    finally:
        await other.close()
        await driver.close()
    current = await live(kit_url, ["app"])
    changes = diff(current, normalized, compare_expressions=False, normalized=True)
    if reader:  # kept as written, at once (lock_timeout is 2s)
        assert elapsed < 1.5 and changes
    else:
        assert changes == []


def calc(expression: str, with_view: bool = False) -> Snapshot:
    @final
    class Calc(Table, name="calc"):
        id = serial().primary_key()
        name = text()
        b = text().generated_always_as(sql(expression)).comment("computed")

    @final
    class VCalc(View, name="v_calc"):
        b = text()

        @classmethod
        @override
        def query(cls, db: QueryBuilder) -> Node:
            return sql("SELECT b FROM calc")

    return build_snapshot([Calc, VCalc] if with_view else [Calc], dialect)


async def comment_of_b(url: str) -> object:
    row = await fetch_one(
        url,
        "SELECT col_description('calc'::regclass, (SELECT attnum FROM pg_attribute "
        "WHERE attrelid = 'calc'::regclass AND attname = 'b')) AS c",
    )
    return row["c"]


async def test_making_a_column_generated_keeps_its_comment(kit_url: str) -> None:
    # K7-4: the rebuilt column lost its comment, and generate saw no change.
    @final
    class Plain(Table, name="calc"):
        id = serial().primary_key()
        name = text()
        b = text().comment("computed")

    v1 = build_snapshot([Plain], dialect)
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    await migrate(kit_url, v1, calc("lower(name)"))
    assert await comment_of_b(kit_url) == "computed"


async def test_a_new_expression_before_pg17_keeps_the_comment_and_managed_views(
    kit_url: str,
) -> None:
    # K7-4: the < 17 branch drops the column and adds it again without its comment.
    # K7-5: a managed view on the column was taken for an unmanaged one, so it failed.
    v1, v2 = calc("lower(name)", with_view=True), calc("upper(name)", with_view=True)
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    await execute(kit_url, "INSERT INTO calc (name) VALUES ('Ab')")
    old_branch = [
        Statement(s.sql.replace(">=) 170000", ">=) 9900000")) for s in dialect.render(diff(v1, v2))
    ]
    await apply(kit_url, old_branch)
    assert await comment_of_b(kit_url) == "computed"
    assert (await fetch_one(kit_url, "SELECT b FROM v_calc"))["b"] == "AB"


def mail(width: int) -> Snapshot:
    @final
    class Mail(Table, name="mail"):
        id = serial().primary_key()
        email = varchar(width)
        lower_email = text().generated_always_as(sql("lower(email)"))

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [index("mail_lower").on(cls.lower_email)]

    return build_snapshot([Mail], dialect)


async def test_a_type_change_under_a_generated_column(kit_url: str) -> None:
    # H11-1: PostgreSQL cannot change the type of a column a generated column uses, so
    # migrate failed. The generated column is dropped and added again after the change,
    # with its index; no data is lost (it is computed), so no --allow-data-loss either.
    v1, v2 = mail(20), mail(40)
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    await execute(kit_url, "INSERT INTO mail (email) VALUES ('A@X.io')")
    steps = diff(v1, v2)
    assert not any(s.data_loss for s in steps)
    await migrate(kit_url, v1, v2)
    row = await fetch_one(kit_url, "SELECT email, lower_email FROM mail")
    assert (row["email"], row["lower_email"]) == ("A@X.io", "a@x.io")
    assert await fetch_one(kit_url, "SELECT 1 AS i FROM pg_indexes WHERE indexname = 'mail_lower'")
    current = await live(kit_url, ["public"])
    assert diff(current, v2, compare_expressions=False) == []


def kinds(values: tuple[str, ...]) -> Snapshot:
    kind_enum = cast(type[enum.StrEnum], enum.StrEnum("Kind", {v.upper(): v for v in values}))

    @final
    class Kinds(Table, name="kinds"):
        kind = pg_enum(kind_enum, type_name="kind").primary_key()
        is_a = integer().generated_always_as(sql("CASE WHEN kind = 'a' THEN 1 ELSE 0 END"))

    @final
    class Things(Table, name="things"):
        id = serial().primary_key()
        kind = pg_enum(kind_enum, type_name="kind").not_null().references(lambda: Kinds.kind)

    return build_snapshot([Kinds, Things], dialect)


async def test_an_enum_under_a_foreign_key_is_created_again(kit_url: str) -> None:
    # H14-1: converting a key column while a foreign key joined it to a column still of the
    # old type failed ("foreign key constraint cannot be implemented").
    v1, v2 = kinds(("a", "b", "c")), kinds(("b", "a"))
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    await execute(
        kit_url, "INSERT INTO kinds VALUES ('a'), ('b'); INSERT INTO things (kind) VALUES ('a')"
    )
    await migrate(kit_url, v1, v2)
    row = await fetch_one(
        kit_url, "SELECT kind::text AS k, is_a FROM kinds JOIN things USING (kind)"
    )
    assert (row["k"], row["is_a"]) == ("a", 1)
    current = await live(kit_url, ["public"])
    assert diff(current, v2, compare_expressions=False) == []


async def test_open_connections_survive_an_enum_created_again(kit_url: str) -> None:
    # H15-1: before PostgreSQL 17, every pooled connection kept plans for the old type and
    # failed each query on it ("cache lookup failed for type") until the app restarted.
    v1, v2 = kinds(("a", "b")), kinds(("b", "a"))
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    await execute(
        kit_url, "INSERT INTO kinds VALUES ('a'), ('b'); INSERT INTO things (kind) VALUES ('a')"
    )
    things = v1["tables"]["public.things"]
    assert things["columns"]["kind"]["type"] == "kind"
    async with await Database.connect(kit_url, pool_size=1) as db:
        query = sql("SELECT kind::text AS k FROM things WHERE kind = {}::kind", "a")
        assert await db.execute(query) == [{"k": "a"}]
        await migrate(kit_url, v1, v2)
        assert await db.execute(query) == [{"k": "a"}]
        async with db.transaction() as tx:  # a fresh transaction works as well
            assert await tx.execute(query) == [{"k": "a"}]

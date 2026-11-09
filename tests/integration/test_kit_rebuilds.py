"""Re-created views, rebuilt columns and unmanaged references keep what they had (K7)."""

import uuid
from typing import final, override

from mizzle import Constraint, QueryBuilder, Table, View, policy, rls, sql
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import Statement
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import integer, serial, text
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


async def test_a_policy_naming_its_own_table_is_normalized(kit_url: str) -> None:
    # K7-3: `refs.t` failed on the temporary table, so the text stayed as written and
    # differed from the database's forever.
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
    driver = await PostgresDriver.connect(kit_url, pool_size=1)
    try:
        async with driver.acquire() as conn:
            normalized = await dialect.normalize_snapshot(conn, v1)
    finally:
        await driver.close()
    current = await live(kit_url)
    assert diff(current, normalized, compare_expressions=False, normalized=True) == []


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
        Statement(s.sql.replace(">= 170000", ">= 9900000")) for s in dialect.render(diff(v1, v2))
    ]
    await apply(kit_url, old_branch)
    assert await comment_of_b(kit_url) == "computed"
    assert (await fetch_one(kit_url, "SELECT b FROM v_calc"))["b"] == "AB"

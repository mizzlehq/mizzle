"""Objects mizzle does not manage are neither read, dropped nor lost (K6)."""

import copy
import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import final, override
from urllib.parse import quote

import asyncpg
import pytest

from mizzle import QueryBuilder, Table, View, sql
from mizzle.core.errors import DatabaseError, SchemaError
from mizzle.core.snapshot import GeneratedSnap, Snapshot, empty_snapshot
from mizzle.core.sql.nodes import Node
from mizzle.core.steps import Statement
from mizzle.dialects.postgres import PostgresDialect, ddl
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.diff import diff
from mizzle.kit.migrate import apply_statements
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import integer, serial, text
from tests.integration.test_kit_cli import cli, kit_url, project, rewrite_schema

__all__ = ["kit_url", "project"]  # fixtures

dialect = PostgresDialect()


async def execute(url: str, statement: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


async def fetch_one(url: str, query: str) -> asyncpg.Record:
    conn = await asyncpg.connect(url)
    try:
        [row] = await conn.fetch(query)
        return row
    finally:
        await conn.close()


async def live(url: str, schemas: Sequence[str] = ("public",)) -> Snapshot:
    driver = await PostgresDriver.connect(url, pool_size=1)
    try:
        async with driver.acquire() as conn:
            return await dialect.introspect(conn, schemas)
    finally:
        await driver.close()


async def migrate(url: str, old: Snapshot, new: Snapshot) -> None:
    await apply(url, dialect.render(diff(old, new)))


async def apply(url: str, statements: list[Statement]) -> None:
    driver = await PostgresDriver.connect(url, pool_size=1)
    try:
        async with driver.acquire() as conn:
            await apply_statements(conn, statements)
    finally:
        await driver.close()


@final
class Items(Table, name="items"):
    id = serial().primary_key()
    name = text().not_null()


def items_view(where: str) -> type[View]:
    @final
    class VItems(View, name="v_items"):
        id = integer()
        name = text()

        @classmethod
        @override
        def query(cls, db: QueryBuilder) -> Node:
            return sql(f"SELECT id, name FROM items{where}")

    return VItems


async def test_recreated_view_keeps_triggers_rules_and_column_grants(kit_url: str) -> None:
    # K6-1: only the view's ACL, owner and comment were kept.
    v1 = build_snapshot([Items, items_view("")], dialect)
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    await execute(
        kit_url,
        "CREATE FUNCTION trg_v() RETURNS trigger LANGUAGE plpgsql AS "
        "$f$ BEGIN INSERT INTO items(name) VALUES (NEW.name); RETURN NEW; END $f$; "
        "CREATE TRIGGER t_view INSTEAD OF INSERT ON v_items FOR EACH ROW EXECUTE FUNCTION trg_v(); "
        "GRANT UPDATE (name) ON v_items TO PUBLIC; "
        "CREATE RULE r_del AS ON DELETE TO v_items DO INSTEAD NOTHING",
    )
    await migrate(kit_url, v1, build_snapshot([Items, items_view(" WHERE id > 0")], dialect))
    row = await fetch_one(
        kit_url,
        "SELECT (SELECT array_agg(tgname) FROM pg_trigger WHERE tgrelid = 'v_items'::regclass) "
        "AS triggers, (SELECT array_agg(rulename) FROM pg_rules WHERE tablename = 'v_items') "
        "AS rules, (SELECT attacl::text FROM pg_attribute WHERE attrelid = 'v_items'::regclass "
        "AND attname = 'name') AS acl",
    )
    assert row["triggers"] == ["t_view"] and row["rules"] == ["r_del"]
    assert row["acl"] is not None and "=w/" in row["acl"]
    await execute(kit_url, "INSERT INTO v_items (name) VALUES ('a')")  # the trigger works


@pytest.mark.parametrize(
    ("unmanaged", "catalog"),
    [
        (
            "CREATE TRIGGER t_view INSTEAD OF INSERT ON v_items FOR EACH ROW "
            "EXECUTE FUNCTION trg_v()",
            "UPDATE pg_trigger SET tgenabled = 'D' WHERE tgname = 't_view'",
        ),
        (
            "CREATE RULE r_del AS ON DELETE TO v_items DO INSTEAD NOTHING",
            "UPDATE pg_rewrite SET ev_enabled = 'R' WHERE rulename = 'r_del'",
        ),
    ],
)
async def test_recreating_a_view_refuses_to_enable_its_disabled_triggers_and_rules(
    kit_url: str, unmanaged: str, catalog: str
) -> None:
    # K6-11: a disabled trigger or rule came back enabled. No ALTER disables one on a view,
    # so it is a catalog edit, and making it again would need one too.
    v1 = build_snapshot([Items, items_view("")], dialect)
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    await execute(
        kit_url,
        "CREATE FUNCTION trg_v() RETURNS trigger LANGUAGE plpgsql AS "
        f"$f$ BEGIN RETURN NEW; END $f$; {unmanaged}; {catalog}",
    )
    v2 = build_snapshot([Items, items_view(" WHERE id > 0")], dialect)
    with pytest.raises(DatabaseError, match="disabled trigger or rule"):
        await migrate(kit_url, v1, v2)
    await execute(kit_url, catalog.replace("'D'", "'O'").replace("'R'", "'O'"))
    await migrate(kit_url, v1, v2)  # enabled: kept as it is


@pytest.mark.parametrize(
    ("setup", "tables"),
    [
        pytest.param(  # K6-2: an FK to a partitioned table has a copy per partition
            "CREATE TABLE events (id int, at date, PRIMARY KEY (id, at)) PARTITION BY RANGE (at); "
            "CREATE TABLE events_2026 PARTITION OF events "
            "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01'); "
            "CREATE TABLE refs (id int PRIMARY KEY, eid int, eat date, "
            "FOREIGN KEY (eid, eat) REFERENCES events (id, at))",
            ["public.events", "public.refs"],
            id="partition-fk",
        ),
        pytest.param(  # K6-3: a table and an enum that belong to an extension
            "CREATE EXTENSION pg_trgm; CREATE TABLE ext_table (id int); "
            "CREATE TYPE ext_enum AS ENUM ('a'); ALTER EXTENSION pg_trgm ADD TABLE ext_table; "
            "ALTER EXTENSION pg_trgm ADD TYPE ext_enum",
            [],
            id="extension",
        ),
        pytest.param(  # K6-6: a table inheritance child
            "CREATE TABLE parent_t (id int PRIMARY KEY, x int); "
            "CREATE TABLE child_t (y int) INHERITS (parent_t)",
            ["public.parent_t"],
            id="inheritance",
        ),
    ],
)
async def test_unmanaged_relations_and_constraints_are_not_read(
    kit_url: str, setup: str, tables: list[str]
) -> None:
    await execute(kit_url, setup)
    snapshot = await live(kit_url)
    assert sorted(snapshot["tables"]) == tables
    assert snapshot["enums"] == {}
    if refs := snapshot["tables"].get("public.refs"):
        [fk] = refs["foreign_keys"].values()
        assert fk["ref_table"] == "events"


async def test_rebuilding_a_column_refuses_to_drop_unmanaged_objects_on_it(kit_url: str) -> None:
    # K6-7: making a column generated dropped it, and the exclusion constraint and column
    # grants on it went silently.
    @final
    class Plain(Table, name="bookings"):
        id = serial().primary_key()
        room = integer().not_null()

    @final
    class Generated(Table, name="bookings"):
        id = serial().primary_key()
        room = integer().generated_always_as(sql("id % 10"))

    v1, v2 = build_snapshot([Plain], dialect), build_snapshot([Generated], dialect)
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    for unmanaged, undo in [
        (
            "ALTER TABLE bookings ADD CONSTRAINT no_overlap EXCLUDE USING btree (room WITH =)",
            "ALTER TABLE bookings DROP CONSTRAINT no_overlap",
        ),
        (
            "GRANT SELECT (room) ON bookings TO PUBLIC",
            "REVOKE SELECT (room) ON bookings FROM PUBLIC",
        ),
    ]:
        await execute(kit_url, unmanaged)
        with pytest.raises(DatabaseError, match="needs a custom migration"):
            await migrate(kit_url, v1, v2)
        await execute(kit_url, undo)
    await migrate(kit_url, v1, v2)  # nothing on it: rebuilt


async def test_changing_an_expression_before_pg17_refuses_to_drop_unmanaged_objects(
    kit_url: str,
) -> None:
    # K6-10: PostgreSQL < 17 drops the generated column and adds it again; a column grant
    # or trigger on it went silently. Run that branch here by asking for a later version.
    def calc(expression: str) -> Snapshot:
        @final
        class Calc(Table, name="calc"):
            id = serial().primary_key()
            b = integer().generated_always_as(sql(expression)).unique()

        return build_snapshot([Calc], dialect)

    v1, v2 = calc("id * 2"), calc("id * 3")
    await migrate(kit_url, empty_snapshot("postgresql"), v1)
    [change] = dialect.render(diff(v1, v2))
    old_branch = [Statement(change.sql.replace(">=) 170000", ">=) 9900000"))]
    for unmanaged, undo in [
        ("GRANT SELECT (b) ON calc TO PUBLIC", "REVOKE SELECT (b) ON calc FROM PUBLIC"),
        (
            "CREATE FUNCTION trg_c() RETURNS trigger LANGUAGE plpgsql AS "
            "$f$ BEGIN RETURN NEW; END $f$; CREATE TRIGGER t_b AFTER UPDATE OF b ON calc "
            "FOR EACH ROW EXECUTE FUNCTION trg_c()",
            "DROP TRIGGER t_b ON calc",
        ),
    ]:
        await execute(kit_url, unmanaged)
        with pytest.raises(DatabaseError, match="needs a custom migration"):
            await apply(kit_url, old_branch)
        await execute(kit_url, undo)
    await apply(kit_url, old_branch)  # only the unique key on it: made again
    row = await fetch_one(
        kit_url, "SELECT count(*) AS n FROM pg_constraint WHERE conname = 'calc_b_key'"
    )
    assert row["n"] == 1


def test_custom_type_spellings_normalize_as_the_server_prints_them() -> None:
    # K6-4: digits, dots and capitals never matched format_type().
    for written, printed in [
        ("int4", "integer"),
        ("int8", "bigint"),
        ("float8", "double precision"),
        ("int4[]", "integer[]"),
        ("VARCHAR(10)", "character varying(10)"),
        ("public.posint", "public.posint"),  # K7-7: offline, a schema is never dropped
        ("public.int4", "public.int4"),  # a domain named int4, not integer
        ("pg_catalog.char", "pg_catalog.char"),  # "char", not character
        ("PUBLIC.User", "public.user"),
        ("s2.dom2", "s2.dom2"),
        ('"Weird Type"', '"Weird Type"'),
        ("numeric(10, 2)", "numeric(10,2)"),
    ]:
        assert ddl.normalize_type(written) == printed


SPELLINGS = """
from mizzle import Table
from mizzle.pg import serial, custom

class Items(Table, name="items"):
    id = serial().primary_key()
    c0 = custom("TIMESTAMP(3) WITH TIME ZONE")
    c1 = custom("TIME(3) WITHOUT TIME ZONE")
    c2 = custom("decimal(10,2)")
    c3 = custom("dec(5)")
    c4 = custom("float")
    c5 = custom("float(24)")
    c6 = custom('pg_catalog."char"')
    c7 = custom("int[3]")
    c8 = custom("integer array")
    c9 = custom("national character(2)")
    c10 = custom("INTERVAL DAY TO SECOND(3)")
    c11 = custom("BIT VARYING(4)")
"""

QUALIFIED = """
from mizzle import Table
from mizzle.pg import serial, custom

class Items(Table, name="items"):
    id = serial().primary_key()
    c0 = custom("public.int4")
    c1 = custom("public.user")
    c2 = custom("pg_catalog.char")
"""


async def no_drift(capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert await cli("push", "--dry-run") == 0
    assert "Database is up to date." in capsys.readouterr().out
    assert await cli("doctor") == 0
    assert "drift:       none" in capsys.readouterr().out


async def test_declared_type_spellings_are_compared_as_the_server_prints_them(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # K7-6: no spelling rule guessed decimal, float(24), int[3]... so they drifted forever.
    rewrite_schema(project, SPELLINGS, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    await no_drift(capsys)


async def test_a_qualified_type_keeps_its_schema(
    project: Path,
    kit_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # K7-7: public.int4 (a domain) became integer, public.user a bare user (a syntax error)
    # and pg_catalog.char (the one-byte "char") character.
    await execute(kit_url, "CREATE DOMAIN public.int4 AS text")
    await execute(kit_url, 'CREATE DOMAIN public."user" AS integer')
    rewrite_schema(project, QUALIFIED, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    conn = await asyncpg.connect(kit_url)
    try:
        rows = await conn.fetch(
            "SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute "
            "WHERE attrelid = 'items'::regclass AND attname LIKE 'c_' ORDER BY attname"
        )
    finally:
        await conn.close()
    assert [r["t"] for r in rows] == ["public.int4", '"user"', '"char"']
    await no_drift(capsys)


ARRAYS = """
from mizzle import Table
from mizzle.pg import array, integer, serial

class T(Table, name="t"):
    id = serial().primary_key()
    xs = array(integer())
"""


async def test_an_old_snapshot_with_nested_array_types_is_no_change(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # K6-5: a snapshot an older mizzle wrote holds "integer[][]".
    rewrite_schema(project, ARRAYS, monkeypatch)
    assert await cli("generate") == 0
    path = next(project.glob("migrations/0001_*/snapshot.json"))
    data = json.loads(path.read_text())
    data["tables"]["public.t"]["columns"]["xs"]["type"] = "integer[][]"
    path.write_text(json.dumps(data))
    capsys.readouterr()
    assert await cli("generate") == 0
    assert "No schema changes." in capsys.readouterr().out


OLD_SPELLINGS = """
from mizzle import Table
from mizzle.pg import serial, custom

class Items(Table, name="items"):
    id = serial().primary_key()
    a = custom("public.posint")
    b = custom("pg_catalog.int4")
    c = custom("public.int4")
"""


async def test_an_old_snapshot_with_unqualified_types_is_no_change(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # K8-2: an older mizzle stored "posint" and "integer" for these, so generate reported
    # type changes (that lose data) on an unchanged schema. public.int4 stays a domain (K7-7).
    rewrite_schema(project, OLD_SPELLINGS, monkeypatch)
    assert await cli("generate") == 0
    path = next(project.glob("migrations/0001_*/snapshot.json"))
    data = json.loads(path.read_text())
    columns = data["tables"]["public.items"]["columns"]
    columns["a"]["type"], columns["b"]["type"] = "posint", "integer"
    path.write_text(json.dumps(data))
    capsys.readouterr()
    assert await cli("generate", "--sql") == 0
    assert "No schema changes." in capsys.readouterr().out
    columns["c"]["type"] = "integer"
    path.write_text(json.dumps(data))
    assert await cli("generate", "--sql") == 0
    assert capsys.readouterr().out == (
        'ALTER TABLE "items" ALTER COLUMN "c" SET DATA TYPE public.int4 USING "c"::public.int4;\n'
    )


BACKSLASHES = r"""
from mizzle import Constraint, Table, check, sql
from mizzle.pg import serial, text

class Items(Table, name="items"):
    id = serial().primary_key()
    p = text().default("C:\\dir\\x'")
    q = text().default(sql("'a\\'"))
    r = text().comment("a\\b")

    @classmethod
    def config(cls) -> list[Constraint]:
        return [check("items_q_check", sql("q <> 'z\\'"))]
"""


async def test_backslashes_in_string_literals_with_standard_conforming_strings_off(
    project: Path,
    kit_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # K8-3: a default '...' lost its backslashes there, or one before the closing quote
    # escaped it and the migration failed (and so did a pulled baseline's text).
    database = kit_url.rpartition("/")[2]
    await execute(kit_url, f'ALTER DATABASE "{database}" SET standard_conforming_strings = off')
    rewrite_schema(project, BACKSLASHES, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    row = await fetch_one(
        kit_url,
        "INSERT INTO items DEFAULT VALUES "
        "RETURNING p, q, col_description('items'::regclass, 4) AS r",
    )
    assert (row["p"], row["q"], row["r"]) == ("C:\\dir\\x'", "a\\", "a\\b")
    await no_drift(capsys)


GENERATED_V1 = r"""
from mizzle import Table, sql
from mizzle.pg import serial, text

class Items(Table, name="items"):
    id = serial().primary_key()
    a = text()
    g = text().generated_always_as(sql("lower(a)"))
"""


async def test_executed_strings_with_standard_conforming_strings_off(
    project: Path,
    kit_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # K9-3: a changed generated column's expression is EXECUTEd in a DO block, where
    # '\d+' lost its backslash.
    database = kit_url.rpartition("/")[2]
    await execute(kit_url, f'ALTER DATABASE "{database}" SET standard_conforming_strings = off')
    rewrite_schema(project, GENERATED_V1, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    v2 = GENERATED_V1.replace('sql("lower(a)")', r"""sql("regexp_replace(a, '\d+', 'Y')")""")
    rewrite_schema(project, v2, monkeypatch)
    assert await cli("generate") == 0
    assert await cli("migrate") == 0
    row = await fetch_one(kit_url, "INSERT INTO items (a) VALUES ('a12') RETURNING a, g")
    assert row["g"] == "aY"
    await no_drift(capsys)


ROLES = """
from mizzle import Table, policy, rls, role, sql
from mizzle.pg import integer, serial

app = role("mizzle_k6_app")

class Docs(Table, name="docs"):
    id = serial().primary_key()
    owner = integer()

    @classmethod
    def config(cls):
        return [rls(), policy("own", to=[app], using=sql("owner = 1"))]
"""


async def test_pull_update_keeps_roles_and_records_no_phantom_change(
    project: Path,
    kit_url: str,
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # K6-8: the policy text was compared unnormalized, and the roles were not read.
    await execute(pg_url, "DROP ROLE IF EXISTS mizzle_k6_app")
    try:
        rewrite_schema(project, ROLES, monkeypatch)
        assert await cli("generate") == 0
        assert await cli("migrate") == 0
        capsys.readouterr()
        assert await cli("pull", "--update") == 0
        assert "Up to date with the database." in capsys.readouterr().out
        assert len(list(project.glob("migrations/0*"))) == 1
        await execute(kit_url, "CREATE TABLE extra (id int PRIMARY KEY)")
        assert await cli("pull", "--update") == 0
        source = next(p for p in project.glob("schema_*.py")).read_text()
        assert "role('mizzle_k6_app')" in source, source
        sql_text = next(project.glob("migrations/0002_*/migration.sql")).read_text()
        assert "POLICY" not in sql_text and "ROLE" not in sql_text, sql_text
    finally:
        await execute(kit_url, "DROP TABLE IF EXISTS docs")
        await execute(pg_url, "DROP ROLE IF EXISTS mizzle_k6_app")


async def test_schema_names_with_a_dot_are_refused(kit_url: str) -> None:
    # K6-9: public."x.y" and "public.x".y shared the key "public.x.y".
    @final
    class Dotted(Table, name="y", schema="public.x"):
        id = integer().primary_key()

    with pytest.raises(SchemaError, match="not supported in schema names"):
        build_snapshot([Dotted], dialect)
    await execute(
        kit_url,
        'CREATE TABLE "x.y" (id int); CREATE SCHEMA "public.x"; CREATE TABLE "public.x".y (id int)',
    )
    with pytest.raises(SchemaError, match="not supported in schema names"):
        await live(kit_url, ["public", "public.x"])
    assert list((await live(kit_url))["tables"]) == ["public.x.y"]  # a dotted table is fine


# K8-6: built-ins the kit's own SQL calls, planted in public with the exact signature. Each
# bumps a sequence (it survives the rollback introspection ends in) and calls the real one.
_PLANTED = (
    ("format_type", "oid, integer", "text"),
    ("pg_get_expr", "pg_node_tree, oid", "text"),
    ("pg_get_constraintdef", "oid", "text"),
    ("pg_get_indexdef", "oid, integer, boolean", "text"),
    ("pg_get_viewdef", "oid", "text"),
    ("pg_get_serial_sequence", "text, text", "text"),
    ("obj_description", "oid, name", "text"),
    ("col_description", "oid, integer", "text"),
    ("quote_ident", "text", "text"),
    ("pg_get_userbyid", "oid", "name"),
    ("current_setting", "text", "text"),
    ("set_config", "text, text, boolean", "text"),
    ("to_regclass", "text", "regclass"),
    ("pg_has_role", "oid, text", "boolean"),
    ("pg_try_advisory_lock", "bigint", "boolean"),
    ("pg_advisory_unlock", "bigint", "boolean"),
    ("now", "", "timestamptz"),
    # pg_catalog's is polymorphic (anyarray): an exact int2[] one wins wherever it is.
    ("unnest", "smallint[]", "SETOF smallint"),
    (
        "aclexplode",
        "aclitem[]",
        "TABLE (grantor oid, grantee oid, privilege_type text, is_grantable boolean)",
    ),
)

_PLANTED_V1 = """
from mizzle import Table, View, sql
from mizzle.pg import integer, text

class Docs(Table, name="docs"):
    id = integer().primary_key()
    body = text().not_null()
    shout = text().generated_always_as(sql("upper(body)"))

class VDocs(View, name="v_docs"):
    id = integer()
    body = text()

    @classmethod
    def query(cls, db):
        return sql("SELECT id, body FROM docs")
"""


def _plant(name: str, args: str, result: str) -> str:
    params = ", ".join(f"${i + 1}" for i in range(args.count(",") + 1 if args else 0))
    real = f"pg_catalog.{name}({params})"
    give = f"QUERY SELECT * FROM {real}" if result.startswith(("SETOF", "TABLE")) else real
    return (
        f"CREATE FUNCTION public.{name}({args}) RETURNS {result} LANGUAGE plpgsql AS $f$ "
        f"BEGIN PERFORM pg_catalog.nextval('public.k86_hits'); RETURN {give}; END $f$"
    )


async def test_functions_planted_in_public_never_run_as_the_migrator(
    project: Path, kit_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # K8-6: the kit's own SQL called built-ins unqualified. A role that can create in public
    # planted an exact-signature function there (or any, with public before pg_catalog on
    # the path), and introspection, generated DO blocks and the migrations table ran it as
    # the migrating role. User expressions (upper(body)) still resolve through the path.
    planter = f"k86_{uuid.uuid4().hex[:8]}"

    async def ran() -> bool:
        return bool((await fetch_one(kit_url, "SELECT is_called FROM k86_hits"))[0])

    await execute(
        kit_url,
        f"CREATE ROLE {planter}; GRANT CREATE ON SCHEMA public TO {planter}; "
        f"CREATE SEQUENCE public.k86_hits; GRANT USAGE ON SEQUENCE public.k86_hits TO PUBLIC",
    )
    try:
        await execute(kit_url, f"SET ROLE {planter}; " + "; ".join(_plant(*p) for p in _PLANTED))
        url = f"{kit_url}?search_path={quote('public, pg_catalog')}"
        monkeypatch.setenv("DATABASE_URL", url)
        rewrite_schema(project, _PLANTED_V1, monkeypatch)
        assert await cli("generate") == 0
        assert await cli("migrate") == 0
        await execute(url, "GRANT SELECT ON v_docs TO PUBLIC; COMMENT ON VIEW v_docs IS 'v'")
        await execute(url, "INSERT INTO docs (id, body) VALUES (1, 'a')")
        # A recreated view (save and restore grants), an identity added to a filled column
        # (setval) and a changed generated expression (SET EXPRESSION).
        v2 = (
            _PLANTED_V1.replace(
                "id = integer().primary_key()",
                "id = integer().primary_key().generated_by_default_as_identity()",
            )
            .replace("upper(body)", "lower(body)")
            .replace("FROM docs", "FROM docs WHERE id > 0")
        )
        rewrite_schema(project, v2, monkeypatch)
        assert await cli("generate") == 0
        assert await cli("migrate") == 0
        assert await cli("doctor") == 0
        driver = await PostgresDriver.connect(url, pool_size=1)
        try:
            async with driver.acquire() as conn:
                assert "grants on v_docs" in await dialect.skipped_objects(conn, ["public"])
                current = await dialect.introspect(conn, ["public"])
                await dialect.normalize_snapshot(conn, current)
        finally:
            await driver.close()
        assert not await ran(), "a planted function ran"
        # The rebuild PostgreSQL before 17 runs instead of SET EXPRESSION.
        changed = copy.deepcopy(current)
        changed["tables"]["public.docs"]["columns"]["shout"]["generated"] = GeneratedSnap(
            expression="upper(body)", stored=True
        )
        old_branch = [
            Statement(s.sql.replace(">=) 170000", ">=) 9900000"))
            for s in dialect.render(diff(current, changed))
        ]
        assert any("9900000" in s.sql for s in old_branch)
        await apply(url, old_branch)
        assert (await fetch_one(kit_url, "SELECT shout FROM docs"))[0] == "A"
        assert not await ran(), "a planted function ran"
        # The trap is live: the same path runs it for an unqualified call.
        await fetch_one(url, "SELECT format_type(23, -1)")
        assert await ran()
    finally:
        await execute(kit_url, f"DROP OWNED BY {planter} CASCADE; DROP ROLE {planter}")

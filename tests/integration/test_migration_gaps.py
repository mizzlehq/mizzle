"""Grants, function overloads and options, trigger kinds, table <-> view swaps, SQL-standard
function bodies, operator classes, generated column rebuilds, enums in other schemas, table
moves and identity options: applied, introspected without drift, and pulled back."""

import enum
import importlib.util
import sys
import uuid
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import final, override

import pytest

from mizzle import (
    Constraint,
    QueryBuilder,
    Table,
    View,
    check,
    function,
    grant,
    index,
    primary_key,
    role,
    sql,
    trigger,
    unique,
    unique_index,
)
from mizzle.core.schema.column import Computed
from mizzle.core.schema.objects import Extension, Function, SchemaObject, Trigger
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.steps import Statement, Step
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.codegen import render_schema_module
from mizzle.kit.diff import StrictRenames, diff
from mizzle.kit.migrate import apply_statements
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import bigint, date, integer, pg_enum, smallint, text, timestamp
from tests.integration.test_generated_columns import server_version

dialect = PostgresDialect()
SCHEMAS = ["mg", "mg2"]
READER, OTHER = "mizzle_mg_reader", "mizzle_mg_other"
reader = role(READER, existing=True)

RESET = f"""
DROP SCHEMA IF EXISTS mg CASCADE;
DROP SCHEMA IF EXISTS mg2 CASCADE;
DROP ROLE IF EXISTS {READER};
DROP ROLE IF EXISTS {OTHER};
"""


@pytest.fixture
async def driver(pg_url: str) -> AsyncGenerator[PostgresDriver]:
    drv = await PostgresDriver.connect(pg_url, pool_size=2)
    async with drv.acquire() as conn:
        await conn.execute(RESET, [])
        await conn.execute(f"CREATE ROLE {READER}; CREATE ROLE {OTHER}", [])
    yield drv
    async with drv.acquire() as conn:
        await conn.execute(RESET, [])
    await drv.close()


def snap(tables: Sequence[type[Table]], objects: Sequence[SchemaObject] = ()) -> Snapshot:
    return build_snapshot(tables, dialect, objects=objects)


async def execute(driver: PostgresDriver, statement: str) -> list[dict[str, object]]:
    async with driver.acquire() as conn:
        return await conn.fetch_dicts(statement, [])


async def migrate(
    driver: PostgresDriver, old: Snapshot, new: Snapshot, renames: dict[str, str] | None = None
) -> list[Step]:
    """Apply the generated migration, then check the database has no drift from ``new``."""
    steps = diff(old, new, StrictRenames(renames or {}))
    async with driver.acquire() as conn:
        await apply_statements(conn, dialect.render(steps))
    await assert_no_drift(driver, new)
    return steps


async def live(driver: PostgresDriver) -> Snapshot:
    async with driver.acquire() as conn:
        return await dialect.introspect(conn, SCHEMAS)


async def assert_no_drift(driver: PostgresDriver, desired: Snapshot) -> None:
    async with driver.acquire() as conn:
        current = await dialect.introspect(conn, SCHEMAS)
        expected = await dialect.normalize_snapshot(conn, desired)
    remaining = diff(current, expected, compare_expressions=False)
    assert remaining == [], [s.describe() for s in remaining]
    for k, fn in expected.get("functions", {}).items():  # the parts push does not compare
        assert current.get("functions", {})[k] == fn
    for k, table in expected["tables"].items():
        assert current["tables"][k]["indexes"] == table["indexes"]


async def assert_pull_round_trip(driver: PostgresDriver, tmp_path: Path) -> str:
    """The pulled module builds the snapshot of the database it was pulled from."""
    current = await live(driver)
    current["extensions"] = {}
    source = render_schema_module(current)
    name = f"pulled_mg_{uuid.uuid4().hex[:8]}"
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    members = list(vars(module).values())
    tables = [
        m
        for m in members
        if isinstance(m, type) and issubclass(m, Table) and "__table__" in vars(m)
    ]
    objects = [m for m in members if isinstance(m, Extension | Function | Trigger)]
    pulled = build_snapshot(tables, dialect, objects=objects)
    remaining = diff(current, pulled)
    assert remaining == [], (source, [s.describe() for s in remaining])
    return source


# -- grants ------------------------------------------------------------------------------


@final
class Docs(Table, name="docs", schema="mg"):
    id = integer().primary_key()
    title = text()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [grant(["select", "insert"], to=[reader]), grant("all", to=["public"])]


@final
class Docs2(Table, name="docs", schema="mg"):
    id = integer().primary_key()
    title = text()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [grant("select", to=[reader])]


def titles(where: str, grants: bool = True) -> type[View]:
    @final
    class Titles(View, name="titles", schema="mg"):
        title = text()

        @classmethod
        @override
        def query(cls, db: QueryBuilder):
            return sql(f"SELECT title FROM mg.docs WHERE {where}")

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [grant("select", to=[reader])] if grants else []

    return Titles


async def test_grants_are_declared_and_kept_across_view_recreation(
    driver: PostgresDriver, tmp_path: Path
) -> None:
    v1 = snap([Docs, titles("id > 0")])
    await migrate(driver, empty_snapshot("postgresql"), v1)
    docs = (await live(driver))["tables"]["mg.docs"].get("grants", {})
    assert docs[READER] == ["insert", "select"]
    assert "maintain" in docs["public"] or len(docs["public"]) == 7  # ALL, PostgreSQL 17+
    # Made outside the schema: kept when the view is created again.
    await execute(driver, f"GRANT UPDATE ON mg.titles TO {OTHER}")
    await execute(driver, "COMMENT ON VIEW mg.titles IS 'kept'")

    v2 = snap([Docs2, titles("id > 1")])
    steps = [s.describe() for s in await migrate(driver, v1, v2)]
    assert "keep privileges of titles" in steps and "restore privileges of titles" in steps
    assert "revoke insert on docs from mizzle_mg_reader" in steps
    current = await live(driver)
    assert current["tables"]["mg.docs"].get("grants", {}) == {READER: ["select"]}
    assert current.get("views", {})["mg.titles"].get("grants", {}) == {
        OTHER: ["update"],
        READER: ["select"],
    }
    [comment] = await execute(driver, "SELECT obj_description('mg.titles'::regclass) AS c")
    assert comment["c"] == "kept"

    # A declared grant that goes away is revoked, also after a re-create.
    v3 = snap([Docs2, titles("id > 2", grants=False)])
    await migrate(driver, v2, v3)
    grants = (await live(driver)).get("views", {})["mg.titles"].get("grants", {})
    assert grants == {OTHER: ["update"]}

    # push compares with the live database: roles the schema does not name are left alone.
    async with driver.acquire() as conn:
        target = await dialect.normalize_snapshot(conn, snap([Docs2, titles("id > 2")]))
        steps = diff(await dialect.introspect(conn, SCHEMAS), target, compare_expressions=False)
    assert [s.describe() for s in steps] == ["grant select on titles to mizzle_mg_reader"]
    source = await assert_pull_round_trip(driver, tmp_path)
    assert f"grant(['update'], to=['{OTHER}'])" in source


# -- functions -----------------------------------------------------------------------------

add1 = function(
    "add", schema="mg", returns="integer", language="sql", args="a integer", body="RETURN a + 1"
)
add2 = function(
    "add",
    schema="mg",
    returns="integer",
    language="sql",
    args="a integer, b integer, c integer DEFAULT 0",
    body="BEGIN ATOMIC\n SELECT a + b + c;\nEND",
    strict=True,
    parallel="safe",
    cost=5,
    volatility="immutable",
)
tidy = function(
    "tidy",
    schema="mg",
    returns="text",
    args="t text",
    body="BEGIN RETURN trim(t); END",
    settings={"search_path": "mg, pg_temp", "work_mem": "'64MB'"},
    leakproof=True,
    security_definer=True,
)
tidy2 = function("tidy", schema="mg", returns="text", args="t text", body="BEGIN RETURN t; END")


@final
class Holder(Table, name="holder", schema="mg"):  # a table, so the schema is in a snapshot
    id = integer()


async def test_function_overloads_options_and_sql_bodies(
    driver: PostgresDriver, tmp_path: Path
) -> None:
    v1 = snap([Holder], [add1, add2, tidy])
    assert set(v1.get("functions", {})) == {
        "mg.add(a integer)",
        "mg.add(a integer, b integer, c integer DEFAULT 0)",
        "mg.tidy(t text)",
    }
    await migrate(driver, empty_snapshot("postgresql"), v1)
    [row] = await execute(driver, "SELECT mg.add(1) AS one, mg.add(1, 5) AS two")
    assert (row["one"], row["two"]) == (2, 6)
    functions = (await live(driver)).get("functions", {})
    assert functions["mg.add(a integer)"]["body"] == "RETURN (a + 1)"  # as the server prints it
    tidy_live = functions["mg.tidy(t text)"]
    assert tidy_live.get("settings") == {"search_path": "mg, pg_temp", "work_mem": "'64MB'"}
    assert tidy_live.get("leakproof") is True
    await assert_pull_round_trip(driver, tmp_path)

    # One overload goes, the other stays; options are replaced in place.
    v2 = snap([Holder], [add2, tidy2])
    steps = [s.describe() for s in await migrate(driver, v1, v2)]
    assert steps == ["drop function add", "create function tidy"]
    tidy_live = (await live(driver)).get("functions", {})["mg.tidy(t text)"]
    assert "settings" not in tidy_live and "leakproof" not in tidy_live


# -- triggers ------------------------------------------------------------------------------


@final
class Items(Table, name="items", schema="mg"):
    id = integer().primary_key()
    name = text()
    price = integer()
    seen = integer()


@final
class ItemNames(View, name="item_names", schema="mg"):
    id = integer()
    name = text()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return db.select(Items.id, Items.name).from_(Items)


bump = function("bump", schema="mg", returns="trigger", body="BEGIN RETURN NEW; END")
log = function("log", schema="mg", returns="trigger", body="BEGIN RETURN NULL; END")
insert_into_items = function(
    "insert_item",
    schema="mg",
    returns="trigger",
    body="BEGIN INSERT INTO mg.items (id, name) VALUES (NEW.id, NEW.name); RETURN NEW; END",
)


async def test_trigger_kinds(driver: PostgresDriver, tmp_path: Path) -> None:
    def objects(*update_of: str) -> list[SchemaObject]:
        columns = [Items.__table__.columns[c] for c in update_of]
        return [
            trigger("on_price", on=Items, execute=bump, events=["update"], update_of=columns),
            trigger(
                "audit",
                on=Items,
                execute=log,
                timing="after",
                events=["insert"],
                for_each="statement",
                new_table="added",
            ),
            trigger(
                "insert_name",
                on=ItemNames,
                execute=insert_into_items,
                timing="instead of",
                events=["insert"],
            ),
        ]

    v1 = snap([Items, ItemNames], objects("price", "name"))
    trg = v1["tables"]["mg.items"].get("triggers", {})["on_price"]
    assert trg.get("columns") == ["name", "price"]
    await migrate(driver, empty_snapshot("postgresql"), v1)
    await execute(driver, "INSERT INTO mg.item_names (id, name) VALUES (1, 'a')")
    [row] = await execute(driver, "SELECT name FROM mg.items WHERE id = 1")
    assert row["name"] == "a"
    await assert_pull_round_trip(driver, tmp_path)

    v2 = snap([Items, ItemNames], objects("seen"))
    steps = [s.describe() for s in await migrate(driver, v1, v2)]
    assert steps == ["replace trigger on_price on items"]


# -- table <-> view ------------------------------------------------------------------------


@final
class Report(Table, name="report", schema="mg"):
    id = integer()


@final
class ReportView(View, name="report", schema="mg"):
    id = integer()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return sql("SELECT 1 AS id")


async def test_table_replaced_by_view_and_back(driver: PostgresDriver) -> None:
    tables = snap([Holder, Report])
    views = snap([Holder, ReportView])
    await migrate(driver, empty_snapshot("postgresql"), tables)
    steps = [s.describe() for s in await migrate(driver, tables, views)]
    assert steps.index("drop table report") < steps.index("create view report")
    steps = [s.describe() for s in await migrate(driver, views, tables)]
    assert steps.index("drop view report") < steps.index("create table report")


# -- indexes -------------------------------------------------------------------------------


def people(opclass: str | list[str | None] | None) -> type[Table]:
    @final
    class People(Table, name="people", schema="mg"):
        first = text()
        last = text()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            ix = index("people_names").using("btree", opclass) if opclass else index("people_names")
            return [ix.on(cls.first, cls.last)]

    return People


async def test_default_and_per_key_operator_classes(driver: PostgresDriver, tmp_path: Path) -> None:
    named_default = snap([people("text_ops")])
    await migrate(driver, empty_snapshot("postgresql"), named_default)
    index_snap = (await live(driver))["tables"]["mg.people"]["indexes"]["people_names"]
    assert "opclasses" not in index_snap  # the default one: the same as naming none
    assert diff(await live(driver), snap([people(None)])) == []

    per_key = snap([people([None, "text_pattern_ops"])])
    await migrate(driver, named_default, per_key)
    index_snap = (await live(driver))["tables"]["mg.people"]["indexes"]["people_names"]
    assert index_snap.get("opclasses") == [None, "text_pattern_ops"]
    source = await assert_pull_round_trip(driver, tmp_path)
    assert "[None, 'text_pattern_ops']" in source


# -- generated columns ----------------------------------------------------------------------


def calc(kind: str) -> type[Table]:
    @final
    class Calc(Table, name="calc", schema="mg"):
        a = integer()
        b = integer()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [] if kind == "virtual" else [index("calc_b").on(cls.b)]  # not supported

    column = Calc.__table__.columns["b"]
    if kind != "plain":
        column.computed = Computed(sql("a * 2"), stored=kind == "stored")
        column.generated = True
    return Calc


async def test_generated_column_rebuilds(driver: PostgresDriver) -> None:
    plain, stored, virtual = snap([calc("plain")]), snap([calc("stored")]), snap([calc("virtual")])
    await migrate(driver, empty_snapshot("postgresql"), plain)
    await execute(driver, "INSERT INTO mg.calc (a, b) VALUES (3, 1)")
    steps = await migrate(driver, plain, stored)
    assert [s.describe() for s in steps] == [
        "drop index calc_b",
        "drop column calc.b",
        "add column calc.b",
        "create index calc_b",
    ]
    assert any(s.data_loss for s in steps)  # the CLI asks for --allow-data-loss
    [row] = await execute(driver, "SELECT b FROM mg.calc")
    assert row["b"] == 6
    if await server_version(driver) >= 180000:  # VIRTUAL is PostgreSQL 18+
        await migrate(driver, stored, virtual)
        await migrate(driver, virtual, plain)
    else:
        await migrate(driver, stored, plain)


def keyed(kind: str) -> list[type[Table]]:
    """calc.b is unique and another table references it."""

    @final
    class Calc(Table, name="calc", schema="mg"):
        a = integer()
        b = integer().unique()

    @final
    class Uses(Table, name="uses", schema="mg"):
        b = integer().references(lambda: Calc.b)

    if kind != "plain":
        Calc.b.computed = Computed(sql(f"a * {kind}"))
        Calc.b.generated = True
    return [Calc, Uses]


async def test_rebuilt_column_referenced_by_a_foreign_key(driver: PostgresDriver) -> None:
    plain, stored = snap(keyed("plain")), snap(keyed("2"))
    await migrate(driver, empty_snapshot("postgresql"), plain)
    steps = [s.describe() for s in await migrate(driver, plain, stored)]
    assert steps.index("drop foreign key uses_b_fkey") < steps.index("drop column calc.b")
    assert steps[-1] == "add foreign key uses_b_fkey"

    # PostgreSQL 16 and older: SET EXPRESSION is drop + add in a DO block, which keeps the
    # foreign keys of other tables. Run that branch here by asking for a later version.
    await execute(driver, "INSERT INTO mg.calc (a) VALUES (1), (2), (3)")
    await execute(driver, "INSERT INTO mg.uses VALUES (6)")  # checked again after the change
    [change] = dialect.render(diff(stored, snap(keyed("3"))))
    assert ">= 170000" in change.sql
    async with driver.acquire() as conn:
        await apply_statements(conn, [Statement(change.sql.replace(">= 170000", ">= 9900000"))])
    await assert_no_drift(driver, snap(keyed("3")))
    rows = await execute(driver, "SELECT b FROM mg.calc ORDER BY a")
    assert [r["b"] for r in rows] == [3, 6, 9]
    [ref] = await execute(
        driver, "SELECT count(*) AS n FROM pg_constraint WHERE conname = 'uses_b_fkey'"
    )
    assert ref["n"] == 1


# -- enums in another schema, moving tables, identity options -------------------------------


class Mood(enum.Enum):
    HAPPY = "happy"
    SAD = "sad"


@final
class Moods(Table, name="moods", schema="mg"):
    id = integer().generated_always_as_identity(
        start=5, increment=2, min_value=5, max_value=1000, cache=10, cycle=True
    )
    down = smallint().generated_by_default_as_identity(increment=-1, min_value=-100)
    big = bigint().generated_by_default_as_identity(max_value=10**12)
    mood = pg_enum(Mood, type_name="mg_mood", schema="mg")


@final
class Moods2(Table, name="moods", schema="mg2"):  # moved
    id = integer().generated_always_as_identity(
        start=5, increment=2, min_value=5, max_value=1000, cache=10, cycle=True
    )
    down = smallint().generated_by_default_as_identity(increment=-1, min_value=-100)
    big = bigint().generated_by_default_as_identity(max_value=10**12)
    mood = pg_enum(Mood, type_name="mg_mood", schema="mg")


@final
class Moods3(Table, name="moods", schema="mg2"):  # sequence options changed
    id = integer().generated_always_as_identity(start=-5, increment=-2)
    down = smallint().generated_by_default_as_identity(increment=-1)
    big = bigint().generated_by_default_as_identity(max_value=10**12, cycle=True)
    mood = pg_enum(Mood, type_name="mg_mood", schema="mg")


async def test_enum_schema_table_move_and_identity_options(
    driver: PostgresDriver, tmp_path: Path
) -> None:
    v1 = snap([Moods])
    assert v1["enums"] == {
        "mg.mg_mood": {"schema": "mg", "name": "mg_mood", "values": ["happy", "sad"]}
    }
    assert v1["tables"]["mg.moods"]["columns"]["mood"]["type"] == "mg.mg_mood"
    await migrate(driver, empty_snapshot("postgresql"), v1)
    await execute(driver, "INSERT INTO mg.moods (mood) VALUES ('sad')")
    await assert_pull_round_trip(driver, tmp_path)

    v2 = snap([Moods2])
    steps = await migrate(driver, v1, v2, renames={"mg.moods": "mg2.moods"})
    assert [s.describe() for s in steps] == [
        "create schema mg2",
        "move table moods from schema mg to mg2",
    ]
    [row] = await execute(driver, "SELECT id, mood::text FROM mg2.moods")
    assert (row["id"], row["mood"]) == (5, "sad")  # moved, not dropped
    with pytest.raises(Exception, match=r"--rename mg\.moods=mg2\.moods"):
        diff(v1, v2, StrictRenames())

    v3 = snap([Moods3])
    await migrate(driver, v2, v3)
    await assert_pull_round_trip(driver, tmp_path)


@final
class Audit(Table, name="audit", schema="mg"):
    actor = text().default(sql("current_user"))
    at = timestamp(tz=True).default(sql("current_timestamp(3)"))
    day = date().default(sql("current_date"))


async def test_sql_keyword_defaults_converge(driver: PostgresDriver) -> None:
    """PostgreSQL prints them in upper case: push must find nothing left to do."""
    await migrate(driver, empty_snapshot("postgresql"), snap([Audit]))


# -- NULLS NOT DISTINCT (PostgreSQL 15+) ----------------------------------------------------


def nulls(kind: str) -> type[Table]:
    @final
    class Nulls(Table, name="nulls", schema="mg"):
        a = integer().unique(nulls_not_distinct=True) if kind == "column" else integer()
        b = text()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            if kind == "unique":
                return [unique(cls.b, nulls_not_distinct=True)]
            if kind == "index":
                return [unique_index("nulls_b", nulls_not_distinct=True).on(cls.b)]
            return []

    return Nulls


async def test_nulls_not_distinct_round_trip(driver: PostgresDriver, tmp_path: Path) -> None:
    column_unique = snap([nulls("column")])
    await migrate(driver, empty_snapshot("postgresql"), column_unique)
    assert (await live(driver))["tables"]["mg.nulls"]["uniques"]["nulls_a_key"].get(
        "nulls_not_distinct"
    )
    await assert_pull_round_trip(driver, tmp_path)

    distinct = snap([nulls("distinct")])
    steps = await migrate(driver, column_unique, distinct)
    assert any(s.describe().startswith("drop unique") for s in steps)
    await assert_pull_round_trip(driver, tmp_path)

    constraint = snap([nulls("unique")])
    await migrate(driver, distinct, constraint)
    live_unique = (await live(driver))["tables"]["mg.nulls"]["uniques"]
    assert any(u.get("nulls_not_distinct") for u in live_unique.values())
    await assert_pull_round_trip(driver, tmp_path)

    named_index = snap([nulls("index")])
    await migrate(driver, constraint, named_index)
    index_snap = (await live(driver))["tables"]["mg.nulls"]["indexes"]["nulls_b"]
    assert index_snap.get("nulls_not_distinct") is True
    await assert_pull_round_trip(driver, tmp_path)


# -- deferrable / NOT VALID constraints ----------------------------------------------------


@final
class DefColumn(Table, name="def_column", schema="mg"):
    id = integer().primary_key(deferrable=True, initially_deferred=True)
    code = text().unique(deferrable=True)


@final
class DefColumnPlain(Table, name="def_column", schema="mg"):
    id = integer().primary_key()
    code = text().unique()


@final
class DefTable(Table, name="def_table", schema="mg"):
    code = text()
    ref = text()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            primary_key(
                cls.code, name="def_table_code_pk", deferrable=True, initially_deferred=True
            ),
            unique(cls.ref, name="def_table_ref_uniq", deferrable=True),
        ]


@final
class DefParent(Table, name="def_parent", schema="mg"):
    code = text().primary_key()


@final
class DefFk(Table, name="def_fk", schema="mg"):
    ref = text().references(
        lambda: DefParent.code, deferrable=True, initially_deferred=True, not_valid=True
    )


@final
class DefCheck(Table, name="def_check", schema="mg"):
    amount = integer()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [check("def_check_amount", sql("amount is null or amount > 0"), not_valid=True)]


async def test_deferrable_and_not_valid_round_trip(driver: PostgresDriver, tmp_path: Path) -> None:
    tables = [DefColumn, DefTable, DefParent, DefFk, DefCheck]
    await migrate(driver, empty_snapshot("postgresql"), snap(tables))
    live_tables = (await live(driver))["tables"]
    pk = live_tables["mg.def_column"]["primary_key"]
    assert pk is not None
    assert pk.get("deferrable") and pk.get("initially_deferred")
    assert live_tables["mg.def_column"]["uniques"]["def_column_code_key"].get("deferrable")
    table_pk = live_tables["mg.def_table"]["primary_key"]
    assert table_pk is not None
    assert table_pk.get("initially_deferred")
    assert live_tables["mg.def_table"]["uniques"]["def_table_ref_uniq"].get("deferrable")
    fk = live_tables["mg.def_fk"]["foreign_keys"]["def_fk_ref_fkey"]
    assert fk.get("deferrable") and fk.get("initially_deferred") and fk.get("not_valid")
    assert live_tables["mg.def_check"]["checks"]["def_check_amount"].get("not_valid")
    await assert_pull_round_trip(driver, tmp_path)

    # Turning the column-level options off rewrites the constraints, and the result is clean.
    plain = snap([DefColumnPlain, DefTable, DefParent, DefFk, DefCheck])
    steps = await migrate(driver, snap(tables), plain)
    described = [s.describe() for s in steps]
    assert any(d.startswith("drop primary key") for d in described), described
    remaining = (await live(driver))["tables"]["mg.def_column"]
    remaining_pk = remaining["primary_key"]
    assert remaining_pk is not None
    assert not remaining_pk.get("deferrable")
    assert not remaining["uniques"]["def_column_code_key"].get("deferrable")
    await assert_pull_round_trip(driver, tmp_path)

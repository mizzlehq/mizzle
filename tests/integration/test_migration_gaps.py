"""Grants, function overloads and options, trigger kinds, table <-> view swaps, SQL-standard
function bodies, operator classes, generated column rebuilds, enums in other schemas, table
moves and identity options: applied, introspected without drift, and pulled back."""

import copy
import enum
import importlib.util
import sys
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from pathlib import Path
from typing import cast, final, override

import pytest
from asyncpg import Range

from mizzle import (
    Constraint,
    DatabaseError,
    QueryBuilder,
    Table,
    View,
    check,
    composite,
    domain,
    exclusion,
    extension,
    function,
    grant,
    index,
    partition,
    primary_key,
    role,
    sql,
    trigger,
    unique,
    unique_index,
)
from mizzle.core.errors import MigrationError
from mizzle.core.schema.column import Computed
from mizzle.core.schema.objects import (
    Composite,
    Domain,
    Extension,
    Function,
    SchemaObject,
    Trigger,
)
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.steps import DropExtension, Statement, Step
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.codegen import render_schema_module
from mizzle.kit.diff import StrictRenames, diff
from mizzle.kit.migrate import apply_statements
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import (
    bigint,
    bit,
    box,
    char,
    cidr,
    circle,
    citext,
    date,
    daterange,
    hstore,
    int4multirange,
    int4range,
    integer,
    interval,
    line,
    lseg,
    ltree,
    macaddr,
    macaddr8,
    money,
    numrange,
    path,
    pg_composite,
    pg_domain,
    pg_enum,
    point,
    polygon,
    smallint,
    text,
    timestamp,
    tsrange,
    tstzrange,
    varbit,
    xml,
)
from tests.integration.test_generated_columns import server_version

dialect = PostgresDialect()
SCHEMAS = ["mg", "mg2"]
READER, OTHER = "mizzle_mg_reader", "mizzle_mg_other"
reader = role(READER, existing=True)

RESET = f"""
DROP SCHEMA IF EXISTS mg CASCADE;
DROP SCHEMA IF EXISTS mg2 CASCADE;
DROP SCHEMA IF EXISTS mg3 CASCADE;
DROP ROLE IF EXISTS {READER};
DROP ROLE IF EXISTS {OTHER};
DROP EXTENSION IF EXISTS hstore, citext, ltree CASCADE;
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
    remaining = diff(current, expected, compare_expressions=False, normalized=True)
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
    objects = [
        m for m in members if isinstance(m, Composite | Domain | Extension | Function | Trigger)
    ]
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
    assert steps == ["create function tidy", "drop function add"]  # (K1) drops go last
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
    # S6: NULLS NOT DISTINCT is PostgreSQL 15+. On 14 the guard names the version instead
    # of a raw syntax error.
    if await server_version(driver) < 150000:
        query = snap([nulls("column")])
        with pytest.raises(DatabaseError, match=r"NULLS NOT DISTINCT needs PostgreSQL 15"):
            await migrate(driver, empty_snapshot("postgresql"), query)
        return
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


# -- fixed-length strings (character / bpchar) ---------------------------------------------


@final
class Chars(Table, name="chars", schema="mg"):
    fixed = char(4)
    single = char()


async def test_char_types_round_trip(driver: PostgresDriver, tmp_path: Path) -> None:
    await migrate(driver, empty_snapshot("postgresql"), snap([Chars]))
    columns = (await live(driver))["tables"]["mg.chars"]["columns"]
    assert columns["fixed"]["type"] == "character(4)"
    assert columns["single"]["type"] == "character(1)"
    await execute(driver, "INSERT INTO mg.chars (fixed, single) VALUES ('abcd', 'x')")
    [row] = await execute(driver, "SELECT fixed, single FROM mg.chars")
    assert (row["fixed"], row["single"]) == ("abcd", "x")
    await assert_pull_round_trip(driver, tmp_path)


# -- hand-written CHECK and exclusion predicates (T10) -------------------------------------


@final
class Bookings(Table, name="bookings", schema="mg"):
    during = tsrange().not_null()
    status = text().not_null()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        # A range key needs no extra operator class, so the test needs no btree_gist.
        return [
            # Written the way a person writes it: no outer parens, no ::text casts.
            check("bookings_status_valid", sql("status <> ''"), not_valid=True),
            exclusion(
                (cls.during, "&&"),
                name="bookings_no_overlap",
                using="gist",
                where=sql("status = 'active'"),
            ),
        ]


async def test_hand_written_constraint_text_does_not_drift(
    driver: PostgresDriver, tmp_path: Path
) -> None:
    # T10: PostgreSQL prints ``(status <> ''::text)``; a hand-written ``status <> ''`` must
    # compare equal instead of being dropped and re-added on every generate.
    await migrate(driver, empty_snapshot("postgresql"), snap([Bookings]))
    live_left = await live(driver)
    async with driver.acquire() as conn:
        desired = await dialect.normalize_snapshot(conn, snap([Bookings]))
    live_table, wanted = live_left["tables"]["mg.bookings"], desired["tables"]["mg.bookings"]
    assert live_table["checks"]["bookings_status_valid"]["expression"].endswith("::text)")
    assert (
        wanted["checks"]["bookings_status_valid"]["expression"]
        == live_table["checks"]["bookings_status_valid"]["expression"]
    )
    live_exclusions = live_table.get("exclusions", {})
    wanted_exclusions = wanted.get("exclusions", {})
    assert wanted_exclusions["bookings_no_overlap"].get("where") == (
        live_exclusions["bookings_no_overlap"].get("where")
    )
    remaining = diff(live_left, desired, compare_expressions=True)
    assert [s for s in remaining if not isinstance(s, DropExtension)] == []
    await assert_pull_round_trip(driver, tmp_path)


# -- column collation ----------------------------------------------------------------------


@final
class CollatedText(Table, name="collated_text", schema="mg"):
    label = text().collate("C")


@final
class PlainText(Table, name="collated_text", schema="mg"):
    label = text()


async def test_column_collation_round_trip(driver: PostgresDriver, tmp_path: Path) -> None:
    collated = snap([CollatedText])
    await migrate(driver, empty_snapshot("postgresql"), collated)
    columns = (await live(driver))["tables"]["mg.collated_text"]["columns"]
    assert columns["label"].get("collation") == "C"
    await assert_pull_round_trip(driver, tmp_path)

    plain = snap([PlainText])
    steps = await migrate(driver, collated, plain)
    assert any(s.describe().startswith("change collation") for s in steps)
    columns = (await live(driver))["tables"]["mg.collated_text"]["columns"]
    assert "collation" not in columns["label"]
    await assert_pull_round_trip(driver, tmp_path)


# -- comments ------------------------------------------------------------------------------


@final
class Documented(Table, name="documented", schema="mg", comment="a table"):
    id = integer().comment("the id")
    note = text().comment("a note")


@final
class DocumentedLater(Table, name="documented", schema="mg", comment="a renamed table"):
    id = integer()  # its comment is removed
    note = text().comment("a note")


async def test_table_and_column_comments(driver: PostgresDriver, tmp_path: Path) -> None:
    v1 = snap([Documented])
    await migrate(driver, empty_snapshot("postgresql"), v1)
    table = (await live(driver))["tables"]["mg.documented"]
    assert table.get("comment") == "a table"
    assert table["columns"]["id"].get("comment") == "the id"
    assert table["columns"]["note"].get("comment") == "a note"
    await assert_pull_round_trip(driver, tmp_path)

    v2 = snap([DocumentedLater])
    steps = await migrate(driver, v1, v2)
    assert any(s.describe().endswith("on table documented") for s in steps)
    assert any(s.describe().endswith("on documented.id") for s in steps)
    table = (await live(driver))["tables"]["mg.documented"]
    assert table.get("comment") == "a renamed table"
    assert "comment" not in table["columns"]["id"]
    await assert_pull_round_trip(driver, tmp_path)


# -- unlogged tables and storage parameters -------------------------------------------------


@final
class Cache(Table, name="cache", schema="mg", unlogged=True, storage={"fillfactor": 70}):
    id = integer().not_null()
    payload = text()


@final
class CacheLogged(
    Table, name="cache", schema="mg", storage={"fillfactor": 90, "autovacuum_enabled": "false"}
):
    id = integer().not_null()
    payload = text()


async def test_unlogged_and_storage_parameters(driver: PostgresDriver, tmp_path: Path) -> None:
    v1 = snap([Cache])
    await migrate(driver, empty_snapshot("postgresql"), v1)
    table = (await live(driver))["tables"]["mg.cache"]
    assert table.get("unlogged") is True
    assert table.get("options") == {"fillfactor": "70"}
    await assert_pull_round_trip(driver, tmp_path)

    v2 = snap([CacheLogged])
    steps = await migrate(driver, v1, v2)
    described = [s.describe() for s in steps]
    assert any(d.startswith("set cache logged") for d in described), described
    assert any("storage of cache" in d for d in described), described
    table = (await live(driver))["tables"]["mg.cache"]
    assert "unlogged" not in table
    assert table.get("options") == {"fillfactor": "90", "autovacuum_enabled": "false"}
    await assert_pull_round_trip(driver, tmp_path)


# -- exclusion constraints -----------------------------------------------------------------


@final
class Reservation(Table, name="reservation", schema="mg"):
    id = integer().primary_key()
    room = text().not_null()
    slot = integer().not_null()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            exclusion(
                (cls.room, "="),
                (cls.slot, "="),
                name="reservation_booked",
                using="btree",
                deferrable=True,
            ),
            exclusion(
                (sql("lower(room)"), "="),
                name="reservation_lower_room",
                using="btree",
                where=sql("slot > 0"),
            ),
        ]


@final
class ReservationPlain(Table, name="reservation", schema="mg"):
    id = integer().primary_key()
    room = text().not_null()
    slot = integer().not_null()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            exclusion(
                (cls.room, "="),
                (cls.slot, "="),
                name="reservation_booked",
                using="btree",
            )
        ]


@final
class ReservationNone(Table, name="reservation", schema="mg"):
    id = integer().primary_key()
    room = text().not_null()
    slot = integer().not_null()


async def test_exclusion_constraints_round_trip(driver: PostgresDriver, tmp_path: Path) -> None:
    v1 = snap([Reservation])
    await migrate(driver, empty_snapshot("postgresql"), v1)
    exclusions = (await live(driver))["tables"]["mg.reservation"].get("exclusions", {})
    booked = exclusions["reservation_booked"]
    assert booked["method"] == "btree"
    assert booked.get("deferrable")
    assert [(e.get("column"), e["operator"]) for e in booked["elements"]] == [
        ("room", "="),
        ("slot", "="),
    ]
    lower = exclusions["reservation_lower_room"]
    assert lower["elements"][0].get("expression") == "lower(room)"
    assert lower.get("where")

    # The constraints are enforced: a duplicate room+slot is rejected, and the expression key
    # makes "A" collide with "a".
    await execute(driver, "INSERT INTO mg.reservation (id, room, slot) VALUES (1, 'A', 1)")
    with pytest.raises(DatabaseError):
        await execute(driver, "INSERT INTO mg.reservation (id, room, slot) VALUES (2, 'A', 1)")
    with pytest.raises(DatabaseError):
        await execute(driver, "INSERT INTO mg.reservation (id, room, slot) VALUES (3, 'a', 1)")
    await assert_pull_round_trip(driver, tmp_path)

    # A changed option is a drop and add; the constraint can go away entirely.
    v2 = snap([ReservationPlain])
    steps = await migrate(driver, v1, v2)
    described = [s.describe() for s in steps]
    assert any(d.startswith("drop exclusion") for d in described), described
    assert any(d.startswith("add exclusion") for d in described), described
    assert (
        not (await live(driver))["tables"]["mg.reservation"]
        .get("exclusions", {})["reservation_booked"]
        .get("deferrable")
    )
    await assert_pull_round_trip(driver, tmp_path)

    v3 = snap([ReservationNone])
    steps = await migrate(driver, v2, v3)
    assert any(d.startswith("drop exclusion") for d in [s.describe() for s in steps])
    assert "exclusions" not in (await live(driver))["tables"]["mg.reservation"]
    await assert_pull_round_trip(driver, tmp_path)


# -- range and multirange types ------------------------------------------------------------


@final
class Spans(Table, name="spans", schema="mg"):
    id = integer().primary_key()
    ints = int4range()
    nums = numrange()
    stamps = tstzrange()
    days = daterange()
    multi = int4multirange()


async def test_range_types_round_trip(driver: PostgresDriver, tmp_path: Path) -> None:
    await migrate(driver, empty_snapshot("postgresql"), snap([Spans]))
    columns = (await live(driver))["tables"]["mg.spans"]["columns"]
    assert columns["ints"]["type"] == "int4range"
    assert columns["nums"]["type"] == "numrange"
    assert columns["stamps"]["type"] == "tstzrange"
    assert columns["days"]["type"] == "daterange"
    assert columns["multi"]["type"] == "int4multirange"

    await execute(
        driver,
        "INSERT INTO mg.spans (id, ints, nums, stamps, days, multi) VALUES "
        "(1, '[1,5)', '(1.5,2.5)', '[2020-01-01,2020-02-01)', "
        "'[2020-01-01,2020-02-01)', '{[1,3),[5,9)}')",
    )
    [row] = await execute(driver, "SELECT ints, multi FROM mg.spans")
    ints = cast("Range[int]", row["ints"])
    multi = cast("list[Range[int]]", row["multi"])
    assert (ints.lower, ints.upper, ints.lower_inc, ints.upper_inc) == (1, 5, True, False)
    assert [r.lower for r in multi] == [1, 5]
    await assert_pull_round_trip(driver, tmp_path)


# -- more column types: network, bit, xml, money, geometric, extension types ----------------


@final
class Assorted(Table, name="assorted", schema="mg"):
    id = integer().primary_key()
    net = cidr()
    mac = macaddr()
    mac8 = macaddr8()
    flags = bit(4)
    variable = varbit(8)
    doc = xml()
    amount = money()
    pt = point()
    ln = line()
    seg = lseg()
    rect = box()
    trace = path()
    area = polygon()
    disc = circle()
    attrs = hstore()
    label = citext()
    tree = ltree()


EXTENSIONS: list[SchemaObject] = [extension("hstore"), extension("citext"), extension("ltree")]


async def test_more_column_types_round_trip(driver: PostgresDriver, tmp_path: Path) -> None:
    await migrate(driver, empty_snapshot("postgresql"), snap([Assorted], EXTENSIONS))
    columns = (await live(driver))["tables"]["mg.assorted"]["columns"]
    expected = {
        "net": "cidr",
        "mac": "macaddr",
        "mac8": "macaddr8",
        "flags": "bit(4)",
        "variable": "bit varying(8)",
        "doc": "xml",
        "amount": "money",
        "pt": "point",
        "ln": "line",
        "seg": "lseg",
        "rect": "box",
        "trace": "path",
        "area": "polygon",
        "disc": "circle",
        "attrs": "hstore",
        "label": "citext",
        "tree": "ltree",
    }
    assert {name: columns[name]["type"] for name in expected} == expected

    await execute(
        driver,
        "INSERT INTO mg.assorted (id, net, mac, mac8, flags, variable, doc, amount, pt, ln, seg, "
        "rect, trace, area, disc, attrs, label, tree) VALUES "
        "(1, '10.0.0.0/8', '08:00:2b:01:02:03', '08:00:2b:01:02:03:04:05', B'1010', B'1010', "
        "'<a/>', '12.34', '(1,2)', '{1,2,3}', '[(1,2),(3,4)]', '(1,2),(3,4)', '[(1,2),(3,4)]', "
        "'((1,2),(3,4),(5,6))', '<(1,2),3>', 'a=>1', 'Hello', 'a.b')",
    )
    [row] = await execute(
        driver, "SELECT net, mac, mac8, flags, variable, doc, attrs, label, tree FROM mg.assorted"
    )
    assert str(row["net"]) == "10.0.0.0/8"
    assert row["mac"] == "08:00:2b:01:02:03"
    assert row["mac8"] == "08:00:2b:01:02:03:04:05"
    assert "1010" in str(row["flags"])
    assert "1010" in str(row["variable"])
    assert row["doc"] == "<a/>"
    assert '"a"=>"1"' in cast(str, row["attrs"])
    assert row["label"] == "Hello"
    assert row["tree"] == "a.b"
    await assert_pull_round_trip(driver, tmp_path)


# -- domains -------------------------------------------------------------------------------

email_domain = domain(
    "mg_email",
    schema="mg",
    as_="text",
    not_null=True,
    default="'none'::text",
    check="VALUE ~ '@'",
)


@final
class Subscribers(Table, name="subscribers", schema="mg"):
    id = integer().primary_key()
    email = pg_domain("mg_email", str, schema="mg")


async def test_domains(driver: PostgresDriver, tmp_path: Path) -> None:
    v1 = snap([Subscribers], objects=[email_domain])
    assert v1.get("domains") == {
        "mg.mg_email": {
            "schema": "mg",
            "name": "mg_email",
            "base_type": "text",
            "not_null": True,
            "default": "'none'",
            "checks": ["VALUE ~ '@'"],
        }
    }
    assert v1["tables"]["mg.subscribers"]["columns"]["email"]["type"] == "mg.mg_email"

    await migrate(driver, empty_snapshot("postgresql"), v1)
    [row] = await execute(
        driver, "INSERT INTO mg.subscribers (id, email) VALUES (1, 'a@b') RETURNING email"
    )
    assert row["email"] == "a@b"  # asyncpg decodes the domain as its base type
    with pytest.raises(DatabaseError, match="mg_email"):
        await execute(driver, "INSERT INTO mg.subscribers (id, email) VALUES (2, 'nope')")
    await assert_pull_round_trip(driver, tmp_path)

    # The domain goes after the table that uses it, on the way out.
    v2 = snap([])
    steps = await migrate(driver, v1, v2)
    assert [s.describe() for s in steps] == ["drop table subscribers", "drop domain mg_email"]

    changed = domain("mg_email", schema="mg", as_="text", not_null=False, check="VALUE ~ '@'")
    v3 = snap([Subscribers], objects=[changed])
    with pytest.raises(MigrationError, match="domain mg_email"):
        diff(v1, v3, StrictRenames())


# -- partitions --------------------------------------------------------------------------


def _partitioned(name: str, key: str, parts: list[tuple[str, str]]) -> type[Table]:
    @final
    class P(Table, name=name, schema="mg", partition_by=key):
        id = integer()
        logged_at = date()
        region = text()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [partition(n, for_values=b) for n, b in parts]

    return P


async def test_partitions(driver: PostgresDriver, tmp_path: Path) -> None:
    r2026 = ("measurements_2026", "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')")
    r2027 = ("measurements_2027", "FOR VALUES FROM ('2027-01-01') TO ('2028-01-01')")
    rdef = ("measurements_def", "DEFAULT")
    v1 = snap([_partitioned("measurements", "RANGE (logged_at)", [r2026, rdef])])
    assert v1["tables"]["mg.measurements"].get("partition_by") == "RANGE (logged_at)"
    assert v1["tables"]["mg.measurements"].get("partitions") == {
        r2026[0]: r2026[1],
        rdef[0]: rdef[1],
    }

    await migrate(driver, empty_snapshot("postgresql"), v1)
    # A row routes to the partition its key falls in; the rest lands in the default.
    await execute(
        driver,
        "INSERT INTO mg.measurements (id, logged_at, region) VALUES "
        "(1, DATE '2026-06-01', 'eu'), (2, DATE '2030-01-01', 'us')",
    )
    [row] = await execute(driver, "SELECT count(*) AS n FROM mg.measurements_2026")
    assert row["n"] == 1
    [row] = await execute(driver, "SELECT count(*) AS n FROM mg.measurements_def")
    assert row["n"] == 1
    await assert_pull_round_trip(driver, tmp_path)

    # Swap the default partition for a new range one.
    v2 = snap([_partitioned("measurements", "RANGE (logged_at)", [r2026, r2027])])
    steps = [s.describe() for s in await migrate(driver, v1, v2)]
    assert steps == [
        "create partition measurements_2027 of measurements",
        "drop partition measurements_def",
    ]

    # A list partition keeps its bound text through pull round trips (real PostgreSQL
    # reformats the bound, and the snapshot matches it).
    eu = ("regions_eu", "FOR VALUES IN ('eu', 'uk')")
    other = ("regions_other", "FOR VALUES IN ('us')")
    v3 = snap([_partitioned("regions", "LIST (region)", [eu, other])])
    await migrate(driver, v2, snap([]))  # only drops: partitions go with the table
    await migrate(driver, empty_snapshot("postgresql"), v3)
    await execute(
        driver, "INSERT INTO mg.regions (id, logged_at, region) VALUES (1, DATE '2020-01-01', 'uk')"
    )
    [row] = await execute(driver, "SELECT count(*) AS n FROM mg.regions_eu")
    assert row["n"] == 1
    with pytest.raises(DatabaseError, match="no partition"):
        await execute(
            driver,
            "INSERT INTO mg.regions (id, logged_at, region) VALUES (2, DATE '2020-01-01', 'fr')",
        )
    source = await assert_pull_round_trip(driver, tmp_path)
    assert "FOR VALUES IN ('eu', 'uk')" in source
    assert "partition_by='LIST (region)'" in source

    # Changing the key in place is not expressible: it needs a hand-written migration.
    changed = snap([_partitioned("regions", "LIST (id)", [eu, other])])
    with pytest.raises(MigrationError, match="partition key"):
        diff(v3, changed, StrictRenames())

    # Dropping the partitioned table takes its partitions with it.
    steps = await migrate(driver, v3, snap([]))
    assert [s.describe() for s in steps] == ["drop table regions"]


@final
class Events(Table, name="events", schema="mg", partition_by="RANGE (at)"):
    id = integer()
    at = timestamp(tz=True)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            partition("events_2026", for_values="for values from ('2026-01-01') to ('2027-01-01')"),
            partition("events_def", for_values="default"),
        ]


@final
class Codes(Table, name="codes", schema="mg", partition_by="LIST (code)"):
    code = integer()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [partition("codes_low", for_values="for values in (1, 2)")]


async def test_partition_bounds_in_any_spelling_do_not_drift(driver: PostgresDriver) -> None:
    # B30: the bound must be normalized against an existing parent too (info/doctor/push).
    desired = snap([Events, Codes])
    await migrate(driver, empty_snapshot("postgresql"), desired)  # asserts no drift
    async with driver.acquire() as conn:
        normalized = await dialect.normalize_snapshot(conn, desired)
    assert normalized["tables"]["mg.events"].get("partitions", {})["events_def"] == "DEFAULT"


class NewMood(enum.Enum):
    CALM = "calm"


@final
class EventsWithMood(Table, name="events", schema="mg", partition_by="RANGE (at)"):
    id = integer()
    at = timestamp(tz=True)
    mood = pg_enum(NewMood, schema="mg")

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return Events.config()


async def test_partition_bounds_normalize_with_a_column_of_a_new_type(
    driver: PostgresDriver,
) -> None:
    # B55: the temporary parent copied every column, so one of a type not created yet
    # failed the copy and the bound compared as spelled: "changing the bound" on push.
    await migrate(driver, empty_snapshot("postgresql"), snap([Events]))
    async with driver.acquire() as conn:
        current = await dialect.introspect(conn, SCHEMAS)
        desired = await dialect.normalize_snapshot(conn, snap([EventsWithMood]))
    assert desired["tables"]["mg.events"].get("partitions") == current["tables"]["mg.events"].get(
        "partitions"
    )
    diff(current, desired, compare_expressions=False)  # does not raise


async def test_partition_bounds_normalize_without_the_temp_privilege(
    driver: PostgresDriver,
) -> None:
    # B59: without TEMP every bound compared as spelled, on a schema that had not changed.
    await migrate(driver, empty_snapshot("postgresql"), snap([Events]))
    notemp = "mizzle_mg_notemp"
    async with driver.acquire() as conn:
        [row] = await conn.fetch_dicts("SELECT current_database() AS d", [])
        database = f'"{row["d"]}"'
        current = await dialect.introspect(conn, SCHEMAS)
        await conn.execute(
            f"REVOKE TEMP ON DATABASE {database} FROM PUBLIC; CREATE ROLE {notemp}; "
            f"GRANT USAGE, CREATE ON SCHEMA mg TO {notemp}; SET ROLE {notemp}",
            [],
        )
        try:
            with pytest.raises(DatabaseError, match="permission denied"):
                await conn.execute("CREATE TEMP TABLE t_probe (x int)", [])
            desired = await dialect.normalize_snapshot(conn, snap([Events]))
        finally:
            await conn.execute(
                f"RESET ROLE; GRANT TEMP ON DATABASE {database} TO PUBLIC; "
                f"DROP OWNED BY {notemp}; DROP ROLE {notemp}",
                [],
            )
    assert desired["tables"]["mg.events"].get("partitions") == current["tables"]["mg.events"].get(
        "partitions"
    )


def test_a_changed_bound_names_both_and_hints_at_the_time_zone() -> None:
    # B64: a changed database timezone made a bare date bound look changed, with no hint.
    old = snap([Events])
    new = copy.deepcopy(old)
    new["tables"]["mg.events"]["partitions"] = {
        **new["tables"]["mg.events"].get("partitions", {}),
        "events_2026": "FOR VALUES FROM ('2026-02-01') TO ('2027-01-01')",
    }
    with pytest.raises(MigrationError, match=r"database: .*schema: .*TimeZone"):
        diff(old, new, StrictRenames())


# -- composite types ---------------------------------------------------------------------

caddr = composite(
    "caddr",
    [("street", "varchar(40)"), ("city", "text"), ("zip", "integer")],
    schema="mg",
)


@final
class Homes(Table, name="homes", schema="mg"):
    id = integer().primary_key()
    at = pg_composite("caddr", dict, schema="mg")


async def test_composites(driver: PostgresDriver, tmp_path: Path) -> None:
    v1 = snap([Homes], objects=[caddr])
    assert v1.get("composites") == {
        "mg.caddr": {
            "schema": "mg",
            "name": "caddr",
            "fields": [
                {"name": "street", "type": "character varying(40)"},
                {"name": "city", "type": "text"},
                {"name": "zip", "type": "integer"},
            ],
        }
    }
    assert v1["tables"]["mg.homes"]["columns"]["at"]["type"] == "mg.caddr"

    await migrate(driver, empty_snapshot("postgresql"), v1)
    await execute(
        driver,
        "INSERT INTO mg.homes (id, at) VALUES (1, ROW('1 Main', 'Springfield', 12345))",
    )
    [row] = await execute(driver, "SELECT at, (at).city AS city FROM mg.homes")
    assert row["city"] == "Springfield"
    # asyncpg reads a composite as a Record (field values by name).
    assert cast(Mapping[str, object], row["at"])["zip"] == 12345
    source = await assert_pull_round_trip(driver, tmp_path)
    assert "composite('caddr'" in source
    assert "pg_composite('mg.caddr'" in source

    # A composite drops after the table that uses it.
    steps = await migrate(driver, v1, snap([]))
    assert [s.describe() for s in steps] == ["drop table homes", "drop type caddr"]


# -- S8: a domain outside schema_filter, used by a pulled table -----------------------------


billing_price = domain(
    "mg_price",
    schema="mg3",
    as_="numeric",
    not_null=True,
    checks=["VALUE > 0", "VALUE < 1000000"],
)


@final
class Invoices(Table, name="invoices", schema="mg"):
    id = integer().primary_key()
    price = pg_domain("mg_price", None, schema="mg3")


async def test_domain_outside_schema_filter_round_trips(driver: PostgresDriver) -> None:
    # S8: introspecting only "mg" still pulls the "mg3" domain its column uses, and the
    # pulled module generates no drift (the hand-written CHECK text is normalized).
    v1 = snap([Invoices], objects=[billing_price])
    await migrate(driver, empty_snapshot("postgresql"), v1)

    async with driver.acquire() as conn:
        only_mg = await dialect.introspect(conn, ["mg"])
    assert "mg3.mg_price" in only_mg.get("domains", {})
    assert only_mg["tables"]["mg.invoices"]["columns"]["price"]["type"] == "mg3.mg_price"

    source = render_schema_module(only_mg)
    compile(source, "pulled_s8.py", "exec")
    assert "checks=[" in source  # two CHECKs, not check=..., check=...
    assert "mg3.mg_price" in source

    # The hand-written checks compare equal to what the server prints.
    async with driver.acquire() as conn:
        expected = await dialect.normalize_snapshot(conn, v1)
    remaining = diff(only_mg, expected)
    assert remaining == [], [s.describe() for s in remaining]


async def test_a_sub_partitioned_partition_is_reported(
    driver: PostgresDriver, caplog: pytest.LogCaptureFixture
) -> None:
    # Sub-partitions are not modelled: introspection said nothing, and pull lost them.
    async with driver.acquire() as conn:
        await conn.execute(
            "CREATE SCHEMA mg; "
            "CREATE TABLE mg.sp (id int, region text, at date) PARTITION BY LIST (region); "
            "CREATE TABLE mg.sp_eu PARTITION OF mg.sp FOR VALUES IN ('eu') "
            "PARTITION BY RANGE (at); "
            "CREATE TABLE mg.sp_eu_2026 PARTITION OF mg.sp_eu "
            "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
            [],
        )
    with caplog.at_level("WARNING", logger="mizzle"):
        snapshot = await live(driver)
    assert snapshot["tables"]["mg.sp"].get("partitions", {}).keys() == {"sp_eu"}
    assert any("mg.sp_eu is itself partitioned" in r.getMessage() for r in caplog.records)


async def test_an_identity_added_to_a_column_with_rows_starts_after_them(
    driver: PostgresDriver,
) -> None:
    # C29: the new sequence started at 1, so the next insert hit a duplicate key.
    @final
    class Plain(Table, name="ids", schema="mg"):
        id = integer().primary_key()
        note = text()

    @final
    class Identity(Table, name="ids", schema="mg"):
        id = integer().primary_key().generated_by_default_as_identity()
        note = text()

    v1 = snap([Plain])
    await migrate(driver, empty_snapshot("postgresql"), v1)
    await execute(driver, "INSERT INTO mg.ids (id, note) VALUES (1, 'a'), (2, 'b')")
    await migrate(driver, v1, snap([Identity]))
    [row] = await execute(driver, "INSERT INTO mg.ids (note) VALUES ('c') RETURNING id")
    assert row["id"] == 3


async def test_pull_writes_valid_code_for_awkward_objects(
    driver: PostgresDriver, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # C26: a table or enum named with a leading digit gave `class 2e`, NO INHERIT broke the
    # next migrate, MATCH FULL was dropped silently, and a column on a shared sequence
    # became serial().
    async with driver.acquire() as conn:
        await conn.execute(
            "CREATE SCHEMA mg; CREATE TYPE mg.\"2e\" AS ENUM ('a'); "
            'CREATE TABLE mg."1stuff" (id int PRIMARY KEY, e mg."2e"); '
            "CREATE TABLE mg.chk (a int, CONSTRAINT pos CHECK (a > 0) NO INHERIT); "
            "CREATE TABLE mg.ref (id int PRIMARY KEY); "
            "CREATE TABLE mg.fk (a int, CONSTRAINT f FOREIGN KEY (a) REFERENCES mg.ref "
            "MATCH FULL); CREATE SEQUENCE mg.shared START 100; "
            "CREATE TABLE mg.seq (n int DEFAULT nextval('mg.shared'))",
            [],
        )
    with caplog.at_level("WARNING", logger="mizzle"):
        source = await assert_pull_round_trip(driver, tmp_path)
    assert "class T1stuff(" in source and "class T2e(" in source
    assert "NO INHERIT" not in source
    assert "serial()" not in source
    messages = [r.getMessage() for r in caplog.records]
    assert any("NO INHERIT" in m for m in messages)
    assert any("MATCH FULL" in m for m in messages)


async def test_pull_keeps_index_order_and_collation_and_key_columns(
    driver: PostgresDriver, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # C25: INCLUDE columns were read as keys (a weaker unique rule), and DESC, NULLS LAST
    # and COLLATE were dropped, with no drift reported.
    async with driver.acquire() as conn:
        await conn.execute(
            "CREATE SCHEMA mg; CREATE TABLE mg.ix (code text, n int); "
            "CREATE UNIQUE INDEX ix_u ON mg.ix (code) INCLUDE (n); "
            "CREATE INDEX ix_d ON mg.ix (code DESC NULLS LAST, n NULLS FIRST); "
            'CREATE INDEX ix_c ON mg.ix (code COLLATE "C" text_pattern_ops)',
            [],
        )
    with caplog.at_level("WARNING", logger="mizzle"):
        indexes = (await live(driver))["tables"]["mg.ix"]["indexes"]
        await assert_pull_round_trip(driver, tmp_path)
    assert indexes["ix_u"]["columns"] == ["code"]
    assert indexes["ix_d"].get("expression") == "code DESC NULLS LAST, n NULLS FIRST"
    assert indexes["ix_c"].get("expression") == 'code COLLATE "C" text_pattern_ops'
    assert any("INCLUDE" in r.getMessage() for r in caplog.records)


async def test_odd_literal_defaults_apply_and_show_no_drift(driver: PostgresDriver) -> None:
    # C44: DEFAULT inf read as a column name. C45: interval and bytea defaults were refused.
    import datetime
    from decimal import Decimal

    from mizzle.pg import bytea, double_precision, numeric

    @final
    class Odd(Table, name="odd", schema="mg"):
        id = integer().primary_key()
        hi = double_precision().default(float("inf"))
        lo = double_precision().default(float("-inf"))
        nan = double_precision().default(float("nan"))
        big = numeric().default(Decimal("Infinity"))
        day = interval().default(datetime.timedelta(days=1))
        back = interval().default(datetime.timedelta(days=-1, hours=1, microseconds=500))
        zero = interval().default(datetime.timedelta())
        raw = bytea().default(b"\x00\xff")

    desired = snap([Odd])
    await migrate(driver, empty_snapshot("postgresql"), desired)
    [row] = await execute(driver, "INSERT INTO mg.odd (id) VALUES (1) RETURNING *")
    assert row["hi"] == float("inf") and row["lo"] == float("-inf") and row["nan"] != row["nan"]
    assert row["big"] == Decimal("Infinity")
    assert row["day"] == datetime.timedelta(days=1)
    assert row["back"] == datetime.timedelta(days=-1, hours=1, microseconds=500)
    assert row["zero"] == datetime.timedelta() and row["raw"] == b"\x00\xff"
    async with driver.acquire() as conn:
        current = await dialect.introspect(conn, SCHEMAS)
        expected = await dialect.normalize_snapshot(conn, desired)
    assert diff(current, expected, normalized=True) == []


async def test_json_scalar_and_postgres_type_defaults_apply(driver: PostgresDriver) -> None:
    # Q10: DEFAULT 5 on jsonb. Q12: inet, cidr, range, bit and geometry defaults refused.
    import ipaddress

    from asyncpg import BitString, Box, Point
    from asyncpg import Range as R

    from mizzle.pg import inet, jsonb

    @final
    class Typed(Table, name="typed", schema="mg"):
        id = integer().primary_key()
        n = jsonb[int]().default(5)
        s = jsonb[str]().default("x")
        ip = inet().default(ipaddress.ip_address("10.0.0.1"))
        net = cidr().default(ipaddress.ip_network("10.0.0.0/8"))
        span = int4range().default(R(1, 5))
        spans = int4multirange().default([R(1, 2), R(4, 6)])
        bits = bit(4).default(BitString.from_int(5, length=4))
        vbits = varbit().default(BitString.from_int(3, length=3))
        at = point().default(Point(1, 2.5))
        area = box().default(Box((0, 0), (2, 3)))

    desired = snap([Typed])
    await migrate(driver, empty_snapshot("postgresql"), desired)
    [row] = await execute(driver, "INSERT INTO mg.typed (id) VALUES (1) RETURNING *")
    assert row["n"] == 5 and row["s"] == "x" and row["span"] == R(1, 5)
    assert str(row["ip"]) == "10.0.0.1" and str(row["net"]) == "10.0.0.0/8"
    assert row["bits"] == BitString.from_int(5, length=4) and row["at"] == Point(1, 2.5)


# -- functions called by CHECKs and DEFAULTs, defaults of new types, raw defaults ------------

positive = function(
    "positive",
    schema="mg",
    returns="boolean",
    args="x integer",
    language="sql",
    volatility="immutable",
    body="SELECT x > 0",
)
seven = function("seven", schema="mg", returns="integer", language="sql", body="SELECT 7")


@final
class Calls(Table, name="calls", schema="mg"):
    id = integer().primary_key()
    n = integer().default(sql("mg.seven()"))

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [check("calls_n_positive", sql("mg.positive(n)"))]


@final
class Calls2(Table, name="calls", schema="mg"):
    id = integer().primary_key()
    n = integer()


async def test_functions_used_by_checks_and_defaults(driver: PostgresDriver) -> None:
    # K1: functions were created after the tables and dropped before the constraints.
    v1 = snap([Calls], [positive, seven])
    await migrate(driver, empty_snapshot("postgresql"), v1)
    await migrate(driver, v1, snap([Calls2]))


@final
class K4Users(Table, name="k4users", schema="mg"):
    id = integer().primary_key()


async def test_function_body_naming_a_table_in_upper_case(driver: PostgresDriver) -> None:
    # K4-2: `Users` was not matched with users, so the function came before the table.
    users = function(
        "count_users",
        schema="mg",
        returns="bigint",
        language="sql",
        body="SELECT count(*) FROM MG.K4Users",
    )
    await migrate(driver, empty_snapshot("postgresql"), snap([K4Users], [users]))


valid_code = function(
    "valid_code",
    schema="mg",
    returns="boolean",
    args="c text",
    body="BEGIN RETURN NOT EXISTS (SELECT 1 FROM mg.banned WHERE code = c); END",
)


@final
class Banned(Table, name="banned", schema="mg"):
    code = text().primary_key()


@final
class Coded(Table, name="coded", schema="mg"):
    code = text()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [check("coded_valid", sql("mg.valid_code(code)"))]


@final
class Coded2(Table, name="coded", schema="mg"):
    code = text()


async def test_check_calling_a_plpgsql_function_that_reads_a_table(
    driver: PostgresDriver,
) -> None:
    # K4-3: the function was created after the CHECK calling it and dropped before it.
    v1 = snap([Banned, Coded], [valid_code])
    await migrate(driver, empty_snapshot("postgresql"), v1)
    await migrate(driver, v1, snap([Banned, Coded2]))


async def test_functions_calling_each_other(driver: PostgresDriver) -> None:
    # K4-9: created in name order, a_fn before the z_fn it calls.
    a_fn = function(
        "a_fn", schema="mg", returns="integer", language="sql", body="SELECT mg.z_fn() + 1"
    )
    z_fn = function("z_fn", schema="mg", returns="integer", language="sql", body="SELECT 1")
    # b_fn reads a table: created after it, still after the z_fn it calls.
    b_fn = function(
        "b_fn",
        schema="mg",
        returns="bigint",
        language="sql",
        body="SELECT count(*) + mg.y_fn() FROM mg.k4users",
    )
    y_fn = function(
        "y_fn",
        schema="mg",
        returns="bigint",
        language="sql",
        body="SELECT count(*) FROM mg.k4users",
    )
    await migrate(driver, empty_snapshot("postgresql"), snap([K4Users], [a_fn, z_fn, b_fn, y_fn]))


@final
class K4Numbers(View, name="k4numbers", schema="mg"):
    n = integer()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return sql("SELECT 1 AS n")


async def test_function_reading_a_view(driver: PostgresDriver) -> None:
    # K4-13: the function was created before the view; a SQL-standard body depends on the
    # view, so it is also dropped before it.
    count = function(
        "count_numbers",
        schema="mg",
        returns="bigint",
        language="sql",
        body="RETURN (SELECT count(*) FROM mg.k4numbers)",
    )
    v1 = snap([K4Numbers], [count])
    await migrate(driver, empty_snapshot("postgresql"), v1)
    await migrate(driver, v1, snap([]))


class K4Mood(enum.Enum):
    HAPPY = "happy"


@final
class Waits(Table, name="waits", schema="mg"):
    id = integer().primary_key()
    wait = interval().default(sql("'1 day 5 seconds'::interval"))
    mood = pg_enum(K4Mood, type_name="mg_k4_mood", schema="mg")  # not created yet


async def test_column_of_a_new_type_leaves_other_defaults_normalized(
    driver: PostgresDriver,
) -> None:
    # K4: the temporary table failed on the new type, so `wait` kept its text (drift).
    async with driver.acquire() as conn:
        printed = await dialect.normalize_snapshot(conn, snap([Waits]))
    assert printed["tables"]["mg.waits"]["columns"]["wait"]["default"] == "'1 day 00:00:05'"


@final
class Stamps(Table, name="stamps", schema="mg"):
    id = integer().primary_key()
    at = timestamp().default(sql("now() AT TIME ZONE 'utc'"))


@final
class Stamps2(Table, name="stamps", schema="mg"):
    id = integer().primary_key()
    at = timestamp().default(sql("now() AT TIME ZONE 'utc'"))
    on = timestamp().default(sql("now() AT TIME ZONE 'utc'"))


async def test_raw_operator_default_is_parenthesized(driver: PostgresDriver) -> None:
    # K11: DEFAULT now() AT TIME ZONE 'utc' is a syntax error without parentheses.
    v1 = snap([Stamps])
    await migrate(driver, empty_snapshot("postgresql"), v1)  # CREATE TABLE
    await migrate(driver, v1, snap([Stamps2]))  # ADD COLUMN

"""Table <-> view swaps, operator classes, generated column rebuilds, enums in other schemas,
table moves and identity options: applied, introspected without drift, and pulled back."""

import datetime as dt
import enum
import importlib.util
import re
import sys
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import cast, final, override

import pytest
from asyncpg import BitString, Point, Range

from mizzle import (
    Constraint,
    DatabaseError,
    Expr,
    QueryBuilder,
    Table,
    View,
    check,
    contains,
    eq,
    index,
    primary_key,
    sql,
    unique,
    unique_index,
)
from mizzle.core.schema.column import ColumnBase, Computed
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.steps import Statement, Step
from mizzle.dialects.postgres import PostgresDialect, ddl
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.codegen import render_schema_module
from mizzle.kit.diff import StrictRenames, diff
from mizzle.kit.migrate import apply_statements
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import (
    array,
    bigint,
    bit,
    box,
    bytea,
    char,
    cidr,
    circle,
    custom,
    date,
    daterange,
    int4multirange,
    int4range,
    integer,
    interval,
    jsonb,
    line,
    lseg,
    macaddr,
    macaddr8,
    money,
    nummultirange,
    numrange,
    path,
    pg_enum,
    point,
    polygon,
    smallint,
    text,
    timestamp,
    tsrange,
    tstzmultirange,
    tstzrange,
    varbit,
    varchar,
    xml,
)
from tests.integration.test_generated_columns import server_version

dialect = PostgresDialect()
SCHEMAS = ["mg", "mg2"]
READER, OTHER = "mizzle_mg_reader", "mizzle_mg_other"

RESET = f"""
DROP SCHEMA IF EXISTS mg CASCADE;
DROP SCHEMA IF EXISTS mg2 CASCADE;
DROP SCHEMA IF EXISTS mg3 CASCADE;
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


def snap(tables: Sequence[type[Table]]) -> Snapshot:
    return build_snapshot(tables, dialect)


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
    for k, table in expected["tables"].items():
        assert current["tables"][k]["indexes"] == table["indexes"]


async def assert_pull_round_trip(driver: PostgresDriver, tmp_path: Path) -> str:
    """The pulled module builds the snapshot of the database it was pulled from."""
    current = await live(driver)
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
    pulled = build_snapshot(tables, dialect)
    remaining = diff(current, pulled)
    assert remaining == [], (source, [s.describe() for s in remaining])
    return source


@final
class Holder(Table, name="holder", schema="mg"):  # a table, so the schema is in a snapshot
    id = integer()


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


# -- hand-written CHECK expressions (T10) -------------------------------------------------


@final
class Bookings(Table, name="bookings", schema="mg"):
    status = text().not_null()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        # Written the way a person writes it: no outer parens, no ::text casts.
        return [check("bookings_status_valid", sql("status <> ''"), not_valid=True)]


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
    assert diff(live_left, desired, compare_expressions=True) == []
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


async def test_more_column_types_round_trip(driver: PostgresDriver, tmp_path: Path) -> None:
    await migrate(driver, empty_snapshot("postgresql"), snap([Assorted]))
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
    }
    assert {name: columns[name]["type"] for name in expected} == expected

    await execute(
        driver,
        "INSERT INTO mg.assorted (id, net, mac, mac8, flags, variable, doc, amount, pt, ln, seg, "
        "rect, trace, area, disc) VALUES "
        "(1, '10.0.0.0/8', '08:00:2b:01:02:03', '08:00:2b:01:02:03:04:05', B'1010', B'1010', "
        "'<a/>', '12.34', '(1,2)', '{1,2,3}', '[(1,2),(3,4)]', '(1,2),(3,4)', '[(1,2),(3,4)]', "
        "'((1,2),(3,4),(5,6))', '<(1,2),3>')",
    )
    [row] = await execute(driver, "SELECT net, mac, mac8, flags, variable, doc FROM mg.assorted")
    assert str(row["net"]) == "10.0.0.0/8"
    assert row["mac"] == "08:00:2b:01:02:03"
    assert row["mac8"] == "08:00:2b:01:02:03:04:05"
    assert "1010" in str(row["flags"])
    assert "1010" in str(row["variable"])
    assert row["doc"] == "<a/>"
    await assert_pull_round_trip(driver, tmp_path)


# -- domains -------------------------------------------------------------------------------


# -- partitions --------------------------------------------------------------------------


# -- composite types ---------------------------------------------------------------------


# -- S8: a domain outside schema_filter, used by a pulled table -----------------------------


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


async def test_column_type_change_recreates_the_checks_and_indexes_using_it(
    driver: PostgresDriver,
) -> None:
    # K4-21: PostgreSQL rewrites `c_c >= 0` as `(c_c)::numeric >= (0)::numeric` when c_c
    # becomes bigint, so the next diff dropped and added the check again, forever.
    from mizzle.pg import numeric

    @final
    class Beta(Table, name="beta", schema="mg"):
        id = integer().primary_key()
        c_c = numeric()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [
                check("beta_c_c", sql("c_c >= 0")),
                index("beta_part").on(cls.id).where(sql("c_c > 1")),
                index("beta_expr").on(sql("(c_c + 2)")),
            ]

    @final
    class Beta2(Table, name="beta", schema="mg"):
        id = integer().primary_key()
        c_c = bigint()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [
                check("beta_c_c", sql("c_c >= 0")),
                index("beta_part").on(cls.id).where(sql("c_c > 1")),
                index("beta_expr").on(sql("(c_c + 2)")),
            ]

    v1 = snap([Beta])
    await migrate(driver, empty_snapshot("postgresql"), v1)
    await migrate(driver, v1, snap([Beta2]))


async def test_a_wider_column_keeps_its_checks_and_indexes(driver: PostgresDriver) -> None:
    # K4-21 dropped and added again every check and index on a retyped column, also for
    # varchar(40) -> varchar(64): the expression reads the same, and the added check scans
    # the whole table.
    from mizzle.pg import numeric, varchar

    def gamma(size: int) -> type[Table]:
        @final
        class Gamma(Table, name="gamma", schema="mg"):
            id = integer().primary_key()
            slug = varchar(size)
            price = numeric(size // 2, 2)

            @classmethod
            @override
            def config(cls) -> list[Constraint]:
                return [
                    check("gamma_slug", sql("slug ~ '^[a-z]+$'")),
                    check("gamma_price", sql("price >= 0")),
                    index("gamma_part").on(cls.id).where(sql("price > 1")),
                ]

        return Gamma

    v1, v2 = snap([gamma(40)]), snap([gamma(64)])
    steps = diff(v1, v2)
    assert [type(s).__name__ for s in steps] == ["AlterColumnType", "AlterColumnType"], steps
    await migrate(driver, empty_snapshot("postgresql"), v1)
    await migrate(driver, v1, v2)


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


# -- defaults and DDL literals of every type, as PostgreSQL prints them back ------------------


class Q4Color(enum.StrEnum):
    RED = "red"
    BLUE = "blue"


_UTC5 = dt.timezone(dt.timedelta(hours=5))
_T0 = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
_D1, _D5 = dt.date(2020, 1, 1), dt.date(2020, 1, 5)

type _Check = Callable[[Expr[object]], Expr[bool]] | None
# (column with its default, a CHECK comparing it with a literal); point has no `=`.
_TYPED_DEFAULTS: dict[str, tuple[Callable[[], ColumnBase], _Check]] = {
    "int[]": (lambda: array(integer()).default([1, 2]), lambda v: contains(v, [1])),
    "varchar[]": (
        lambda: array(varchar(20)).default(["a b", 'q"\\', "NULL", "", "{x}"]),
        lambda v: contains(v, ["a b", ""]),
    ),
    "enum[]": (
        lambda: array(pg_enum(Q4Color, type_name="q4_color", schema="mg")).default([Q4Color.RED]),
        lambda v: contains(v, [Q4Color.RED]),
    ),
    "date[]": (lambda: array(date()).default([_D1]), lambda v: contains(v, [_D1])),
    "jsonb": (
        lambda: jsonb[object]().default({"k": "\u00fcn\u00ef", "b": 1, "aa": [1e20, 1.5]}),
        lambda v: contains(v, {"b": 1}),
    ),
    "jsonb list": (
        lambda: jsonb[object]().default([1, 2, {"b": 1, "a": 2}]),
        lambda v: contains(v, [1, 2]),
    ),
    "jsonb[]": (
        lambda: array(jsonb[object]()).default([{"b": 1, "a": "\u00fc"}, [1, "x"]]),
        lambda v: contains(v, [[1, "x"]]),
    ),
    "bytea[]": (lambda: array(bytea()).default([b"\x00a"]), lambda v: contains(v, [b"\x00a"])),
    "int4range": (
        lambda: int4range().default(Range(1, 5, lower_inc=False, upper_inc=True)),
        lambda v: eq(v, Range(2, 6)),
    ),
    "daterange": (
        lambda: daterange().default(Range(_D1, _D5, upper_inc=True)),
        lambda v: eq(v, Range(_D1, _D5, upper_inc=True)),
    ),
    "tstzrange": (
        lambda: tstzrange().default(Range(dt.datetime(2020, 1, 1, tzinfo=_UTC5), None)),
        lambda v: eq(v, Range(dt.datetime(2019, 12, 31, 19, tzinfo=dt.UTC), None)),
    ),
    "tsrange": (
        lambda: tsrange().default(Range(dt.datetime(2020, 1, 1, 0, 0, 0, 500000), None)),
        lambda v: eq(v, Range(dt.datetime(2020, 1, 1, 0, 0, 0, 500000), None)),
    ),
    "int4range[]": (
        lambda: array(int4range()).default([Range(1, 2), Range(3, 5, upper_inc=True)]),
        lambda v: contains(v, [Range(1, 2), Range(3, 6)]),
    ),
    # Q4-14: stored sorted, overlapping and adjacent ranges merged.
    "int4multirange": (
        lambda: int4multirange().default(
            [Range(7, 8, upper_inc=True), Range(1, 3), Range(2, 5), Range(9, 10), Range(4, 4)]
        ),
        # Q4-19: written {[1,3)} in the CHECK as well
        lambda v: cast(
            Expr[bool], cast(object, sql("{} <> {}::int4multirange", v, [Range(2, 3), Range(1, 2)]))
        ),
    ),
    "nummultirange": (
        lambda: nummultirange().default(
            [
                Range(Decimal(1), Decimal(3)),
                Range(Decimal(2), Decimal(5), upper_inc=True),
                Range(Decimal(5), Decimal(6), lower_inc=False),
                Range(Decimal(-1), Decimal(0), upper_inc=True),
                Range(Decimal(7), Decimal(8), lower_inc=False),
                Range(Decimal(8), Decimal(9), lower_inc=False),
            ]
        ),
        lambda v: contains(v, [Range(Decimal(2), Decimal(3)), Range(Decimal(1), Decimal(2))]),
    ),
    "tstzmultirange": (
        lambda: tstzmultirange().default(
            [
                Range(dt.datetime(2020, 1, 2, tzinfo=_UTC5), dt.datetime(2020, 1, 3, tzinfo=_UTC5)),
                Range(_T0, dt.datetime(2020, 1, 1, 19, tzinfo=dt.UTC)),
                Range(None, dt.datetime(2019, 1, 1, tzinfo=dt.UTC)),
            ]
        ),
        None,
    ),
    "timestamptz": (
        lambda: timestamp(tz=True).default(dt.datetime(2020, 1, 1, tzinfo=_UTC5)),
        lambda v: eq(v, dt.datetime(2019, 12, 31, 19, tzinfo=dt.UTC)),
    ),
    "point[]": (lambda: array(point()).default([Point(1, 2.5)]), None),
    "varbit": (
        lambda: varbit().default(BitString.from_int(341, length=10)),
        lambda v: eq(v, BitString.from_int(341, length=10)),
    ),
    "varbit[]": (
        lambda: array(varbit()).default([BitString.from_int(1, length=2)]),
        lambda v: contains(v, [BitString.from_int(1, length=2)]),
    ),
    "timestamptz[]": (
        lambda: array(timestamp(tz=True)).default([_T0, dt.datetime(2020, 1, 1, tzinfo=_UTC5)]),
        lambda v: contains(v, [_T0]),
    ),
    "timestamp[]": (
        lambda: array(timestamp()).default([dt.datetime(2020, 1, 1, 0, 0, 0, 500000)]),
        lambda v: contains(v, [dt.datetime(2020, 1, 1, 0, 0, 0, 500000)]),
    ),
    "custom": (lambda: custom("int4range").default("[2,6)"), lambda v: eq(v, "[2,6)")),
    "interval[]": (
        lambda: array(interval()).default([dt.timedelta(hours=1), dt.timedelta(days=-1, hours=1)]),
        lambda v: contains(v, [dt.timedelta(hours=1)]),
    ),
}


@pytest.mark.parametrize("kind", list(_TYPED_DEFAULTS))
async def test_typed_defaults_and_check_literals_do_not_drift(
    driver: PostgresDriver, kind: str
) -> None:
    # Q4-1 varchar[]/enum[]/date[] literals were text[]; Q4-2 CAST(ARRAY[1, 2] AS jsonb);
    # Q4-4 ranges and Q4-7 jsonb defaults drifted; Q4-11 array elements were Python repr;
    make, condition = _TYPED_DEFAULTS[kind]
    column = make()

    @final
    class Typed(Table, name="q4_typed", schema="mg"):
        id = integer().primary_key()
        v = column

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return (
                []
                if condition is None
                else [check("q4_typed_ck", condition(cast(Expr[object], cast(object, cls.v))))]
            )

    desired = snap([Typed])
    await migrate(driver, empty_snapshot("postgresql"), desired)
    current = await live(driver)
    # Compared as written too: the default is what PostgreSQL prints.
    assert diff(current, desired, compare_expressions=False) == []
    # And so is each literal in the CHECK (Q4-19).
    for name, ck in desired["tables"]["mg.q4_typed"]["checks"].items():
        written = current["tables"]["mg.q4_typed"]["checks"][name]["expression"]
        for literal in re.findall(r"'(?:[^']|'')*'", ck["expression"]):
            assert literal in written
    # In any session TimeZone: the server prints a timestamptz constant in it (Q4-15).
    async with driver.acquire() as conn:
        await conn.execute("SET TimeZone = 'Asia/Dhaka'", [])
        try:
            current = await dialect.introspect(conn, SCHEMAS)
            normalized = await dialect.normalize_snapshot(conn, desired)
        finally:
            await conn.execute("RESET TimeZone", [])
    assert diff(current, desired, compare_expressions=False) == []
    assert diff(current, normalized, compare_expressions=False, normalized=True) == []
    await execute(driver, "INSERT INTO mg.q4_typed (id) VALUES (1)")


async def test_type_named_like_a_keyword_the_server_lacks(
    driver: PostgresDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    # K4-22: mizzle quotes PostgreSQL 18's keywords; on 14-17 json_table is none, so
    # format_type() printed it bare and the column type drifted. Faked here with a word
    # mizzle takes for a keyword and this server does not.
    keywords = ddl._KEYWORDS | {"k4_word"}
    monkeypatch.setattr(ddl, "_KEYWORDS", keywords)

    @final
    class Worded(Table, name="k4_worded", schema="mg"):
        id = integer().primary_key()
        v = pg_enum(Q4Color, type_name="k4_word", schema="mg")
        w = array(pg_enum(Q4Color, type_name="k4_word", schema="mg"))

    desired = snap([Worded])
    assert desired["tables"]["mg.k4_worded"]["columns"]["w"]["type"] == 'mg."k4_word"[]'
    await migrate(driver, empty_snapshot("postgresql"), desired)
    assert diff(await live(driver), desired, compare_expressions=False) == []


def test_requote_type() -> None:
    old = ddl._KEYWORDS - {"json", "json_table"}
    assert ddl.requote_type("json_table", old) == '"json_table"'
    assert ddl.requote_type("json_table.json_table[]", old) == '"json_table"."json_table"[]'
    assert ddl.requote_type("json[]", old) == "json[]"  # the built-in type
    assert ddl.requote_type("mg.json", old) == 'mg."json"'
    assert ddl.requote_type('mg."json_table"', old) == 'mg."json_table"'
    assert ddl.requote_type("timestamp(3) without time zone", old) == (
        "timestamp(3) without time zone"
    )

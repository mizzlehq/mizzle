"""Snapshots, diffs and SQL for grants, overloads, trigger kinds, moves, rebuilt columns,
identity options and long generated names (no database)."""

import copy
import sys
from pathlib import Path
from typing import final, override

import pytest

from mizzle import (
    Constraint,
    QueryBuilder,
    QueryError,
    SchemaError,
    Table,
    View,
    composite,
    domain,
    exclusion,
    extension,
    function,
    grant,
    index,
    make_interval,
    partition,
    sql,
)
from mizzle import trigger as make_trigger
from mizzle.core.errors import MigrationError
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.ddl import _without_defaults
from mizzle.kit.cli import main
from mizzle.kit.config import Config
from mizzle.kit.diff import StrictRenames, diff, no_renames
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import bit, integer, pg_composite, pg_domain, text
from mizzle.studio.ddl import build_steps, statement_rows

dialect = PostgresDialect()


def sqls(old: Snapshot, new: Snapshot) -> list[str]:
    return [s.sql for s in dialect.render(diff(old, new))]


def test_long_generated_names_never_collide_and_are_stable() -> None:
    def table(name: str) -> type[Table]:
        @final
        class T(Table, name=name):
            id = integer().primary_key()
            ref = integer().unique()

        return T

    # The same first 63 bytes: plain truncation gave both tables the same key names.
    a, b = "t_" + "x" * 70 + "_a", "t_" + "x" * 70 + "_b"
    snap = build_snapshot([table(a), table(b)], dialect)
    names = [
        n
        for t in snap["tables"].values()
        for n in [*t["uniques"], *(pk["name"] for pk in [t["primary_key"]] if pk)]
    ]
    assert len(set(names)) == 4 and all(len(n) == 63 for n in names), names
    assert all(n.endswith(("_key", "_pkey")) for n in names)
    again = build_snapshot([table(a), table(b)], dialect)
    assert diff(snap, again) == []  # stable


def test_grants() -> None:
    @final
    class Docs(Table, name="docs"):
        id = integer()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [
                grant(["select", "insert"], to=["reader", "public"]),
                grant("all", to=["admin"]),
            ]

    snap = build_snapshot([Docs], dialect)
    assert snap["tables"]["public.docs"].get("grants") == {
        "reader": ["insert", "select"],
        "public": ["insert", "select"],
        "admin": ["all"],
    }
    assert sqls(empty_snapshot("postgresql"), snap)[1:] == [
        'GRANT ALL ON "docs" TO "admin"',
        'GRANT INSERT, SELECT ON "docs" TO PUBLIC',
        'GRANT INSERT, SELECT ON "docs" TO "reader"',
    ]
    fewer = copy.deepcopy(snap)
    fewer["tables"]["public.docs"]["grants"] = {"reader": ["select"]}
    assert sqls(snap, fewer) == [
        'REVOKE ALL ON "docs" FROM "admin"',
        'REVOKE INSERT, SELECT ON "docs" FROM PUBLIC',
        'REVOKE INSERT ON "docs" FROM "reader"',
    ]
    # Against a live database (push), roles the schema does not name are left alone.
    assert diff(snap, fewer, compare_expressions=False) != []
    assert all(s.role == "reader" for s in diff(snap, fewer, compare_expressions=False))  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(SchemaError, match="unknown privileges"):
        grant(["read"], to=["x"])  # pyright: ignore[reportArgumentType]


def test_function_overloads_and_options() -> None:
    one = function("f", returns="integer", language="sql", args="a integer", body="RETURN a")
    two = function(
        "f",
        returns="integer",
        language="sql",
        args="a integer, b integer DEFAULT 1",
        body="SELECT a + b",
        strict=True,
        leakproof=True,
        parallel="safe",
        cost=3,
        settings={"search_path": "public, pg_temp"},
    )

    @final
    class T(Table, name="t"):
        id = integer()

    snap = build_snapshot([T], dialect, objects=[one, two])
    assert list(snap.get("functions", {})) == [
        "public.f(a integer)",
        "public.f(a integer, b integer DEFAULT 1)",
    ]
    statements = sqls(empty_snapshot("postgresql"), snap)
    assert 'FUNCTION "f"(a integer) RETURNS integer LANGUAGE sql\nRETURN a' in statements[1]
    assert (
        "LANGUAGE sql STRICT LEAKPROOF PARALLEL SAFE COST 3 SET search_path = public, pg_temp "
        "AS $mizzle$SELECT a + b$mizzle$"
    ) in statements[2]
    only_two = build_snapshot([T], dialect, objects=[two])
    assert sqls(snap, only_two) == ['DROP FUNCTION "f"(a integer)']
    assert _without_defaults("a integer, b text DEFAULT 'x, y', c int[] DEFAULT '{1,2}'") == (
        "a integer, b text, c int[]"
    )
    # Snapshots written before overloads were keyed "schema.name": nothing changes.
    old = copy.deepcopy(only_two)
    old["functions"] = {"public.f": next(iter(only_two.get("functions", {}).values()))}
    assert diff(old, only_two) == []


@final
class Items(Table, name="items"):
    id = integer()
    name = text()
    price = integer()


@final
class Names(View, name="names"):
    id = integer()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return db.select(Items.id).from_(Items)


fn = function("t", returns="trigger", body="BEGIN RETURN NEW; END")


def test_trigger_kinds() -> None:
    triggers = [
        make_trigger(
            "upd", on=Items, execute=fn, events=["update"], update_of=[Items.price, Items.name]
        ),
        make_trigger(
            "log",
            on=Items,
            execute=fn,
            timing="after",
            events=["insert"],
            for_each="statement",
            new_table="added",
        ),
        make_trigger("ins", on=Names, execute=fn, timing="instead of", events=["insert"]),
    ]
    snap = build_snapshot([Items, Names], dialect, objects=triggers)
    statements = sqls(empty_snapshot("postgresql"), snap)
    assert 'BEFORE UPDATE OF "name", "price" ON "items" FOR EACH ROW' in "\n".join(statements)
    assert 'AFTER INSERT ON "items" REFERENCING NEW TABLE AS "added" FOR EACH STATEMENT' in (
        "\n".join(statements)
    )
    assert 'INSTEAD OF INSERT ON "names" FOR EACH ROW' in "\n".join(statements)
    with pytest.raises(SchemaError, match="INSTEAD OF"):
        build_snapshot(
            [Items], dialect, objects=[make_trigger("x", on=Items, execute=fn, timing="instead of")]
        )
    with pytest.raises(SchemaError, match="update_of"):
        make_trigger("x", on=Items, execute=fn, events=["insert"], update_of=[Items.price])


def test_table_moves_use_the_rename_resolver() -> None:
    @final
    class A(Table, name="a", schema="one"):
        id = integer()

    @final
    class B(Table, name="a", schema="two"):
        id = integer()

    old, new = build_snapshot([A], dialect), build_snapshot([B], dialect)
    assert [s.describe() for s in diff(old, new, no_renames)][1:] == [
        "create table a",
        "drop table a",
    ]
    with pytest.raises(MigrationError, match=r"--rename one\.a=two\.a"):
        diff(old, new, StrictRenames())
    moved = diff(old, new, StrictRenames({"one.a": "two.a"}))
    assert [s.sql for s in dialect.render(moved)] == [
        'CREATE SCHEMA IF NOT EXISTS "two"',
        'ALTER TABLE "one"."a" SET SCHEMA "two"',
    ]


def test_plain_column_made_generated_is_dropped_and_added() -> None:
    def calc(generated: bool) -> type[Table]:
        @final
        class Calc(Table, name="calc"):
            a = integer()
            b = integer().generated_always_as(sql("a + 1")) if generated else integer()

            @classmethod
            @override
            def config(cls) -> list[Constraint]:
                return [index("calc_b").on(cls.b)]

        return Calc

    old, new = build_snapshot([calc(False)], dialect), build_snapshot([calc(True)], dialect)
    steps = diff(old, new)
    assert [s.describe() for s in steps] == [
        "drop index calc_b",
        "drop column calc.b",
        "add column calc.b",
        "create index calc_b",
    ]
    assert steps[1].data_loss


def test_rebuilt_column_drops_a_renamed_foreign_key_to_it() -> None:
    def tables(after: bool) -> list[type[Table]]:
        @final
        class Gamma(Table, name="gamma"):
            code = (integer().generated_always_as(sql("2")) if after else integer()).unique()

        @final
        class Beta(Table, name="beta"):
            if after:
                new_ref = integer().references(lambda: Gamma.code)
            else:
                old_ref = integer().references(lambda: Gamma.code)

        return [Gamma, Beta]

    old, new = build_snapshot(tables(False), dialect), build_snapshot(tables(True), dialect)
    steps = diff(old, new, StrictRenames({"beta.old_ref": "new_ref"}))
    # The foreign key (renamed with its column) must go before gamma.code's unique key.
    assert [s.describe() for s in steps] == [
        "rename column beta.old_ref to new_ref",
        "rename constraint beta_old_ref_fkey to beta_new_ref_fkey",
        "drop foreign key beta_new_ref_fkey",
        "drop unique gamma_code_key",
        "drop column gamma.code",
        "add column gamma.code",
        "add unique gamma_code_key",
        "add foreign key beta_new_ref_fkey",
    ]


def test_identity_options() -> None:
    @final
    class T(Table, name="t"):
        id = integer().generated_always_as_identity(min_value=10, cache=5, cycle=True)
        down = integer().generated_by_default_as_identity(increment=-1, max_value=-10)

    snap = build_snapshot([T], dialect)
    columns = snap["tables"]["public.t"]["columns"]
    assert columns["id"].get("identity") == {
        "always": True,
        "start": 10,  # the database starts at MINVALUE
        "increment": 1,
        "min_value": 10,
        "cache": 5,
        "cycle": True,
    }
    create = sqls(empty_snapshot("postgresql"), snap)[0]
    assert '"id" integer GENERATED ALWAYS AS IDENTITY (MINVALUE 10 CACHE 5 CYCLE)' in create
    assert '"down" integer GENERATED BY DEFAULT AS IDENTITY (INCREMENT BY -1 MAXVALUE -10)' in (
        create
    )

    # Same values as the defaults are not recorded.
    @final
    class U(Table, name="t"):
        id = integer().generated_always_as_identity(min_value=1, max_value=2**31 - 1, cache=1)
        down = integer().generated_by_default_as_identity(increment=-1, max_value=-10)

    default_id = build_snapshot([U], dialect)["tables"]["public.t"]["columns"]["id"]
    assert default_id.get("identity") == {"always": True, "start": 1, "increment": 1}


def test_renamed_column_used_by_a_trigger_changes_type() -> None:
    """Found by fuzzing: the old snapshot names the column as before the rename, and the
    trigger (UPDATE OF it) must still be dropped before its type changes."""

    def items(name: str, sql_type: str) -> type[Table]:
        column = integer() if sql_type == "integer" else text()
        return type("I", (Table,), {name: column, "__module__": __name__}, name="items")

    old_t, new_t = items("price", "integer"), items("cost", "text")
    old = build_snapshot(
        [old_t],
        dialect,
        objects=[
            make_trigger(
                "t",
                on=old_t,
                execute=fn,
                events=["update"],
                update_of=[old_t.__table__.columns["price"]],
            )
        ],
    )
    new = build_snapshot(
        [new_t],
        dialect,
        objects=[
            make_trigger(
                "t",
                on=new_t,
                execute=fn,
                events=["update"],
                update_of=[new_t.__table__.columns["cost"]],
            )
        ],
    )
    steps = [s.describe() for s in diff(old, new, StrictRenames({"items.price": "cost"}))]
    assert steps.index("drop trigger t on items") < steps.index(
        "change type of items.cost from integer to text"
    )


def test_table_dropped_before_one_reusing_its_index_names() -> None:
    """Found by fuzzing: a rename without the resolver (--no-renames) creates the new table
    with the old one's index name, so the old table must be gone first."""

    def table(name: str) -> type[Table]:
        def config(cls: type[Table]) -> list[Constraint]:
            return [index("t_ix").on(cls.__table__.columns["id"])]

        attrs = {"id": integer(), "config": classmethod(config), "__module__": __name__}
        return type("T", (Table,), attrs, name=name)

    old, new = build_snapshot([table("a")], dialect), build_snapshot([table("b")], dialect)
    assert [s.describe() for s in diff(old, new)] == [
        "drop table a",
        "create table b",
        "create index t_ix",
    ]


@pytest.mark.parametrize(
    ("old", "new", "safe"),
    [
        ("character varying(40)", "character varying(64)", True),
        ("character varying(40)", "text", True),
        ("character varying(40)", "character varying", True),
        ("character varying", "text", True),
        ("numeric(10,2)", "numeric(12,2)", True),
        ("numeric(10,2)", "numeric", True),
        ("smallint", "integer", True),
        ("integer", "bigint", True),
        ("real", "double precision", True),
        ("text[]", "text[]", True),
        ("character varying(10)[]", "text[]", True),
        ("character(4)", "character(8)", True),
        ("character(4)", "text", True),
        ("character", "character(8)", True),
        ("character(8)", "character(4)", False),
        ("character(4)", "character varying(8)", False),
        ("character varying(64)", "character varying(40)", False),
        ("text", "character varying(40)", False),
        ("numeric(10,2)", "numeric(12,3)", False),
        ("numeric", "numeric(12,2)", False),
        ("bigint", "integer", False),
        ("double precision", "real", False),
        ("integer", "text", True),
        ("numeric(10,2)", "text", True),
        ("real", "text", False),  # printed with the server's extra_float_digits
        ("timestamp with time zone", "text", False),  # printed in the session time zone
    ],
)
def test_only_lossy_type_changes_need_allow_data_loss(old: str, new: str, safe: bool) -> None:
    from mizzle.core.steps import AlterColumnType

    assert AlterColumnType("public", "t", "c", old, new).data_loss is not safe


def test_sql_keyword_defaults_compare_as_printed() -> None:
    from mizzle.dialects.postgres.ddl import normalize_default

    for written, printed in [
        ("current_user", "CURRENT_USER"),
        ("current_timestamp(3)", "CURRENT_TIMESTAMP(3)"),
        ("localtime", "LOCALTIME"),
        ("session_user", "SESSION_USER"),
        ("CURRENT_DATE", "CURRENT_DATE"),
    ]:
        assert normalize_default(written) == normalize_default(printed) == printed
    assert normalize_default("current_setting('x')") == "current_setting('x')"


def test_keyword_default_in_an_older_snapshot_is_not_a_change() -> None:
    """Snapshots written before keyword defaults were normalized hold ``current_user`` as
    declared; the schema now gives ``CURRENT_USER``. That is no migration."""

    @final
    class AuditLog(Table, name="audit_log"):
        actor = text().default(sql("current_user"))
        at = text().default(sql("current_timestamp(3)"))

    new = build_snapshot([AuditLog], dialect)
    columns = new["tables"]["public.audit_log"]["columns"]
    assert columns["actor"]["default"] == "CURRENT_USER"
    old = copy.deepcopy(new)
    old_columns = old["tables"]["public.audit_log"]["columns"]
    old_columns["actor"]["default"] = "current_user"  # as 0.2.0 before the fix wrote it
    old_columns["at"]["default"] = "current_timestamp(3)"
    assert diff(old, new) == []
    assert diff(new, old) == []
    old_columns["actor"]["default"] = "session_user"  # a real change still is one
    assert [s.describe() for s in diff(old, new)] == ["change default of audit_log.actor"]


# -- domains -------------------------------------------------------------------------------


def test_domains_render_create_drop_and_reject_changes() -> None:
    email = domain(
        "email",
        as_="text",
        not_null=True,
        default="'x'::text",
        check="VALUE ~ '@'",
        collation="C",
    )

    @final
    class Users(Table, name="users"):
        id = integer().primary_key()
        email = pg_domain("email", str)

    v1 = build_snapshot([Users], dialect, objects=[email])
    assert v1.get("domains") == {
        "public.email": {
            "schema": "public",
            "name": "email",
            "base_type": "text",
            "not_null": True,
            "default": "'x'",
            "checks": ["VALUE ~ '@'"],
            "collation": "C",
        }
    }
    assert v1["tables"]["public.users"]["columns"]["email"]["type"] == "email"

    statements = dialect.render(diff(empty_snapshot("postgresql"), v1))
    assert statements[0].sql == (
        "CREATE DOMAIN \"email\" AS text COLLATE \"C\" DEFAULT 'x' NOT NULL CHECK (VALUE ~ '@')"
    )
    assert diff(v1, v1) == []

    steps = diff(v1, build_snapshot([], dialect))
    assert [s.describe() for s in steps] == ["drop table users", "drop domain email"]

    # A definition change is not expressible: it needs a hand-written migration.
    changed = domain("email", as_="integer")
    v2 = build_snapshot([], dialect, objects=[changed])
    with pytest.raises(MigrationError, match="domain email"):
        diff(v1, v2)

    # push (compare_expressions=False) tolerates reformatted CHECK text, but not a change.
    retexted = domain(
        "email",
        as_="text",
        not_null=True,
        default="'x'::text",
        check="VALUE <> ''",
        collation="C",
    )
    v3 = build_snapshot([Users], dialect, objects=[retexted])
    assert diff(v1, v3, compare_expressions=False) == []
    with pytest.raises(MigrationError, match="domain email"):
        diff(v1, v3)


def test_domain_in_another_schema_qualifies_the_column() -> None:
    @final
    class T(Table, name="t", schema="app"):
        d = pg_domain("email", str, schema="app")

    snap = build_snapshot([T], dialect, objects=[domain("email", as_="text", schema="app")])
    assert set(snap.get("domains", {})) == {"app.email"}
    assert snap["tables"]["app.t"]["columns"]["d"]["type"] == "app.email"


# -- partitions ----------------------------------------------------------------------------


def _measurements(key: str, parts: list[tuple[str, str]]) -> type[Table]:
    @final
    class M(Table, name="measurements", partition_by=key):
        id = integer()
        logged_at = text()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [partition(n, for_values=b) for n, b in parts]

    return M


P2026 = ("measurements_2026", "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')")
P_DEF = ("measurements_def", "DEFAULT")


def test_partitions_render_create_and_drop() -> None:
    v1 = build_snapshot([_measurements("RANGE (logged_at)", [P2026, P_DEF])], dialect)
    table = v1["tables"]["public.measurements"]
    assert table.get("partition_by") == "RANGE (logged_at)"
    assert table.get("partitions") == {P2026[0]: P2026[1], P_DEF[0]: P_DEF[1]}

    sql = sqls(empty_snapshot("postgresql"), v1)
    assert sql[0] == (
        'CREATE TABLE "measurements" (\n    "id" integer,\n    "logged_at" text\n) '
        "PARTITION BY RANGE (logged_at)"
    )
    assert sql[1] == (
        'CREATE TABLE "measurements_2026" PARTITION OF "measurements" '
        "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
    )
    assert sql[2] == 'CREATE TABLE "measurements_def" PARTITION OF "measurements" DEFAULT'
    assert diff(v1, v1) == []

    # Removing the table takes its partitions along: no separate drop.
    steps = diff(v1, build_snapshot([], dialect))
    assert [s.describe() for s in steps] == ["drop table measurements"]

    # A partition added and one dropped on a table that stays.
    p2027 = ("measurements_2027", "FOR VALUES FROM ('2027-01-01') TO ('2028-01-01')")
    v2 = build_snapshot([_measurements("RANGE (logged_at)", [P2026, p2027])], dialect)
    steps = diff(v1, v2)
    assert [s.describe() for s in steps] == [
        "create partition measurements_2027 of measurements",
        "drop partition measurements_def",
    ]
    assert dialect.render(steps)[0].sql == (
        'CREATE TABLE "measurements_2027" PARTITION OF "measurements" '
        "FOR VALUES FROM ('2027-01-01') TO ('2028-01-01')"
    )
    assert dialect.render(steps)[1].sql == 'DROP TABLE "measurements_def"'

    # A moved bound and a changed key are not expressible in place.
    moved = ("measurements_2026", "FOR VALUES FROM ('2020-01-01') TO ('2021-01-01')")
    with pytest.raises(MigrationError, match="bound of partition"):
        diff(v1, build_snapshot([_measurements("RANGE (logged_at)", [moved, P_DEF])], dialect))
    with pytest.raises(MigrationError, match="partition key"):
        diff(v1, build_snapshot([_measurements("RANGE (id)", [P2026, P_DEF])], dialect))


def test_partition_needs_partition_by() -> None:
    @final
    class Bad(Table, name="bad"):
        id = integer()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [partition("bad_1", for_values="DEFAULT")]

    with pytest.raises(SchemaError, match="partition_by"):
        build_snapshot([Bad], dialect)


# -- composite types -----------------------------------------------------------------------


def test_composites_render_create_drop_and_reject_changes() -> None:
    caddr = composite("caddr", [("street", "varchar(40)"), ("city", "text"), ("zip", "integer")])

    @final
    class Users(Table, name="users"):
        id = integer().primary_key()
        home = pg_composite("caddr", dict)

    v1 = build_snapshot([Users], dialect, objects=[caddr])
    assert v1.get("composites") == {
        "public.caddr": {
            "schema": "public",
            "name": "caddr",
            "fields": [
                {"name": "street", "type": "character varying(40)"},
                {"name": "city", "type": "text"},
                {"name": "zip", "type": "integer"},
            ],
        }
    }
    assert v1["tables"]["public.users"]["columns"]["home"]["type"] == "caddr"

    statements = dialect.render(diff(empty_snapshot("postgresql"), v1))
    assert statements[0].sql == (
        'CREATE TYPE "caddr" AS ("street" character varying(40), "city" text, "zip" integer)'
    )
    assert diff(v1, v1) == []

    # A composite is dropped after the table that uses it.
    steps = diff(v1, build_snapshot([], dialect))
    assert [s.describe() for s in steps] == ["drop table users", "drop type caddr"]

    # Changing its fields needs a custom migration.
    changed = composite("caddr", [("street", "varchar(40)"), ("city", "text")])
    v2 = build_snapshot([Users], dialect, objects=[changed])
    with pytest.raises(MigrationError, match="composite type caddr"):
        diff(v1, v2)


def test_pg_composite_decodes_and_accepts_tuples() -> None:
    column = pg_composite("caddr", dict, name="home")
    assert column.sql_type == "caddr"
    assert column.decode({"city": "x"}) == {"city": "x"}
    column.validate(("street", "city", 1))
    with pytest.raises(QueryError, match="expects tuple"):
        column.validate("nope")
    assert pg_composite("addr", schema="app").sql_type == "app.addr"


# -- studio DDL (allow-listed) -------------------------------------------------------------


def test_studio_ddl_renders_only_allow_listed_operations() -> None:
    steps = build_steps(
        dialect,
        {
            "operation": "create_table",
            "schema": "public",
            "name": "notes",
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "title", "type": "character varying(80)", "not_null": True},
                {"name": "created_at", "type": "timestamp with time zone", "default": "now()"},
            ],
        },
    )
    assert statement_rows(dialect, steps) == [
        {
            "sql": (
                'CREATE TABLE "notes" (\n'
                '    "id" integer,\n'
                '    "title" character varying(80) NOT NULL,\n'
                '    "created_at" timestamp with time zone DEFAULT now(),\n'
                '    CONSTRAINT "notes_pkey" PRIMARY KEY ("id")\n'
                ")"
            ),
            "description": "create table notes",
            "data_loss": False,
        }
    ]

    for bad in (  # only these five operations, and only safe shapes
        {"operation": "drop_database", "name": "x"},
        {"operation": "create_table", "name": "x", "columns": []},
        {
            "operation": "create_table",
            "name": "x",
            "columns": [{"name": "c", "type": "text; drop"}],
        },
        {
            "operation": "create_table",
            "name": "x",
            "columns": [{"name": "c", "type": "text", "default": "0; drop table users"}],
        },
        {"operation": "create_table", "name": "x", "columns": [{"name": "c; --", "type": "text"}]},
        {
            "operation": "create_table",
            "name": "x",
            "columns": [{"name": "c", "type": "int references users(id)"}],
        },
    ):
        with pytest.raises(SchemaError):
            build_steps(dialect, bad)


def test_studio_ddl_changes_an_existing_table() -> None:
    @final
    class Notes(Table, name="notes"):
        id = integer().primary_key()
        title = text()

    current = build_snapshot([Notes], dialect)["tables"]["public.notes"]
    assert current is not None

    add = build_steps(
        dialect,
        {
            "operation": "add_column",
            "schema": "public",
            "table": "notes",
            "name": "body",
            "column": {"name": "body", "type": "text", "not_null": True},
        },
        current,
    )
    assert (
        statement_rows(dialect, add)[0]["sql"]
        == 'ALTER TABLE "notes" ADD COLUMN "body" text NOT NULL'
    )

    altered = build_steps(
        dialect,
        {
            "operation": "alter_column",
            "schema": "public",
            "table": "notes",
            "name": "title",
            "type": "character varying(50)",
            "default": "'x'::text",
        },
        current,
    )
    assert [s.describe() for s in altered] == [
        "change type of notes.title from text to character varying(50)",
        "change default of notes.title",
    ]
    assert statement_rows(dialect, altered)[1]["sql"] == (
        'ALTER TABLE "notes" ALTER COLUMN "title" SET DEFAULT \'x\'::text'
    )

    drop = build_steps(
        dialect,
        {"operation": "drop_column", "schema": "public", "table": "notes", "name": "title"},
        current,
    )
    assert [s.describe() for s in drop] == ["drop column notes.title"]

    with pytest.raises(SchemaError, match="nothing to change"):
        build_steps(
            dialect,
            {
                "operation": "alter_column",
                "schema": "public",
                "table": "notes",
                "name": "title",
                "type": current["columns"]["title"]["type"],
            },
            current,
        )
    with pytest.raises(SchemaError, match="does not exist"):
        build_steps(
            dialect,
            {"operation": "drop_column", "schema": "public", "table": "notes", "name": "nope"},
            current,
        )


# -- ISSUES.md fixes: injection, drift and ordering ---------------------------------------


def test_storage_options_cannot_break_out_of_the_with_clause() -> None:
    @final
    class Widget(Table, name="widget", storage={"fillfactor": "70); DROP TABLE victim; --"}):
        id = integer().primary_key()

    sql = sqls(empty_snapshot("postgresql"), build_snapshot([Widget], dialect))[0]
    assert "WITH (fillfactor='70); DROP TABLE victim; --')" in sql

    @final
    class Bad(Table, name="bad", storage={"fillfactor; DROP TABLE victim; --": 70}):
        id = integer().primary_key()

    with pytest.raises(SchemaError, match="storage parameter"):
        sqls(empty_snapshot("postgresql"), build_snapshot([Bad], dialect))


def test_exclusion_operators_cannot_break_out_of_the_constraint() -> None:
    @final
    class Room(Table, name="room"):
        slot = integer().not_null()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [exclusion((cls.slot, "=); DROP TABLE victim; --"), name="bad")]

    with pytest.raises(SchemaError, match="exclusion operator"):
        sqls(empty_snapshot("postgresql"), build_snapshot([Room], dialect))


@pytest.mark.parametrize("operator", ["--", "--x", "/*", "*/", "&&/*"])
def test_exclusion_operators_reject_comment_starts(operator: str) -> None:
    # B24: these match the operator-character pattern but become comments in the SQL.
    @final
    class Note(Table, name="note"):
        slot = integer().not_null()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [exclusion((cls.slot, operator), name="bad")]

    with pytest.raises(SchemaError, match="exclusion operator"):
        sqls(empty_snapshot("postgresql"), build_snapshot([Note], dialect))


def test_make_interval_rejects_values_that_are_not_numbers() -> None:
    assert make_interval(days=7) is not None
    with pytest.raises(QueryError, match="must be an int"):
        make_interval(days="7")  # pyright: ignore[reportArgumentType]
    with pytest.raises(QueryError, match="must be a number"):
        make_interval(secs="0.5")  # pyright: ignore[reportArgumentType]


def test_initially_deferred_implies_deferrable() -> None:
    @final
    class Order(Table, name="order"):
        id = integer().primary_key(initially_deferred=True)
        code = text().unique(initially_deferred=True)

    table = build_snapshot([Order], dialect)["tables"]["public.order"]
    pk = table["primary_key"]
    assert pk is not None and pk.get("deferrable")
    assert table["uniques"]["order_code_key"].get("deferrable")


def test_changing_a_collated_column_keeps_the_collation() -> None:
    @final
    class Item(Table, name="item"):
        code = text().collate("C")

    before = build_snapshot([Item], dialect)
    after = copy.deepcopy(before)
    after["tables"]["public.item"]["columns"]["code"]["type"] = "character varying(20)"
    statements = dialect.render(diff(before, after))
    assert statements[0].sql == (
        'ALTER TABLE "item" ALTER COLUMN "code" SET DATA TYPE character varying(20) '
        'COLLATE "C" USING "code"::character varying(20)'
    )


def test_composites_are_created_after_the_composites_they_use() -> None:
    inner = composite("z_inner", [("a", "integer")])
    outer = composite("a_outer", [("inner", "z_inner")])
    snap = build_snapshot([], dialect, objects=[outer, inner])  # declared out of order
    creates = [s for s in sqls(empty_snapshot("postgresql"), snap) if s.startswith("CREATE TYPE")]
    assert creates[0].startswith('CREATE TYPE "z_inner"')
    assert creates[1].startswith('CREATE TYPE "a_outer"')


def test_storage_option_keys_and_bools_are_normalized() -> None:
    @final
    class Cache(Table, name="cache", storage={"FillFactor": 70, "autovacuum_enabled": False}):
        id = integer().primary_key()

    options = build_snapshot([Cache], dialect)["tables"]["public.cache"].get("options")
    assert options == {"fillfactor": "70", "autovacuum_enabled": "false"}


def test_bit_without_a_length_is_bit_one() -> None:
    assert bit().sql_type == "bit(1)"
    assert bit(4).sql_type == "bit(4)"


def test_a_partitioned_table_cannot_be_unlogged() -> None:
    @final
    class P(Table, name="p", partition_by="RANGE (at)", unlogged=True):
        at = text()

    with pytest.raises(SchemaError, match="cannot be unlogged"):
        build_snapshot([P], dialect)


def test_schema_objects_in_a_list_or_tuple_are_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D4: `exts = [extension("hstore"), ...]` (as the docs show) must not be ignored.
    module = "d4_schema_objects"
    (tmp_path / f"{module}.py").write_text(
        "from mizzle import extension\n"
        'exts = [extension("hstore"), extension("citext")]\n'
        'more = (extension("ltree"),)\n'
        'single = extension("hstore")\n'
        'unrelated = [1, "x"]\n'
    )
    monkeypatch.syspath_prepend(tmp_path)
    monkeypatch.delitem(sys.modules, module, raising=False)
    config = Config(root=tmp_path, schema=[module], out=tmp_path / "migrations")
    assert config.load_objects() == [extension("hstore"), extension("citext"), extension("ltree")]


def test_typegen_help_mentions_row_types(capsys: pytest.CaptureFixture[str]) -> None:
    # D2: typegen also writes the <Table>Row TypedDicts.
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "write row/insert/update TypedDicts" in capsys.readouterr().out


def test_a_squash_is_adopted_only_for_a_whole_history() -> None:
    # B57: only the last replaced name was checked, so a database with a gap was adopted.
    from mizzle.kit.migrate import history_complete

    names = ["0001_a", "0002_b", "0003_c"]
    assert history_complete(names, dict.fromkeys(names, ""))
    assert not history_complete(names, dict.fromkeys(["0001_a", "0003_c"], ""))
    # An earlier squash stands for the names before it: either history counts.
    twice = ["0001_a", "0002_b", "0001_squash", "0003_d"]
    assert history_complete(twice, dict.fromkeys(["0001_squash", "0003_d"], ""))
    assert history_complete(twice, dict.fromkeys(["0001_a", "0002_b", "0003_d"], ""))
    assert not history_complete(twice, dict.fromkeys(["0001_a", "0003_d"], ""))


def test_a_journal_entry_cannot_name_a_folder_outside_migrations(tmp_path: Path) -> None:
    # squash and drop delete root / name: "../victim" would reach outside migrations/.
    import json

    from mizzle.core.errors import MigrationError
    from mizzle.kit.journal import MigrationsDir

    (tmp_path / "victim").mkdir()
    meta = tmp_path / "migrations" / "meta"
    meta.mkdir(parents=True)
    entry = {"index": 1, "name": "../victim", "hash": "x", "created_at": "2026-01-01"}
    journal = {"version": 1, "dialect": "postgresql", "entries": [entry]}
    (meta / "journal.json").write_text(json.dumps(journal))
    with pytest.raises(MigrationError, match="malformed journal entry"):
        MigrationsDir(tmp_path / "migrations", "postgresql").clear()
    assert (tmp_path / "victim").is_dir()


def test_an_extension_declared_twice_with_different_options_is_refused() -> None:
    # Two declarations (top level and in a list) silently kept the last one.
    from mizzle.kit.snapshot import build_snapshot

    same = [extension("hstore"), extension("hstore")]
    assert "hstore" in build_snapshot([], dialect, objects=same).get("extensions", {})
    clash = [extension("hstore"), extension("hstore", schema="ext")]
    with pytest.raises(SchemaError, match=r"hstore.*declared twice"):
        build_snapshot([], dialect, objects=clash)

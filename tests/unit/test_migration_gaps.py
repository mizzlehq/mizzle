"""Snapshots, diffs and SQL for grants, overloads, trigger kinds, moves, rebuilt columns,
identity options and long generated names (no database)."""

import copy
from typing import final, override

import pytest

from mizzle import Constraint, QueryBuilder, SchemaError, Table, View, function, grant, index, sql
from mizzle import trigger as make_trigger
from mizzle.core.errors import MigrationError
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.ddl import _without_defaults
from mizzle.kit.diff import StrictRenames, diff, no_renames
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import integer, text

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

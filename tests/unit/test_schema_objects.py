"""Extensions, views, row-level security and triggers: snapshot, diff and SQL."""

import importlib.util
import sys
from pathlib import Path
from typing import final, override

import pytest

from mizzle import (
    Constraint,
    QueryBuilder,
    SchemaError,
    Table,
    View,
    count,
    eq,
    extension,
    function,
    policy,
    rls,
    sql,
    trigger,
    unique_index,
)
from mizzle.core.schema.objects import SchemaObject
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.dialects.postgres import PostgresDialect
from mizzle.kit.codegen import render_schema_module, render_types_module
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import bigint, boolean, integer, serial, text, timestamp, vector

dialect = PostgresDialect()


@final
class Users(Table, name="users"):
    id = serial().primary_key()
    email = text().not_null()
    active = boolean().not_null().default(True)
    owner = text()
    updated_at = timestamp(tz=True)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            rls(),
            policy("own", for_="select", to=["anon", "app"], using=sql("owner = current_user")),
            policy("add", for_="insert", with_check=sql("owner = current_user")),
        ]


@final
class ActiveUsers(View, name="active_users"):
    id = integer().not_null()
    email = text().not_null()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return db.select(Users.id, Users.email).from_(Users).where(eq(Users.active, True))


@final
class Counts(View, name="user_counts", materialized=True):
    active = boolean().not_null()
    n = bigint().not_null()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return db.select({"active": Users.active, "n": count()}).from_(Users).group_by(Users.active)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [unique_index("user_counts_active").on(cls.active)]


@final
class Emails(View, name="emails"):  # a view over a view
    email = text()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return sql("SELECT email FROM active_users")


trgm = extension("pg_trgm")
touch = function("touch", returns="trigger", body="BEGIN NEW.updated_at = now(); RETURN NEW; END")
users_touch = trigger(
    "users_touch",
    on=Users,
    execute=touch,
    events=["update"],
    when=sql("OLD.* IS DISTINCT FROM NEW.*"),
)

TABLES: list[type[Table]] = [Users, Emails, ActiveUsers, Counts]
OBJECTS: list[SchemaObject] = [trgm, touch, users_touch]


def snap(tables: list[type[Table]] = TABLES, objects: list[SchemaObject] = OBJECTS) -> Snapshot:
    return build_snapshot(tables, dialect, objects=objects)


def described(old: Snapshot, new: Snapshot, compare_expressions: bool = True) -> list[str]:
    return [s.describe() for s in diff(old, new, compare_expressions=compare_expressions)]


def sqls(old: Snapshot, new: Snapshot) -> list[str]:
    return [s.sql for s in dialect.render(diff(old, new))]


def test_snapshot() -> None:
    s = snap()
    users = s["tables"]["public.users"]
    assert users.get("rls") == {"enabled": True, "force": False}
    assert users.get("policies", {})["own"] == {
        "name": "own",
        "command": "select",
        "permissive": True,
        "roles": ["anon", "app"],
        "using": "owner = current_user",
        "with_check": None,
    }
    assert users.get("triggers", {})["users_touch"] == {
        "name": "users_touch",
        "timing": "before",
        "events": ["update"],
        "for_each": "row",
        "function": "public.touch",
        "when": "OLD.* IS DISTINCT FROM NEW.*",
    }
    views = s.get("views", {})
    assert views["public.active_users"]["definition"] == (
        'SELECT "users"."id", "users"."email" FROM "users" WHERE "users"."active" = true'
    )
    assert list(views["public.user_counts"].get("indexes", {})) == ["user_counts_active"]
    assert s.get("extensions") == {"pg_trgm": {"name": "pg_trgm"}}
    assert s.get("functions", {})["public.touch()"]["returns"] == "trigger"


def test_create_order_and_sql() -> None:
    assert described(empty_snapshot("postgresql"), snap()) == [
        "create extension pg_trgm",
        "create table users",
        "create function touch",
        "create view active_users",
        "create materialized view user_counts",
        "create index user_counts_active",
        "create view emails",  # after the view it uses, though declared first
        "enable row level security on users",
        "create policy own on users",
        "create policy add on users",
        "create trigger users_touch on users",
    ]
    statements = sqls(empty_snapshot("postgresql"), snap())
    assert statements[0] == "CREATE EXTENSION IF NOT EXISTS pg_trgm"
    assert statements[2] == (
        'CREATE OR REPLACE FUNCTION "touch"() RETURNS trigger LANGUAGE plpgsql '
        "AS $mizzle$BEGIN NEW.updated_at = now(); RETURN NEW; END$mizzle$"
    )
    assert 'CREATE MATERIALIZED VIEW "user_counts" AS\nSELECT' in statements[4]
    assert statements[7:] == [
        'ALTER TABLE "users" ENABLE ROW LEVEL SECURITY, NO FORCE ROW LEVEL SECURITY',
        'CREATE POLICY "own" ON "users" AS PERMISSIVE FOR SELECT TO "anon", "app" '
        "USING (owner = current_user)",
        'CREATE POLICY "add" ON "users" AS PERMISSIVE FOR INSERT TO PUBLIC '
        "WITH CHECK (owner = current_user)",
        'CREATE TRIGGER "users_touch" BEFORE UPDATE ON "users" FOR EACH ROW '
        'WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION "touch"()',
    ]
    assert diff(snap(), snap()) == []


def test_drop_order_is_reversed() -> None:
    assert described(snap(), empty_snapshot("postgresql")) == [
        "drop trigger users_touch on users",
        "drop policy own on users",
        "drop policy add on users",
        "drop view emails",
        "drop materialized view user_counts",
        "drop view active_users",
        "drop function touch",
        "drop table users",
        "drop extension pg_trgm",
    ]


def test_changed_view_recreates_views_that_use_it() -> None:
    @final
    class ActiveUsers2(View, name="active_users"):
        id = integer()

        @classmethod
        @override
        def query(cls, db: QueryBuilder):
            return db.select(Users.id).from_(Users)

    steps = described(snap(), snap([Users, Emails, ActiveUsers2, Counts]))
    assert steps == [
        "keep privileges of emails",
        "drop view emails",
        "keep privileges of active_users",
        "drop view active_users",
        "create view active_users",
        "restore privileges of active_users",
        "create view emails",
        "restore privileges of emails",
    ]


def test_dropped_column_recreates_views_policies_and_triggers_using_it() -> None:
    @final
    class Users2(Table, name="users"):  # owner and active are gone
        id = serial().primary_key()
        email = text().not_null()
        updated_at = timestamp(tz=True)

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [rls(), policy("own", for_="select", using=sql("id > 0"))]

    @final
    class AllUsers(View, name="active_users"):
        id = integer()

        @classmethod
        @override
        def query(cls, db: QueryBuilder):
            return db.select(Users2.id).from_(Users2)

    touch2 = trigger("users_touch", on=Users2, execute=touch, events=["update"])
    old = snap([Users, ActiveUsers], [touch, users_touch])
    new = snap([Users2, AllUsers], [touch, touch2])
    assert described(old, new) == [
        "drop policy own on users",  # its USING reads owner
        "drop policy add on users",
        "keep privileges of active_users",
        "drop view active_users",
        "drop column users.active",
        "drop column users.owner",
        "create view active_users",
        "restore privileges of active_users",
        "create policy own on users",
        # A whole-row WHEN (OLD.*) does not block dropping a column: replaced in place.
        "replace trigger users_touch on users",
    ]


def test_function_changes() -> None:
    body = function("touch", returns="trigger", body="BEGIN RETURN NEW; END")
    new_trigger = trigger("users_touch", on=Users, execute=body, events=["update"])
    assert described(snap(objects=[users_touch]), snap(objects=[new_trigger])) == [
        "create function touch",  # CREATE OR REPLACE
        "replace trigger users_touch on users",  # WHEN removed
    ]
    args = function("touch", returns="trigger", body="BEGIN RETURN NEW; END", args="x integer")
    moved = trigger("users_touch", on=Users, execute=args, events=["update"])
    assert described(snap(objects=[new_trigger]), snap(objects=[moved])) == [
        "drop trigger users_touch on users",
        "drop function touch",
        "create function touch",
        "create trigger users_touch on users",
    ]
    assert sqls(snap(objects=[new_trigger]), snap(objects=[moved]))[1] == (
        'DROP FUNCTION "touch"()'
    )


def test_policy_changes() -> None:
    def users(*items: Constraint) -> type[Table]:
        @final
        class U(Table, name="users"):
            id = serial().primary_key()
            owner = text()

            @classmethod
            @override
            def config(cls) -> list[Constraint]:
                return list(items)

        return U

    base = snap([users(policy("p", using=sql("owner = current_user")))], [])
    changed = snap([users(policy("p", to=["anon"], using=sql("true")))], [])
    assert described(base, changed) == ["change policy p on users"]
    assert sqls(base, changed) == ['ALTER POLICY "p" ON "users" TO "anon" USING (true)']
    restrictive = snap([users(policy("p", as_="restrictive", using=sql("true")))], [])
    assert described(base, restrictive) == ["drop policy p on users", "create policy p on users"]
    checked = snap([users(policy("p", with_check=sql("true")))], [])
    assert described(base, checked) == ["drop policy p on users", "create policy p on users"]
    assert described(base, snap([users()], [])) == ["drop policy p on users"]
    forced = snap([users(rls(force=True))], [])
    assert described(base, forced) == [
        "drop policy p on users",
        "enable row level security on users",
    ]
    assert sqls(forced, base)[0] == (
        'ALTER TABLE "users" DISABLE ROW LEVEL SECURITY, NO FORCE ROW LEVEL SECURITY'
    )


def test_extensions() -> None:
    plain = snap([Users], [trgm])
    pinned = snap([Users], [extension("pg_trgm", schema="ext", version="1.6")])
    assert sqls(plain, pinned) == [
        'CREATE SCHEMA IF NOT EXISTS "ext"',
        'ALTER EXTENSION pg_trgm SET SCHEMA "ext"',
        "ALTER EXTENSION pg_trgm UPDATE TO '1.6'",
    ]
    assert sqls(empty_snapshot("postgresql"), pinned)[:2] == [
        'CREATE SCHEMA IF NOT EXISTS "ext"',
        """CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA "ext" VERSION '1.6'""",
    ]
    # Undeclared extensions a live database has (push) are left alone.
    none = snap([Users], [])
    assert described(plain, none) == ["drop extension pg_trgm"]
    assert described(plain, none, compare_expressions=False) == []
    assert diff(plain, none)[0].data_loss
    # What the schema does not pin is not compared.
    live = snap([Users], [extension("pg_trgm", schema="public", version="1.6")])
    assert diff(live, plain) == []


def test_vector_columns_need_the_extension() -> None:
    @final
    class Docs(Table, name="docs"):
        id = serial().primary_key()
        embedding = vector(3)

    s = build_snapshot([Docs], dialect)
    assert s.get("extensions") == {"vector": {"name": "vector"}}
    explicit = build_snapshot([Docs], dialect, objects=[extension("vector", schema="ext")])
    assert explicit.get("extensions") == {"vector": {"name": "vector", "schema": "ext"}}


def test_mistakes_are_reported() -> None:
    @final
    class Bad(View, name="bad"):
        id = integer()

        @classmethod
        @override
        def query(cls, db: QueryBuilder):
            return sql("SELECT 1 AS id")

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [unique_index("bad_id").on(cls.id)]  # not materialized

    with pytest.raises(SchemaError, match="materialized"):
        build_snapshot([Bad], dialect)

    @final
    class NoQuery(View, name="no_query"):
        id = integer()

    with pytest.raises(SchemaError, match="must define query"):
        build_snapshot([NoQuery], dialect)
    on_view = trigger("t", on=ActiveUsers, execute=touch)
    with pytest.raises(SchemaError, match="INSTEAD OF"):
        build_snapshot([Users, ActiveUsers], dialect, objects=[on_view])
    with pytest.raises(SchemaError, match="declared twice"):
        other = function("touch", returns="trigger", body="BEGIN RETURN NULL; END")
        build_snapshot([Users], dialect, objects=[touch, other])


def test_typegen_skips_views() -> None:
    source = render_types_module(TABLES)
    assert "class UsersInsert" in source and "ActiveUsers" not in source


def test_pull_round_trip(tmp_path: Path) -> None:
    original = snap()
    source = render_schema_module(original)
    path = tmp_path / "pulled_objects.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("pulled_objects", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pulled_objects"] = module
    spec.loader.exec_module(module)
    members = list(vars(module).values())
    tables = [
        v
        for v in members
        if isinstance(v, type) and issubclass(v, Table) and "__table__" in vars(v)
    ]
    objects = [v for v in members if isinstance(v, type(trgm) | type(touch) | type(users_touch))]
    assert diff(original, build_snapshot(tables, dialect, objects=objects)) == []

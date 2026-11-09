import copy
import hashlib
import re
from pathlib import Path
from typing import override

import pytest

from mizzle import Constraint, Table, policy, rls, role, sql
from mizzle.core.errors import MigrationError
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.steps import (
    AddEnumValue,
    CreateIndex,
    CreateTable,
    DropTable,
    RenameConstraint,
    Statement,
)
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.ddl import (
    escape_backslashes,
    normalize_default,
    normalize_type,
    same_type,
)
from mizzle.kit.codegen import render_schema_module
from mizzle.kit.config import load_config
from mizzle.kit.diff import StrictRenames, diff
from mizzle.kit.journal import BREAKPOINT, Migration, MigrationsDir, format_sql, parse_sql
from mizzle.kit.migrate import pending_migrations
from mizzle.kit.snapshot import build_snapshot
from mizzle.kit.sqltext import transaction_control
from mizzle.pg import serial
from tests.models import Posts, Users

dialect = PostgresDialect()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("'user'::role", "'user'"),
        ("'-1'::integer", "-1"),
        ("now()", "now()"),
        ("'x'::character varying", "'x'"),
        ("'{}'::jsonb", "'{}'"),
        ("('2'::numeric)", "2"),
        ('\'happy\'::"App"."Mood"', "'happy'"),  # K4-4
        ("'happy'::app.\"Mood\"", "'happy'"),
        ("'happy'::\"App\".mood", "'happy'"),
        (None, None),
    ],
)
def test_normalize_default(raw: str | None, expected: str | None) -> None:
    assert normalize_default(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            r"""DEFAULT 'x\y' CHECK (("c\" <> '\'::text))""",
            r"""DEFAULT E'x\\y' CHECK (("c\" <> E'\\'::text))""",
        ),
        (r"x = E'a\\b' AND y = 'q' AND z = U&'\0041'", None),  # escapes, or no backslash
        (r"DO $x$ BEGIN PERFORM 'a\b'; END $x$", None),  # a body runs later
        (r"-- 'a\b'", None),
        (r"name'a\b' $1", r"name E'a\\b' $1"),
        # K9-2: N E'..' is no string, nor is a continued part with an E: both kept.
        (r"DEFAULT N'C:\dir'", None),
        ("'a'\n'\\b' || 'c\\d'", "'a'\n'\\b' || E'c\\\\d'"),
    ],
)
def test_ddl_strings_read_the_same_with_standard_conforming_strings_off(
    raw: str, expected: str | None
) -> None:
    # K8-3: a pulled default or check printed as 'x\y' lost the backslash there, or failed.
    assert escape_backslashes(raw) == (raw if expected is None else expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("varchar(255)", "character varying(255)"),
        ("numeric(10, 2)", "numeric(10,2)"),
        ("timestamptz", "timestamp with time zone"),
        ("text[]", "text[]"),
        ("varchar(5)[]", "character varying(5)[]"),
        ("integer[][]", "integer[]"),  # Q5-8: one array type, whatever the dimensions
        ("char(4)", "character(4)"),
        ("char(1)", "character(1)"),
        ("char", "character"),
        ("bpchar(2)", "character(2)"),
    ],
)
def test_normalize_type(raw: str, expected: str) -> None:
    assert normalize_type(raw) == expected


def snap() -> Snapshot:
    return build_snapshot([Users, Posts], dialect)


def test_initial_diff_creates_everything_in_order() -> None:
    steps = diff(empty_snapshot("postgresql"), snap())
    kinds = [type(s).__name__ for s in steps]
    assert kinds == ["CreateEnum", "CreateTable", "CreateTable", "AddForeignKey"]


def test_no_changes() -> None:
    assert diff(snap(), snap()) == []


def test_enum_value_added_and_removed() -> None:
    old, new = snap(), snap()
    new["enums"]["public.role"]["values"] = ["owner", "admin", "user"]
    [step] = diff(old, new)
    assert step == AddEnumValue("public", "role", "owner", None, "admin")
    new["enums"]["public.role"]["values"] = ["admin"]
    with pytest.raises(MigrationError, match="removing values"):
        diff(old, new)


def test_constraint_rename_is_detected() -> None:
    old, new = snap(), snap()
    fk = new["tables"]["public.posts"]["foreign_keys"].pop("posts_author_id_fkey")
    fk["name"] = "posts_author_fk"
    new["tables"]["public.posts"]["foreign_keys"]["posts_author_fk"] = fk
    assert diff(old, new) == [
        RenameConstraint("public", "posts", "posts_author_id_fkey", "posts_author_fk")
    ]


def test_strict_renames_refuse_to_guess() -> None:
    old, new = snap(), snap()
    table = new["tables"].pop("public.posts")
    table["name"] = "articles"
    new["tables"]["public.articles"] = table
    with pytest.raises(MigrationError, match="--rename"):
        diff(old, new, StrictRenames())
    steps = diff(old, new, StrictRenames({"posts": "articles"}))
    assert [s.describe() for s in steps] == ["rename table posts to articles"] + [
        s.describe() for s in steps[1:]
    ]
    no_rename = diff(old, new)
    assert any(isinstance(s, DropTable) for s in no_rename)
    assert any(isinstance(s, CreateTable) for s in no_rename)


def test_index_changes_drop_and_create() -> None:
    old = snap()
    new = copy.deepcopy(old)
    new["tables"]["public.users"]["indexes"]["users_role_idx"] = {
        "name": "users_role_idx",
        "columns": ["role"],
        "unique": False,
        "where": None,
    }
    [step] = diff(old, new)
    assert isinstance(step, CreateIndex)


def test_sql_file_roundtrip() -> None:
    statements = [
        Statement("CREATE TABLE a (x int)"),
        Statement("ALTER TYPE t ADD VALUE 'v'", False),
    ]
    text = format_sql(statements, "-- header\n")
    assert BREAKPOINT in text
    parsed = parse_sql(text)
    assert [p.transactional for p in parsed] == [True, False]
    assert parse_sql("-- only comments\n") == []


def test_config_sources(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.mizzle]\nschema = "a.b"\n')
    assert load_config(cwd=tmp_path).schema == ["a.b"]
    (tmp_path / "mizzle.toml").write_text('schema = ["x", "y"]\nout = "db/migrations"\n')
    config = load_config(cwd=tmp_path)
    assert config.schema == ["x", "y"]
    assert config.out == tmp_path / "db/migrations"
    (tmp_path / "mizzle.toml").write_text('schema = "x"\nbogus = 1\n')
    with pytest.raises(MigrationError, match="unknown settings"):
        load_config(cwd=tmp_path)


def test_config_is_not_read_from_parent_folders(tmp_path: Path) -> None:
    (tmp_path / "mizzle.toml").write_text('schema = "planted"\n')
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(MigrationError, match="pass --config"):
        load_config(cwd=child)


def test_pulled_names_cannot_inject_code() -> None:
    from mizzle.kit.codegen import render_schema_module

    snapshot = empty_snapshot("postgresql")
    evil = "x\nimport os; os.system('echo pwned')"
    snapshot["tables"]["public.t"] = {
        "schema": "public",
        "name": "t",
        "columns": {"a": {"name": "a", "type": "integer", "not_null": False, "default": None}},
        "primary_key": None,
        "uniques": {},
        "checks": {},
        "foreign_keys": {
            evil: {
                "name": evil,
                "columns": ["a"],
                "ref_schema": "other",
                "ref_table": evil,
                "ref_columns": ["id"],
                "on_delete": None,
                "on_update": None,
            }
        },
        "indexes": {},
    }
    source = render_schema_module(snapshot)
    assert "\nimport os" not in source
    compile(source, "pulled.py", "exec")


def test_pulled_class_names_are_unique_identifiers() -> None:
    # (K9) T, t, t_, table/Table, none and 2fa need distinct, valid, non-shadowing classes.
    from mizzle.kit.codegen import render_schema_module

    snapshot = empty_snapshot("postgresql")
    names = ["T", "t", "t_", "table", "Table", "none", "2fa"]
    for name in names:
        snapshot["tables"][f"public.{name}"] = {
            "schema": "public",
            "name": name,
            "columns": {"a": {"name": "a", "type": "integer", "not_null": False, "default": None}},
            "primary_key": None,
            "uniques": {},
            "checks": {},
            "foreign_keys": {},
            "indexes": {},
        }
    source = render_schema_module(snapshot)
    exec(compile(source, "pulled.py", "exec"), {})
    classes = re.findall(r"^class (\w+)\(Table, name='([^']*)'", source, re.M)
    assert sorted(n for _, n in classes) == sorted(names)
    assert len({c for c, _ in classes}) == len(names)
    assert "Table" not in {c for c, _ in classes}


def test_init_creates_config_schema_and_folder(tmp_path: Path) -> None:
    from mizzle.kit.init import init_project

    done = init_project(tmp_path, schema_file=Path("app/db/schema.py"))
    assert done == [
        "wrote mizzle.toml",
        "wrote starter schema app/db/schema.py",
        "created migrations/",
    ]
    config = load_config(cwd=tmp_path)
    assert config.schema == ["app.db.schema"]
    assert config.out == tmp_path / "migrations"
    compile((tmp_path / "app/db/schema.py").read_text(), "schema.py", "exec")

    with pytest.raises(MigrationError, match="already set up"):
        init_project(tmp_path, schema_file=Path("app/db/schema.py"))
    (tmp_path / "app/db/schema.py").write_text("# mine\n")
    done = init_project(tmp_path, schema_file=Path("app/db/schema.py"), force=True)
    assert "kept existing app/db/schema.py" in done
    assert (tmp_path / "app/db/schema.py").read_text() == "# mine\n"  # never overwritten


def test_init_in_pyproject(tmp_path: Path) -> None:
    from mizzle.kit.init import init_project

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    init_project(tmp_path, schema_file=Path("schema.py"), in_pyproject=True)
    assert load_config(cwd=tmp_path).schema == ["schema"]
    assert '[project]\nname = "x"\n' in (tmp_path / "pyproject.toml").read_text()


def test_init_rejects_bad_schema_paths(tmp_path: Path) -> None:
    from mizzle.kit.init import init_project

    for bad in ("schema.txt", "my-app/schema.py", "class/schema.py"):
        with pytest.raises(MigrationError):
            init_project(tmp_path, schema_file=Path(bad))


def test_dotenv_is_loaded_without_overriding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mizzle.kit.config import load_dotenv

    (tmp_path / ".env").write_text(
        "# comment\n"
        "export DATABASE_URL='postgresql://u:p@h/db'\n"
        "PLAIN=value # trailing comment\n"
        'QUOTED="a # not a comment"\n'
        "ALREADY=from-file\n"
        "not a variable line\n"
    )
    for key in ("DATABASE_URL", "PLAIN", "QUOTED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALREADY", "from-env")
    load_dotenv(tmp_path / ".env")
    import os

    assert os.environ["DATABASE_URL"] == "postgresql://u:p@h/db"
    assert os.environ["PLAIN"] == "value"
    assert os.environ["QUOTED"] == "a # not a comment"
    assert os.environ["ALREADY"] == "from-env"


def test_status_lists_migrations_without_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mizzle.kit.cli import main

    (tmp_path / "mizzle.toml").write_text('schema = "x"\nout = "migrations"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["status"]) == 0
    assert "No migrations." in capsys.readouterr().out

    directory = MigrationsDir(tmp_path / "migrations", "postgresql")
    directory.write("create_users", [], empty_snapshot("postgresql"))
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "0001_create_users" in out
    assert "unknown" in out
    assert "Set $DATABASE_URL" in out


def test_studio_download_ui_uses_an_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mizzle.kit.cli import main

    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html>")
    (tmp_path / "mizzle.toml").write_text('schema = "x"\n')
    monkeypatch.chdir(tmp_path)
    assert main(["studio", "--download-ui", "--ui-path", str(ui)]) == 0
    out = capsys.readouterr().out
    assert "Studio UI at" in out
    assert str(ui) in out


def test_schema_file(tmp_path: Path) -> None:
    (tmp_path / "mizzle.toml").write_text('schema = "app.db.schema"\n')
    config = load_config(cwd=tmp_path)
    assert config.schema_file() == tmp_path / "app" / "db" / "schema.py"  # not created yet
    (tmp_path / "app" / "db").mkdir(parents=True)
    (tmp_path / "app" / "db" / "schema.py").write_text("")
    assert config.schema_file().resolve() == (tmp_path / "app" / "db" / "schema.py").resolve()


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # K2: CASE ... END inside a BEGIN ATOMIC body does not close the body.
        (
            "CREATE FUNCTION f(x int) RETURNS int LANGUAGE sql BEGIN ATOMIC\n"
            "  SELECT CASE WHEN x > 0 THEN 1 ELSE 0 END;\nEND;\nSELECT 1;",
            None,
        ),
        (
            "CREATE FUNCTION f() RETURNS int BEGIN ATOMIC SELECT CASE 1 WHEN 1 THEN 2 END; END;"
            "\nCOMMIT;",
            "COMMIT",
        ),
        # K3: "begin atomic" in a string, comment or quoted name hides nothing.
        ("SELECT 'begin atomic';\nCOMMIT;", "COMMIT"),
        ("-- begin atomic\nCOMMIT;", "COMMIT"),
        ("/* begin atomic */ SELECT 1;\nCOMMIT;", "COMMIT"),
        ('SELECT 1 AS "begin atomic";\nCOMMIT;', "COMMIT"),
        ("SELECT $$begin atomic$$;\nCOMMIT;", "COMMIT"),
    ],
)
def test_transaction_control_reads_atomic_bodies(sql: str, expected: str | None) -> None:
    assert transaction_control(sql) == expected


def test_migration_hash_ignores_crlf(tmp_path: Path) -> None:
    # K12: a CRLF checkout of an applied migration must not look edited.
    (tmp_path / "migration.sql").write_bytes(b"SELECT 1;\nSELECT 2;\n")
    migration = Migration(index=1, name="0001_x", path=tmp_path)
    lf = migration.hash()
    (tmp_path / "migration.sql").write_bytes(b"SELECT 1;\r\nSELECT 2;\r\n")
    assert migration.hash() == lf


def test_crlf_migration_sql_and_old_hash(tmp_path: Path) -> None:
    # K4-7: sql() gives LF, so a function body does not keep \r.
    raw = b"CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$\r\nSELECT 1\r\n$$;\r\n"
    (tmp_path / "migration.sql").write_bytes(raw)
    migration = Migration(index=1, name="0001_x", path=tmp_path)
    assert "\r" not in migration.sql()
    # K4-16: a hash an older mizzle stored (sha256 of the CRLF bytes) still matches.
    old = hashlib.sha256(raw).hexdigest()
    assert pending_migrations([migration], {"0001_x": old}) == []
    with pytest.raises(MigrationError):
        pending_migrations([migration], {"0001_x": "0" * 64})


def test_commit_after_a_breakpoint_is_transaction_control(tmp_path: Path) -> None:
    # K4-17: no ';' before the breakpoint, so the COMMIT did not start a statement.
    sql = (
        "INSERT INTO t VALUES (1)\n-- mizzle:breakpoint\nCOMMIT\n"
        "-- mizzle:breakpoint\nINSERT INTO t VALUES (1)\n"
    )
    (tmp_path / "migration.sql").write_text(sql)
    migration = Migration(index=1, name="0001_x", path=tmp_path)
    with pytest.raises(MigrationError, match="contains COMMIT"):
        pending_migrations([migration], {})


def test_non_migration_folders_are_not_unlisted(tmp_path: Path) -> None:
    # K4-11: .cache or __pycache__ in migrations/ blocked every command.
    directory = MigrationsDir(tmp_path, "postgresql")
    for name in (".cache", "__pycache__", "meta", "0001_merged"):
        (tmp_path / name).mkdir()
    assert directory.unlisted() == ["0001_merged"]


def test_pulled_role_variables_never_shadow_imported_names() -> None:
    # K7-8: `policy = role("policy")` shadowed the policy() the table's config() calls.
    roles = [role("policy", login=True), role("role"), role("print")]

    class Items(Table, name="items"):
        id = serial().primary_key()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [rls(), policy("p_admin", to=[roles[0]], using=sql("true"))]

    source = render_schema_module(build_snapshot([Items], dialect, objects=roles))
    for name in ("policy", "role", "print"):
        assert f"\n{name} = " not in source
    namespace: dict[str, object] = {}
    exec(compile(source, "pulled", "exec"), namespace)
    pulled = namespace["Items"]
    assert isinstance(pulled, type) and issubclass(pulled, Table)
    assert build_snapshot([pulled], dialect)["tables"]["public.items"].get("policies")


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("posint", "public.posint", True),  # K8-2: an older snapshot's spelling
        ("integer", "pg_catalog.int4", True),
        ("int4", "public.int4", False),  # K7-7: public.int4 can be a domain
        ("jsonpath", "public.jsonpath", False),  # K9-4: a built-in missing from the list
        ("int4multirange", "public.int4multirange", False),
        ("xid8", "public.xid8", False),
    ],
)
def test_same_type(a: str, b: str, same: bool) -> None:
    assert same_type(a, b) is same

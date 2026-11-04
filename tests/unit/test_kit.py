import copy
from pathlib import Path

import pytest

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
from mizzle.dialects.postgres.ddl import normalize_default, normalize_type
from mizzle.kit.config import load_config
from mizzle.kit.diff import StrictRenames, diff
from mizzle.kit.journal import BREAKPOINT, MigrationsDir, format_sql, parse_sql
from mizzle.kit.snapshot import build_snapshot
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
        (None, None),
    ],
)
def test_normalize_default(raw: str | None, expected: str | None) -> None:
    assert normalize_default(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("varchar(255)", "character varying(255)"),
        ("numeric(10, 2)", "numeric(10,2)"),
        ("timestamptz", "timestamp with time zone"),
        ("text[]", "text[]"),
        ("varchar(5)[]", "character varying(5)[]"),
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

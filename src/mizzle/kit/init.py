"""``mizzle init``: create the config, a starter schema module and the migrations folder."""

from __future__ import annotations

import keyword
from pathlib import Path
from typing import cast

from mizzle.core.errors import MigrationError
from mizzle.kit.config import load_toml

STARTER_SCHEMA = '''"""Database schema. Edit it, then run `mizzle generate` and `mizzle migrate`."""

from typing import final

from mizzle import Table, sql
from mizzle.pg import serial, text, timestamp, varchar


@final  # tables are never subclassed; also lets strict type checkers skip annotations
class Users(Table, name="users"):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    name = text()
    created_at = timestamp(tz=True).not_null().default(sql("now()"))
'''


def _module_name(schema_file: Path, root: Path) -> str:
    """``app/db/schema.py`` -> ``app.db.schema`` (must be importable from the project root)."""
    if schema_file.suffix != ".py":
        raise MigrationError(f"--schema must be a .py file, got {schema_file}")
    try:
        relative = schema_file.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise MigrationError(f"{schema_file} must be inside the project folder {root}") from exc
    parts = relative.with_suffix("").parts
    if not parts or any(not p.isidentifier() or keyword.iskeyword(p) for p in parts):
        raise MigrationError(f"{relative} is not an importable module path")
    return ".".join(parts)


def _toml_settings(module: str, out: str, url_env: str) -> str:
    return (
        "# Module(s) with your Table classes.\n"
        f'schema = "{module}"\n'
        "# Migrations folder. Commit it.\n"
        f'out = "{out}"\n'
        "# Environment variable holding the database URL (never put the URL in this file).\n"
        f'url_env = "{url_env}"\n'
    )


def init_project(
    root: Path,
    *,
    schema_file: Path,
    out: str = "migrations",
    url_env: str = "DATABASE_URL",
    in_pyproject: bool = False,
    force: bool = False,
) -> list[str]:
    """Create the mizzle files under ``root``. Returns human-readable lines of what happened.

    Never overwrites a schema file. Refuses to replace an existing config unless ``force``.
    """
    done: list[str] = []
    if not schema_file.is_absolute():
        schema_file = root / schema_file
    module = _module_name(schema_file, root)
    settings = _toml_settings(module, out, url_env)

    own = root / "mizzle.toml"
    pyproject = root / "pyproject.toml"
    has_tool_section = pyproject.exists() and "mizzle" in cast(
        "dict[str, object]", load_toml(pyproject).get("tool", {})
    )
    if (own.exists() or has_tool_section) and not force:
        where = own.name if own.exists() else "pyproject.toml [tool.mizzle]"
        raise MigrationError(f"mizzle is already set up ({where}); pass --force to replace it")

    if in_pyproject:
        if has_tool_section:
            raise MigrationError("edit [tool.mizzle] in pyproject.toml by hand; --force won't")
        text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
        separator = (
            "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        )
        pyproject.write_text(f"{text}{separator}[tool.mizzle]\n{settings}", encoding="utf-8")
        done.append("added [tool.mizzle] to pyproject.toml")
    else:
        own.write_text(settings, encoding="utf-8")
        done.append("wrote mizzle.toml")

    if schema_file.exists():
        done.append(f"kept existing {schema_file.relative_to(root)}")
    else:
        schema_file.parent.mkdir(parents=True, exist_ok=True)
        schema_file.write_text(STARTER_SCHEMA, encoding="utf-8")
        done.append(f"wrote starter schema {schema_file.relative_to(root)}")

    migrations = root / out
    if not migrations.exists():
        migrations.mkdir(parents=True)
        done.append(f"created {out}/")
    return done

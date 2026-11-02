"""Kit configuration: ``mizzle.toml``, or ``[tool.mizzle]`` in ``pyproject.toml``.

schema = "app.db.schema"      # module(s) holding Table classes
out = "migrations"
dialect = "postgresql"
url_env = "DATABASE_URL"      # the URL is read from the environment, never the file
"""

from __future__ import annotations

import importlib
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from mizzle.core.errors import MigrationError
from mizzle.core.naming import Casing
from mizzle.core.schema.table import Table
from mizzle.dialects.base import Dialect
from mizzle.dialects.postgres import PostgresDialect

_KEYS = {
    "schema",
    "out",
    "dialect",
    "url_env",
    "casing",
    "schema_filter",
    "migrations_schema",
}


@dataclass(frozen=True)
class Config:
    root: Path
    schema: list[str]
    out: Path
    dialect_name: str = "postgresql"
    url_env: str = "DATABASE_URL"
    casing: Casing | None = None
    schema_filter: list[str] = field(default_factory=lambda: ["public"])
    migrations_schema: str = "public"

    def dialect(self) -> Dialect:
        if self.dialect_name == "postgresql":
            return PostgresDialect()
        raise MigrationError(f"unsupported dialect {self.dialect_name!r}")

    def url(self) -> str:
        url = os.environ.get(self.url_env)
        if not url:
            raise MigrationError(f"set the {self.url_env} environment variable to the database URL")
        return url

    def load_tables(self) -> list[type[Table]]:
        """Import the schema modules and collect every Table subclass they expose."""
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        tables: list[type[Table]] = []
        for module_name in self.schema:
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                raise MigrationError(f"cannot import schema module {module_name!r}: {exc}") from exc
            for value in vars(module).values():
                if (
                    isinstance(value, type)
                    and issubclass(value, Table)
                    and value is not Table
                    and value not in tables
                ):
                    tables.append(value)
        if not tables:
            raise MigrationError(f"no Table classes found in {self.schema}")
        return tables


def _from_mapping(root: Path, data: dict[str, Any], source: Path) -> Config:
    if unknown := set(data) - _KEYS:
        raise MigrationError(f"{source}: unknown settings {sorted(unknown)}")
    if "schema" not in data:
        raise MigrationError(f"{source}: 'schema' is required (the module with your tables)")
    schema = data["schema"]
    schemas = [schema] if isinstance(schema, str) else list(cast(list[str], schema))
    casing = data.get("casing")
    if casing not in (None, "snake_case"):
        raise MigrationError(f"{source}: casing must be 'snake_case'")
    return Config(
        root=root,
        schema=schemas,
        out=root / data.get("out", "migrations"),
        dialect_name=data.get("dialect", "postgresql"),
        url_env=data.get("url_env", "DATABASE_URL"),
        casing=casing,
        schema_filter=list(data.get("schema_filter", ["public"])),
        migrations_schema=data.get("migrations_schema", "public"),
    )


def load_config(path: Path | None = None, cwd: Path | None = None) -> Config:
    """Read ``path``, else ``mizzle.toml``, else ``pyproject.toml [tool.mizzle]`` in ``cwd``.

    Only ``cwd`` is searched, never parent folders: loading a config imports (runs) the
    schema modules it names, so a config planted in a shared parent must not be picked up.
    """
    cwd = cwd or Path.cwd()
    if path is not None:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if path.name == "pyproject.toml":
            data = data.get("tool", {}).get("mizzle", {})
        return _from_mapping(path.parent.resolve(), data, path)
    for directory in [cwd]:
        own = directory / "mizzle.toml"
        if own.exists():
            return _from_mapping(directory, tomllib.loads(own.read_text(encoding="utf-8")), own)
        pyproject = directory / "pyproject.toml"
        if pyproject.exists():
            tool = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {})
            if "mizzle" in tool:
                return _from_mapping(directory, tool["mizzle"], pyproject)
    raise MigrationError(
        f"no mizzle.toml or [tool.mizzle] in {cwd}; run from the project root or pass --config"
    )

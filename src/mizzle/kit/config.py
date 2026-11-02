"""Kit configuration: ``mizzle.toml``, or ``[tool.mizzle]`` in ``pyproject.toml``.

schema = "app.db.schema"      # module(s) holding Table classes
out = "migrations"
dialect = "postgresql"
url_env = "DATABASE_URL"      # the URL is read from the environment, never the file
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

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
        load_dotenv(self.root / ".env")
        url = os.environ.get(self.url_env)
        if not url:
            raise MigrationError(f"set the {self.url_env} environment variable to the database URL")
        return url

    def schema_file(self) -> Path:
        """The file of the first schema module, even if it does not exist yet."""
        module = self.schema[0]
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError:  # a parent package is missing too
            spec = None
        if spec is not None and spec.origin not in (None, "namespace"):
            return Path(spec.origin)
        return self.root.joinpath(*module.split(".")).with_suffix(".py")

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
            for value in cast(dict[str, object], vars(module)).values():
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


def _str(data: dict[str, object], name: str, default: str, source: Path) -> str:
    value = data.get(name, default)
    if not isinstance(value, str):
        raise MigrationError(f"{source}: {name!r} must be a string")
    return value


def _str_list(data: dict[str, object], name: str, default: list[str], source: Path) -> list[str]:
    value = data.get(name, default)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in cast(list[object], value)):
        return list(cast(list[str], value))
    raise MigrationError(f"{source}: {name!r} must be a string or a list of strings")


def _table(value: object, source: Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MigrationError(f"{source}: [tool.mizzle] must be a table")
    return cast(dict[str, object], value)  # TOML tables have string keys


def load_dotenv(path: Path) -> None:
    """Load ``KEY=value`` lines from a ``.env`` file. Variables already set in the
    environment win, so ``export DATABASE_URL=...`` overrides the file."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()  # unquoted values may end in a comment
        os.environ.setdefault(key, value)


def _from_mapping(root: Path, data: dict[str, object], source: Path) -> Config:
    if unknown := set(data) - _KEYS:
        raise MigrationError(f"{source}: unknown settings {sorted(unknown)}")
    if "schema" not in data:
        raise MigrationError(f"{source}: 'schema' is required (the module with your tables)")
    casing = data.get("casing")
    if casing not in (None, "snake_case"):
        raise MigrationError(f"{source}: casing must be 'snake_case'")
    return Config(
        root=root,
        schema=_str_list(data, "schema", [], source),
        out=root / _str(data, "out", "migrations", source),
        dialect_name=_str(data, "dialect", "postgresql", source),
        url_env=_str(data, "url_env", "DATABASE_URL", source),
        casing=None if casing is None else "snake_case",
        schema_filter=_str_list(data, "schema_filter", ["public"], source),
        migrations_schema=_str(data, "migrations_schema", "public", source),
    )


def load_config(path: Path | None = None, cwd: Path | None = None) -> Config:
    """Read ``path``, else ``mizzle.toml``, else ``pyproject.toml [tool.mizzle]`` in ``cwd``.

    Only ``cwd`` is searched, never parent folders: loading a config imports (runs) the
    schema modules it names, so a config planted in a shared parent must not be picked up.
    """
    cwd = cwd or Path.cwd()
    if path is not None:
        data: dict[str, object] = tomllib.loads(path.read_text(encoding="utf-8"))
        if path.name == "pyproject.toml":
            data = _table(_table(data.get("tool", {}), path).get("mizzle", {}), path)
        return _from_mapping(path.parent.resolve(), data, path)
    for directory in [cwd]:
        own = directory / "mizzle.toml"
        if own.exists():
            return _from_mapping(directory, tomllib.loads(own.read_text(encoding="utf-8")), own)
        pyproject = directory / "pyproject.toml"
        if pyproject.exists():
            document: dict[str, object] = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            tool = _table(document.get("tool", {}), pyproject)
            if "mizzle" in tool:
                return _from_mapping(directory, _table(tool["mizzle"], pyproject), pyproject)
    raise MigrationError(
        f"no mizzle.toml or [tool.mizzle] in {cwd}; run from the project root or pass --config"
    )

"""The migrations folder: one sub-folder per migration plus ``meta/journal.json``.

migrations/
  0001_create_users/migration.sql
  0001_create_users/snapshot.json
  meta/journal.json
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from mizzle.core.errors import MigrationError
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.steps import Statement

BREAKPOINT = "-- mizzle:breakpoint"
NO_TRANSACTION = "-- mizzle:no-transaction"
JOURNAL_VERSION = 1


def format_sql(statements: list[Statement], header: str = "") -> str:
    chunks = [
        (f"{NO_TRANSACTION}\n" if not s.transactional else "") + s.sql.rstrip().rstrip(";") + ";"
        for s in statements
    ]
    return header + f"\n{BREAKPOINT}\n".join(chunks) + "\n"


def parse_sql(text: str) -> list[Statement]:
    statements: list[Statement] = []
    for chunk in re.split(rf"^{re.escape(BREAKPOINT)}\s*$", text, flags=re.MULTILINE):
        lines = chunk.strip().splitlines()
        if all(not line.strip() or line.strip().startswith("--") for line in lines):
            continue  # nothing but comments
        transactional = NO_TRANSACTION not in (line.strip() for line in lines)
        statements.append(Statement(chunk.strip(), transactional))
    return statements


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:50] or "migration"


@dataclass(frozen=True)
class Migration:
    index: int
    name: str  # folder name, e.g. "0001_create_users"; also the id stored in the database
    path: Path

    @property
    def sql_path(self) -> Path:
        return self.path / "migration.sql"

    @property
    def snapshot_path(self) -> Path:
        return self.path / "snapshot.json"

    def sql(self) -> str:
        return self.sql_path.read_text(encoding="utf-8")

    def hash(self) -> str:
        return hashlib.sha256(self.sql_path.read_bytes()).hexdigest()

    def statements(self) -> list[Statement]:
        return parse_sql(self.sql())

    def snapshot(self) -> Snapshot:
        return cast(Snapshot, json.loads(self.snapshot_path.read_text(encoding="utf-8")))


class MigrationsDir:
    def __init__(self, root: Path, dialect: str) -> None:
        self.root = root
        self.dialect = dialect

    @property
    def journal_path(self) -> Path:
        return self.root / "meta" / "journal.json"

    def journal(self) -> dict[str, Any]:
        if not self.journal_path.exists():
            return {"version": JOURNAL_VERSION, "dialect": self.dialect, "entries": []}
        data = cast(dict[str, Any], json.loads(self.journal_path.read_text(encoding="utf-8")))
        if data.get("dialect") != self.dialect:
            raise MigrationError(
                f"{self.journal_path} is for dialect {data.get('dialect')!r}, not {self.dialect!r}"
            )
        return data

    def _save_journal(self, journal: dict[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")

    def migrations(self) -> list[Migration]:
        return [
            Migration(e["index"], e["name"], self.root / e["name"])
            for e in self.journal()["entries"]
        ]

    def latest_snapshot(self) -> Snapshot:
        migrations = self.migrations()
        return migrations[-1].snapshot() if migrations else empty_snapshot(self.dialect)

    def write(
        self, name: str, statements: list[Statement], snapshot: Snapshot, header: str = ""
    ) -> Migration:
        journal = self.journal()
        index = journal["entries"][-1]["index"] + 1 if journal["entries"] else 1
        folder = f"{index:04d}_{slugify(name)}"
        migration = Migration(index, folder, self.root / folder)
        migration.path.mkdir(parents=True, exist_ok=False)
        migration.sql_path.write_text(format_sql(statements, header), encoding="utf-8")
        migration.snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        journal["entries"].append(
            {
                "index": index,
                "name": folder,
                "hash": migration.hash(),
                "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            }
        )
        self._save_journal(journal)
        return migration

    def drop_latest(self) -> Migration:
        journal = self.journal()
        if not journal["entries"]:
            raise MigrationError("there are no migrations to drop")
        entry = journal["entries"].pop()
        migration = Migration(entry["index"], entry["name"], self.root / entry["name"])
        shutil.rmtree(migration.path, ignore_errors=True)
        self._save_journal(journal)
        return migration

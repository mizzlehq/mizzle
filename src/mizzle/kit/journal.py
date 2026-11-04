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
from typing import NotRequired, TypedDict, cast

from mizzle.core.errors import MigrationError
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.steps import Statement

BREAKPOINT = "-- mizzle:breakpoint"
NO_TRANSACTION = "-- mizzle:no-transaction"
JOURNAL_VERSION = 1
_INDEX = re.compile(r"^(\d+)_")


class JournalEntry(TypedDict):
    index: int
    name: str
    hash: str
    created_at: str
    # A hand-written (or templated) migration: its SQL is expected to change after generate.
    custom: NotRequired[bool]


class Journal(TypedDict):
    version: int
    dialect: str
    entries: list[JournalEntry]


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
    custom: bool = False  # a hand-written migration may be edited after generate

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


def _parse_entry(raw: object, source: Path) -> JournalEntry:
    if isinstance(raw, dict):
        entry = cast(dict[str, object], raw)  # JSON object: string keys
        index, name = entry.get("index"), entry.get("name")
        hash_, created_at = entry.get("hash"), entry.get("created_at")
        if (
            isinstance(index, int)
            and isinstance(name, str)
            and isinstance(hash_, str)
            and isinstance(created_at, str)
        ):
            parsed = JournalEntry(index=index, name=name, hash=hash_, created_at=created_at)
            if entry.get("custom") is True:
                parsed["custom"] = True
            return parsed
    raise MigrationError(f"{source}: malformed journal entry {raw!r}")


class MigrationsDir:
    root: Path
    dialect: str

    def __init__(self, root: Path, dialect: str) -> None:
        self.root = root
        self.dialect = dialect

    @property
    def journal_path(self) -> Path:
        return self.root / "meta" / "journal.json"

    def journal(self) -> Journal:
        if not self.journal_path.exists():
            return Journal(version=JOURNAL_VERSION, dialect=self.dialect, entries=[])
        raw = cast(object, json.loads(self.journal_path.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise MigrationError(f"{self.journal_path}: expected a JSON object")
        data = cast(dict[str, object], raw)  # JSON object: string keys
        dialect = data.get("dialect")
        if dialect != self.dialect:
            raise MigrationError(
                f"{self.journal_path} is for dialect {dialect!r}, not {self.dialect!r}"
            )
        version, entries = data.get("version"), data.get("entries")
        if not isinstance(version, int) or not isinstance(entries, list):
            raise MigrationError(f"{self.journal_path}: missing 'version' or 'entries'")
        return Journal(
            version=version,
            dialect=self.dialect,
            entries=[_parse_entry(e, self.journal_path) for e in cast(list[object], entries)],
        )

    def _save_journal(self, journal: Journal) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")

    def migrations(self) -> list[Migration]:
        return [
            Migration(e["index"], e["name"], self.root / e["name"], bool(e.get("custom")))
            for e in self.journal()["entries"]
        ]

    def latest_snapshot(self) -> Snapshot:
        migrations = self.migrations()
        return migrations[-1].snapshot() if migrations else empty_snapshot(self.dialect)

    def write(
        self,
        name: str,
        statements: list[Statement],
        snapshot: Snapshot,
        header: str = "",
        custom: bool = False,
    ) -> Migration:
        journal = self.journal()
        index = journal["entries"][-1]["index"] + 1 if journal["entries"] else 1
        folder = f"{index:04d}_{slugify(name)}"
        migration = Migration(index, folder, self.root / folder, custom)
        migration.path.mkdir(parents=True, exist_ok=False)
        migration.sql_path.write_text(format_sql(statements, header), encoding="utf-8")
        migration.snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        entry = JournalEntry(
            index=index,
            name=folder,
            hash=migration.hash(),
            created_at=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        )
        if custom:
            entry["custom"] = True
        journal["entries"].append(entry)
        self._save_journal(journal)
        return migration

    def drop_latest(self) -> Migration:
        journal = self.journal()
        if not journal["entries"]:
            raise MigrationError("there are no migrations to drop")
        entry = journal["entries"].pop()
        migration = Migration(
            entry["index"], entry["name"], self.root / entry["name"], bool(entry.get("custom"))
        )
        shutil.rmtree(migration.path, ignore_errors=True)
        self._save_journal(journal)
        return migration

    def repair(self) -> list[str]:
        """Reconcile the journal with the folders on disk: add folders it is missing, drop
        entries whose folder is gone, refresh hashes and re-link the ``prev_id`` chain.
        Returns the fixes applied (empty when nothing changed)."""
        if not self.root.exists():
            return []
        journal = self.journal()
        known = {e["index"]: e for e in journal["entries"]}
        on_disk: dict[int, str] = {}
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir() and p.name != "meta"):
            match = _INDEX.match(folder.name)
            if match is not None:
                on_disk[int(match.group(1))] = folder.name
        fixes: list[str] = []
        entries: list[JournalEntry] = []
        prev_id: str | None = None
        for index in sorted(on_disk):
            name = on_disk[index]
            migration = Migration(index, name, self.root / name)
            if not migration.sql_path.exists() or not migration.snapshot_path.exists():
                continue  # `check` reports the missing file as an error
            old = known.get(index)
            digest = migration.hash()
            if old is None:
                created = datetime.datetime.fromtimestamp(
                    migration.path.stat().st_mtime, datetime.UTC
                ).isoformat(timespec="seconds")
                fixes.append(f"{name}: added to the journal")
            else:
                created = old["created_at"]
                if old["name"] != name:
                    fixes.append(f"{name}: journal name updated")
                if old["hash"] != digest:
                    fixes.append(f"{name}: hash refreshed")
            entry = JournalEntry(index=index, name=name, hash=digest, created_at=created)
            if old is not None and old.get("custom"):
                entry["custom"] = True
            entries.append(entry)
            snapshot = migration.snapshot()
            if snapshot["prev_id"] != prev_id:
                snapshot["prev_id"] = prev_id
                migration.snapshot_path.write_text(
                    json.dumps(snapshot, indent=2) + "\n", encoding="utf-8"
                )
                fixes.append(f"{name}: prev_id re-linked")
            prev_id = snapshot["id"]
        for index, old in known.items():
            if index not in on_disk:
                fixes.append(f"{old['name']}: removed from the journal (folder is gone)")
        journal["entries"] = entries
        if fixes:
            self._save_journal(journal)
        return fixes

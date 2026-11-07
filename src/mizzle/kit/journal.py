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
from collections.abc import Mapping, Sequence
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
_FOLDER = re.compile(r"\d+_[\w-]+")


class JournalEntry(TypedDict):
    index: int
    name: str
    hash: str
    created_at: str
    # A hand-written (or templated) migration: its SQL is expected to change after generate.
    custom: NotRequired[bool]
    # A squashed migration: the names it replaces, so a database that applied them already
    # treats this migration as applied instead of failing "missing on disk".
    supersedes: NotRequired[list[str]]


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
    supersedes: tuple[str, ...] = ()  # names of the migrations a squash replaced

    @property
    def sql_path(self) -> Path:
        return self.path / "migration.sql"

    @property
    def snapshot_path(self) -> Path:
        return self.path / "snapshot.json"

    def sql(self) -> str:
        return self._read(self.sql_path).decode("utf-8")

    def hash(self) -> str:
        # A CRLF checkout hashes as the LF file did, so it does not look edited (K12).
        return hashlib.sha256(self._read(self.sql_path).replace(b"\r\n", b"\n")).hexdigest()

    def statements(self) -> list[Statement]:
        return parse_sql(self.sql())

    def snapshot(self) -> Snapshot:
        return cast(Snapshot, load_json(self.snapshot_path, self._read(self.snapshot_path)))

    def _read(self, path: Path) -> bytes:
        try:
            return path.read_bytes()
        except FileNotFoundError:  # a MigrationError, not a traceback (B61)
            raise MigrationError(
                f"{self.name}/{path.name} is missing; restore it from version control"
            ) from None


def _parse_entry(raw: object, source: Path) -> JournalEntry:
    if isinstance(raw, dict):
        entry = cast(dict[str, object], raw)  # JSON object: string keys
        index, name = entry.get("index"), entry.get("name")
        hash_, created_at = entry.get("hash"), entry.get("created_at")
        if (
            isinstance(index, int)
            and isinstance(name, str)
            # A plain folder name: squash and drop delete self.root / name, so "../x" from
            # a merged journal.json would delete a folder outside migrations/.
            and _FOLDER.fullmatch(name) is not None
            and isinstance(hash_, str)
            and isinstance(created_at, str)
        ):
            parsed = JournalEntry(index=index, name=name, hash=hash_, created_at=created_at)
            if entry.get("custom") is True:
                parsed["custom"] = True
            raw_supersedes = entry.get("supersedes")
            if isinstance(raw_supersedes, list):
                names = [s for s in cast("list[object]", raw_supersedes) if isinstance(s, str)]
                if names:
                    parsed["supersedes"] = names
            return parsed
    raise MigrationError(f"{source}: malformed journal entry {raw!r}")


def load_json(path: Path, data: bytes) -> object:
    """JSON from a migrations file; a broken one (a merge conflict left in it) is a clean
    MigrationError naming the file, not a traceback (C27)."""
    try:
        return cast(object, json.loads(data))
    except ValueError as exc:
        raise MigrationError(
            f"{path} is not valid JSON ({exc}); fix it (look for merge conflict markers)"
        ) from None


def _schema(snapshot: Snapshot) -> dict[str, object]:
    """A snapshot without its identity: what the schema is, not where it sits."""
    return {k: v for k, v in snapshot.items() if k not in ("id", "prev_id")}


def _hand_written_folder(
    snapshot: Snapshot, snapshots: Mapping[str, Snapshot], empty: Snapshot
) -> bool:
    """Whether a migration folder holds hand-written SQL, judged by its snapshot alone. A
    generated migration always changes the schema; a ``--custom`` one leaves it as its
    parent had it. An unknown parent counts as hand-written: squash then refuses rather
    than lose SQL."""
    claimed = snapshot["prev_id"]
    parent = snapshots.get(claimed) if claimed is not None else empty
    return parent is None or _schema(parent) == _schema(snapshot)


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
        raw = load_json(self.journal_path, self.journal_path.read_bytes())
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

    def migrations(self, *, unlisted_ok: bool = False) -> list[Migration]:
        # (K5) a folder merged without its journal entry was skipped silently.
        if not unlisted_ok and (unlisted := self.unlisted()):
            raise MigrationError(
                f"{', '.join(unlisted)} is not in the journal; run `mizzle check --fix` "
                "(or remove the folder)"
            )
        return [
            Migration(
                e["index"],
                e["name"],
                self.root / e["name"],
                bool(e.get("custom")),
                tuple(e.get("supersedes", ())),
            )
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
        supersedes: Sequence[str] = (),
    ) -> Migration:
        journal = self.journal()
        index = journal["entries"][-1]["index"] + 1 if journal["entries"] else 1
        folder = f"{index:04d}_{slugify(name)}"
        migration = Migration(index, folder, self.root / folder, custom, tuple(supersedes))
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
        if supersedes:
            entry["supersedes"] = list(supersedes)
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

    def unlisted(self) -> list[str]:
        """Migration folders on disk that the journal does not list (a branch merged in)."""
        if not self.root.exists():
            return []
        listed = {e["name"] for e in self.journal()["entries"]}
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and p.name != "meta" and p.name not in listed
        )

    def clear(self) -> None:
        """Remove the journal's migration folders and empty the journal. A folder the
        journal does not list is left alone: it may hold SQL nothing else has (B54)."""
        for entry in self.journal()["entries"]:
            shutil.rmtree(self.root / entry["name"], ignore_errors=True)
        self._save_journal(Journal(version=JOURNAL_VERSION, dialect=self.dialect, entries=[]))

    def repair(self) -> list[str]:
        """Reconcile the journal with the folders on disk: add folders it is missing, drop
        entries whose folder is gone and re-link the ``prev_id`` chain. Returns the fixes
        applied (empty when nothing changed).

        It never replaces an entry with a different folder that reuses its number: that
        branch is ambiguous and refusing keeps an applied migration from being swapped out.
        It never refreshes a recorded hash, so ``check`` keeps reporting SQL edited after
        generate. It re-links a snapshot only when the parent it was generated on has the
        same schema as the migration before it; otherwise the snapshot is stale (another
        branch) and the migration must be generated again.
        """
        if not self.root.exists():
            return []
        journal = self.journal()
        known = {e["index"]: e for e in journal["entries"]}
        on_disk: dict[int, list[str]] = {}
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir() and p.name != "meta"):
            match = _INDEX.match(folder.name)
            if match is not None:
                on_disk.setdefault(int(match.group(1)), []).append(folder.name)
        for index, names in sorted(on_disk.items()):
            if len(names) > 1:
                raise MigrationError(
                    f"two migrations share number {index:04d}: {', '.join(names)}; "
                    "rename one of them before --fix"
                )
        snapshots: dict[str, Snapshot] = {}
        for names in on_disk.values():
            path = self.root / names[0] / "snapshot.json"
            if path.exists():
                snap = cast(Snapshot, load_json(path, path.read_bytes()))
                snapshots[snap["id"]] = snap
        empty = empty_snapshot(self.dialect)
        no_snapshot: list[str] = []  # folders whose snapshot.json is gone
        fixes: list[str] = []
        entries: list[JournalEntry] = []
        relinked: list[tuple[Migration, Snapshot]] = []
        prev_id: str | None = None
        prev_name = "the empty schema"
        for index in sorted(on_disk):
            name = on_disk[index][0]
            migration = Migration(index, name, self.root / name)
            old = known.get(index)
            if old is not None and old["name"] != name:
                raise MigrationError(
                    f"journal entry {index:04d} is {old['name']!r} but the folder is {name!r}; "
                    "refusing to rewrite an entry that may be applied"
                )
            if not migration.snapshot_path.exists():
                no_snapshot.append(name)
            if not migration.sql_path.exists() or not migration.snapshot_path.exists():
                # Do not remove the entry: `check` reports the missing file, and dropping it
                # here would silently re-link the chain and hide a migration that may be
                # applied. Keep what the journal already knows.
                if old is not None:
                    entries.append(old)
                    fixes.append(f"{name}: left as is; migration.sql or snapshot.json is missing")
                    if migration.snapshot_path.exists():
                        prev_id, prev_name = migration.snapshot()["id"], name
                else:
                    fixes.append(f"{name}: ignored; migration.sql or snapshot.json is missing")
                continue  # `check` reports the missing file as an error
            if old is None:
                digest = migration.hash()
                created = datetime.datetime.fromtimestamp(
                    migration.path.stat().st_mtime, datetime.UTC
                ).isoformat(timespec="seconds")
                fixes.append(f"{name}: added to the journal")
            else:  # the generated hash stays: `check` must still see an edit
                digest, created = old["hash"], old["created_at"]
            entry = JournalEntry(index=index, name=name, hash=digest, created_at=created)
            snapshot = migration.snapshot()
            if old is not None and old.get("custom"):
                entry["custom"] = True
            elif old is None and _hand_written_folder(snapshot, snapshots, empty):
                # A folder the journal lost (a merged branch) may be a --custom migration.
                # Recording it as generated, with its current hash, hid its SQL from squash,
                # which then deleted it (B53).
                entry["custom"] = True
                fixes[-1] += " as a custom migration"
            if old is not None and (old_supersedes := old.get("supersedes")):
                entry["supersedes"] = old_supersedes
            entries.append(entry)
            if (claimed := snapshot["prev_id"]) != prev_id:
                parent = snapshots.get(claimed) if claimed is not None else empty
                expected = snapshots[prev_id] if prev_id is not None else empty
                if parent is None and no_snapshot:
                    # The parent is unknown because a snapshot file is gone, not because of a
                    # branch: deleting this migration would be the wrong fix (B58).
                    raise MigrationError(
                        f"snapshot.json is missing in {', '.join(no_snapshot)}, so the chain "
                        f"before {name} cannot be checked; restore it from version control "
                        "and run `mizzle check --fix` again"
                    )
                if parent is None or _schema(parent) != _schema(expected):
                    raise MigrationError(
                        f"{name} was generated on top of another migration than {prev_name} "
                        "(two branches?), so its snapshot lacks the changes before it and the "
                        f"next generate would repeat them. Delete {name} and run `mizzle "
                        "generate` again on the merged history (keep any SQL you wrote by hand)"
                    )
                snapshot["prev_id"] = prev_id
                relinked.append((migration, snapshot))
                fixes.append(f"{name}: prev_id re-linked")
            prev_id, prev_name = snapshot["id"], name
        for index, old in known.items():
            if index not in on_disk:
                fixes.append(f"{old['name']}: dropped from the journal (folder is gone)")
        for migration, snapshot in relinked:  # only once nothing was refused
            migration.snapshot_path.write_text(
                json.dumps(snapshot, indent=2) + "\n", encoding="utf-8"
            )
        journal["entries"] = entries
        if fixes:
            self._save_journal(journal)
        return fixes

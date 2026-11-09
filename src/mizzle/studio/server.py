"""Studio backend: a local JSON API over the live database plus the built React UI.

Security model (Studio can read and change data):
- binds to 127.0.0.1 by default, and rejects other Host headers (DNS rebinding);
- every /api request needs ``Authorization: Bearer <token>``; the random token is only
  in the URL fragment that ``mizzle studio`` prints, which browsers never send to servers;
- for remote use, bind another host with ``--host`` and pass ``--auth USER:PASSWORD``
  (HTTP Basic) and/or ``--token`` / ``MIZZLE_STUDIO_TOKEN``; mizzle refuses to listen on a
  non-loopback host with no credentials. A non-loopback bind drops the Host check (clients
  reach it by an address it cannot know), which Basic auth covers;
- no CORS headers, so other websites cannot call the API from a browser;
- ``--read-only`` blocks every write, and raw SQL runs in a read-only transaction; it
  refuses to start as a role with server-side abilities (superuser and friends), which a
  READ ONLY transaction does not stop;
- table and column names are checked against the introspected schema and quoted;
  values are always bind parameters.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import datetime
import enum
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import time
import webbrowser
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, Protocol, cast, override
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.convertors import PathConvertor, register_url_convertor
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from mizzle.core.errors import DatabaseError, MigrationError, MizzleError
from mizzle.core.query.base import Row
from mizzle.core.snapshot import ColumnSnap, EnumSnap, TableSnap
from mizzle.dialects.base import Connection, Dialect, Driver
from mizzle.dialects.postgres.ddl import SERIAL_TYPES, literal, quote
from mizzle.kit.config import Config
from mizzle.kit.migrate import apply_statements
from mizzle.kit.sqltext import first_token, split_statements
from mizzle.studio.ddl import build_steps, statement_rows

STATIC_DIR = Path(__file__).parent / "static"
MAX_SQL_ROWS = 1000
MAX_PAGE = 500
MAX_HISTORY = 100
_COMPARE = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}

# Failed-login throttle (SS4, B7, B34): a client address with this many wrong credentials
# within the window gets 429 for every API request, a correct one included, until the oldest
# failure ages out. That caps guessing at LOGIN_MAX_FAILURES per window per address. It is per
# client, so an attacker only blocks themselves, never the real user. At most
# LOGIN_MAX_CLIENTS addresses are tracked (oldest dropped first), so a spray from many
# addresses cannot grow the table without limit.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 60.0
LOGIN_MAX_CLIENTS = 1024

# Read-only mode's control is the role check in ``privileged_roles``: a role with no
# server-side abilities, in a READ ONLY transaction. The lists below are defence in depth on
# top of that. A text blocklist cannot be complete (``U&"..."`` names, dynamic SQL), so it
# is never the reason read-only holds.
#
# Statements that change the transaction, the session or the process rather than the data.
# A batch must not be able to leave the READ ONLY transaction it runs in (``commit; insert
# ...`` was a working write). Matched on the first token after comments, as one word:
# ``START``/``PREPARE`` alone, since a comment may sit between ``START`` and ``TRANSACTION``
# (``start/**/transaction read write`` made the open transaction writable, B65, B66).
# CLUSTER, REINDEX and ANALYZE are allowed in a READ ONLY transaction but rewrite tables or
# take exclusive locks (B70).
_READ_ONLY_DENIED = re.compile(
    r"(?:BEGIN|START|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE"
    r"|SET|RESET|DISCARD|LOCK|PREPARE|EXECUTE|NOTIFY|LISTEN|UNLISTEN"
    r"|COPY|DO|CHECKPOINT|LOAD|CLUSTER|REINDEX|ANALYZE|ANALYSE|VACUUM|REFRESH)\b",
    re.IGNORECASE,
)
# Server-side functions that read or change things outside the transaction, which READ ONLY
# does not stop, plus the ways found to spell or call them indirectly.
_READ_ONLY_DENIED_CALLS = re.compile(
    r"\b(?:pg_read_file|pg_read_binary_file|pg_ls_\w+|pg_stat_file|pg_terminate_backend"
    r"|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_switch_wal|pg_promote"
    r"|pg_notify|pg_\w*replication_slot\w*|pg_logical_\w+|pg_log_backend_memory_contexts"
    r"|lo_import|lo_export|dblink|dblink_exec|pg_file_write|pg_file_rename|pg_file_unlink"
    r"|query_to_xml\w*"
    # These run a query given as text, so any blocked call hides inside the string (C2).
    r"|ts_stat|ts_rewrite|cursor_to_xml\w*)\b"
    r"|\bU&\"",
    re.IGNORECASE,
)
# S8-1: every function, catalog, type and operator Studio's own SQL uses is named in
# pg_catalog.
# Unqualified, a planted public.to_jsonb(public.t) (an exact type match) or a public ``=``
# won over the built-in wherever the role's path searched public first, and ran as the
# Studio user. A column's own type and its operators are the user's, and stay as they are.
_EQ = "OPERATOR(pg_catalog.=)"
# A role that is, or can become, one of these can act on the server from inside a READ ONLY
# transaction, so read-only Studio refuses to run as it (B18).
_PRIVILEGED_ROLES = (
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_execute_server_program",
    "pg_signal_backend",
    "pg_maintain",  # PostgreSQL 17+: CLUSTER/REINDEX on any table
)
# ``session_user`` as well as ``current_user``: a login role with ``ALTER ROLE ... SET role``
# starts as the plain role but can ``set_config('role', 'none', false)`` back to itself
# (B69). Every role the login role can SET ROLE to is in its MEMBER chain.
_PRIVILEGED_SQL = (
    "SELECT rolname, rolsuper, rolreplication FROM pg_catalog.pg_roles "
    "WHERE (pg_catalog.pg_has_role(session_user, oid, 'MEMBER') "
    "OR pg_catalog.pg_has_role(current_user, oid, 'MEMBER')) AND (rolsuper OR rolreplication "
    f"OR rolname {_EQ} ANY (ARRAY[{', '.join(repr(r) for r in _PRIVILEGED_ROLES)}]"
    "::pg_catalog.name[])) "
    "ORDER BY rolname"
)

# Server-side functions PostgreSQL does not grant to PUBLIC. A direct GRANT EXECUTE on one
# gives the abilities of the server roles above without their membership (C2).
_SERVER_FUNCTIONS = (
    "pg_read_file",
    "pg_read_binary_file",
    "pg_stat_file",
    "pg_ls_dir",
    "pg_ls_logdir",
    "pg_ls_waldir",
    "pg_ls_tmpdir",
    "pg_ls_archive_statusdir",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_promote",
    "pg_switch_wal",
    "lo_import",
    "lo_export",
)
_SERVER_FUNCTIONS_SQL = (
    "SELECT DISTINCT p.proname::pg_catalog.text FROM pg_catalog.pg_proc p "
    f"JOIN pg_catalog.pg_namespace n ON n.oid {_EQ} p.pronamespace "
    f"WHERE n.nspname {_EQ} 'pg_catalog' AND p.proname {_EQ} ANY "
    f"(ARRAY[{', '.join(repr(f) for f in _SERVER_FUNCTIONS)}]::pg_catalog.name[]) "
    "AND (pg_catalog.has_function_privilege(session_user, p.oid, 'EXECUTE') "
    "OR pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')) ORDER BY 1"
)

log = logging.getLogger("mizzle")

_NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>mizzle studio</title>
<p>The Studio UI is not built.
Run <code>pnpm install && pnpm build</code> in <code>studio-ui/</code>.
Release packages include it already.</p>"""

type JSON = bool | int | float | str | list[JSON] | dict[str, JSON] | None

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


class StudioError(Exception):
    status: int

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Studio:
    dialect: Dialect
    url: str
    schemas: list[str]
    read_only: bool
    driver: Driver | None = None
    # (schema, name) -> table. Not the snapshot's "schema.name" key: public."x.y" and
    # "public.x".y share it, and one table's rows were read and deleted for the other (S6-1).
    tables: dict[tuple[str, str], TableSnap] | None = None
    enums: dict[str, EnumSnap] = field(default_factory=dict)
    # (schema, table) -> column -> the type it behaves as (domains resolved by the server)
    bases: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    # (schema, table) -> its composite columns (and arrays of them): read-only in the grid
    composites: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    # (schema, table) -> its columns of a type no built-in one (an extension's hstore, ...):
    # the grid reads and writes them as their text (S7-4)
    texts: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    history: list[dict[str, object]] = field(default_factory=list)
    saved: dict[str, str] = field(default_factory=dict)
    state_path: Path | None = None

    def pool(self) -> Driver:
        if self.driver is None:
            raise StudioError("not connected", 503)
        return self.driver

    async def load_snapshot(self, refresh: bool = False) -> dict[tuple[str, str], TableSnap]:
        if self.tables is None or refresh:
            async with self.pool().acquire() as conn, conn.transaction(readonly=True):
                # One schema at a time: within one, the "schema.name" key cannot collide.
                snapshots = [await self.dialect.introspect(conn, [s]) for s in self.schemas]
                rows = await conn.fetch_dicts(_BASE_TYPES, [self.schemas])
            self.tables = {
                (t["schema"], t["name"]): t for s in snapshots for t in s["tables"].values()
            }
            self.enums = {k: e for s in snapshots for k, e in s["enums"].items()}
            self.bases, self.composites, self.texts = {}, {}, {}
            for row in rows:
                k, column = (str(row["schema"]), str(row["table"])), str(row["name"])
                self.bases.setdefault(k, {})[column] = str(row["base"])
                if row["composite"]:
                    self.composites.setdefault(k, []).append(column)
                if row["extension"]:
                    self.texts.setdefault(k, []).append(column)
        return self.tables

    async def table(
        self, schema: str, name: str, *, fresh: bool = False
    ) -> tuple[TableSnap, dict[str, str]]:
        """The table and each column's base type (``_BASE_TYPES``). ``fresh`` re-reads the
        schema: a write built from a stale snapshot matched rows by a dropped key and cast
        values to a column's old type (S4-3)."""
        k = (schema, name)
        table = (await self.load_snapshot(refresh=fresh)).get(k)
        if table is None and not fresh:
            table = (await self.load_snapshot(refresh=True)).get(k)
        if table is None:
            raise StudioError(f"unknown table {schema}.{name}", 404)
        return table, {
            c: self.bases.get(k, {}).get(c, t["type"]) for c, t in table["columns"].items()
        }

    async def checked_table(self, schema: str, name: str) -> tuple[TableSnap, dict[str, str]]:
        """For a read: the cached table, re-read when its columns changed outside Studio. A
        stale type decoded a column changed to jsonb as JSON, and saving that wrote numbers
        back as strings (S5-4)."""
        table, base = await self.table(schema, name)
        async with self.pool().acquire() as conn, conn.transaction(readonly=True):
            rows = await conn.fetch_dicts(f"{_BASE_TYPES} AND c.relname {_EQ} $2", [[schema], name])
        if {str(r["name"]): str(r["base"]) for r in rows} != self.bases.get((schema, name), {}):
            return await self.table(schema, name, fresh=True)
        return table, base

    def read_only_columns(self, table: TableSnap) -> list[str]:
        """Composite columns: the grid shows them, but does not write them (S5-2)."""
        composites = self.composites.get((table["schema"], table["name"]), [])
        return [c for c in table["columns"] if c in composites]

    def text_columns(self, table: TableSnap) -> list[str]:
        return self.texts.get((table["schema"], table["name"]), [])

    # -- query history and saved queries ----------------------------------------------

    def load_state(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            data = cast(object, json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            log.warning("ignoring unreadable Studio state %s", self.state_path)
            return
        if not isinstance(data, dict):
            return
        state = cast(dict[str, object], data)
        history = state.get("history")
        # S15: a read-only Studio does not see the read-write history (it keeps its own,
        # in memory only).
        if isinstance(history, list) and not self.read_only:
            self.history = [h for h in cast(list[object], history) if isinstance(h, dict)]
            del self.history[:-MAX_HISTORY]
        saved = state.get("saved")
        if isinstance(saved, dict):
            self.saved = {
                k: v for k, v in cast(dict[str, object], saved).items() if isinstance(v, str)
            }

    def save_state(self) -> None:
        if self.state_path is None or self.read_only:  # S15: read-only never writes the file
            return
        try:
            # The history holds whatever SQL was run, passwords included (C6): keep the file
            # private to this user, and the folder out of version control.
            folder = self.state_path.parent
            folder.mkdir(parents=True, exist_ok=True)
            ignore = folder / ".gitignore"
            if not ignore.exists():
                ignore.write_text("# mizzle Studio state: query history may hold secrets\n*\n")
            payload = json.dumps({"history": self.history, "saved": self.saved}, indent=2)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            with os.fdopen(os.open(self.state_path, flags, 0o600), "w", encoding="utf-8") as f:
                f.write(payload + "\n")
            self.state_path.chmod(0o600)  # a file written by an older version kept its mode
        except OSError:
            log.warning("could not write Studio state %s", self.state_path)

    def record_history(self, sql: str, *, ok: bool) -> None:
        self.history.append(
            {
                "sql": sql,
                "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
                "ok": ok,
            }
        )
        del self.history[:-MAX_HISTORY]
        self.save_state()

    def save_query(self, name: str, sql: str) -> None:
        self.saved[name] = sql
        self.save_state()

    def delete_query(self, name: str) -> bool:
        existed = self.saved.pop(name, None) is not None
        if existed:
            self.save_state()
        return existed


# -- SQL helpers ----------------------------------------------------------------------


def _column(table: TableSnap, name: object) -> ColumnSnap:
    if not isinstance(name, str) or name not in table["columns"]:
        raise StudioError(f"unknown column {name!r} in {table['name']}")
    return table["columns"][name]


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StudioError(f"{what} must be a JSON object")
    return cast(dict[str, object], value)  # JSON object keys are always strings


def _array(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise StudioError(f"{what} must be a JSON list")
    return cast(list[object], value)


# A size or precision modifier, or a quoted name (kept as it is).
_TYPMOD = re.compile(r'"(?:[^"]|"")*"|\(-?\d+(?:\s*,\s*-?\d+)?\)')
# Without its modifier, character means character(1) and bit means bit(1).
_UNBOUNDED = {"character": "bpchar", "bit": "bit varying"}


# The interval type as format_type() prints it: interval, interval(3), interval day to
# second(3), and arrays of them. Not a type that merely starts with "interval" (S5-3).
_INTERVAL = re.compile(r"interval(?:\(\d+\)| [a-z ]+(?:\(\d+\))?)?((?:\[\])*)")


def _relation(table: TableSnap) -> str:
    """Always schema-qualified: ddl.qualified leaves public names bare, and a bare name
    resolves through search_path, so a "$user" schema's same-named table was used (S5-1)."""
    return f"{quote(table['schema'])}.{quote(table['name'])}"


def _cast_type(column: ColumnSnap, base: str) -> str:
    """The column's type without its modifier. An explicit CAST to varchar(5) or
    numeric(5,2) cuts or rounds the value silently, so an insert stored "qwert" and a key
    filter matched the wrong row (C4). Without it, the column's own assignment and
    comparison check the value."""
    # Introspected types come from format_type(), which already quotes when needed.
    full = SERIAL_TYPES.get(column["type"], column["type"])
    bare = _TYPMOD.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", full)
    # Array suffixes only: a "[]" inside a quoted name ("a[]b") is part of the name (S7-6).
    arrays = 0
    while bare.endswith("[]"):
        bare, arrays = bare[:-2].rstrip(), arrays + 1
    bare = bare.strip()
    if interval := _INTERVAL.fullmatch(base):  # "interval year to month" cuts the days off
        return "interval" + interval.group(1)
    return _UNBOUNDED.get(bare, bare) + "[]" * arrays


def _as_text(value: object) -> str | None:
    """Values travel as text and are CAST server-side, so every type uses its input syntax."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


# Each column's type as it behaves: a domain resolved to its base type, through domains over
# domains and arrays of domains (``d[]`` -> ``jsonb[]``), by the server (S5-6). Every
# type-dependent rule (exact text, CSV signs, key casts, sorting) reads it, so a domain acts
# like its base type everywhere (S7, S4-1, S4-5, S4-6).
_BASE_TYPES = f"""
WITH RECURSIVE r(rel, name, oid, typmod, dims) AS (
  SELECT a.attrelid, a.attname::pg_catalog.text, a.atttypid, a.atttypmod, 0
  FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid {_EQ} a.attrelid
  JOIN pg_catalog.pg_namespace n ON n.oid {_EQ} c.relnamespace
  WHERE n.nspname {_EQ} ANY($1::pg_catalog.text[])
    AND c.relkind {_EQ} ANY('{{r,p}}'::pg_catalog."char"[])
    AND a.attnum OPERATOR(pg_catalog.>) 0 AND NOT a.attisdropped
  UNION ALL
  SELECT r.rel, r.name,
         CASE WHEN t.typtype {_EQ} 'd' THEN t.typbasetype ELSE t.typelem END,
         CASE WHEN t.typtype {_EQ} 'd' THEN t.typtypmod ELSE r.typmod END,
         r.dims OPERATOR(pg_catalog.+) CASE WHEN t.typtype {_EQ} 'd' THEN 0 ELSE 1 END
  FROM r JOIN pg_catalog.pg_type t ON t.oid {_EQ} r.oid
  WHERE t.typtype {_EQ} 'd' OR t.typcategory {_EQ} 'A'
)
SELECT n.nspname AS schema, c.relname AS table, r.name,
       pg_catalog.format_type(r.oid, r.typmod)
         OPERATOR(pg_catalog.||) pg_catalog.repeat('[]', r.dims) AS base,
       t.typtype {_EQ} 'c' AS composite,
       -- a base type no built-in one (16384: FirstNormalObjectId), an extension's
       t.typtype {_EQ} 'b' AND t.oid OPERATOR(pg_catalog.>=) 16384 AS extension
FROM r JOIN pg_catalog.pg_type t ON t.oid {_EQ} r.oid
JOIN pg_catalog.pg_class c ON c.oid {_EQ} r.rel
JOIN pg_catalog.pg_namespace n ON n.oid {_EQ} c.relnamespace
WHERE t.typtype OPERATOR(pg_catalog.<>) 'd' AND t.typcategory OPERATOR(pg_catalog.<>) 'A'
"""

# A column type that is JSON itself (after domains), not something to_jsonb() turned into JSON.
_JSON_TYPE = re.compile(r"jsonb?")


def _param(params: list[object], column: ColumnSnap, value: object, base: str) -> str:
    if not _JSON_TYPE.fullmatch(base) and isinstance(value, dict | list):
        # S4-8: an array (or a composite in a filter) as the grid has it (to_jsonb's JSON).
        # Its text form is not the type's input syntax ("[1, 2]"), so the server's own
        # JSON-to-type conversion reads it back.
        params.append(json.dumps(value))
        return (
            "(SELECT x.v FROM pg_catalog.jsonb_to_record(pg_catalog.jsonb_build_object("
            f"'v', ${len(params)}::pg_catalog.text::pg_catalog.jsonb)) "
            f"AS x(v {_cast_type(column, base)}))"
        )
    params.append(_as_text(value))
    return f"CAST(${len(params)}::pg_catalog.text AS {_cast_type(column, base)})"


def _where(
    table: TableSnap, base: Mapping[str, str], filters: list[object], params: list[object]
) -> str:
    parts: list[str] = []
    for item in filters:
        f = _object(item, "filter")
        column = _column(table, f.get("column"))
        ref = f"t.{quote(column['name'])}"
        op = f.get("op")
        if isinstance(op, str) and op in _COMPARE:
            value = _param(params, column, f.get("value"), base[column["name"]])
            parts.append(f"{ref} {_COMPARE[op]} {value}")
        elif op in ("like", "ilike"):
            params.append(_as_text(f.get("value")) or "")
            like = "~~" if op == "like" else "~~*"
            parts.append(f"{ref}::pg_catalog.text OPERATOR(pg_catalog.{like}) ${len(params)}")
        elif op == "is_null":
            parts.append(f"{ref} IS NULL")
        elif op == "is_not_null":
            parts.append(f"{ref} IS NOT NULL")
        else:
            raise StudioError(f"unknown filter operator {op!r}")
    return f" WHERE {' AND '.join(parts)}" if parts else ""


def _key_match(
    table: TableSnap, base: Mapping[str, str], key_values: object, params: list[object]
) -> str:
    pk = table["primary_key"]
    if pk is None:
        raise StudioError(f"{table['name']} has no primary key; change its rows with SQL")
    values = _object(key_values, "key")
    if set(values) != set(pk["columns"]):
        raise StudioError(f"key must give exactly the primary key columns {pk['columns']}")
    return " AND ".join(
        f"t.{quote(c)} = {_param(params, _column(table, c), values[c], base[c])}"
        for c in pk["columns"]
    )


# S4-9: built-in types with no default ordering (and arrays of them).
# ponytail: a fixed list; a composite with such a field still fails in the database (a 400).
_UNSORTABLE = re.compile(
    r"(?:json|xml|point|line|lseg|box|path|polygon|circle|jsonpath|xid|cid|aclitem"
    r"|txid_snapshot|pg_snapshot)(?:\[\])*"
)


def _unsortable(base: Mapping[str, str]) -> list[str]:
    return [name for name, t in base.items() if _UNSORTABLE.fullmatch(t)]


def _order_sql(table: TableSnap, base: Mapping[str, str], q: Mapping[str, str]) -> str:
    order = q.get("order")
    if order:
        column = _column(table, order)
        if _UNSORTABLE.fullmatch(base[column["name"]]):
            raise StudioError(f"cannot sort by {column['name']}: {column['type']} has no order")
        direction = "DESC" if q.get("dir") == "desc" else "ASC"
        sort = [f"t.{quote(column['name'])} {direction}"]
    else:
        sort = []
    # S13: the key (or ctid) breaks ties, or OFFSET paging repeats and skips rows. A ctid
    # is unique per partition or child table only, so tableoid comes first (S4-7).
    pk = table["primary_key"]
    sort += [f"t.{quote(c)}" for c in pk["columns"]] if pk else ["t.tableoid", "t.ctid"]
    return " ORDER BY " + ", ".join(sort)


# A cell a spreadsheet would evaluate as a formula. CSV has no types, so the only defence is
# to make the text unambiguous: a leading apostrophe is the conventional "this is text" mark
# (Excel, LibreOffice and Sheets all show the rest of the cell and keep it inert).
_FORMULA_LEAD = ("=", "+", "-", "@")
# Types whose text form is a number, interval, date or time: a leading "-" is a sign, not a
# formula, and prefixing it corrupted the value (B19). to_jsonb() turns intervals, money and
# dates into JSON strings, so the column type, not the Python type, tells them apart.
# JSON columns are not here: they arrive as their exact text (``_EXACT_TEXT``), and only that
# text is safe; a JSON string decoded to a str is not (S4-1).
# The whole type name must match (``fullmatch``): a domain ``"date".note`` over text starts
# with ``date`` too (B73). Only modifiers may follow: ``(3)``, ``with time zone``,
# ``day to second``.
_SIGNED_TYPE = re.compile(
    r"(?:smallint|integer|bigint|(?:small|big)?serial|numeric|decimal|real|double precision"
    r"|money|interval|date|time|timestamp)\b[a-z0-9 (),]*",
    re.IGNORECASE,
)


# What a signed value's text can hold: a sign or "@" (verbose interval), then letters,
# digits, spaces and . , : $ + - and currency signs: "-5", "-1 days +02:00", "-$1.00",
# "-infinity". No ( | ! " ' = ; & that a formula needs to call a function or DDE. The
# column type comes from a parsed type name, so the text must look inert too: a text
# column misread as numeric cannot pass "-2+3+cmd|' /C calc'!A0".
_INERT_SIGNED = re.compile(r"[-+@][\w .,:$+\-€£¥₹]*")


def _csv_safe(text: str) -> str:
    # The spreadsheet skips leading whitespace, so " =1+1" and "\n=cmd" are formulas too.
    risky = text.startswith(("\t", "\r")) or text.lstrip().startswith(_FORMULA_LEAD)
    return f"'{text}" if risky else text


def _cell(value: object, base: str) -> str:
    """One CSV cell. Only text is neutralised: a number, a string from a numeric,
    interval, date or time column, or a column's exact text, cannot be a formula."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value)  # starts with { or [, never a formula
    if isinstance(value, str):
        signed = _SIGNED_TYPE.fullmatch(base) or _EXACT_TEXT.fullmatch(base)
        return value if signed and _INERT_SIGNED.fullmatch(value) else _csv_safe(value)
    return str(value)


def jsonable(value: object) -> JSON:
    match value:
        case None | bool() | str():
            return value
        case int():  # a browser would round a bigger one (C9)
            return value if abs(value) <= _EXACT_INT else str(value)
        case float():
            return value if value == value and abs(value) != float("inf") else str(value)
        case dict():
            return {str(k): jsonable(v) for k, v in cast(dict[object, object], value).items()}
        case list() | tuple():
            return [jsonable(v) for v in cast(list[object] | tuple[object, ...], value)]
        case bytes():
            return "\\x" + value.hex()
        case datetime.datetime() | datetime.date() | datetime.time():
            return value.isoformat()
        case enum.Enum():
            return jsonable(cast(object, value.value))
        case _:  # Decimal, UUID, timedelta, IP addresses, ...
            return str(value)


class _Named(Protocol):
    """A driver row that knows its column names (asyncpg's Record), duplicates included."""

    def keys(self) -> Iterable[str]: ...


# A browser reads a JSON number as a double: past 2**53 an integer is no longer exact.
_EXACT_INT = 2**53
# Each row as the server's JSON text, decoded by ``_row_json``.
_ROW = "pg_catalog.to_jsonb(t.*)::pg_catalog.text"
# Columns sent as their exact text: a JSON value or a numeric decoded into the browser's
# doubles lost digits (1.50 -> 1.5) and turned big numbers inside JSON into strings, which
# an edit then wrote back (S1, S3).
_EXACT_TEXT = re.compile(r"(?:jsonb?|numeric)(?:\([^)]*\))?(?:\[\])*")
_FLOAT = re.compile(r"(?:real|double precision)(?:\[\])*")
# jsonb_build_object takes at most 100 arguments: 50 columns per call.
_PAIRS = 50


def _row_sql(base: Mapping[str, str], texts: Collection[str] = ()) -> str:
    """``_ROW`` with the json, jsonb and numeric columns (and arrays of them, and domains
    over them: S4-5) as text. So are the values to_jsonb changes: an array whose bounds do
    not start at 1 ("[0:1]={5,6}" became [5, 6]) and a float's -0 (became 0), which saving
    the cell unchanged then wrote back (S6-4). ``texts`` are columns of an extension's type
    (hstore): to_jsonb's JSON for them is not their input syntax, so a save failed (S7-4)."""
    pairs: list[str] = []
    for name, t in base.items():
        label, ref = literal(name), f"t.{quote(name)}::pg_catalog.text"
        if _EXACT_TEXT.fullmatch(t) or name in texts:
            pairs.append(f"{label}, {ref}")
        elif t.endswith("[]") or _FLOAT.fullmatch(t):
            changed = [f"{ref} OPERATOR(pg_catalog.~~) '[%'"] if t.endswith("[]") else []
            if _FLOAT.fullmatch(t):
                changed.append(f"{ref} OPERATOR(pg_catalog.~) '(^|[{{,])-0($|[,}}])'")
            pairs.append(f"{label}, CASE WHEN {' OR '.join(changed)} THEN {ref} END")
    if not pairs:
        return _ROW
    # A NULL here is stripped, leaving to_jsonb's own value (NULL, or the plain array).
    parts = [
        "pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object("
        + ", ".join(pairs[i : i + _PAIRS])
        + "))"
        for i in range(0, len(pairs), _PAIRS)
    ]
    return (
        "("
        + " OPERATOR(pg_catalog.||) ".join(["pg_catalog.to_jsonb(t.*)", *parts])
        + ")::pg_catalog.text"
    )


def _exact_int(text: str) -> int | str:
    value = int(text)
    return value if abs(value) <= _EXACT_INT else text


def _exact_float(text: str) -> float | str:
    value = float(text)
    return value if math.isfinite(value) and Decimal(repr(value)) == Decimal(text) else text


def _row_json(text: str) -> JSON:
    """A ``to_jsonb(t.*)::text`` row, with every number a double cannot hold kept as its
    exact text. Decoding through ``float`` showed ``numeric(40,20)`` values rounded (B71)."""
    return cast(JSON, json.loads(text, parse_int=_exact_int, parse_float=_exact_float))


# -- handlers -------------------------------------------------------------------------


async def _body(request: Request) -> dict[str, object]:
    try:
        data = cast(object, await request.json())
    except ValueError as exc:
        raise StudioError("request body must be JSON") from exc
    return _object(data, "request body")


def _writable(studio: Studio) -> None:
    if studio.read_only:
        raise StudioError("Studio is in read-only mode", 403)


async def _table(
    studio: Studio, request: Request, *, fresh: bool = False
) -> tuple[TableSnap, dict[str, str]]:
    """The table named in ``/api/tables/<schema>/<table>/<action>``. Read from the raw
    path: the decoded one turns a "/" inside a name (sent as %2F) into a separator (S5)."""
    raw = request.scope.get("raw_path")
    path = raw.decode("ascii") if isinstance(raw, bytes) else str(request.url.path)
    parts = path.split("/")
    if len(parts) < 6:
        raise StudioError("expected /api/tables/<schema>/<table>/...", 404)
    schema, name = unquote(parts[-3]), unquote(parts[-2])
    if fresh:
        return await studio.table(schema, name, fresh=True)
    return await studio.checked_table(schema, name)


async def meta(studio: Studio, _request: Request) -> Response:
    return JSONResponse({"read_only": studio.read_only, "dialect": studio.dialect.name})


async def tables(studio: Studio, _request: Request) -> Response:
    loaded = await studio.load_snapshot(refresh=True)
    # S4-9: which columns the grid may offer to sort by.
    listed: list[dict[str, object]] = []
    for t in loaded.values():
        _, base = await studio.table(t["schema"], t["name"])
        listed.append(
            {**t, "unsortable": _unsortable(base), "read_only": studio.read_only_columns(t)}
        )
    return JSONResponse({"tables": listed, "enums": studio.enums})


async def relationships(studio: Studio, _request: Request) -> Response:
    """Nodes and foreign-key edges for the ER diagram: enough to draw the schema without
    shipping the full snapshots."""
    loaded = await studio.load_snapshot(refresh=True)
    nodes = [
        {"schema": t["schema"], "name": t["name"], "columns": list(t["columns"])}
        for t in loaded.values()
    ]
    edges: list[dict[str, object]] = []
    for table in loaded.values():
        for fk in table["foreign_keys"].values():
            edges.append(
                {
                    "name": fk["name"],
                    "from_schema": table["schema"],
                    "from_table": table["name"],
                    "from_columns": fk["columns"],
                    "to_schema": fk["ref_schema"],
                    "to_table": fk["ref_table"],
                    "to_columns": fk["ref_columns"],
                }
            )
    return JSONResponse({"tables": nodes, "relationships": edges})


async def list_rows(studio: Studio, request: Request) -> Response:
    table, base = await _table(studio, request)
    q = request.query_params
    try:
        limit = min(int(q.get("limit", "50")), MAX_PAGE)
        offset = int(q.get("offset", "0"))
        filters = _array(cast(object, json.loads(q.get("filters", "[]"))), "filters")
    except ValueError as exc:
        raise StudioError(f"bad query parameters: {exc}") from exc
    if limit < 0 or offset < 0:
        raise StudioError("limit and offset must be >= 0")
    params: list[object] = []
    where = _where(table, base, filters, params)
    order_sql = _order_sql(table, base, q)
    source = f"{_relation(table)} AS t"
    # S7-1: READ ONLY, also in a read-write Studio. Reading a table runs code of the
    # database's (an RLS policy, a type's output function): it must not be able to write.
    async with studio.pool().acquire() as conn, conn.transaction(readonly=True):
        [total] = await conn.fetch(f"SELECT pg_catalog.count(*) FROM {source}{where}", params)
        rows = await conn.fetch(
            f"SELECT {_row_sql(base, studio.text_columns(table))} FROM {source}{where}{order_sql}"
            f" LIMIT {limit} OFFSET {offset}",
            params,
        )
    return JSONResponse({"total": total[0], "rows": [_row_json(cast(str, r[0])) for r in rows]})


def _download_name(schema: str, table: str, fmt: str) -> str:
    """``Content-Disposition`` for an arbitrary table name.

    Names come from the database, so they can be non-Latin-1 (which used to raise
    ``UnicodeEncodeError`` inside the header) or contain ``"`` (which broke the quoting).
    RFC 5987 ``filename*`` carries the real name; ``filename`` keeps an ASCII fallback.
    """
    name = f"{schema}.{table}.{fmt}"
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    ascii_name = re.sub(r"[\x00-\x1f\x7f]", "_", ascii_name)  # S5-8: no CR/LF in a header
    from urllib.parse import quote as url_quote

    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{url_quote(name, safe='')}"


async def export_rows(studio: Studio, request: Request) -> Response:
    """Download every matching row as CSV or JSON (not just the current page).

    The rows are streamed from a server-side cursor, so a large export does not have to fit
    in memory. A query error before the first byte is a 400; a later one cuts the
    connection, so the download fails instead of ending early.
    """
    table, base = await _table(studio, request)
    q = request.query_params
    fmt = q.get("format", "csv").lower()
    if fmt not in ("csv", "json"):
        raise StudioError("format must be 'csv' or 'json'")
    try:
        filters = _array(cast(object, json.loads(q.get("filters", "[]"))), "filters")
    except ValueError as exc:
        raise StudioError(f"bad query parameters: {exc}") from exc
    params: list[object] = []
    where = _where(table, base, filters, params)
    order_sql = _order_sql(table, base, q)
    source = f"{_relation(table)} AS t"
    row = (
        _row_sql(base, studio.text_columns(table)) if fmt == "csv" else _ROW
    )  # JSON export: the server's own JSON
    sql = f"SELECT {row} FROM {source}{where}{order_sql}"
    headers = {
        "Content-Disposition": _download_name(table["schema"], table["name"], fmt),
        "Cache-Control": "no-store",
    }
    columns = list(table["columns"])
    types = [base[c] for c in columns]
    batch_size = 1000

    def chunk(batch: Sequence[Row], first: bool) -> bytes:
        if fmt == "csv":
            return _csv_chunk(columns, types, batch)
        # The server's own JSON text: exact numbers, nothing re-encoded.
        rows = [cast(str, row[0]).encode() for row in batch]
        return (b"" if first or not rows else b",") + b",".join(rows)

    async def body() -> AsyncGenerator[bytes]:
        async with studio.pool().acquire() as conn, conn.transaction(readonly=True):
            async with aclosing(conn.cursor(sql, params, batch_size)) as batches:
                # The first batch is read before any byte is sent, so a query that fails
                # (a bad filter value) is a 400, not a 200 with a cut-off file (C7).
                none: list[Row] = []
                batch = await anext(batches, none)
                if fmt == "csv":
                    buffer = io.StringIO()
                    csv.writer(buffer).writerow([_csv_safe(c) for c in columns])  # S10
                    head = buffer.getvalue().encode()
                else:
                    head = b"["
                yield head + chunk(batch, True)
                empty = not batch
                async for batch in batches:
                    yield chunk(batch, empty)
                    empty = empty and not batch
            if fmt == "json":
                yield b"]"

    stream = body()
    try:
        first_chunk = await anext(stream)
    except BaseException:
        await stream.aclose()
        raise

    async def rest() -> AsyncGenerator[bytes]:
        # A later failure raises out of the response, so the connection is cut instead of
        # ending the file cleanly.
        try:
            yield first_chunk
            async for part in stream:
                yield part
        finally:
            await stream.aclose()

    media = "text/csv; charset=utf-8" if fmt == "csv" else "application/json"
    return StreamingResponse(rest(), media_type=media, headers=headers)


def _csv_chunk(columns: Sequence[str], types: Sequence[str], batch: Sequence[Row]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in batch:
        values = cast("Mapping[str, object]", _row_json(cast(str, row[0])))
        writer.writerow([_cell(values.get(c), t) for c, t in zip(columns, types, strict=True)])
    return buffer.getvalue().encode()


async def insert_row(studio: Studio, request: Request) -> Response:
    _writable(studio)
    table, base = await _table(studio, request, fresh=True)
    values = _object((await _body(request)).get("values", {}), "values")
    # S6-3: a new row may give a composite column as its text ("(1,2)"), cast by the server:
    # a NOT NULL one could not be added at all.
    _reject_composites(studio, table, values, text_ok=True)
    params: list[object] = []
    target = f"{_relation(table)} AS t"
    returning = _row_sql(base, studio.text_columns(table))
    if values:
        columns = [_column(table, c) for c in values]
        names = ", ".join(quote(c["name"]) for c in columns)
        placeholders = ", ".join(
            _param(params, c, values[c["name"]], base[c["name"]]) for c in columns
        )
        sql = f"INSERT INTO {target} ({names}) VALUES ({placeholders}) RETURNING {returning}"
    else:
        sql = f"INSERT INTO {target} DEFAULT VALUES RETURNING {returning}"
    async with studio.pool().acquire() as conn:
        rows = await conn.fetch(sql, params)
    if not rows:  # S14: a BEFORE trigger returned NULL (or a rule did nothing)
        raise StudioError("no row was inserted: a trigger or rule on the table skipped it", 409)
    return JSONResponse({"row": _row_json(cast(str, rows[0][0]))}, status_code=201)


async def update_row(studio: Studio, request: Request) -> Response:
    _writable(studio)
    table, base = await _table(studio, request, fresh=True)
    body = await _body(request)
    values = _object(body.get("values"), "values")
    if not values:
        raise StudioError("values must not be empty")
    _reject_composites(studio, table, values)
    params: list[object] = []
    match = _key_match(table, base, body.get("key"), params)
    key_params = list(params)  # the key's placeholders come first, so ``match`` reuses them
    sets = ", ".join(
        f"{quote(c['name'])} = {_param(params, c, values[c['name']], base[c['name']])}"
        for c in (_column(table, name) for name in values)
    )
    target = f"{_relation(table)} AS t"
    returning = _row_sql(base, studio.text_columns(table))
    sql = f"UPDATE {target} SET {sets} WHERE {match} RETURNING {returning}"
    async with studio.pool().acquire() as conn, conn.transaction():
        rows = await conn.fetch(sql, params)
        _one_row(len(rows))
        if not rows and await conn.fetch(f"SELECT 1 FROM {target} WHERE {match}", key_params):
            # S4-10: the row is there, a BEFORE trigger (or a rule) skipped the update.
            raise StudioError("no row was updated: a trigger or rule on the table skipped it", 409)
    if not rows:
        raise StudioError("row not found", 404)
    return JSONResponse({"row": _row_json(cast(str, rows[0][0]))})


def _reject_composites(
    studio: Studio, table: TableSnap, values: Mapping[str, object], *, text_ok: bool = False
) -> None:
    for c in studio.read_only_columns(table):
        if c in values and not (text_ok and (values[c] is None or isinstance(values[c], str))):
            hint = "give it as text, e.g. (1,2)" if text_ok else "change it with SQL"
            raise StudioError(f"{c} is a composite column: {hint}")


def _one_row(count: int) -> None:
    """S4-3: a key that matches several rows (its primary key was dropped since the UI
    loaded the table) changes none of them: raising rolls the transaction back."""
    if count > 1:
        raise StudioError(
            f"the key matches {count} rows, so nothing was changed; reload the table", 409
        )


async def delete_rows(studio: Studio, request: Request) -> Response:
    _writable(studio)
    table, base = await _table(studio, request, fresh=True)
    keys = _array((await _body(request)).get("keys"), "keys")
    if not keys:
        raise StudioError("keys must not be empty")
    deleted = 0
    async with studio.pool().acquire() as conn, conn.transaction():
        for k in keys:
            params: list[object] = []
            match = _key_match(table, base, k, params)
            sql = f"DELETE FROM {_relation(table)} AS t WHERE {match}"
            count = await conn.execute(sql, params)
            _one_row(count)
            deleted += count
    return JSONResponse({"deleted": deleted})


# "cannot run inside a transaction block"
_NO_TRANSACTION_BLOCK = "25001"
# Statements that start, end or split a transaction (first token, as in _READ_ONLY_DENIED).
_TX_CONTROL = re.compile(
    r"(?:BEGIN|START|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE|PREPARE)\b", re.IGNORECASE
)
# A procedure call or an anonymous block: either may COMMIT, which only works outside a
# transaction block.
_RUNS_ALONE = re.compile(r"(?:CALL|DO)\b", re.IGNORECASE)


def _reject_read_only(statements: Sequence[str]) -> None:
    """Refuse a read-only batch that could leave the READ ONLY transaction or act on the server.

    The transaction is one per request, so a ``COMMIT``/``ROLLBACK`` in the middle ended it
    and the rest of the batch ran read-write. ``SET transaction_read_only = off`` did the
    same. Rejecting the statement class is cheaper to reason about than tracking whether the
    transaction is still the one we opened.
    """
    for statement in statements:
        body = statement[first_token(statement) :]
        if (match := _READ_ONLY_DENIED.match(body)) is not None:
            raise StudioError(f"{match.group(0).upper()} is not allowed in read-only mode", 403)
        if (call := _READ_ONLY_DENIED_CALLS.search(body)) is not None:
            raise StudioError(f"{call.group(0)} is not allowed in read-only mode", 403)


async def run_sql(studio: Studio, request: Request) -> Response:
    text = (await _body(request)).get("sql")
    if not isinstance(text, str) or not text.strip():
        raise StudioError("sql must be a non-empty string")
    statements = split_statements(text)
    if not statements:
        raise StudioError("sql must be a non-empty string")

    if studio.read_only:
        _reject_read_only(statements)

    async def first_rows(conn: Connection, statement: str) -> list[Row]:
        """At most MAX_SQL_ROWS + 1 rows, with the statement run to its end. A huge result
        is never loaded whole, whatever its first word (C5, S6).

        A query is a server-side cursor: FETCH keeps the first rows and MOVE runs the rest
        without sending it. A portal stopped after 1001 rows, so a volatile function in a
        SELECT ran for those rows only (S4-4). DECLARE takes plain queries only; anything
        else (INSERT ... RETURNING, a data-modifying WITH, EXPLAIN, FETCH) runs to its end
        on a portal's first fetch."""
        # A new name each time: the driver caches each FETCH with its first result's columns.
        cursor = f"mizzle_studio_{secrets.token_hex(8)}"
        try:
            async with conn.transaction():  # a savepoint: DECLARE may refuse the statement
                await conn.fetch(f"DECLARE {cursor} NO SCROLL CURSOR FOR {statement}", [])
        except DatabaseError:
            none: list[Row] = []
            async with aclosing(conn.cursor(statement, [], MAX_SQL_ROWS + 1)) as batches:
                return await anext(batches, none)
        rows = await conn.fetch(f"FETCH {MAX_SQL_ROWS + 1} FROM {cursor}", [])
        if len(rows) > MAX_SQL_ROWS:
            await conn.execute(f"MOVE FORWARD ALL IN {cursor}", [])
        await conn.execute(f"CLOSE {cursor}", [])
        return rows

    async def statement_rows(conn: Connection, statement: str) -> list[Row]:
        body = statement[first_token(statement) :]
        if studio.read_only:  # already inside the READ ONLY transaction
            return await first_rows(conn, statement)
        if _TX_CONTROL.match(body) or _RUNS_ALONE.match(body):
            # S4-2: BEGIN/COMMIT/ROLLBACK/SAVEPOINT run as written, never inside a
            # transaction of ours (which committed the rows the user rolled back). S5-5: so
            # do CALL and DO, which may COMMIT themselves (inside a BEGIN of the user's they
            # run in it, as in psql). They return at most one row.
            return await conn.fetch(statement, [])
        # A cursor needs a transaction; each statement commits on its own (a savepoint
        # inside a BEGIN of the user's).
        try:
            async with conn.transaction():
                return await first_rows(conn, statement)
        except DatabaseError as exc:
            if exc.sqlstate != _NO_TRANSACTION_BLOCK:
                raise
            # VACUUM, CREATE DATABASE, ...: refused before they started, so running them on
            # their own is safe. They return no rows.
            return await conn.fetch(statement, [])

    async def run(conn: Connection) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for statement in statements:
            try:
                rows = await statement_rows(conn, statement)
            except DatabaseError as exc:
                if exc.sql is None or exc.sql == statement:
                    raise
                # S5-7: name the user's statement, not Studio's FETCH or MOVE.
                message = str(exc).removesuffix(f"\nSQL: {exc.sql}")
                raise DatabaseError(message, sql=statement, original=exc.original) from exc
            shown = rows[:MAX_SQL_ROWS]
            # By position: a dict would merge two columns with the same name (C9).
            columns = list(cast(_Named, cast(object, rows[0])).keys()) if rows else []
            results.append(
                {
                    "statement": statement,
                    "columns": columns,
                    "rows": [[jsonable(v) for v in r] for r in shown],
                    "truncated": len(rows) > MAX_SQL_ROWS,
                    "count": len(shown),
                }
            )
        return results

    try:
        async with studio.pool().acquire() as conn:
            if studio.read_only:
                async with conn.transaction(readonly=True):
                    results = await run(conn)
            else:
                try:
                    results = await run(conn)
                finally:
                    # S4-2: a transaction the batch left open (BEGIN with no COMMIT, or a
                    # failed statement inside it) is rolled back, never committed.
                    if any(_TX_CONTROL.match(s[first_token(s) :]) for s in statements):
                        await conn.execute("ROLLBACK", [])
                    # S4-3: the batch may have changed the schema.
                    studio.tables = None
    except MizzleError:
        studio.record_history(text, ok=False)
        raise
    studio.record_history(text, ok=True)
    payload: dict[str, object] = {"results": results}
    if len(results) == 1:  # keep the single-statement shape clients already use
        payload.update(results[0])
    return JSONResponse(payload)


async def get_history(studio: Studio, _request: Request) -> Response:
    return JSONResponse({"history": studio.history})


async def clear_history(studio: Studio, _request: Request) -> Response:
    studio.history = []
    studio.save_state()
    return JSONResponse({"cleared": True})


async def _current_table(studio: Studio, data: dict[str, object]) -> TableSnap | None:
    """The table an operation changes, from the live snapshot (None if it does not exist)."""
    schema = data.get("schema", "public")
    # The field build_steps uses: a table-level operation names the table in "name", a
    # column one in "table" (where "name" is the column). Mixing them checked one table
    # and dropped another (C8).
    field = "name" if data.get("operation") in ("create_table", "drop_table") else "table"
    name = data.get(field)
    if not isinstance(schema, str) or not isinstance(name, str):
        return None
    current = (await studio.load_snapshot()).get((schema, name))
    if current is None:
        current = (await studio.load_snapshot(refresh=True)).get((schema, name))
    return current


def _configured_schema(studio: Studio, data: Mapping[str, object]) -> None:
    """DDL may only touch the schemas Studio was told to manage.

    ``_schema`` checks the name is a plain identifier, not that it is one of ours, so
    ``information_schema`` used to be created in.
    """
    schema = data.get("schema", "public")
    if not isinstance(schema, str) or schema not in studio.schemas:
        raise StudioError(
            f"schema {schema!r} is outside this Studio's schema filter "
            f"({', '.join(studio.schemas)})",
            403,
        )


async def ddl_preview(studio: Studio, request: Request) -> Response:
    """Generate the SQL for an operation without running it (the review screen)."""
    data = await _body(request)
    _configured_schema(studio, data)
    steps = build_steps(studio.dialect, data, await _current_table(studio, data))
    return JSONResponse({"statements": statement_rows(studio.dialect, steps)})


async def ddl_apply(studio: Studio, request: Request) -> Response:
    _writable(studio)
    data = await _body(request)
    _configured_schema(studio, data)
    steps = build_steps(studio.dialect, data, await _current_table(studio, data))
    # S8-2: tables are named with their schema ("public"."t", never a "$user" schema's t),
    # and everything else in the DDL (a default's uuid_generate_v4(), a type) resolves
    # through the role's own path, as psql would. Only pg_temp is moved, to the end:
    # unlisted it is searched first, and a temporary type left on this pooled connection
    # must not stand in for a real one. The statement calls only pg_catalog functions.
    statements = studio.dialect.render(steps, qualify=True)
    async with studio.pool().acquire() as conn:
        await conn.execute(
            "SELECT pg_catalog.set_config('search_path', "
            "pg_catalog.concat_ws(', ', "
            "NULLIF(pg_catalog.current_setting('search_path'), ''), 'pg_temp'), "
            "false)",
            [],
        )
        try:
            await apply_statements(conn, statements)
        finally:
            await conn.execute("RESET search_path", [])
    await studio.load_snapshot(refresh=True)
    return JSONResponse(
        {"applied": len(statements), "statements": statement_rows(studio.dialect, steps)}
    )


async def list_saved(studio: Studio, _request: Request) -> Response:
    return JSONResponse({"saved": [{"name": n, "sql": s} for n, s in studio.saved.items()]})


async def put_saved(studio: Studio, request: Request) -> Response:
    _writable(studio)  # S15
    path: dict[str, object] = request.path_params
    name = str(path["name"])
    if not name.strip():  # S15
        raise StudioError("saved query name must not be empty")
    sql = (await _body(request)).get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise StudioError("sql must be a non-empty string")
    studio.save_query(name, sql)
    return JSONResponse({"name": name}, status_code=201)


async def delete_saved(studio: Studio, request: Request) -> Response:
    _writable(studio)  # S15
    path: dict[str, object] = request.path_params
    name = str(path["name"])
    if not studio.delete_query(name):
        raise StudioError(f"no saved query {name!r}", 404)
    return JSONResponse({"deleted": name})


def _api(
    studio: Studio,
    handler: Callable[[Studio, Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    async def wrapped(request: Request) -> Response:
        try:
            return await handler(studio, request)
        except StudioError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except DatabaseError as exc:
            # S9: the driver hides class-22 messages (they quote the value); here the user
            # typed that value, so show the server's message (never its DETAIL).
            message = getattr(exc.original, "message", None)
            if (exc.sqlstate or "").startswith("22") and isinstance(message, str):
                return JSONResponse({"error": message}, status_code=400)
            return JSONResponse({"error": str(exc)}, status_code=400)
        except MizzleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return wrapped


def _basic_credentials(user: str, password: str) -> bytes:
    return b"Basic " + base64.b64encode(f"{user}:{password}".encode())


def parse_auth(value: str) -> tuple[str, str]:
    """Parse ``USER:PASSWORD`` for HTTP Basic auth. The password may contain ``:``."""
    user, sep, password = value.partition(":")
    if not sep or not user or not password:
        raise MigrationError("--auth must be USER:PASSWORD, with a non-empty user and password")
    return user, password


MIN_TOKEN_LENGTH = 32
MIN_PASSWORD_LENGTH = 12


def resolve_token(explicit: str | None = None) -> str:
    """The bearer token: ``--token``, then ``MIZZLE_STUDIO_TOKEN``, then a random one."""
    if explicit is not None:  # ``--token ''`` is refused by the length check, not replaced
        return explicit
    return os.environ.get("MIZZLE_STUDIO_TOKEN") or secrets.token_urlsafe(32)


def resolve_auth(explicit: str | None = None) -> str | None:
    """``--auth``, then ``MIZZLE_STUDIO_AUTH``.

    The environment form exists so the password need not be in argv, where ``ps`` shows it.
    """
    return explicit or os.environ.get("MIZZLE_STUDIO_AUTH") or None


def check_token_length(token: str) -> None:
    """Refuse a guessable token. It is checked for every bind and alongside ``--auth``: the
    bearer token is accepted whenever it is set, and other users of this machine can reach a
    loopback bind too (B11)."""
    if _strength(token) < MIN_TOKEN_LENGTH:
        raise MigrationError(
            f"a bearer token needs at least {MIN_TOKEN_LENGTH} non-space characters "
            f"(got {_strength(token)}); omit --token / MIZZLE_STUDIO_TOKEN to have one generated"
        )


def _strength(secret: str) -> int:
    """The length that counts: whitespace adds nothing to guess (B75)."""
    return len("".join(secret.split()))


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _writes_json(request: Request) -> bool:
    """A body must be sent as JSON. Form encodings (``text/plain``, urlencoded, multipart) are
    what a cross-site form can produce, so refusing them removes that surface entirely.

    A request with no body (``DELETE /api/...`` with the name in the path) needs no type.
    """
    if request.method in _SAFE_METHODS:
        return True
    length = request.headers.get("content-length", "")
    has_body = (length.isdigit() and int(length) > 0) or "transfer-encoding" in request.headers
    if not has_body:
        return True
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return content_type == "application/json"


class _Guard(BaseHTTPMiddleware):
    expected: bytes
    allowed_hosts: set[str] | None
    basic: bytes | None
    origins: set[str] | None
    # Recent failures per client address, oldest client first (B7): when, and a digest of
    # the wrong credential.
    _failures: dict[str, list[tuple[float, bytes]]]

    def __init__(
        self,
        app: ASGIApp,
        token: str,
        allowed_hosts: set[str] | None,
        basic_auth: tuple[str, str] | None = None,
        origins: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self.expected = f"Bearer {token}".encode()
        self.allowed_hosts = allowed_hosts
        self.basic = None if basic_auth is None else _basic_credentials(*basic_auth)
        # The origins a browser request may come from. Loopback serves the UI itself; a
        # remote bind is behind a reverse proxy, whose public origin is passed in. None means
        # "the origin of the Host the request was sent to" (a same-origin request).
        self.origins = origins
        self._failures = {}

    def _retry_after(self, client: str) -> int | None:
        """Seconds until ``client`` may try a wrong credential again, or None if it may now."""
        now = time.monotonic()
        recent = [t for t, _ in self._failures.get(client, ()) if now - t < LOGIN_WINDOW_SECONDS]
        if len(recent) < LOGIN_MAX_FAILURES:
            return None
        return max(1, math.ceil(recent[-LOGIN_MAX_FAILURES] + LOGIN_WINDOW_SECONDS - now))

    def _record_failure(self, client: str, given: bytes) -> None:
        """Count a wrong credential. Only a new guess counts: no credential at all, or the
        same wrong one again, is not guessing. A tab still holding the token of an earlier
        Studio run re-sends it on every reload, and on loopback that locked out the real
        user, who shares the address (B68)."""
        if given.strip() in (b"", b"Bearer", b"Basic"):  # no credential at all (C3)
            return
        now = time.monotonic()
        digest = hashlib.sha256(given).digest()
        # Popped and re-inserted, so the dict stays ordered by each client's latest failure.
        recent = [f for f in self._failures.pop(client, []) if now - f[0] < LOGIN_WINDOW_SECONDS]
        if all(d != digest for _, d in recent):
            recent.append((now, digest))
        self._failures[client] = recent[-LOGIN_MAX_FAILURES:]
        while len(self._failures) > LOGIN_MAX_CLIENTS:
            del self._failures[next(iter(self._failures))]

    def _authorized(self, request: Request) -> bool:
        given = request.headers.get("authorization", "").encode()
        if hmac.compare_digest(given, self.expected):
            return True
        return self.basic is not None and hmac.compare_digest(given, self.basic)

    def _cross_site(self, request: Request) -> bool:
        """Whether this looks like another site driving the browser's cached credentials.

        Browsers attach cached Basic credentials to a cross-site form post, and a
        ``text/plain`` form body parses as JSON, so neither the credentials nor
        ``request.json()`` is proof that our own UI sent the request. ``Origin`` and
        ``Sec-Fetch-Site`` are the signals a page cannot forge.

        Every method is checked, reads included: the UI is same-origin, and a page's
        no-credential requests must not count as failed logins (B35).
        """
        fetch_site = request.headers.get("sec-fetch-site", "")
        if fetch_site in ("cross-site", "same-site"):
            return True
        raw = request.headers.get("origin")
        if raw is None:  # not a browser request (curl, a test client): no CSRF surface
            return False
        if self.origins is None:
            host = request.headers.get("host", "")
            return raw not in (f"http://{host}", f"https://{host}")
        return raw not in self.origins

    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            self.allowed_hosts is not None
            and request.headers.get("host", "") not in self.allowed_hosts
        ):
            return self._error("forbidden host", 403)
        if request.url.path.startswith("/api/") and self._cross_site(request):
            # Before the throttle and the credential check, and not counted as a failure: on a
            # loopback bind every client is 127.0.0.1, so a web page driving the browser could
            # otherwise trip the throttle and lock the real user out (B35).
            return self._error("cross-site request refused", 403)
        if request.url.path.startswith("/api/"):
            # The throttle comes before the credential check: if a throttled client's correct
            # guess still got through, the throttle would not slow guessing at all (B34). It
            # is per client address, so only the guessing client waits, not the real user.
            # The client address is the TCP peer. Behind a reverse proxy, FORWARDED_ALLOW_IPS
            # names the proxy and its X-Forwarded-For then gives the real client (B67).
            client = request.client.host if request.client else ""
            if (wait := self._retry_after(client)) is not None:
                return self._error(
                    "too many failed attempts; try again shortly", 429, {"Retry-After": str(wait)}
                )
        if request.url.path.startswith("/api/") and not self._authorized(request):
            client = request.client.host if request.client else ""
            self._record_failure(client, request.headers.get("authorization", "").encode())
            headers = {"WWW-Authenticate": 'Basic realm="mizzle studio"'} if self.basic else None
            return self._error("missing or wrong credentials", 401, headers)
        if request.url.path.startswith("/api/") and not _writes_json(request):
            return self._error("Content-Type must be application/json", 415)
        response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    @staticmethod
    def _error(message: str, status: int, headers: dict[str, str] | None = None) -> Response:
        # Error responses go through the same header set: a 401 without them is a hole a
        # browser will happily render.
        response = JSONResponse({"error": message}, status_code=status, headers=headers)
        response.headers.update(_SECURITY_HEADERS)
        return response


async def _not_built(_request: Request) -> Response:
    return HTMLResponse(_NOT_BUILT)


class _NamesConvertor(PathConvertor):
    """A path that may hold any character: ``:path`` has no DOTALL, so a table name with a
    newline was listed but never routed (S5-8)."""

    regex: ClassVar[str] = "(?s:.*)"


register_url_convertor("names", _NamesConvertor())


def create_app(
    *,
    dialect: Dialect,
    url: str,
    schemas: list[str],
    token: str,
    allowed_hosts: set[str] | None,
    read_only: bool = False,
    static_dir: Path = STATIC_DIR,
    state_path: Path | None = None,
    basic_auth: tuple[str, str] | None = None,
    origins: set[str] | None = None,
) -> Starlette:
    studio = Studio(dialect, url, schemas, read_only, state_path=state_path)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        studio.driver = await dialect.connect(url, pool_size=4, statement_cache_size=100)
        try:
            if read_only:
                async with studio.driver.acquire() as conn:
                    _refuse_privileged(await _privileged(conn))
            studio.load_state()
            yield
        finally:
            await studio.driver.close()

    tables_path = "/api/tables/{names:names}/rows"
    routes: list[BaseRoute] = [
        Route("/api/meta", _api(studio, meta)),
        Route("/api/tables", _api(studio, tables)),
        Route("/api/schema/relationships", _api(studio, relationships)),
        Route(tables_path, _api(studio, list_rows), methods=["GET"]),
        Route(tables_path, _api(studio, insert_row), methods=["POST"]),
        Route(tables_path, _api(studio, update_row), methods=["PATCH"]),
        Route(tables_path, _api(studio, delete_rows), methods=["DELETE"]),
        Route(
            "/api/tables/{names:names}/export",
            _api(studio, export_rows),
            methods=["GET"],
        ),
        Route("/api/sql", _api(studio, run_sql), methods=["POST"]),
        Route("/api/ddl/preview", _api(studio, ddl_preview), methods=["POST"]),
        Route("/api/ddl/apply", _api(studio, ddl_apply), methods=["POST"]),
        Route("/api/queries/history", _api(studio, get_history), methods=["GET"]),
        Route("/api/queries/history", _api(studio, clear_history), methods=["DELETE"]),
        Route("/api/queries/saved", _api(studio, list_saved), methods=["GET"]),
        # ``:path`` so a saved-query name may contain "/" (it is a user-chosen label, not a
        # path segment); the default converter stopped at the slash and answered 405.
        Route("/api/queries/saved/{name:path}", _api(studio, put_saved), methods=["PUT"]),
        Route("/api/queries/saved/{name:path}", _api(studio, delete_saved), methods=["DELETE"]),
    ]
    if (static_dir / "index.html").exists():
        routes.append(Mount("/", StaticFiles(directory=static_dir, html=True)))
    else:
        routes.append(Route("/", _not_built))
    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[
            Middleware(
                _Guard,
                token=token,
                allowed_hosts=allowed_hosts,
                basic_auth=basic_auth,
                origins=origins,
            )
        ],
    )


async def _privileged(conn: Connection) -> list[str]:
    """The roles and function grants that give the current user server-side abilities
    (empty when none)."""
    names: list[str] = []
    for name, superuser, replication in await conn.fetch(_PRIVILEGED_SQL, []):
        suffix = " (superuser)" if superuser else " (replication)" if replication else ""
        names.append(f"{name}{suffix}")
    for (name,) in await conn.fetch(_SERVER_FUNCTIONS_SQL, []):
        names.append(f"EXECUTE on {name}()")
    return names


def _refuse_privileged(names: Sequence[str]) -> None:
    if names:
        raise MigrationError(
            "refusing to start read-only Studio: the database role is, can become or has "
            f"{', '.join(names)}, which can act on the server from inside a read-only "
            "transaction (read files, run programs, signal backends). Connect as a role "
            "without these privileges."
        )


async def privileged_roles(dialect: Dialect, url: str) -> list[str]:
    """``_privileged`` over a short-lived connection, for the CLI's start-up check."""
    driver = await dialect.connect(url, pool_size=1, statement_cache_size=0)
    try:
        async with driver.acquire() as conn:
            return await _privileged(conn)
    finally:
        await driver.close()


def _server_origins(host: str, port: int) -> set[str] | None:
    """The origins a same-site browser request can carry.

    Loopback is unambiguous: the UI is served from one of these three. A remote bind sits
    behind a TLS reverse proxy whose public origin the server cannot derive, so
    ``MIZZLE_STUDIO_ORIGIN`` (comma-separated) supplies it; without it a browser request
    carrying an ``Origin`` header must match the ``Host`` it was sent to. Guessing
    ``http://<bind address>`` broke every write behind ``--host 0.0.0.0`` (B72)."""
    if host in {"127.0.0.1", "localhost", "::1"}:
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}
    extra = os.environ.get("MIZZLE_STUDIO_ORIGIN", "")
    origins = {o.strip().rstrip("/") for o in extra.split(",") if o.strip()}
    return origins or None


def serve(
    config: Config,
    *,
    port: int,
    read_only: bool,
    open_browser: bool,
    ui_dir: str | None = None,
    host: str = "127.0.0.1",
    token: str | None = None,
    auth: str | None = None,
) -> None:
    import uvicorn

    from mizzle.studio.assets import ui_path

    auth = resolve_auth(auth)
    basic_auth = parse_auth(auth) if auth else None
    token_value = resolve_token(token)
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if (
        not loopback
        and token is None
        and not os.environ.get("MIZZLE_STUDIO_TOKEN")
        and not basic_auth
    ):
        raise MigrationError(
            f"refusing to listen on {host} without credentials; pass --auth USER:PASSWORD "
            "or --token (or set MIZZLE_STUDIO_TOKEN / MIZZLE_STUDIO_AUTH)"
        )
    check_token_length(token_value)
    if basic_auth is not None and _strength(basic_auth[1]) < MIN_PASSWORD_LENGTH:
        raise MigrationError(
            f"the --auth / MIZZLE_STUDIO_AUTH password needs at least {MIN_PASSWORD_LENGTH} "
            f"non-space characters (got {_strength(basic_auth[1])})"
        )
    if read_only:
        _refuse_privileged(asyncio.run(privileged_roles(config.dialect(), config.url())))
    static_dir = ui_path(ui_dir)  # downloads and verifies the UI on first run
    app = create_app(
        dialect=config.dialect(),
        url=config.url(),
        schemas=config.schema_filter,
        token=token_value,
        # A remote client reaches the server by an address it cannot know, so the Host check
        # only makes sense on loopback (Basic auth covers the difference).
        allowed_hosts=(
            {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"} if loopback else None
        ),
        # The UI is served from the same origin, so the loopback URLs are the only origins a
        # browser request can legitimately carry. A remote bind is behind a proxy: its
        # origin is unknown here, so it is passed in (see MIZZLE_STUDIO_ORIGIN).
        origins=_server_origins(host, port),
        read_only=read_only,
        static_dir=static_dir,
        state_path=config.root / ".mizzle" / "studio.json",
        basic_auth=basic_auth,
    )
    mode = " (read-only)" if read_only else ""
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"  # S12: IPv6 in brackets
    if loopback:
        link = f"http://{netloc if host == '::1' else f'127.0.0.1:{port}'}/#token={token_value}"
        print(f"mizzle studio{mode} running at {link}\nPress Ctrl+C to stop.")
        if open_browser:
            webbrowser.open(link)
    else:
        how = "HTTP Basic" if basic_auth else "the bearer token"
        print(
            f"mizzle studio{mode} listening on {netloc} (authenticate with {how}).\n"
            "Press Ctrl+C to stop."
        )
    # uvicorn trusts X-Forwarded-For from 127.0.0.1 by default, so any local process could
    # pick a fresh address per guess and never be throttled (B67). Only a proxy the user
    # names in FORWARDED_ALLOW_IPS is trusted.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        proxy_headers="FORWARDED_ALLOW_IPS" in os.environ,
    )

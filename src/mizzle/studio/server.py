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
- ``--read-only`` blocks every write, and raw SQL runs in a read-only transaction;
- table and column names are checked against the introspected schema and quoted;
  values are always bind parameters.
"""

from __future__ import annotations

import base64
import csv
import datetime
import enum
import hmac
import io
import json
import logging
import os
import re
import secrets
import sys
import webbrowser
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, override

import asyncpg
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from mizzle.core.errors import MigrationError, MizzleError
from mizzle.core.snapshot import ColumnSnap, Snapshot, TableSnap, key
from mizzle.dialects.base import Connection, Dialect, Driver
from mizzle.dialects.postgres.ddl import SERIAL_TYPES, qualified, quote
from mizzle.kit.config import Config
from mizzle.kit.migrate import apply_statements
from mizzle.studio.ddl import build_steps, statement_rows

STATIC_DIR = Path(__file__).parent / "static"
MAX_SQL_ROWS = 1000
MAX_PAGE = 500
MAX_EXPORT_ROWS = 100_000
MAX_HISTORY = 100
_COMPARE = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
_DOLLAR = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")

# Statements that change the transaction, the session or the process rather than the data.
# In read-only mode a batch must not be able to leave the READ ONLY transaction it runs in
# (``commit; insert ...`` was a working write), so these are refused outright.
_READ_ONLY_DENIED = re.compile(
    r"(?:BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE"
    r"|SET|RESET|DISCARD|LOCK|PREPARE\s+TRANSACTION|NOTIFY|LISTEN|UNLISTEN"
    r"|COPY|DO)\b",
    re.IGNORECASE,
)
# Server-side functions that read or change things outside the transaction, which READ ONLY
# does not (and cannot) stop.
_READ_ONLY_DENIED_CALLS = re.compile(
    r"\b(?:pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_terminate_backend"
    r"|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_switch_wal|pg_promote"
    r"|lo_import|lo_export|dblink|dblink_exec|pg_file_write|pg_file_rename|pg_file_unlink)\b",
    re.IGNORECASE,
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
    snapshot: Snapshot | None = field(default=None)
    history: list[dict[str, object]] = field(default_factory=list)
    saved: dict[str, str] = field(default_factory=dict)
    state_path: Path | None = None

    def pool(self) -> Driver:
        if self.driver is None:
            raise StudioError("not connected", 503)
        return self.driver

    async def load_snapshot(self, refresh: bool = False) -> Snapshot:
        if self.snapshot is None or refresh:
            async with self.pool().acquire() as conn:
                self.snapshot = await self.dialect.introspect(conn, self.schemas)
        return self.snapshot

    async def table(self, schema: str, name: str) -> TableSnap:
        snapshot = await self.load_snapshot()
        table = snapshot["tables"].get(key(schema, name))
        if table is None:
            snapshot = await self.load_snapshot(refresh=True)
            table = snapshot["tables"].get(key(schema, name))
        if table is None:
            raise StudioError(f"unknown table {schema}.{name}", 404)
        return table

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
        if isinstance(history, list):
            self.history = [h for h in cast(list[object], history) if isinstance(h, dict)]
            del self.history[:-MAX_HISTORY]
        saved = state.get("saved")
        if isinstance(saved, dict):
            self.saved = {
                k: v for k, v in cast(dict[str, object], saved).items() if isinstance(v, str)
            }

    def save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"history": self.history, "saved": self.saved}
            self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
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


def _cast_type(column: ColumnSnap) -> str:
    # Introspected types come from format_type(), which already quotes when needed.
    return SERIAL_TYPES.get(column["type"], column["type"])


def _as_text(value: object) -> str | None:
    """Values travel as text and are CAST server-side, so every type uses its input syntax."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def _param(params: list[object], column: ColumnSnap, value: object) -> str:
    params.append(_as_text(value))
    return f"CAST(${len(params)}::text AS {_cast_type(column)})"


def _where(table: TableSnap, filters: list[object], params: list[object]) -> str:
    parts: list[str] = []
    for item in filters:
        f = _object(item, "filter")
        column = _column(table, f.get("column"))
        ref = f"t.{quote(column['name'])}"
        op = f.get("op")
        if isinstance(op, str) and op in _COMPARE:
            parts.append(f"{ref} {_COMPARE[op]} {_param(params, column, f.get('value'))}")
        elif op in ("like", "ilike"):
            params.append(_as_text(f.get("value")) or "")
            parts.append(f"{ref}::text {op.upper()} ${len(params)}")
        elif op == "is_null":
            parts.append(f"{ref} IS NULL")
        elif op == "is_not_null":
            parts.append(f"{ref} IS NOT NULL")
        else:
            raise StudioError(f"unknown filter operator {op!r}")
    return f" WHERE {' AND '.join(parts)}" if parts else ""


def _key_match(table: TableSnap, key_values: object, params: list[object]) -> str:
    pk = table["primary_key"]
    if pk is None:
        raise StudioError(f"{table['name']} has no primary key; change its rows with SQL")
    values = _object(key_values, "key")
    if set(values) != set(pk["columns"]):
        raise StudioError(f"key must give exactly the primary key columns {pk['columns']}")
    return " AND ".join(
        f"t.{quote(c)} = {_param(params, _column(table, c), values[c])}" for c in pk["columns"]
    )


def _order_sql(table: TableSnap, q: Mapping[str, str]) -> str:
    order = q.get("order")
    if order:
        column = _column(table, order)
        direction = "DESC" if q.get("dir") == "desc" else "ASC"
        return f" ORDER BY t.{quote(column['name'])} {direction}"
    if table["primary_key"]:
        return " ORDER BY " + ", ".join(f"t.{quote(c)}" for c in table["primary_key"]["columns"])
    return ""


def _skip_quoted(text: str, i: int, quote: str) -> int:
    i += 1
    while i < len(text):
        if text[i] == quote:
            if i + 1 < len(text) and text[i + 1] == quote:  # doubled: an escaped quote
                i += 2
                continue
            return i + 1
        i += 1
    return len(text)


def split_statements(text: str) -> list[str]:
    """Split SQL on top-level semicolons, honouring single/double quotes, dollar-quoted
    bodies and comments, so the console can run several statements in one go."""
    parts: list[str] = []
    start, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            i = _skip_quoted(text, i, ch)
        elif ch == "$":
            tag = _DOLLAR.match(text, i)
            if tag is None:
                i += 1
            else:
                close = text.find(tag.group(0), i + len(tag.group(0)))
                i = n if close == -1 else close + len(tag.group(0))
        elif text.startswith("--", i):
            newline = text.find("\n", i)
            i = n if newline == -1 else newline + 1
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close == -1 else close + 2
        elif ch == ";":
            parts.append(text[start:i])
            start = i = i + 1
        else:
            i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if _has_sql(p)]


def _has_sql(chunk: str) -> bool:
    return any(line.strip() and not line.strip().startswith("--") for line in chunk.splitlines())


# A cell a spreadsheet would evaluate as a formula. CSV has no types, so the only defence is
# to make the text unambiguous: a leading apostrophe is the conventional "this is text" mark
# (Excel, LibreOffice and Sheets all show the rest of the cell and keep it inert).
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(text: str) -> str:
    return f"'{text}" if text.startswith(_FORMULA_LEAD) else text


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return _csv_safe(json.dumps(value))
    if isinstance(value, bool):
        return "true" if value else "false"
    return _csv_safe(str(value))


def _to_csv(table: TableSnap, rows: Sequence[object]) -> str:
    columns = list(table["columns"])
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        values: dict[str, object] = cast(dict[str, object], row) if isinstance(row, dict) else {}
        writer.writerow([_cell(values.get(c)) for c in columns])
    return out.getvalue()


def jsonable(value: object) -> JSON:
    match value:
        case None | bool() | int() | str():
            return value
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


async def _table(studio: Studio, request: Request) -> TableSnap:
    path: dict[str, object] = request.path_params
    return await studio.table(str(path["schema"]), str(path["table"]))


async def meta(studio: Studio, _request: Request) -> Response:
    return JSONResponse({"read_only": studio.read_only, "dialect": studio.dialect.name})


async def tables(studio: Studio, _request: Request) -> Response:
    snapshot = await studio.load_snapshot(refresh=True)
    return JSONResponse({"tables": list(snapshot["tables"].values()), "enums": snapshot["enums"]})


async def relationships(studio: Studio, _request: Request) -> Response:
    """Nodes and foreign-key edges for the ER diagram: enough to draw the schema without
    shipping the full snapshots."""
    snapshot = await studio.load_snapshot(refresh=True)
    nodes = [
        {"schema": t["schema"], "name": t["name"], "columns": list(t["columns"])}
        for t in snapshot["tables"].values()
    ]
    edges: list[dict[str, object]] = []
    for table in snapshot["tables"].values():
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
    table = await _table(studio, request)
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
    where = _where(table, filters, params)
    order_sql = _order_sql(table, q)
    source = f"{qualified(table['schema'], table['name'])} AS t"
    async with studio.pool().acquire() as conn:
        [total] = await conn.fetch(f"SELECT count(*) FROM {source}{where}", params)
        rows = await conn.fetch(
            f"SELECT to_jsonb(t) FROM {source}{where}{order_sql} LIMIT {limit} OFFSET {offset}",
            params,
        )
    return JSONResponse({"total": total[0], "rows": [jsonable(r[0]) for r in rows]})


async def export_rows(studio: Studio, request: Request) -> Response:
    """Download every matching row as CSV or JSON (not just the current page)."""
    table = await _table(studio, request)
    q = request.query_params
    fmt = q.get("format", "csv").lower()
    if fmt not in ("csv", "json"):
        raise StudioError("format must be 'csv' or 'json'")
    try:
        filters = _array(cast(object, json.loads(q.get("filters", "[]"))), "filters")
    except ValueError as exc:
        raise StudioError(f"bad query parameters: {exc}") from exc
    params: list[object] = []
    where = _where(table, filters, params)
    order_sql = _order_sql(table, q)
    source = f"{qualified(table['schema'], table['name'])} AS t"
    async with studio.pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT to_jsonb(t) FROM {source}{where}{order_sql} LIMIT {MAX_EXPORT_ROWS}", params
        )
    data = [jsonable(r[0]) for r in rows]
    filename = f"{table['schema']}.{table['name']}.{fmt}"
    disposition = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if fmt == "json":
        return JSONResponse(data, headers=disposition)
    return Response(_to_csv(table, data), media_type="text/csv", headers=disposition)


async def insert_row(studio: Studio, request: Request) -> Response:
    _writable(studio)
    table = await _table(studio, request)
    values = _object((await _body(request)).get("values", {}), "values")
    params: list[object] = []
    target = f"{qualified(table['schema'], table['name'])} AS t"
    if values:
        columns = [_column(table, c) for c in values]
        names = ", ".join(quote(c["name"]) for c in columns)
        placeholders = ", ".join(_param(params, c, values[c["name"]]) for c in columns)
        sql = f"INSERT INTO {target} ({names}) VALUES ({placeholders}) RETURNING to_jsonb(t)"
    else:
        sql = f"INSERT INTO {target} DEFAULT VALUES RETURNING to_jsonb(t)"
    async with studio.pool().acquire() as conn:
        [row] = await conn.fetch(sql, params)
    return JSONResponse({"row": jsonable(row[0])}, status_code=201)


async def update_row(studio: Studio, request: Request) -> Response:
    _writable(studio)
    table = await _table(studio, request)
    body = await _body(request)
    values = _object(body.get("values"), "values")
    if not values:
        raise StudioError("values must not be empty")
    params: list[object] = []
    sets = ", ".join(
        f"{quote(c['name'])} = {_param(params, c, values[c['name']])}"
        for c in (_column(table, name) for name in values)
    )
    match = _key_match(table, body.get("key"), params)
    sql = (
        f"UPDATE {qualified(table['schema'], table['name'])} AS t SET {sets} "
        f"WHERE {match} RETURNING to_jsonb(t)"
    )
    async with studio.pool().acquire() as conn:
        rows = await conn.fetch(sql, params)
    if not rows:
        raise StudioError("row not found", 404)
    return JSONResponse({"row": jsonable(rows[0][0])})


async def delete_rows(studio: Studio, request: Request) -> Response:
    _writable(studio)
    table = await _table(studio, request)
    keys = _array((await _body(request)).get("keys"), "keys")
    if not keys:
        raise StudioError("keys must not be empty")
    deleted = 0
    async with studio.pool().acquire() as conn, conn.transaction():
        for k in keys:
            params: list[object] = []
            match = _key_match(table, k, params)
            sql = f"DELETE FROM {qualified(table['schema'], table['name'])} AS t WHERE {match}"
            deleted += await conn.execute(sql, params)
    return JSONResponse({"deleted": deleted})


def _reject_read_only(statements: Sequence[str]) -> None:
    """Refuse a read-only batch that could leave the READ ONLY transaction or act on the server.

    The transaction is one per request, so a ``COMMIT``/``ROLLBACK`` in the middle ended it
    and the rest of the batch ran read-write. ``SET transaction_read_only = off`` did the
    same. Rejecting the statement class is cheaper to reason about than tracking whether the
    transaction is still the one we opened.
    """
    for statement in statements:
        body = _strip_leading_comments(statement)
        if (match := _READ_ONLY_DENIED.match(body)) is not None:
            raise StudioError(
                f"{match.group(0).upper()} is not allowed in read-only mode", 403
            )
        if (call := _READ_ONLY_DENIED_CALLS.search(body)) is not None:
            raise StudioError(
                f"{call.group(0)} is not allowed in read-only mode", 403
            )


def _strip_leading_comments(statement: str) -> str:
    """The statement without leading ``--``/``/* */`` comments, so the first keyword is found."""
    rest = statement.lstrip()
    while True:
        if rest.startswith("--"):
            newline = rest.find("\n")
            rest = "" if newline == -1 else rest[newline + 1 :].lstrip()
        elif rest.startswith("/*"):
            close = rest.find("*/", 2)
            rest = "" if close == -1 else rest[close + 2 :].lstrip()
        else:
            return rest


async def run_sql(studio: Studio, request: Request) -> Response:
    text = (await _body(request)).get("sql")
    if not isinstance(text, str) or not text.strip():
        raise StudioError("sql must be a non-empty string")
    statements = split_statements(text)
    if not statements:
        raise StudioError("sql must be a non-empty string")

    if studio.read_only:
        _reject_read_only(statements)

    async def run(conn: Connection) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for statement in statements:
            rows = await conn.fetch_dicts(statement, [])
            columns = list(rows[0].keys()) if rows else []
            results.append(
                {
                    "statement": statement,
                    "columns": columns,
                    "rows": [[jsonable(r[c]) for c in columns] for r in rows[:MAX_SQL_ROWS]],
                    "truncated": len(rows) > MAX_SQL_ROWS,
                    "count": len(rows),
                }
            )
        return results

    try:
        async with studio.pool().acquire() as conn:
            if studio.read_only:
                async with conn.transaction(readonly=True):
                    results = await run(conn)
            else:
                results = await run(conn)
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
    name = data.get("table", data.get("name"))
    if not isinstance(schema, str) or not isinstance(name, str):
        return None
    snapshot = await studio.load_snapshot()
    current = snapshot["tables"].get(key(schema, name))
    if current is None:
        snapshot = await studio.load_snapshot(refresh=True)
        current = snapshot["tables"].get(key(schema, name))
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
    statements = studio.dialect.render(steps)
    async with studio.pool().acquire() as conn:
        await apply_statements(conn, statements)
    await studio.load_snapshot(refresh=True)
    return JSONResponse(
        {"applied": len(statements), "statements": statement_rows(studio.dialect, steps)}
    )


async def list_saved(studio: Studio, _request: Request) -> Response:
    return JSONResponse({"saved": [{"name": n, "sql": s} for n, s in studio.saved.items()]})


async def put_saved(studio: Studio, request: Request) -> Response:
    path: dict[str, object] = request.path_params
    name = str(path["name"])
    sql = (await _body(request)).get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise StudioError("sql must be a non-empty string")
    studio.save_query(name, sql)
    return JSONResponse({"name": name}, status_code=201)


async def delete_saved(studio: Studio, request: Request) -> Response:
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


def resolve_token(explicit: str | None = None) -> str:
    """The bearer token: ``--token``, then ``MIZZLE_STUDIO_TOKEN``, then a random one."""
    return explicit or os.environ.get("MIZZLE_STUDIO_TOKEN") or secrets.token_urlsafe(32)


def resolve_auth(explicit: str | None = None) -> str | None:
    """``--auth``, then ``MIZZLE_STUDIO_AUTH``.

    The environment form exists so the password need not be in argv, where ``ps`` shows it.
    """
    return explicit or os.environ.get("MIZZLE_STUDIO_AUTH") or None


def check_token_length(token: str) -> None:
    """Refuse a remote bind with a guessable token. A loopback bind is fine: it is reachable
    only from this machine, and a short token there is just a session marker."""
    if len(token) < MIN_TOKEN_LENGTH:
        raise MigrationError(
            f"a bearer token for a remote bind needs at least {MIN_TOKEN_LENGTH} characters "
            f"(got {len(token)}); omit --token to have one generated"
        )


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
        # remote bind is behind a reverse proxy, whose public origin is passed in.
        self.origins = origins

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
        """
        if request.method in _SAFE_METHODS:
            return False
        fetch_site = request.headers.get("sec-fetch-site", "")
        if fetch_site in ("cross-site", "same-site"):
            return True
        raw = request.headers.get("origin")
        if raw is None:  # not a browser request (curl, a test client): no CSRF surface
            return False
        return raw not in (self.origins or set())

    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            self.allowed_hosts is not None
            and request.headers.get("host", "") not in self.allowed_hosts
        ):
            return self._error("forbidden host", 403)
        if request.url.path.startswith("/api/") and not self._authorized(request):
            headers = {"WWW-Authenticate": 'Basic realm="mizzle studio"'} if self.basic else None
            return self._error("missing or wrong credentials", 401, headers)
        if request.url.path.startswith("/api/") and self._cross_site(request):
            return self._error("cross-site request refused", 403)
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
        studio.load_state()
        try:
            yield
        finally:
            await studio.driver.close()

    tables_path = "/api/tables/{schema}/{table}/rows"
    routes: list[BaseRoute] = [
        Route("/api/meta", _api(studio, meta)),
        Route("/api/tables", _api(studio, tables)),
        Route("/api/schema/relationships", _api(studio, relationships)),
        Route(tables_path, _api(studio, list_rows), methods=["GET"]),
        Route(tables_path, _api(studio, insert_row), methods=["POST"]),
        Route(tables_path, _api(studio, update_row), methods=["PATCH"]),
        Route(tables_path, _api(studio, delete_rows), methods=["DELETE"]),
        Route(
            "/api/tables/{schema}/{table}/export",
            _api(studio, export_rows),
            methods=["GET"],
        ),
        Route("/api/sql", _api(studio, run_sql), methods=["POST"]),
        Route("/api/ddl/preview", _api(studio, ddl_preview), methods=["POST"]),
        Route("/api/ddl/apply", _api(studio, ddl_apply), methods=["POST"]),
        Route("/api/queries/history", _api(studio, get_history), methods=["GET"]),
        Route("/api/queries/history", _api(studio, clear_history), methods=["DELETE"]),
        Route("/api/queries/saved", _api(studio, list_saved), methods=["GET"]),
        Route("/api/queries/saved/{name}", _api(studio, put_saved), methods=["PUT"]),
        Route("/api/queries/saved/{name}", _api(studio, delete_saved), methods=["DELETE"]),
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


def _is_superuser(url: str) -> bool:
    """Whether the configured URL connects as a superuser. Best effort: it opens one
    connection, and a failure (unreachable, no permission) just means no warning."""
    import asyncio

    async def check() -> bool:
        try:
            conn = await asyncpg.connect(url)
        except Exception:
            return False
        try:
            rows = await conn.fetch(
                "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
            )
            [row] = cast("Sequence[Sequence[object]]", rows)
            return bool(row[0])
        finally:
            await conn.close()

    try:
        return asyncio.run(check())
    except Exception:
        return False


def _server_origins(host: str, port: int) -> set[str]:
    """The origins a same-site browser request can carry.

    Loopback is unambiguous: the UI is served from one of these three. A remote bind sits
    behind a TLS reverse proxy whose public origin the server cannot derive, so
    ``MIZZLE_STUDIO_ORIGIN`` (comma-separated) supplies it; without it a browser request
    carrying an ``Origin`` header is refused, which fails closed."""
    if host in {"127.0.0.1", "localhost", "::1"}:
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}
    extra = os.environ.get("MIZZLE_STUDIO_ORIGIN", "")
    origins = {o.strip().rstrip("/") for o in extra.split(",") if o.strip()}
    return origins or {f"http://{host}:{port}", f"https://{host}:{port}"}


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
    if not loopback and basic_auth is None:
        check_token_length(token_value)
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
    if read_only and _is_superuser(config.url()):
        print(
            "warning: Studio is read-only, but this connection is a superuser. Read-only "
            "blocks writes, not server-side abilities (COPY ... PROGRAM, pg_read_file, "
            "pg_terminate_backend are refused, nothing else is). Use a non-superuser role.",
            file=sys.stderr,
        )
    if loopback:
        link = f"http://127.0.0.1:{port}/#token={token_value}"
        print(f"mizzle studio{mode} running at {link}\nPress Ctrl+C to stop.")
        if open_browser:
            webbrowser.open(link)
    else:
        how = "HTTP Basic" if basic_auth else "the bearer token"
        print(
            f"mizzle studio{mode} listening on {host}:{port} (authenticate with {how}).\n"
            "Press Ctrl+C to stop."
        )
    uvicorn.run(app, host=host, port=port, log_level="warning")

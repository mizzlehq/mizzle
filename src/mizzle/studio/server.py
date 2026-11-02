"""Studio backend: a local JSON API over the live database plus the built React UI.

Security model (Studio can read and change data):
- binds to 127.0.0.1 only, and rejects other Host headers (DNS rebinding);
- every /api request needs ``Authorization: Bearer <token>``; the random token is only
  in the URL fragment that ``mizzle studio`` prints, which browsers never send to servers;
- no CORS headers, so other websites cannot call the API from a browser;
- ``--read-only`` blocks every write, and raw SQL runs in a read-only transaction;
- table and column names are checked against the introspected schema and quoted;
  values are always bind parameters.
"""

from __future__ import annotations

import datetime
import enum
import hmac
import json
import secrets
import webbrowser
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, override

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from mizzle.core.errors import MizzleError
from mizzle.core.snapshot import ColumnSnap, Snapshot, TableSnap, key
from mizzle.dialects.base import Connection, Dialect, Driver
from mizzle.dialects.postgres.ddl import SERIAL_TYPES, qualified, quote
from mizzle.kit.config import Config

STATIC_DIR = Path(__file__).parent / "static"
MAX_SQL_ROWS = 1000
MAX_PAGE = 500
_COMPARE = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}

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
    order = q.get("order")
    if order:
        column = _column(table, order)
        direction = "DESC" if q.get("dir") == "desc" else "ASC"
        order_sql = f" ORDER BY t.{quote(column['name'])} {direction}"
    elif table["primary_key"]:
        order_sql = " ORDER BY " + ", ".join(
            f"t.{quote(c)}" for c in table["primary_key"]["columns"]
        )
    else:
        order_sql = ""
    source = f"{qualified(table['schema'], table['name'])} AS t"
    async with studio.pool().acquire() as conn:
        [total] = await conn.fetch(f"SELECT count(*) FROM {source}{where}", params)
        rows = await conn.fetch(
            f"SELECT to_jsonb(t) FROM {source}{where}{order_sql} LIMIT {limit} OFFSET {offset}",
            params,
        )
    return JSONResponse({"total": total[0], "rows": [jsonable(r[0]) for r in rows]})


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


async def run_sql(studio: Studio, request: Request) -> Response:
    text = (await _body(request)).get("sql")
    if not isinstance(text, str) or not text.strip():
        raise StudioError("sql must be a non-empty string")

    async def fetch(conn: Connection) -> list[dict[str, object]]:
        return await conn.fetch_dicts(text, [])

    async with studio.pool().acquire() as conn:
        if studio.read_only:
            async with conn.transaction(readonly=True):
                rows = await fetch(conn)
        else:
            rows = await fetch(conn)
    columns = list(rows[0].keys()) if rows else []
    return JSONResponse(
        {
            "columns": columns,
            "rows": [[jsonable(r[c]) for c in columns] for r in rows[:MAX_SQL_ROWS]],
            "truncated": len(rows) > MAX_SQL_ROWS,
            "count": len(rows),
        }
    )


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


class _Guard(BaseHTTPMiddleware):
    expected: bytes
    allowed_hosts: set[str]

    def __init__(self, app: ASGIApp, token: str, allowed_hosts: set[str]) -> None:
        super().__init__(app)
        self.expected = f"Bearer {token}".encode()
        self.allowed_hosts = allowed_hosts

    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.headers.get("host", "") not in self.allowed_hosts:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        if request.url.path.startswith("/api/"):
            given = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(given, self.expected):
                return JSONResponse({"error": "missing or wrong token"}, status_code=401)
        response = await call_next(request)
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
    allowed_hosts: set[str],
    read_only: bool = False,
    static_dir: Path = STATIC_DIR,
) -> Starlette:
    studio = Studio(dialect, url, schemas, read_only)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        studio.driver = await dialect.connect(url, pool_size=4, statement_cache_size=100)
        try:
            yield
        finally:
            await studio.driver.close()

    tables_path = "/api/tables/{schema}/{table}/rows"
    routes: list[BaseRoute] = [
        Route("/api/meta", _api(studio, meta)),
        Route("/api/tables", _api(studio, tables)),
        Route(tables_path, _api(studio, list_rows), methods=["GET"]),
        Route(tables_path, _api(studio, insert_row), methods=["POST"]),
        Route(tables_path, _api(studio, update_row), methods=["PATCH"]),
        Route(tables_path, _api(studio, delete_rows), methods=["DELETE"]),
        Route("/api/sql", _api(studio, run_sql), methods=["POST"]),
    ]
    if (static_dir / "index.html").exists():
        routes.append(Mount("/", StaticFiles(directory=static_dir, html=True)))
    else:
        routes.append(Route("/", _not_built))
    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(_Guard, token=token, allowed_hosts=allowed_hosts)],
    )


def serve(
    config: Config,
    *,
    port: int,
    read_only: bool,
    open_browser: bool,
    ui_dir: str | None = None,
) -> None:
    import uvicorn

    from mizzle.studio.assets import ui_path

    static_dir = ui_path(ui_dir)  # downloads and verifies the UI on first run
    token = secrets.token_urlsafe(32)
    app = create_app(
        dialect=config.dialect(),
        url=config.url(),
        schemas=config.schema_filter,
        token=token,
        allowed_hosts={f"127.0.0.1:{port}", f"localhost:{port}"},
        read_only=read_only,
        static_dir=static_dir,
    )
    link = f"http://127.0.0.1:{port}/#token={token}"
    mode = " (read-only)" if read_only else ""
    print(f"mizzle studio{mode} running at {link}\nPress Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(link)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

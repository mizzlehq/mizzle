from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from mizzle.core.errors import MigrationError
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.studio.server import (
    _basic_credentials,
    create_app,
    parse_auth,
    resolve_token,
    split_statements,
)

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROWS = "/api/tables/public/users/rows"
EXPORT = "/api/tables/public/users/export"


def make_client(
    pg_url: str,
    read_only: bool = False,
    static: Path | None = None,
    state_path: Path | None = None,
    basic_auth: tuple[str, str] | None = None,
) -> TestClient:
    kwargs: dict[str, Any] = {}
    if static is not None:
        kwargs["static_dir"] = static
    if state_path is not None:
        kwargs["state_path"] = state_path
    if basic_auth is not None:
        kwargs["basic_auth"] = basic_auth
    app = create_app(
        dialect=PostgresDialect(),
        url=pg_url,
        schemas=["public"],
        token=TOKEN,
        allowed_hosts={"testserver"},
        read_only=read_only,
        **kwargs,
    )
    return TestClient(app)


def test_parse_auth_accepts_user_and_password() -> None:
    assert parse_auth("admin:secret") == ("admin", "secret")
    assert parse_auth("admin:pa:ss") == ("admin", "pa:ss")
    for bad in ("nocolon", "user:", ":pass"):
        with pytest.raises(MigrationError):
            parse_auth(bad)


def test_resolve_token_prefers_explicit_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_token("given") == "given"
    monkeypatch.setenv("MIZZLE_STUDIO_TOKEN", "from-env")
    assert resolve_token() == "from-env"
    assert resolve_token("given") == "given"  # explicit still wins
    monkeypatch.delenv("MIZZLE_STUDIO_TOKEN")
    token = resolve_token()
    assert token and token != "given"


def test_basic_auth_guards_the_api(pg_url: str, driver: PostgresDriver) -> None:
    with make_client(pg_url, basic_auth=("admin", "secret")) as client:
        unauthorized = client.get("/api/meta")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"] == 'Basic realm="mizzle studio"'

        wrong = client.get(
            "/api/meta", headers={"Authorization": _basic_credentials("admin", "nope").decode()}
        )
        assert wrong.status_code == 401

        good = client.get(
            "/api/meta", headers={"Authorization": _basic_credentials("admin", "secret").decode()}
        )
        assert good.status_code == 200
        assert good.json()["dialect"] == "postgresql"

        # The bearer token keeps working alongside Basic auth.
        assert client.get("/api/meta", headers=AUTH).status_code == 200


def test_split_statements_handles_quotes_and_comments() -> None:
    assert split_statements("select 1; select 2") == ["select 1", "select 2"]
    assert split_statements("select 'a;b'") == ["select 'a;b'"]
    assert split_statements('select "we;ird"') == ['select "we;ird"']
    assert split_statements("select $$a;b$$") == ["select $$a;b$$"]
    assert split_statements("select $tag$x;y$tag$") == ["select $tag$x;y$tag$"]
    assert split_statements("-- note;\nselect 1;") == ["-- note;\nselect 1"]
    assert split_statements("/* a;b */ select 1;") == ["/* a;b */ select 1"]
    assert split_statements("  ") == []
    assert split_statements("-- only a comment") == []


@pytest.fixture
def client(pg_url: str, driver: PostgresDriver) -> Iterator[TestClient]:
    with make_client(pg_url) as c:
        yield c


def test_requires_token_and_host(client: TestClient) -> None:
    assert client.get("/api/meta").status_code == 401
    assert client.get("/api/meta", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/meta", headers={**AUTH, "Host": "evil.example"}).status_code == 403
    response = client.get("/api/meta", headers=AUTH)
    assert response.json() == {"read_only": False, "dialect": "postgresql"}
    assert response.headers["x-frame-options"] == "DENY"
    assert "access-control-allow-origin" not in response.headers


def test_tables_lists_schema(client: TestClient) -> None:
    tables = client.get("/api/tables", headers=AUTH).json()["tables"]
    users = next(t for t in tables if t["name"] == "users")
    assert list(users["columns"]) == ["id", "email", "bio", "role", "created_at"]
    assert users["primary_key"]["columns"] == ["id"]


def test_relationships_endpoint(client: TestClient) -> None:
    data = client.get("/api/schema/relationships", headers=AUTH).json()
    nodes = {f"{t['schema']}.{t['name']}": t for t in data["tables"]}
    assert {"public.users", "public.posts"} <= set(nodes)
    assert "id" in nodes["public.users"]["columns"]

    edge = next(e for e in data["relationships"] if e["from_table"] == "posts")
    assert edge["from_schema"] == "public"
    assert edge["from_columns"] == ["author_id"]
    assert edge["to_table"] == "users" and edge["to_columns"] == ["id"]


def test_crud_flow(client: TestClient) -> None:
    created = client.post(ROWS, json={"values": {"email": "ann", "role": "admin"}}, headers=AUTH)
    assert created.status_code == 201, created.text
    row = created.json()["row"]
    assert row["email"] == "ann" and row["role"] == "admin"
    client.post(ROWS, json={"values": {"email": "bob"}}, headers=AUTH)

    page = client.get(ROWS, params={"limit": 1, "order": "email", "dir": "desc"}, headers=AUTH)
    assert page.json()["total"] == 2
    assert [r["email"] for r in page.json()["rows"]] == ["bob"]

    filters = '[{"column": "role", "op": "eq", "value": "admin"}]'
    filtered = client.get(ROWS, params={"filters": filters}, headers=AUTH).json()
    assert [r["email"] for r in filtered["rows"]] == ["ann"]
    like = '[{"column": "email", "op": "ilike", "value": "B%"}]'
    assert client.get(ROWS, params={"filters": like}, headers=AUTH).json()["total"] == 1

    updated = client.patch(
        ROWS, json={"key": {"id": row["id"]}, "values": {"bio": "hi"}}, headers=AUTH
    )
    assert updated.json()["row"]["bio"] == "hi"
    missing = client.patch(ROWS, json={"key": {"id": -1}, "values": {"bio": "x"}}, headers=AUTH)
    assert missing.status_code == 404

    deleted = client.request("DELETE", ROWS, json={"keys": [{"id": row["id"]}]}, headers=AUTH)
    assert deleted.json() == {"deleted": 1}


def test_names_are_checked_not_injected(client: TestClient) -> None:
    evil = '[{"column": "email\\" = \'\' OR 1=1 --", "op": "eq", "value": "x"}]'
    assert client.get(ROWS, params={"filters": evil}, headers=AUTH).status_code == 400
    assert (
        client.get(ROWS, params={"order": "id; DROP TABLE users"}, headers=AUTH).status_code == 400
    )
    bad_table = "/api/tables/public/users%3B%20DROP%20TABLE%20users/rows"
    assert client.get(bad_table, headers=AUTH).status_code == 404
    value = '[{"column": "email", "op": "eq", "value": "x\' OR \'1\'=\'1"}]'
    assert client.get(ROWS, params={"filters": value}, headers=AUTH).json()["total"] == 0


def test_sql_endpoint(client: TestClient) -> None:
    client.post(ROWS, json={"values": {"email": "ann"}}, headers=AUTH)
    result = client.post(
        "/api/sql",
        json={"sql": "select email, created_at, 1.5::numeric as n from users"},
        headers=AUTH,
    ).json()
    assert result["columns"] == ["email", "created_at", "n"]
    assert result["rows"][0][0] == "ann"
    assert result["rows"][0][2] == "1.5"
    error = client.post("/api/sql", json={"sql": "select nope"}, headers=AUTH)
    assert error.status_code == 400 and "nope" in error.json()["error"]


def test_bad_requests_are_reported(client: TestClient) -> None:
    bad_json = client.post(
        ROWS, content=b"not json", headers={**AUTH, "content-type": "application/json"}
    )
    assert bad_json.status_code == 400
    assert client.post(ROWS, json={"values": "x"}, headers=AUTH).status_code == 400
    assert (
        client.patch(ROWS, json={"key": {"id": 1}, "values": {}}, headers=AUTH).status_code == 400
    )
    wrong_key = client.patch(
        ROWS, json={"key": {"id": 1, "email": "a"}, "values": {"bio": "x"}}, headers=AUTH
    )
    assert wrong_key.status_code == 400
    assert client.request("DELETE", ROWS, json={"keys": []}, headers=AUTH).status_code == 400
    bad_op = '[{"column": "email", "op": "nope", "value": "x"}]'
    assert client.get(ROWS, params={"filters": bad_op}, headers=AUTH).status_code == 400
    assert client.get(ROWS, params={"limit": "-1"}, headers=AUTH).status_code == 400
    assert client.post("/api/sql", json={"sql": "  "}, headers=AUTH).status_code == 400
    assert client.put("/api/queries/saved/x", json={"sql": ""}, headers=AUTH).status_code == 400


def test_read_only_mode(pg_url: str, driver: PostgresDriver) -> None:
    with make_client(pg_url, read_only=True) as client:
        assert client.post(ROWS, json={"values": {"email": "x"}}, headers=AUTH).status_code == 403
        assert (
            client.patch(ROWS, json={"key": {"id": 1}, "values": {}}, headers=AUTH).status_code
            == 403
        )
        write = client.post("/api/sql", json={"sql": "delete from users"}, headers=AUTH)
        assert write.status_code == 400
        assert "read-only" in write.json()["error"]
        assert (
            client.post("/api/sql", json={"sql": "select 1 as x"}, headers=AUTH).status_code == 200
        )


def test_serves_ui_without_token(pg_url: str, driver: PostgresDriver, tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>studio</title>")
    with make_client(pg_url, static=tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200 and "studio" in page.text
        assert "default-src 'self'" in page.headers["content-security-policy"]


def test_export_csv_and_json(client: TestClient) -> None:
    client.post(ROWS, json={"values": {"email": "ann"}}, headers=AUTH)
    client.post(ROWS, json={"values": {"email": "bob"}}, headers=AUTH)

    csv_resp = client.get(EXPORT, params={"format": "csv", "order": "email"}, headers=AUTH)
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert csv_resp.headers["content-disposition"] == 'attachment; filename="public.users.csv"'
    lines = csv_resp.text.strip().splitlines()
    assert lines[0].split(",") == ["id", "email", "bio", "role", "created_at"]
    assert "ann" in lines[1] and "bob" in lines[2]

    filters = '[{"column": "email", "op": "eq", "value": "ann"}]'
    js = client.get(EXPORT, params={"format": "json", "filters": filters}, headers=AUTH).json()
    assert [r["email"] for r in js] == ["ann"]
    assert client.get(EXPORT, params={"format": "xml"}, headers=AUTH).status_code == 400


def test_sql_console_runs_multiple_statements(client: TestClient) -> None:
    client.post(ROWS, json={"values": {"email": "ann"}}, headers=AUTH)
    result = client.post(
        "/api/sql",
        json={"sql": "select email from users; select count(*) as n from users"},
        headers=AUTH,
    ).json()
    assert [r["columns"] for r in result["results"]] == [["email"], ["n"]]
    assert result["results"][0]["rows"][0][0] == "ann"
    assert result["results"][1]["rows"][0][0] == 1
    assert "columns" not in result  # the flat shape is only for a single statement

    literal = client.post("/api/sql", json={"sql": "select 'a;b' as s"}, headers=AUTH).json()
    assert literal["rows"] == [["a;b"]] and len(literal["results"]) == 1
    assert literal["columns"] == ["s"]  # a single statement still mirrors the flat shape


def test_query_history_records_success_and_failure(client: TestClient) -> None:
    client.post("/api/sql", json={"sql": "select 1"}, headers=AUTH)
    client.post("/api/sql", json={"sql": "select nope"}, headers=AUTH)
    history = client.get("/api/queries/history", headers=AUTH).json()["history"]
    assert [h["sql"] for h in history] == ["select 1", "select nope"]
    assert [h["ok"] for h in history] == [True, False]
    cleared = client.request("DELETE", "/api/queries/history", headers=AUTH)
    assert cleared.json() == {"cleared": True}
    assert client.get("/api/queries/history", headers=AUTH).json()["history"] == []


def test_saved_queries_round_trip(client: TestClient) -> None:
    assert client.get("/api/queries/saved", headers=AUTH).json() == {"saved": []}
    created = client.put("/api/queries/saved/top", json={"sql": "select 1"}, headers=AUTH)
    assert created.status_code == 201
    assert client.get("/api/queries/saved", headers=AUTH).json()["saved"] == [
        {"name": "top", "sql": "select 1"}
    ]
    assert client.request("DELETE", "/api/queries/saved/nope", headers=AUTH).status_code == 404
    assert client.request("DELETE", "/api/queries/saved/top", headers=AUTH).json() == {
        "deleted": "top"
    }
    assert client.get("/api/queries/saved", headers=AUTH).json()["saved"] == []


def test_state_persists_between_runs(pg_url: str, driver: PostgresDriver, tmp_path: Path) -> None:
    state = tmp_path / "studio.json"
    with make_client(pg_url, state_path=state) as client:
        client.put("/api/queries/saved/top", json={"sql": "select 42"}, headers=AUTH)
        client.post("/api/sql", json={"sql": "select 1"}, headers=AUTH)
    assert state.exists()
    with make_client(pg_url, state_path=state) as client:
        assert client.get("/api/queries/saved", headers=AUTH).json()["saved"] == [
            {"name": "top", "sql": "select 42"}
        ]
        history = client.get("/api/queries/history", headers=AUTH).json()["history"]
        assert history[-1]["sql"] == "select 1"


DDL_PREVIEW = "/api/ddl/preview"
DDL_APPLY = "/api/ddl/apply"


def test_ddl_preview_and_apply(client: TestClient) -> None:
    client.post("/api/sql", json={"sql": "drop table if exists st_notes"}, headers=AUTH)
    create = {
        "operation": "create_table",
        "schema": "public",
        "name": "st_notes",
        "columns": [
            {"name": "id", "type": "integer", "primary_key": True},
            {"name": "title", "type": "text", "not_null": True},
        ],
    }
    preview = client.post(DDL_PREVIEW, json=create, headers=AUTH)
    assert preview.status_code == 200, preview.text
    assert preview.json()["statements"] == [
        {
            "sql": (
                'CREATE TABLE "st_notes" (\n'
                '    "id" integer,\n'
                '    "title" text NOT NULL,\n'
                '    CONSTRAINT "st_notes_pkey" PRIMARY KEY ("id")\n'
                ")"
            ),
            "description": "create table st_notes",
            "data_loss": False,
        }
    ]

    applied = client.post(DDL_APPLY, json=create, headers=AUTH)
    assert applied.status_code == 200 and applied.json()["applied"] == 1
    tables = client.get("/api/tables", headers=AUTH).json()["tables"]
    assert any(t["name"] == "st_notes" for t in tables)

    add = {
        "operation": "add_column",
        "schema": "public",
        "table": "st_notes",
        "name": "body",
        "column": {"name": "body", "type": "text"},
    }
    client.post(DDL_APPLY, json=add, headers=AUTH)
    notes = next(
        t
        for t in client.get("/api/tables", headers=AUTH).json()["tables"]
        if t["name"] == "st_notes"
    )
    assert "body" in notes["columns"]

    # An existing table or column is refused, and bad operations never reach SQL.
    assert client.post(DDL_APPLY, json=create, headers=AUTH).status_code == 400
    bad = {
        "operation": "add_column",
        "schema": "public",
        "table": "st_notes",
        "name": "x",
        "column": {"name": "x", "type": "text; drop table users"},
    }
    assert client.post(DDL_PREVIEW, json=bad, headers=AUTH).status_code == 400
    assert (
        client.post(
            DDL_PREVIEW, json={"operation": "truncate", "name": "x"}, headers=AUTH
        ).status_code
        == 400
    )

    dropped = client.post(
        DDL_APPLY,
        json={"operation": "drop_table", "schema": "public", "name": "st_notes"},
        headers=AUTH,
    )
    assert dropped.json()["applied"] == 1
    assert not any(
        t["name"] == "st_notes" for t in client.get("/api/tables", headers=AUTH).json()["tables"]
    )


def test_ddl_preview_works_but_apply_is_blocked_read_only(
    pg_url: str, driver: PostgresDriver
) -> None:
    op = {
        "operation": "create_table",
        "schema": "public",
        "name": "st_ro",
        "columns": [{"name": "id", "type": "integer"}],
    }
    with make_client(pg_url, read_only=True) as client:
        assert client.post(DDL_PREVIEW, json=op, headers=AUTH).status_code == 200
        assert client.post(DDL_APPLY, json=op, headers=AUTH).status_code == 403

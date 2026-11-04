from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.studio.server import create_app, split_statements

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROWS = "/api/tables/public/users/rows"
EXPORT = "/api/tables/public/users/export"


def make_client(
    pg_url: str,
    read_only: bool = False,
    static: Path | None = None,
    state_path: Path | None = None,
) -> TestClient:
    kwargs: dict[str, Any] = {}
    if static is not None:
        kwargs["static_dir"] = static
    if state_path is not None:
        kwargs["state_path"] = state_path
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

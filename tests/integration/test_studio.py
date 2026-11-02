from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.studio.server import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROWS = "/api/tables/public/users/rows"


def make_client(pg_url: str, read_only: bool = False, static: Path | None = None) -> TestClient:
    kwargs: dict[str, Any] = {} if static is None else {"static_dir": static}
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

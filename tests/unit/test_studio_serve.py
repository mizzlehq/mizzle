"""`serve()`'s start-up checks: what it refuses, before any server or UI download happens.

C2: the module was at 88% coverage because these checks had no tests. They are the ST7
"refuse a remote bind without credentials" logic, so they matter more than their size.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from mizzle.core.errors import MigrationError
from mizzle.kit.config import Config
from mizzle.studio import server as studio_server


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:54329/db")
    root = Path("/tmp/mizzle-serve-test")
    return Config(root=root, schema=["schema"], out=root / "migrations")


@pytest.fixture
def no_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never download the UI or listen; a fake app is enough."""
    monkeypatch.setattr(studio_server, "create_app", lambda **_kw: object())
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=_record_run))


def _record_run(app: object, *, host: str, port: int, log_level: str) -> None:
    """uvicorn.run, without listening."""
    SERVED.append((app, host, port))


SERVED: list[tuple[object, str, int]] = []
OPENED: list[str] = []


@pytest.fixture(autouse=True)
def no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never launch a browser from a test (it really would open one)."""
    SERVED.clear()
    OPENED.clear()
    monkeypatch.setattr(studio_server, "webbrowser", types.SimpleNamespace(open=OPENED.append))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MIZZLE_STUDIO_TOKEN", "MIZZLE_STUDIO_AUTH", "MIZZLE_STUDIO_ORIGIN"):
        monkeypatch.delenv(name, raising=False)


def test_remote_bind_without_credentials_is_refused(config: Config, no_ui: None) -> None:
    with pytest.raises(MigrationError, match="without credentials"):
        studio_server.serve(config, port=4983, read_only=False, open_browser=False, host="0.0.0.0")


def test_remote_bind_accepts_a_basic_auth_password(config: Config, no_ui: None) -> None:
    studio_server.serve(
        config,
        port=4983,
        read_only=False,
        open_browser=False,
        host="0.0.0.0",
        auth="admin:correct-horse",
    )
    assert SERVED


def test_remote_bind_accepts_basic_auth_from_the_environment(
    config: Config, no_ui: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SS4: the environment form keeps the password out of `ps`.
    monkeypatch.setenv("MIZZLE_STUDIO_AUTH", "admin:correct-horse")
    studio_server.serve(config, port=4983, read_only=False, open_browser=False, host="0.0.0.0")
    assert SERVED


def test_remote_bind_rejects_a_short_token(config: Config, no_ui: None) -> None:
    # SS4: a one-character token is guessable, so a remote bind needs a real one.
    with pytest.raises(MigrationError, match="at least 32 characters"):
        studio_server.serve(
            config, port=4983, read_only=False, open_browser=False, host="0.0.0.0", token="a"
        )
    studio_server.serve(
        config,
        port=4983,
        read_only=False,
        open_browser=False,
        host="0.0.0.0",
        token="t" * 32,
    )
    assert SERVED


def test_loopback_opens_the_token_link(config: Config, no_ui: None) -> None:
    token = "t" * 32
    studio_server.serve(
        config, port=4983, read_only=False, open_browser=True, host="127.0.0.1", token=token
    )
    assert SERVED
    # The URL it "opens" carries the token in the fragment, so it never reaches the server.
    link = f"http://127.0.0.1:4983/#token={token}"
    assert [link] == OPENED


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1"])
def test_short_token_is_refused_in_every_mode(
    config: Config, no_ui: None, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    # B11: with --auth set the token length was not checked, yet the bearer token was
    # still accepted: --auth admin:x --token a let anyone in with "Bearer a".
    with pytest.raises(MigrationError, match="at least 32 characters"):
        studio_server.serve(
            config,
            port=4983,
            read_only=False,
            open_browser=False,
            host=host,
            token="a",
            auth="admin:correct-horse",
        )
    monkeypatch.setenv("MIZZLE_STUDIO_TOKEN", "short")
    with pytest.raises(MigrationError, match="at least 32 characters"):
        studio_server.serve(config, port=4983, read_only=False, open_browser=False, host=host)
    assert not SERVED


def test_short_basic_password_is_refused(config: Config, no_ui: None) -> None:
    # B11: --auth admin:x was accepted.
    with pytest.raises(MigrationError, match="at least 12 characters"):
        studio_server.serve(
            config, port=4983, read_only=False, open_browser=False, host="0.0.0.0", auth="admin:x"
        )
    assert not SERVED


def test_read_only_refuses_a_privileged_role(
    config: Config, no_ui: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B18: read-only is only a promise for a role without server-side abilities.
    async def privileged(_dialect: object, _url: str) -> list[str]:
        return ["mizzle (superuser)"]

    monkeypatch.setattr(studio_server, "privileged_roles", privileged)
    with pytest.raises(MigrationError, match=r"read-only.*mizzle \(superuser\)"):
        studio_server.serve(config, port=4983, read_only=True, open_browser=False)
    assert not SERVED

    async def plain(_dialect: object, _url: str) -> list[str]:
        return []

    monkeypatch.setattr(studio_server, "privileged_roles", plain)
    studio_server.serve(config, port=4983, read_only=True, open_browser=False)
    assert SERVED


def test_origins_are_the_loopback_urls(config: Config) -> None:
    origins = studio_server._server_origins("127.0.0.1", 4983)
    assert origins == {
        "http://127.0.0.1:4983",
        "http://localhost:4983",
        "http://[::1]:4983",
    }


def test_origins_for_a_remote_bind_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A remote bind is behind a proxy, so its public origin cannot be derived here.
    monkeypatch.setenv("MIZZLE_STUDIO_ORIGIN", "https://studio.example.com, https://b.example")
    assert studio_server._server_origins("10.0.0.5", 80) == {
        "https://studio.example.com",
        "https://b.example",
    }


def test_resolve_auth_prefers_the_argument_then_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert studio_server.resolve_auth("a:b") == "a:b"
    monkeypatch.setenv("MIZZLE_STUDIO_AUTH", "from:env")
    assert studio_server.resolve_auth() == "from:env"
    assert studio_server.resolve_auth("a:b") == "a:b"
    monkeypatch.delenv("MIZZLE_STUDIO_AUTH")
    assert studio_server.resolve_auth() is None


def test_token_length_check_names_the_limit() -> None:
    studio_server.check_token_length("t" * 32)
    with pytest.raises(MigrationError, match="at least 32"):
        studio_server.check_token_length("t" * 31)

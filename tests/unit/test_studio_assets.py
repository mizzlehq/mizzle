"""Studio UI download: verification, safe unpacking, and caching. Runs on Linux, macOS
and Windows in CI (job `studio-assets`)."""

import hashlib
import stat
import zipfile
from pathlib import Path

import pytest

from mizzle.studio import assets
from mizzle.studio.assets import (
    Manifest,
    StudioUIError,
    build_archive,
    download,
    read_manifest,
    write_manifest,
)

URL = "https://example.invalid/mizzle-studio-1.0.0.zip"


def make_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>studio</title>")
    (dist / "assets" / "index-abc.js").write_text("console.log('hi')")
    return dist


def zip_with(path: Path, entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return path.read_bytes()


def manifest_for(body: bytes) -> Manifest:
    return Manifest("1.0.0", URL, hashlib.sha256(body).hexdigest())


def test_build_download_and_cache(tmp_path: Path) -> None:
    archive = tmp_path / "ui.zip"
    digest = build_archive(make_dist(tmp_path), archive)
    body = archive.read_bytes()
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return body

    cache = tmp_path / "cache"
    ui = download(Manifest("1.0.0", URL, digest), cache, fetch)
    assert (ui / "index.html").read_text().startswith("<!doctype html>")
    assert (ui / "assets" / "index-abc.js").exists()
    assert calls == [URL]

    def no_network(url: str) -> bytes:
        raise AssertionError("must use the cache")

    assert download(Manifest("1.0.0", URL, digest), cache, no_network) == ui


def test_archive_is_reproducible(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    first = build_archive(dist, tmp_path / "a.zip")
    (dist / "index.html").touch()  # a newer modification time must not matter
    assert build_archive(dist, tmp_path / "b.zip") == first
    with zipfile.ZipFile(tmp_path / "a.zip") as zf:
        assert zf.namelist() == ["assets/index-abc.js", "index.html"]


def test_hash_mismatch_is_rejected_and_not_cached(tmp_path: Path) -> None:
    body = zip_with(tmp_path / "ui.zip", {"index.html": b"x"})
    wrong = Manifest("1.0.0", URL, "0" * 64)
    with pytest.raises(StudioUIError, match="does not match"):
        download(wrong, tmp_path / "cache", lambda _: body)
    assert not any((tmp_path / "cache").glob("*/index.html"))


@pytest.mark.parametrize(
    "name",
    [
        "../evil.js",
        "/etc/evil",
        "assets\\evil.js",
        "C:/evil.js",
        "a:stream",
        "CON.txt",
        "assets/nul",
        "trailing.",
        "space ",
        "bad?.js",
    ],
)
def test_unsafe_or_non_portable_names_are_rejected(tmp_path: Path, name: str) -> None:
    body = zip_with(tmp_path / "ui.zip", {"index.html": b"x", name: b"evil"})
    with pytest.raises(StudioUIError):
        download(manifest_for(body), tmp_path / "cache", lambda _: body)
    assert not (tmp_path / "evil.js").exists()


def test_links_and_case_clashes_are_rejected(tmp_path: Path) -> None:
    linked = zip_with(tmp_path / "a.zip", {"index.html": b"x"}, symlink="assets/link")
    with pytest.raises(StudioUIError, match="links"):
        download(manifest_for(linked), tmp_path / "c1", lambda _: linked)
    clash = zip_with(tmp_path / "b.zip", {"index.html": b"x", "App.js": b"1", "app.js": b"2"})
    with pytest.raises(StudioUIError, match="case"):
        download(manifest_for(clash), tmp_path / "c2", lambda _: clash)


def test_archive_without_index_is_rejected(tmp_path: Path) -> None:
    body = zip_with(tmp_path / "ui.zip", {"other.html": b"x"})
    with pytest.raises(StudioUIError, match=r"no index\.html"):
        download(manifest_for(body), tmp_path / "cache", lambda _: body)


def test_manifest_validation(tmp_path: Path) -> None:
    path = tmp_path / "ui.json"
    write_manifest(path, version="1.0.0", url=URL, sha256="a" * 64)
    assert read_manifest(path) == Manifest("1.0.0", URL, "a" * 64)
    path.write_text('{"version": "1", "url": "http://x/ui.zip", "sha256": "' + "a" * 64 + '"}')
    with pytest.raises(StudioUIError, match="https"):
        read_manifest(path)
    path.write_text('{"version": "1", "url": "https://x/ui.zip", "sha256": "nothex"}')
    with pytest.raises(StudioUIError, match="sha256"):
        read_manifest(path)
    assert read_manifest(tmp_path / "missing.json") is None


def test_ui_path_lookup_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    own = make_dist(tmp_path)
    monkeypatch.setattr(assets, "DEV_BUILD", tmp_path / "no-dev-build")
    assert assets.ui_path(str(own)) == own
    monkeypatch.setenv(assets.ENV_UI_PATH, str(own))
    assert assets.ui_path() == own
    monkeypatch.delenv(assets.ENV_UI_PATH)
    with pytest.raises(StudioUIError, match=r"no index\.html"):
        assets.ui_path(str(tmp_path))
    monkeypatch.setattr(assets, "MANIFEST", tmp_path / "missing.json")
    with pytest.raises(StudioUIError, match="--ui-path"):
        assets.ui_path()


def test_non_https_download_is_refused() -> None:
    with pytest.raises(StudioUIError, match="non-https"):
        assets._https_fetch("http://example.com/ui.zip")

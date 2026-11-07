"""Studio UI download: verification, safe unpacking, and caching. Runs on Linux, macOS
and Windows in CI (job `studio-assets`)."""

import hashlib
import stat
import sys
import urllib.request
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
            # Set .filename directly: ZipInfo(name) turns "\\" into "/" on Windows,
            # which would silently drop the backslash this test needs to write.
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            zf.writestr(info, data)
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


def test_install_root_prefers_the_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "python"))
    assert assets.install_root() == tmp_path / "venv" / "share" / "mizzle" / "studio"
    monkeypatch.setattr(assets, "_writable", lambda _path: False)  # read-only venv
    assert assets.install_root() == assets.cache_root()
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)  # no venv
    assert assets.install_root() == assets.cache_root()


def test_manifest_rejects_wrong_shapes(tmp_path: Path) -> None:
    path = tmp_path / "ui.json"
    path.write_text("[]")
    with pytest.raises(StudioUIError, match="JSON object"):
        read_manifest(path)
    path.write_text('{"version": "1", "url": "https://x/ui.zip"}')
    with pytest.raises(StudioUIError, match="string fields"):
        read_manifest(path)


def test_cache_root_per_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert assets.cache_root() == tmp_path / "local" / "mizzle" / "studio"
    monkeypatch.delenv("LOCALAPPDATA")
    # Path.home() reads USERPROFILE on Windows and HOME elsewhere: patch it directly.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert assets.cache_root() == tmp_path / "home" / "AppData" / "Local" / "mizzle" / "studio"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert assets.cache_root() == tmp_path / "home" / "Library" / "Caches" / "mizzle" / "studio"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert assets.cache_root() == tmp_path / "xdg" / "mizzle" / "studio"


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, _size: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def test_https_fetch_reads_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(b"hello"))
    assert assets._https_fetch(URL) == b"hello"
    big = b"a" * (assets.MAX_ARCHIVE_BYTES + 1)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(big))
    with pytest.raises(StudioUIError, match="larger"):
        assets._https_fetch(URL)


def test_unpacked_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assets, "MAX_UNPACKED_BYTES", 3)
    body = zip_with(tmp_path / "ui.zip", {"index.html": b"12345"})
    with pytest.raises(StudioUIError, match="unpacks to more"):
        download(manifest_for(body), tmp_path / "cache", lambda _: body)


def test_directory_entries_are_created(tmp_path: Path) -> None:
    path = tmp_path / "ui.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("assets/"), b"")
        zf.writestr("index.html", b"x")
        zf.writestr("assets/app.js", b"1")
    body = path.read_bytes()
    ui = download(manifest_for(body), tmp_path / "cache", lambda _: body)
    assert (ui / "assets").is_dir() and (ui / "assets" / "app.js").exists()


def test_rename_race_keeps_the_other_process_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = zip_with(tmp_path / "ui.zip", {"index.html": b"x"})
    manifest = manifest_for(body)
    real_rename = Path.rename

    def flaky(self: Path, target: str | Path) -> Path:
        if self.name == "ui":  # the freshly unpacked folder: pretend another process won
            Path(target).mkdir(parents=True, exist_ok=True)
            (Path(target) / "index.html").write_text("x")
            raise OSError("busy")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    ui = download(manifest, tmp_path / "cache", lambda _: body)
    assert (ui / "index.html").exists()


def test_rename_failure_without_a_winner_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = zip_with(tmp_path / "ui.zip", {"index.html": b"x"})
    manifest = manifest_for(body)
    real_rename = Path.rename

    def broken(self: Path, target: str | Path) -> Path:
        if self.name == "ui":
            raise OSError("disk full")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", broken)
    with pytest.raises(OSError, match="disk full"):
        download(manifest, tmp_path / "cache", lambda _: body)


def test_ui_path_uses_dev_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = make_dist(tmp_path)
    monkeypatch.setattr(assets, "DEV_BUILD", dev)
    monkeypatch.delenv(assets.ENV_UI_PATH, raising=False)
    assert assets.ui_path() == dev


def test_ui_path_wraps_download_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assets, "DEV_BUILD", tmp_path / "no-dev-build")
    monkeypatch.setattr(assets, "read_manifest", lambda *_a, **_k: Manifest("1.0.0", URL, "a" * 64))
    monkeypatch.delenv(assets.ENV_UI_PATH, raising=False)

    def boom(*_args: object, **_kwargs: object) -> Path:
        raise OSError("no route to host")

    monkeypatch.setattr(assets, "download", boom)
    with pytest.raises(StudioUIError, match="could not download"):
        assets.ui_path()


def test_build_archive_needs_index(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    with pytest.raises(StudioUIError, match=r"no index\.html"):
        build_archive(tmp_path / "dist", tmp_path / "a.zip")


def test_build_archive_rejects_links(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    try:
        (dist / "link.js").symlink_to(dist / "index.html")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported here")
    with pytest.raises(StudioUIError, match="links"):
        build_archive(dist, tmp_path / "a.zip")


def test_main_packages_the_ui(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    archive = tmp_path / "ui.zip"
    manifest = tmp_path / "ui.json"
    assets._main(
        [
            str(dist),
            str(archive),
            "--version",
            "1.0.0",
            "--url",
            URL,
            "--manifest",
            str(manifest),
        ]
    )
    assert archive.exists()
    assert read_manifest(manifest) == Manifest(
        "1.0.0", URL, hashlib.sha256(archive.read_bytes()).hexdigest()
    )

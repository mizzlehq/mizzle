"""Find the Studio UI, downloading it on first use.

The built UI is not shipped in the wheel. Each release uploads
``mizzle-studio-<version>.zip`` to its GitHub Release and writes the archive's URL and
SHA-256 into ``ui.json`` inside the package. On first run the archive is downloaded over
HTTPS, checked against that hash, unpacked safely, and cached per version.

Lookup order:
1. ``--ui-path`` / ``MIZZLE_STUDIO_UI``: a folder with a built UI (offline use).
2. ``src/mizzle/studio/static``: a local ``pnpm build`` (development).
3. The per-version cache, downloading into it if needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPResponse
from pathlib import Path, PurePosixPath
from typing import cast

from mizzle.core.errors import MizzleError

PACKAGE_DIR = Path(__file__).parent
DEV_BUILD = PACKAGE_DIR / "static"
MANIFEST = PACKAGE_DIR / "ui.json"
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
ENV_UI_PATH = "MIZZLE_STUDIO_UI"

type Fetch = Callable[[str], bytes]


class StudioUIError(MizzleError):
    pass


@dataclass(frozen=True)
class Manifest:
    version: str
    url: str
    sha256: str


def read_manifest(path: Path = MANIFEST) -> Manifest | None:
    if not path.exists():
        return None
    data = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(data, dict):
        raise StudioUIError(f"{path} is not a JSON object")
    fields = cast(dict[str, object], data)  # JSON object keys are always strings
    version, url, digest = fields.get("version"), fields.get("url"), fields.get("sha256")
    if not (isinstance(version, str) and isinstance(url, str) and isinstance(digest, str)):
        raise StudioUIError(f"{path} needs string fields version, url and sha256")
    if not url.startswith("https://"):
        raise StudioUIError(f"{path}: the UI must be downloaded over https, not {url!r}")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise StudioUIError(f"{path}: sha256 must be 64 lowercase hex characters")
    return Manifest(version, url, digest)


def cache_root() -> Path:
    """The per-user cache folder for this OS."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "mizzle" / "studio"


def _https_fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise StudioUIError(f"refusing to download the UI over a non-https URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "mizzle-studio"})
    # typeshed types urlopen() as Any; for an https URL it returns an HTTPResponse.
    opened = cast(HTTPResponse, urllib.request.urlopen(request, timeout=30))
    with opened as response:
        body = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(body) > MAX_ARCHIVE_BYTES:
        raise StudioUIError(f"the UI archive is larger than {MAX_ARCHIVE_BYTES} bytes")
    return body


_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(10)
}


def _check_name(filename: str) -> PurePosixPath:
    """Reject names that are unsafe or not portable to Windows, macOS and Linux."""
    name = PurePosixPath(filename)  # zip paths always use "/" (ZIP spec 4.4.17)
    bad = (
        name.is_absolute()
        or "\\" in filename  # a Windows-made archive with backslash separators
        or any(
            part in ("", ".", "..")
            or ":" in part  # drive letters, NTFS alternate data streams
            or part.endswith((" ", "."))  # Windows silently strips these
            or part.split(".")[0].lower() in _WINDOWS_RESERVED
            or any(ord(c) < 32 or c in '<>"|?*' for c in part)
            for part in name.parts
        )
    )
    if bad or not name.parts:
        raise StudioUIError(f"unsafe or non-portable path in UI archive: {filename!r}")
    return name


def _safe_extract(archive: Path, target: Path) -> None:
    """Unpack only regular files that stay inside ``target``, the same way on every OS."""
    total = 0
    seen: set[str] = set()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = _check_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
            folded = str(name).casefold()  # macOS/Windows file names ignore case
            if folded in seen:
                raise StudioUIError(f"UI archive has names differing only by case: {name}")
            seen.add(folded)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise StudioUIError(f"links are not allowed in the UI archive: {info.filename!r}")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise StudioUIError("the UI archive unpacks to more than the size limit")
            destination = target.joinpath(*name.parts)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)


def download(manifest: Manifest, cache: Path, fetch: Fetch = _https_fetch) -> Path:
    """Download, verify and unpack the UI into ``cache``; return the UI folder."""
    final = cache / f"{manifest.version}-{manifest.sha256[:16]}"
    if (final / "index.html").exists():
        return final
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    print(f"Downloading the Studio UI {manifest.version} (first run only)...")
    body = fetch(manifest.url)
    digest = hashlib.sha256(body).hexdigest()
    if digest != manifest.sha256:
        raise StudioUIError(
            f"the downloaded UI does not match its expected SHA-256 "
            f"(got {digest}, expected {manifest.sha256}); it was not used"
        )
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        archive = Path(tmp) / "ui.zip"
        archive.write_bytes(body)
        unpacked = Path(tmp) / "ui"
        unpacked.mkdir()
        _safe_extract(archive, unpacked)
        if not (unpacked / "index.html").exists():
            raise StudioUIError("the UI archive has no index.html")
        try:
            unpacked.rename(final)  # atomic: a half-unpacked UI is never used
        except OSError:
            if not (final / "index.html").exists():  # another process did not win the race
                raise
    return final


def ui_path(override: str | None = None, *, fetch: Fetch = _https_fetch) -> Path:
    """The folder holding the built Studio UI (see the module docstring for the order)."""
    explicit = override or os.environ.get(ENV_UI_PATH)
    if explicit:
        path = Path(explicit)
        if not (path / "index.html").exists():
            raise StudioUIError(f"{path} has no index.html (from --ui-path / {ENV_UI_PATH})")
        return path
    if (DEV_BUILD / "index.html").exists():
        return DEV_BUILD
    manifest = read_manifest()
    if manifest is None:
        raise StudioUIError(
            "this mizzle build has no Studio UI download info. In a source checkout run "
            "`pnpm install && pnpm build` in studio-ui/, or pass --ui-path"
        )
    try:
        return download(manifest, cache_root(), fetch)
    except OSError as exc:
        raise StudioUIError(
            f"could not download the Studio UI from {manifest.url}: {exc}. "
            f"Offline? Download that file, unzip it, and pass --ui-path"
        ) from exc


# -- release side -----------------------------------------------------------------------

_FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # the earliest ZIP timestamp: identical archives anywhere


def build_archive(dist: Path, archive: Path) -> str:
    """Zip a built UI folder portably and reproducibly; return the archive's SHA-256.

    Entries are sorted, use "/" separators, fixed timestamps and plain file permissions,
    and every name passes the same checks the downloader applies.
    """
    if not (dist / "index.html").exists():
        raise StudioUIError(f"{dist} has no index.html; build the UI first")
    files = sorted(p for p in dist.rglob("*") if p.is_file())
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            if path.is_symlink():
                raise StudioUIError(f"links are not allowed in the UI: {path}")
            name = path.relative_to(dist).as_posix()
            _ = _check_name(name)
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def write_manifest(path: Path, *, version: str, url: str, sha256: str) -> None:
    payload = {"version": version, "url": url, "sha256": sha256}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _ = read_manifest(path)  # validate what we wrote


def _main(argv: list[str] | None = None) -> None:
    """``python -m mizzle.studio.assets DIST ZIP --version V --url URL --manifest PATH``."""
    import argparse

    parser = argparse.ArgumentParser(description="package the Studio UI for a release")
    _ = parser.add_argument("dist", type=Path)
    _ = parser.add_argument("archive", type=Path)
    _ = parser.add_argument("--version", required=True)
    _ = parser.add_argument("--url", required=True)
    _ = parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args(argv)
    dist, archive = cast(Path, args.dist), cast(Path, args.archive)
    manifest = cast(Path, args.manifest)
    version, url = cast(str, args.version), cast(str, args.url)
    digest = build_archive(dist, archive)
    write_manifest(manifest, version=version, url=url, sha256=digest)
    print(f"{archive}: sha256 {digest}\n{manifest}: written")


if __name__ == "__main__":
    _main()

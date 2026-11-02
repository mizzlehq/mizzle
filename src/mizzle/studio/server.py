"""Studio server (work in progress)."""

from __future__ import annotations

from mizzle.core.errors import MizzleError
from mizzle.kit.config import Config


def serve(config: Config, *, port: int, read_only: bool, open_browser: bool) -> None:
    raise MizzleError("Studio is not built yet")

from __future__ import annotations

import re
from typing import Literal

type Casing = Literal["snake_case"]

_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def snake_case(name: str) -> str:
    """``BlogPost`` -> ``blog_post``, ``HTTPLog`` -> ``http_log``, ``userId`` -> ``user_id``."""
    return _BOUNDARY.sub("_", name).lower()

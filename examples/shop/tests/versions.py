"""Version 1 of shop/schema.py, rebuilt from its ``# v2`` markers."""

import re

_SWAP = re.compile(r"\s*# v2 \(v1: (.+?) => (.+?)\)$")


def schema_v1(source: str) -> str:
    out: list[str] = []
    skipping = False
    for line in source.splitlines():
        if line.strip() == "# v2-only:start":
            skipping = True
        if skipping:
            skipping = line.strip() != "# v2-only:end"
            continue
        if m := _SWAP.search(line):
            new, old = m.group(1), m.group(2)
            body = line[: m.start()]
            assert body.count(new) == 1, line
            out.append(body.replace(new, old))
        elif not line.rstrip().endswith("# v2"):
            out.append(line)
    return "\n".join(out) + "\n"

"""Write site/llms-full.txt: every docs page in nav order, as Markdown (snippets resolved).
Run after `zensical build`."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNIPPET = re.compile(r'^--8<-- "([^"]+)"$', re.M)
FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def pages() -> list[str]:
    nav = (ROOT / "mkdocs.yml").read_text().split("\nnav:\n", 1)[1]
    found = re.findall(r":\s*(\S+\.md)\s*$", nav, re.M)
    return [p for p in found if not p.startswith("reference/")]  # API pages are generated


def main() -> None:
    parts = [(ROOT / "docs/llms.txt").read_text()]
    for page in pages():
        text = FRONT_MATTER.sub("", (ROOT / "docs" / page).read_text())
        text = SNIPPET.sub(lambda m: (ROOT / m.group(1)).read_text(), text)
        parts.append(f"<!-- {page} -->\n{text.strip()}\n")
    (ROOT / "site/llms-full.txt").write_text("\n\n".join(parts))


if __name__ == "__main__":
    main()

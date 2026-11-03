"""Write the docs/ pages into a checkout of the GitHub wiki (mizzle.wiki.git).

    git clone https://github.com/mizzlehq/mizzle.wiki.git ../mizzle.wiki
    python scripts/sync_wiki.py ../mizzle.wiki
    cd ../mizzle.wiki && git add -A && git commit -m "Sync from docs" && git push

docs/ stays the source: links between pages become wiki links, snippets are inlined, and the
API reference (generated from docstrings) links to Read the Docs.
"""

import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = "https://mizzle.readthedocs.io/en/latest"
SNIPPET = re.compile(r'^--8<-- "([^"]+)"$', re.M)
FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
LINK = re.compile(r"\]\(([^)#\s]+\.md)(#[^)]*)?\)")
ATTRS = re.compile(r"\s*\{[^}\n]*\}$", re.M)  # "# mizzle { .mz-hidden }"


def nav() -> list[tuple[str, str]]:
    """(title, docs path) in menu order."""
    text = (ROOT / "mkdocs.yml").read_text().split("\nnav:\n", 1)[1]
    return re.findall(r"-\s+([^:\n]+):\s*(\S+\.md)\s*$", text, re.M)


def wiki_name(title: str, path: str) -> str:
    if path == "index.md":
        return "Home"
    return re.sub(r"[^A-Za-z0-9.]+", "-", title).strip("-")


def convert(path: str, names: dict[str, str]) -> str:
    text = FRONT_MATTER.sub("", (ROOT / "docs" / path).read_text())
    text = SNIPPET.sub(lambda m: (ROOT / m.group(1)).read_text(), text)
    text = ATTRS.sub("", text)

    def link(m: re.Match[str]) -> str:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(path), m.group(1)))
        anchor = m.group(2) or ""
        if target in names:
            return f"]({names[target]}{anchor})"
        return f"]({DOCS}/{target.removesuffix('.md')}/{anchor})"  # API reference

    return LINK.sub(link, text)


def main(out: Path) -> None:
    pages = [(t, p) for t, p in nav() if not p.startswith("reference/")]
    names = {p: wiki_name(t, p) for t, p in pages}
    for path, name in names.items():
        note = f"> This page mirrors the [docs]({DOCS}/). Edit `docs/{path}` in the repo.\n\n"
        (out / f"{name}.md").write_text(note + convert(path, names))
    sidebar = [f"- [{t}]({names[p]})" for t, p in pages]
    sidebar.insert(len(sidebar) - 1, f"- [API reference]({DOCS}/reference/mizzle/)")
    (out / "_Sidebar.md").write_text("**mizzle**\n\n" + "\n".join(sidebar) + "\n")
    print(f"Wrote {len(names)} pages and _Sidebar.md to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

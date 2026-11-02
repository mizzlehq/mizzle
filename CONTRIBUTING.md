# Contributing to mizzle

## Setup

```bash
uv sync                                   # Python 3.12+, dev tools and optional extras
docker compose up -d                      # PostgreSQL on localhost:54329
export MIZZLE_TEST_URL=postgresql://mizzle:mizzle@localhost:54329/mizzle
```

## Checks (CI runs all of these)

```bash
uv run pytest                             # integration tests skip when MIZZLE_TEST_URL is unset
uv run basedpyright                       # strict; also run with --pythonversion 3.14
uv run mypy
uv run ruff check && uv run ruff format --check
```

## Studio UI (`studio-ui/`)

React + Tailwind + Base UI/shadcn, managed with pnpm and Biome.

```bash
cd studio-ui
pnpm install && pnpm build                # builds into src/mizzle/studio/static (gitignored)
pnpm dev                                  # dev server; proxies /api to `mizzle studio --port 4983`
pnpm check                                # Biome: format, lint, sort imports
```

Releases do not ship the UI inside the wheel: `mizzle studio` downloads it on first run from
the GitHub Release and verifies its SHA-256.

## Releasing

1. Bump the version in `pyproject.toml` and `src/mizzle/__init__.py`.
2. Push a tag: `git tag -a vX.Y.Z -m "mizzle X.Y.Z" && git push origin vX.Y.Z`.

`.github/workflows/release.yml` builds the UI archive, attaches it to a GitHub Release, and
publishes the wheel to PyPI with trusted publishing.

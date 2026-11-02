# mizzle example: a small social network API

FastAPI + mizzle: profiles, follows, posts with visibility (public / followers / private),
comments, likes and bookmarks. It shows the patterns a real app needs:

- **Schema** in `social/schema.py` (`@final` tables, enums, composite keys, checks, indexes,
  relations) and its **migration history** in `migrations/`.
- **Request models with allowlists** (`social/models.py`): clients can only send the listed
  fields, so they cannot set ids, authors or timestamps.
- **Relational queries** for nested data (a post with its comments and their authors) and
  typed `select` queries for counts.
- **Upserts** for idempotent likes, follows and bookmarks.
- **Migrations at startup** (`MIGRATE_ON_START=1`), safe with many workers (advisory lock).
- **Database errors as HTTP**: unique → 409, check constraint → 422, missing reference → 404.
- **End-to-end tests** against a fresh database (`tests/test_api.py`).

## Run it

```bash
docker compose -f ../../docker-compose.yml up -d    # Postgres on localhost:54329
cp .env.example .env
uv sync
uv run mizzle migrate                                # or rely on MIGRATE_ON_START=1
uv run python -m social.seed                         # optional demo data
uv run uvicorn social.app:create_app --factory --reload
```

Open http://127.0.0.1:8000/docs. Authentication is out of scope: send `X-User-Id: <id>`
to act as a user (the seed creates ids 1-24).

```bash
curl -s localhost:8000/feed -H 'X-User-Id: 1' | python -m json.tool | head -40
uv run mizzle studio                                 # browse the data
```

## Change the schema

Edit `social/schema.py`, then:

```bash
uv run mizzle generate --name what_changed
uv run mizzle migrate
```

## Test

```bash
MIZZLE_TEST_URL=postgresql://mizzle:mizzle@localhost:54329/mizzle uv run pytest
```

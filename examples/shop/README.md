# mizzle example: a multi-tenant marketplace

FastAPI + mizzle. Users open stores, stores sell products, customers order them and write
reviews. It uses every 0.2 feature in one app, and its tests run it against a streaming
replica, PgBouncer, a TLS-only server with odd settings and a pgvector server.

- **Schema** (`shop/schema.py`): enums (one in the `shop_audit` schema), identity and
  generated columns, `on_update_now()`, numeric, jsonb, arrays, uuid, tsvector, composite
  keys, checks, partial / expression / GIN / trigram indexes, a view and a materialized
  view, row-level security with Supabase-style `auth.uid()` and `crud_policy`, a managed
  role and an existing one, grants, an audit trigger and an `UPDATE OF price` trigger.
  `shop/vectors.py` adds embeddings (vector, halfvec, sparsevec, HNSW) on servers with
  pgvector.
- **Two migration sets**: `migrations/` (`mizzle.toml`) and `migrations_vector/`
  (`mizzle.vector.toml`, the same plus embeddings). The app picks one at startup.
- **Signed-in endpoints** (`GET /orders`, reviews, notifications) run as the
  `shop_authenticated` role with the user's JWT claims, so PostgreSQL decides which rows
  they see.

## Run it

```bash
docker compose -f ../../docker-compose.yml up -d    # Postgres on localhost:54329
psql postgresql://mizzle:mizzle@localhost:54329/mizzle -c 'CREATE ROLE shop_authenticated'
uv sync
export DATABASE_URL=postgresql://mizzle:mizzle@localhost:54329/mizzle
uv run mizzle migrate          # or MIGRATE_ON_START=1; pgvector servers: --config mizzle.vector.toml
uv run uvicorn shop.app:create_app --factory
```

`shop_authenticated` exists before the first migration, like Supabase's `authenticated`.
Send `X-User-Id: <uuid>` to act as a user. `REPLICA_URLS` (comma-separated) sends reads to
replicas; behind PgBouncer set `STATEMENT_CACHE_SIZE=0` and run migrations directly.

## Test

Every test file reads `SHOP_URL`, the admin URL of the server under test. Tests create
and drop their own databases there (`shop_app`, `shop_load`, `shop_mig`, `shop_push`,
`shop_vec`).

```bash
docker compose -f ../../tests/infra/compose.yml up -d --wait
P=postgresql://mizzle:mizzle@localhost

# primary + streaming replica (reads go to the replica)
SHOP_URL=$P:54331/mizzle SHOP_REPLICA_URL=$P:54332/mizzle uv run pytest -s
# pgvector
SHOP_URL=$P:54335/mizzle uv run pytest -s
# TLS only, Pacific/Chatham time zone, DMY dates, extra_float_digits=0
SHOP_URL="$P:54334/mizzle?sslmode=require" uv run pytest -s
# PgBouncer in transaction mode (listen() refuses) and in session mode (it works)
SHOP_URL=$P:54331/mizzle SHOP_BOUNCER_URL=$P:54333/mizzle \
  MIZZLE_PGBOUNCER_SESSION_URL=$P:54337/mizzle uv run pytest -s

# restart the primary under load, and a 10-minute soak
SHOP_URL=$P:54331/mizzle SHOP_REPLICA_URL=$P:54332/mizzle \
  SHOP_RESTART_CMD="docker compose -f ../../tests/infra/compose.yml restart primary" \
  uv run pytest -s tests/test_load.py -k restart
SHOP_URL=$P:54331/mizzle SHOP_REPLICA_URL=$P:54332/mizzle SHOP_SOAK_SECONDS=600 \
  uv run pytest -s tests/test_load.py -k soak

uv run basedpyright
```

- `tests/test_api.py`: every endpoint, with exact checks on the data.
- `tests/test_runtime.py`: replicas (and a replica going away), killed connections,
  session reset, LISTEN queue limits and reconnects, COPY, streaming, transactions,
  timeouts, retries, prepared statements.
- `tests/test_migrations.py`: the CLI from zero (version 1 is rebuilt from the `# v2`
  markers in `shop/schema.py`), four processes migrating at once, the version 2 change,
  `check`, `drop`, `push` to a fresh database, and `pull` back with no drift.
- `tests/test_load.py`: uvicorn with 4 workers, 50 concurrent clients, then checks that
  stock never went negative, totals add up and no connection leaked.

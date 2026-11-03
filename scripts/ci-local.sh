#!/usr/bin/env bash
# Run what .github/workflows/ci.yml runs, on this machine, before a push.
# Needs docker, uv and pnpm. Logs go to .ci-local/. Exit code 0 = every job passed.
#   scripts/ci-local.sh            all jobs
#   scripts/ci-local.sh test       only the PostgreSQL x Python matrix
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
LOGS=$ROOT/.ci-local
# Venvs live outside the repo, so editors and tools do not see them.
VENVS=${XDG_CACHE_HOME:-$HOME/.cache}/mizzle-ci
mkdir -p "$LOGS"
ONLY=${1:-all}

# The "test" matrix: python/postgres, as in ci.yml.
MATRIX=("3.14/14" "3.14/15" "3.14/16" "3.14/17" "3.14/18" "3.12/18" "3.13/18")

lint() {
  export UV_PROJECT_ENVIRONMENT=$VENVS/3.12
  uv sync -q --python 3.12 &&
    uv run ruff check && uv run ruff format --check &&
    uv run basedpyright && uv run mypy &&
    uv run basedpyright --pythonversion 3.14 && uv run mypy --python-version 3.14 &&
    uv run --group docs zensical build --strict --clean && uv run python scripts/llms_full.py
}

ui() {
  cd studio-ui && pnpm install --frozen-lockfile && pnpm run ci && pnpm build
}

test_one() {  # $1 = python, $2 = postgres, $3 = port
  local name=mizzle-ci-pg$2-py$1 url=postgresql://mizzle:mizzle@localhost:$3/mizzle
  docker rm -f "$name" >/dev/null 2>&1
  docker run -d --name "$name" -p "$3:5432" -e POSTGRES_USER=mizzle \
    -e POSTGRES_PASSWORD=mizzle -e POSTGRES_DB=mizzle "postgres:$2" >/dev/null || return 1
  until docker exec "$name" pg_isready -U mizzle -h 127.0.0.1 >/dev/null 2>&1; do sleep 1; done
  # Own venvs: the matrix runs in parallel.
  export UV_PROJECT_ENVIRONMENT=$VENVS/$1-pg$2 MIZZLE_TEST_URL=$url
  local ok=0
  uv sync -q --python "$1" && uv run pytest -q -p no:cacheprovider || ok=1
  if [ $ok = 0 ]; then
    (cd examples/social && export UV_PROJECT_ENVIRONMENT=$VENVS/social-$1-pg$2 &&
      uv sync -q && uv run basedpyright && uv run pytest -q -p no:cacheprovider) || ok=1
  fi
  docker rm -f "$name" >/dev/null
  return $ok
}

infra() {
  export UV_PROJECT_ENVIRONMENT=$VENVS/3.14
  local P=postgresql://mizzle:mizzle@localhost
  export MIZZLE_TEST_URL=$P:54331/mizzle MIZZLE_PRIMARY_URL=$P:54331/mizzle \
    MIZZLE_REPLICA_URL=$P:54332/mizzle MIZZLE_PGBOUNCER_URL=$P:54333/mizzle \
    MIZZLE_PGBOUNCER_SESSION_URL=$P:54337/mizzle \
    MIZZLE_ODD_URL="$P:54334/mizzle?sslmode=require" \
    MIZZLE_RESTART_CMD="docker compose -f tests/infra/compose.yml restart odd" \
    MIZZLE_PRIMARY_RESTART_CMD="docker compose -f tests/infra/compose.yml restart primary" \
    MIZZLE_VECTOR_URL=$P:54335/mizzle
  uv sync -q --python 3.14 &&
    docker compose -f tests/infra/compose.yml up -d --wait &&
    uv run pytest -q tests/integration/test_infra.py tests/integration/test_infra_io.py \
      tests/integration/test_production_env.py tests/integration/test_vector.py || return 1
  cd examples/shop && unset UV_PROJECT_ENVIRONMENT &&
    export SHOP_RESTART_CMD="docker compose -f ../../tests/infra/compose.yml restart primary" &&
    uv sync -q && uv run basedpyright &&
    SHOP_URL=$P:54331/mizzle SHOP_REPLICA_URL=$P:54332/mizzle uv run pytest -q -k "not restart" &&
    SHOP_URL=$P:54331/mizzle SHOP_BOUNCER_URL=$P:54333/mizzle uv run pytest -q -k "not restart" &&
    SHOP_URL=$P:54335/mizzle uv run pytest -q -k "not restart" &&
    SHOP_URL=$P:54331/mizzle SHOP_REPLICA_URL=$P:54332/mizzle \
      uv run pytest -q tests/test_load.py -k restart
}

declare -A PIDS
run() {  # $1 = job name, rest = command; runs in the background, log in .ci-local/
  ("${@:2}") >"$LOGS/$1.log" 2>&1 &
  PIDS[$1]=$!
}

# The matrix runs in parallel (one server per entry); infra after it, since a restart of the
# infra primary must not overlap anything.
if [ "$ONLY" = all ]; then
  run lint lint
  run ui ui
  run studio-assets bash -c "export UV_PROJECT_ENVIRONMENT=$VENVS/assets && uv sync -q --python 3.12 && uv run pytest -q tests/unit/test_studio_assets.py"
fi
port=55410
for entry in "${MATRIX[@]}"; do
  py=${entry%/*} pg=${entry#*/}
  run "test-py$py-pg$pg" test_one "$py" "$pg" $((port++))
done

failed=0
for job in "${!PIDS[@]}"; do
  if wait "${PIDS[$job]}"; then echo "PASS $job"; else echo "FAIL $job  ($LOGS/$job.log)"; failed=1; fi
done
if [ "$ONLY" = all ]; then
  if (infra) >"$LOGS/infra.log" 2>&1; then echo "PASS infra"; else echo "FAIL infra  ($LOGS/infra.log)"; failed=1; fi
fi
exit $failed

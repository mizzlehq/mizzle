#!/usr/bin/env bash
# Run what .github/workflows/ci.yml runs, on this machine, before a push.
# Needs docker, uv and pnpm. Logs go to .ci-local/. Exit code 0 = every job passed.
# In a terminal it shows a live board: each job, its state, time and last log line.
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
    uv run --group docs zensical build --strict --clean && uv run python scripts/llms_full.py &&
    uv lock --check
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

JOBS=()
START=$SECONDS
run() {  # $1 = job name, rest = command; runs in the background, log in .ci-local/
  rm -f "$LOGS/$1.rc" "$LOGS/$1.start" "$LOGS/$1.end"
  [ " ${JOBS[*]} " == *" $1 "* ]] || JOBS+=("$1")
  (
    echo $SECONDS >"$LOGS/$1.start"
    ("${@:2}") >"$LOGS/$1.log" 2>&1
    rc=$?
    echo $SECONDS >"$LOGS/$1.end"
    echo $rc >"$LOGS/$1.rc"
  ) &
}

# Ctrl-C: stop every job and remove the test servers.
cleanup() {
  kill $(jobs -p) 2>/dev/null
  docker rm -f $(docker ps -aq --filter name=mizzle-ci-) >/dev/null 2>&1
  [ -t 1 ] && tput cnorm
  exit 130
}
trap cleanup INT TERM

state() {  # $1 = job: prints running / PASS / FAIL / waiting
  if [ -f "$LOGS/$1.rc" ]; then
    [ "$(cat "$LOGS/$1.rc")" = 0 ] && echo PASS || echo FAIL
  elif [ -f "$LOGS/$1.start" ]; then echo running
  else echo waiting; fi
}

LIVE=0
[ -t 1 ] && LIVE=1
draw() {  # one screen: a line per job with its state, time and last log line
  local cols job s since ended last color
  cols=$(tput cols 2>/dev/null || echo 120)
  printf '\033[H\033[1mmizzle local CI\033[0m  %ds   logs: %s\033[K\n\033[K\n' \
    $((SECONDS - START)) "$LOGS"
  for job in "${JOBS[@]}"; do
    s=$(state "$job")
    case $s in PASS) color=32 ;; FAIL) color=31 ;; running) color=33 ;; *) color=90 ;; esac
    since=$(cat "$LOGS/$job.start" 2>/dev/null || echo $SECONDS)
    ended=$(cat "$LOGS/$job.end" 2>/dev/null || echo $SECONDS)
    # The last non-empty line; pytest -q prints its progress as "....  [ 45%]".
    last=$(tail -c 4000 "$LOGS/$job.log" 2>/dev/null | tr '\r' '\n' | grep -v '^[[:space:]]*$' |
      tail -n 1 | sed 's/\x1b\[[0-9;]*m//g')
    [ "$s" = waiting ] && last=""
    printf '\033[%sm%-8s\033[0m %-20s %4ss  %s\033[K\n' "$color" "$s" "$job" \
      $((ended - since)) "${last:0:$((cols - 38))}"
  done
  printf '\033[J'
}

watch_jobs() {  # until every started job has its .rc file
  local job busy
  while :; do
    busy=0
    for job in "${JOBS[@]}"; do [ "$(state "$job")" = running ] && busy=1; done
    [ $LIVE = 1 ] && draw
    [ $busy = 0 ] && return
    sleep 1
  done
}

[ $LIVE = 1 ] && { tput civis; printf '\033[2J'; }
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
if [ "$ONLY" = all ]; then
  JOBS+=(infra)  # shown as waiting until the matrix is done
  rm -f "$LOGS/infra.rc" "$LOGS/infra.start" "$LOGS/infra.end"
fi
sleep 1  # every job has written its .start
watch_jobs
if [ "$ONLY" = all ]; then
  run infra infra
  sleep 1
  watch_jobs
fi
[ $LIVE = 1 ] && tput cnorm

failed=0
for job in "${JOBS[@]}"; do
  if [ "$(state "$job")" = PASS ]; then
    [ $LIVE = 1 ] || echo "PASS $job"
  else
    echo "FAIL $job  ($LOGS/$job.log)"
    failed=1
  fi
done
[ $failed = 0 ] && echo "all jobs passed"
exit $failed

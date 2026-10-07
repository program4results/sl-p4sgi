#!/usr/bin/env bash
# One-off: pg_dump local sl-p4sgi-db → restore into Cloud SQL via Auth Proxy.
# Does NOT create GCP resources. Requires: env.sh, cloud-sql-proxy, psql, docker.
#
# Usage:
#   set -a; source ./env.sh; set +a
#   ./migrate-db.sh --dry-run
#   ./migrate-db.sh --dump-only     # write dump file only
#   ./migrate-db.sh --restore-only  # restore existing dump via proxy
#   ./migrate-db.sh --apply         # dump + restore
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=dry-run
DUMP_DIR="${DUMP_DIR:-${SCRIPT_DIR}/.dumps}"
DUMP_FILE=""

usage() {
  cat <<USAGE
Usage: $0 [--dry-run|--apply|--dump-only|--restore-only] [--dump FILE]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) MODE=dry-run; shift ;;
    --apply) MODE=apply; shift ;;
    --dump-only) MODE=dump-only; shift ;;
    --restore-only) MODE=restore-only; shift ;;
    --dump) DUMP_FILE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown: $1" >&2; usage; exit 2 ;;
  esac
done

for f in "${SCRIPT_DIR}/env.sh" "${SCRIPT_DIR}/env.example"; do
  if [[ -f "$f" ]]; then
    set -a; # shellcheck disable=SC1090
    source "$f"; set +a
    break
  fi
done

: "${PROJECT_ID:?}"
: "${REGION:?}"
: "${SQL_INSTANCE:?}"
: "${SQL_DB_NAME:?}"
: "${SQL_USER:?}"
: "${COMPOSE_DB_CONTAINER:=sl-p4sgi-db}"
: "${SECRET_DB_PASSWORD:?}"

INSTANCE_CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
mkdir -p "$DUMP_DIR"
DUMP_FILE="${DUMP_FILE:-${DUMP_DIR}/${COUNTRY:-sl}-p4sgi-$(date -u +%Y%m%dT%H%M%SZ).sql.gz}"
PROXY_PORT="${PROXY_PORT:-15432}"
PROXY_BIN="${PROXY_BIN:-cloud-sql-proxy}"

run() {
  if [[ "$MODE" == "dry-run" ]]; then
    printf '+ '; printf '%q ' "$@"; printf '\n'
  else
    printf '+ '; printf '%q ' "$@"; printf '\n'
    "$@"
  fi
}

echo "# migrate-db mode=${MODE} dump=${DUMP_FILE}"
echo "# instance=${INSTANCE_CONNECTION_NAME}"

do_dump() {
  echo "# Dumping from local Docker container ${COMPOSE_DB_CONTAINER}"
  if [[ "$MODE" == "dry-run" ]]; then
    run docker exec -t "$COMPOSE_DB_CONTAINER" pg_dump -U "$SQL_USER" -d "$SQL_DB_NAME" --no-owner --no-acl
    echo "# | gzip > ${DUMP_FILE}"
  else
    docker exec -t "$COMPOSE_DB_CONTAINER" \
      pg_dump -U "$SQL_USER" -d "$SQL_DB_NAME" --no-owner --no-acl \
      | gzip -c > "$DUMP_FILE"
    ls -lh "$DUMP_FILE"
  fi
}

do_restore() {
  echo "# Restoring via Cloud SQL Auth Proxy on 127.0.0.1:${PROXY_PORT}"
  if [[ "$MODE" == "dry-run" ]]; then
    run "$PROXY_BIN" "${INSTANCE_CONNECTION_NAME}" --port="$PROXY_PORT"
    echo "# POSTGRES_PASSWORD=\$(gcloud secrets versions access latest --secret=${SECRET_DB_PASSWORD})"
    echo "# gunzip -c ${DUMP_FILE} | PGPASSWORD=... psql -h 127.0.0.1 -p ${PROXY_PORT} -U ${SQL_USER} -d ${SQL_DB_NAME}"
    return 0
  fi

  if [[ ! -f "$DUMP_FILE" ]]; then
    echo "Dump file missing: $DUMP_FILE" >&2
    exit 1
  fi
  if ! command -v "$PROXY_BIN" >/dev/null 2>&1; then
    echo "Install Cloud SQL Auth Proxy as '${PROXY_BIN}'." >&2
    echo "https://cloud.google.com/sql/docs/postgres/connect-auth-proxy" >&2
    exit 1
  fi

  "$PROXY_BIN" "${INSTANCE_CONNECTION_NAME}" --port="$PROXY_PORT" &
  PROXY_PID=$!
  cleanup() { kill "$PROXY_PID" 2>/dev/null || true; }
  trap cleanup EXIT
  sleep 3

  SQL_PASS="$(gcloud secrets versions access latest --secret="$SECRET_DB_PASSWORD" --project="$PROJECT_ID")"
  export PGPASSWORD="$SQL_PASS"
  gunzip -c "$DUMP_FILE" | psql -h 127.0.0.1 -p "$PROXY_PORT" -U "$SQL_USER" -d "$SQL_DB_NAME" -v ON_ERROR_STOP=1
  unset PGPASSWORD SQL_PASS
  echo "# Restore complete"
}

case "$MODE" in
  dry-run)
    do_dump
    do_restore
    ;;
  dump-only)
    do_dump
    ;;
  restore-only)
    do_restore
    ;;
  apply)
    do_dump
    do_restore
    ;;
esac

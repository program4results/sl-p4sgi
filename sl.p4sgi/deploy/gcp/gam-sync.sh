#!/usr/bin/env bash
# Upload latest GAM CSV (+ .meta.json) runs from gbu host to GCS.
# Keeps GAM OAuth credentials on gbu; Cloud Run only reads the bucket.
#
# Crontab line (gbu, every 30 minutes):
#   */30 * * * * /mnt/drive_14tb/stacks/sl.p4sgi/deploy/gcp/gam-sync.sh >> /var/log/sl-p4sgi-gam-sync.log 2>&1
#
# Or with env:
#   */30 * * * * bash -lc 'set -a; source /mnt/drive_14tb/stacks/sl.p4sgi/deploy/gcp/env.sh; set +a; /mnt/drive_14tb/stacks/sl.p4sgi/deploy/gcp/gam-sync.sh'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in "${SCRIPT_DIR}/env.sh" "${SCRIPT_DIR}/env.example"; do
  if [[ -f "$f" ]]; then
    set -a; # shellcheck disable=SC1090
    source "$f"; set +a
    break
  fi
done

: "${PROJECT_ID:?Set PROJECT_ID in env.sh}"
: "${COUNTRY:=sl}"
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-${COUNTRY}-p4sgi-gam}"
GAM_OUT_HOST="${GAM_OUT_HOST:-/mnt/drive_14tb/docker-data/sl.p4sgi/gam-out}"
RUNS_DIR="${GAM_OUT_HOST}/runs"
PREFIX="gs://${BUCKET_NAME}/${COUNTRY}/gam-out/runs"

if [[ ! -d "$RUNS_DIR" ]]; then
  echo "GAM runs dir missing: $RUNS_DIR" >&2
  exit 1
fi

# Sync CSVs and meta sidecars; do not delete remote (keep history).
# Requires: gcloud auth application-default or a service account key on gbu
# with roles/storage.objectAdmin on the bucket (narrower: objectCreator).
echo "$(date -Is) sync ${RUNS_DIR} -> ${PREFIX}/"
gcloud storage rsync "$RUNS_DIR" "${PREFIX}/" \
  --project="$PROJECT_ID" \
  --checksums-only \
  --no-clobber

# Also drop a pointer file the app can read for "last sync"
date -u +%Y-%m-%dT%H:%M:%SZ | gcloud storage cp - "gs://${BUCKET_NAME}/${COUNTRY}/gam-out/LAST_SYNC.txt" \
  --project="$PROJECT_ID"

echo "$(date -Is) done"

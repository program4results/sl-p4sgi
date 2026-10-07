#!/usr/bin/env bash
# Idempotent GCP deploy for sl.p4sgi (parameterised by COUNTRY / DOMAIN).
# WRITE-ONLY packaging: this script is meant to be run by George after billing
# and OAuth consent are ready. Default mode is --dry-run (prints only).
#
# Usage:
#   cp env.example env.sh && edit env.sh
#   ./deploy.sh --dry-run          # print commands only (default)
#   ./deploy.sh --apply            # actually create / update resources
#
# Safety: refuses --apply unless PROJECT_ID is set and not the placeholder.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=1
APPLY=0

usage() {
  cat <<USAGE
Usage: $0 [--dry-run|--apply] [--env FILE]
  --dry-run   Print gcloud / docker commands only (default). No changes.
  --apply     Execute commands (creates billable GCP resources).
  --env FILE  Source env file (default: ./env.sh then ./env.example).
USAGE
}

ENV_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; APPLY=0; shift ;;
    --apply)   DRY_RUN=0; APPLY=1; shift ;;
    --env)     ENV_FILE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

load_env() {
  local f
  for f in "${ENV_FILE}" "${SCRIPT_DIR}/env.sh" "${SCRIPT_DIR}/env.example"; do
    [[ -n "$f" && -f "$f" ]] || continue
    # shellcheck disable=SC1090
    set -a
    # shellcheck source=/dev/null
    source "$f"
    set +a
    echo "# Loaded env from $f"
    return 0
  done
  echo "No env file found. Copy env.example to env.sh and fill PROJECT_ID." >&2
  exit 1
}

load_env

: "${PROJECT_ID:?PROJECT_ID required}"
: "${REGION:?REGION required}"
: "${COUNTRY:?COUNTRY required}"
: "${DOMAIN:?DOMAIN required}"
: "${HOSTNAME:?HOSTNAME required}"
: "${ADMIN_GROUP:?ADMIN_GROUP required}"

# Default deploy identity: geb@p4sgi.com (George's decision for sl.p4sgi).
ACCOUNT="${ACCOUNT:-geb@p4sgi.com}"
SUPERADMIN_IAP="${SUPERADMIN_IAP:-user:geb@p4sgi.com}"
SUPERADMIN_EMAILS="${SUPERADMIN_EMAILS:-geb@p4sgi.com}"
ORG_ID="${ORG_ID:-}"
CREATE_PROJECT_IF_MISSING="${CREATE_PROJECT_IF_MISSING:-false}"
PROJECT_NAME="${PROJECT_NAME:-SL P4SGI Dashboard}"

SERVICE_NAME="${SERVICE_NAME:-${COUNTRY}-p4sgi-web}"
SQL_INSTANCE="${SQL_INSTANCE:-${COUNTRY}-p4sgi-pg}"
SQL_DB_NAME="${SQL_DB_NAME:-slp4sgi}"
SQL_USER="${SQL_USER:-slp4sgi}"
AR_REPO="${AR_REPO:-${COUNTRY}-p4sgi}"
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-${COUNTRY}-p4sgi-gam}"
SA_RUNTIME="${SA_RUNTIME:-${COUNTRY}-p4sgi-run}"
SECRET_DB_PASSWORD="${SECRET_DB_PASSWORD:-${COUNTRY}-p4sgi-db-password}"
SQL_TIER="${SQL_TIER:-db-f1-micro}"
SQL_STORAGE_GB="${SQL_STORAGE_GB:-10}"
SQL_VERSION="${SQL_VERSION:-POSTGRES_16}"
CLOUD_RUN_CPU="${CLOUD_RUN_CPU:-1}"
CLOUD_RUN_MEMORY="${CLOUD_RUN_MEMORY:-512Mi}"
CLOUD_RUN_MIN_INSTANCES="${CLOUD_RUN_MIN_INSTANCES:-0}"
CLOUD_RUN_MAX_INSTANCES="${CLOUD_RUN_MAX_INSTANCES:-3}"
CLOUD_RUN_PORT="${CLOUD_RUN_PORT:-8000}"
CLOUD_RUN_TIMEOUT="${CLOUD_RUN_TIMEOUT:-300}"
ENABLE_LB="${ENABLE_LB:-true}"
STACK_DIR="${STACK_DIR:-/mnt/drive_14tb/stacks/sl.p4sgi}"
IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)}"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}:${IMAGE_TAG}"
IMAGE_URI_LATEST="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}:latest"
INSTANCE_CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
SA_EMAIL="${SA_RUNTIME}@${PROJECT_ID}.iam.gserviceaccount.com"

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '+ '; printf '%q ' "$@"; printf '\n'
  else
    printf '+ '; printf '%q ' "$@"; printf '\n'
    "$@"
  fi
}

echo "============================================================"
echo " sl.p4sgi GCP deploy  country=${COUNTRY}  domain=${DOMAIN}"
echo " project=${PROJECT_ID}  region=${REGION}  host=${HOSTNAME}"
echo " account=${ACCOUNT}  superadmin_iap=${SUPERADMIN_IAP}"
echo " mode=$([ "$DRY_RUN" -eq 1 ] && echo DRY-RUN || echo APPLY)"
echo "============================================================"

if [[ "$APPLY" -eq 1 ]]; then
  if [[ "$PROJECT_ID" == "YOUR_GCP_PROJECT_ID" || -z "$PROJECT_ID" ]]; then
    echo "Refusing --apply: set a real PROJECT_ID in env.sh" >&2
    exit 1
  fi
fi

# --- 0. Account + optional project create + APIs -----------------------------
# Guarded: only set account when ACCOUNT is non-empty (default geb@p4sgi.com).
if [[ -n "${ACCOUNT}" ]]; then
  run gcloud config set account "$ACCOUNT"
fi

# Optional: create project under p4sgi.com org when missing (apply only).
if [[ "$CREATE_PROJECT_IF_MISSING" == "true" && -n "$ORG_ID" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "# Optional: create project ${PROJECT_ID} under org ${ORG_ID} if missing"
    run gcloud projects describe "$PROJECT_ID" --account="$ACCOUNT"
    run gcloud projects create "$PROJECT_ID" --name="$PROJECT_NAME" --organization="$ORG_ID" --account="$ACCOUNT"
  else
    if ! gcloud projects describe "$PROJECT_ID" --account="$ACCOUNT" >/dev/null 2>&1; then
      run gcloud projects create "$PROJECT_ID"         --name="$PROJECT_NAME"         --organization="$ORG_ID"         --account="$ACCOUNT"
    else
      echo "# Project ${PROJECT_ID} already exists"
    fi
  fi
elif [[ "$DRY_RUN" -eq 1 && "$CREATE_PROJECT_IF_MISSING" == "true" ]]; then
  echo "# CREATE_PROJECT_IF_MISSING=true but ORG_ID unset — skip org-scoped create"
fi

run gcloud config set project "$PROJECT_ID"
run gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  iap.googleapis.com \
  compute.googleapis.com \
  storage.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com \
  --project="$PROJECT_ID"

# --- 1. Artifact Registry ----------------------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  run gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" --project="$PROJECT_ID"
  echo "# (idempotent) create repo if missing:"
  run gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="${COUNTRY} p4sgi container images" \
    --project="$PROJECT_ID"
else
  if ! gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud artifacts repositories create "$AR_REPO" \
      --repository-format=docker \
      --location="$REGION" \
      --description="${COUNTRY} p4sgi container images" \
      --project="$PROJECT_ID"
  else
    echo "# Artifact Registry repo ${AR_REPO} already exists"
  fi
fi

run gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# --- 2. Runtime service account + IAM ----------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  run gcloud iam service-accounts create "$SA_RUNTIME" \
    --display-name="${COUNTRY} p4sgi Cloud Run runtime" \
    --project="$PROJECT_ID"
else
  if ! gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud iam service-accounts create "$SA_RUNTIME" \
      --display-name="${COUNTRY} p4sgi Cloud Run runtime" \
      --project="$PROJECT_ID"
  else
    echo "# SA ${SA_EMAIL} already exists"
  fi
fi

run gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/cloudsql.client" \
  --condition=None

run gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --condition=None

# --- 3. Secret Manager (DB password) -----------------------------------------
# Generate once; never print the secret value in logs when applying.
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "# openssl rand -base64 32 | gcloud secrets create ${SECRET_DB_PASSWORD} --data-file=- ..."
  run gcloud secrets create "$SECRET_DB_PASSWORD" --replication-policy=automatic --project="$PROJECT_ID"
else
  if ! gcloud secrets describe "$SECRET_DB_PASSWORD" --project="$PROJECT_ID" >/dev/null 2>&1; then
    PASS="$(openssl rand -base64 32)"
    printf '%s' "$PASS" | gcloud secrets create "$SECRET_DB_PASSWORD" \
      --replication-policy=automatic \
      --data-file=- \
      --project="$PROJECT_ID"
    unset PASS
  else
    echo "# Secret ${SECRET_DB_PASSWORD} already exists (not rotated by this script)"
  fi
fi

# --- 4. Cloud SQL (Postgres 16, matches local postgres:16-alpine) -------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  run gcloud sql instances create "$SQL_INSTANCE" \
    --database-version="$SQL_VERSION" \
    --tier="$SQL_TIER" \
    --region="$REGION" \
    --storage-size="${SQL_STORAGE_GB}" \
    --storage-auto-increase \
    --availability-type=ZONAL \
    --assign-ip \
    --project="$PROJECT_ID"
else
  if ! gcloud sql instances describe "$SQL_INSTANCE" --project="$PROJECT_ID" >/dev/null 2>&1; then
    # Password from Secret Manager — fetch into env for create only
    SQL_PASS="$(gcloud secrets versions access latest --secret="$SECRET_DB_PASSWORD" --project="$PROJECT_ID")"
    run gcloud sql instances create "$SQL_INSTANCE" \
      --database-version="$SQL_VERSION" \
      --tier="$SQL_TIER" \
      --region="$REGION" \
      --storage-size="${SQL_STORAGE_GB}" \
      --storage-auto-increase \
      --availability-type=ZONAL \
      --root-password="$SQL_PASS" \
      --assign-ip \
      --project="$PROJECT_ID"
    unset SQL_PASS
  else
    echo "# Cloud SQL instance ${SQL_INSTANCE} already exists"
  fi
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  run gcloud sql databases create "$SQL_DB_NAME" --instance="$SQL_INSTANCE" --project="$PROJECT_ID"
  run gcloud sql users create "$SQL_USER" --instance="$SQL_INSTANCE" --password='***from-secret***' --project="$PROJECT_ID"
else
  if ! gcloud sql databases describe "$SQL_DB_NAME" --instance="$SQL_INSTANCE" --project="$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud sql databases create "$SQL_DB_NAME" --instance="$SQL_INSTANCE" --project="$PROJECT_ID"
  fi
  SQL_PASS="$(gcloud secrets versions access latest --secret="$SECRET_DB_PASSWORD" --project="$PROJECT_ID")"
  # create is not idempotent for users; update password if exists
  if gcloud sql users list --instance="$SQL_INSTANCE" --project="$PROJECT_ID" --format='value(name)' | grep -qx "$SQL_USER"; then
    run gcloud sql users set-password "$SQL_USER" --instance="$SQL_INSTANCE" --password="$SQL_PASS" --project="$PROJECT_ID"
  else
    run gcloud sql users create "$SQL_USER" --instance="$SQL_INSTANCE" --password="$SQL_PASS" --project="$PROJECT_ID"
  fi
  unset SQL_PASS
fi

# --- 5. GCS bucket for GAM CSVs ----------------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  run gcloud storage buckets create "gs://${BUCKET_NAME}" --location="$REGION" --project="$PROJECT_ID" --uniform-bucket-level-access
else
  if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" >/dev/null 2>&1; then
    run gcloud storage buckets create "gs://${BUCKET_NAME}" \
      --location="$REGION" \
      --project="$PROJECT_ID" \
      --uniform-bucket-level-access
  else
    echo "# Bucket gs://${BUCKET_NAME} already exists"
  fi
fi

run gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectViewer"

# --- 6. Build & push image ---------------------------------------------------
run docker build -t "$IMAGE_URI" -t "$IMAGE_URI_LATEST" "${STACK_DIR}/apps/api"
run docker push "$IMAGE_URI"
run docker push "$IMAGE_URI_LATEST"

# --- 7. Cloud Run with direct IAP (GA) + Cloud SQL connector -----------------
# Docs: https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run
run gcloud run deploy "$SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --image="$IMAGE_URI" \
  --service-account="$SA_EMAIL" \
  --port="$CLOUD_RUN_PORT" \
  --cpu="$CLOUD_RUN_CPU" \
  --memory="$CLOUD_RUN_MEMORY" \
  --min-instances="$CLOUD_RUN_MIN_INSTANCES" \
  --max-instances="$CLOUD_RUN_MAX_INSTANCES" \
  --timeout="$CLOUD_RUN_TIMEOUT" \
  --no-allow-unauthenticated \
  --iap \
  --add-cloudsql-instances="$INSTANCE_CONNECTION_NAME" \
  --set-env-vars="APP_ENV=production,POSTGRES_HOST=/cloudsql/${INSTANCE_CONNECTION_NAME},POSTGRES_PORT=5432,POSTGRES_DB=${SQL_DB_NAME},POSTGRES_USER=${SQL_USER},DATA_DIR=/tmp/data,GAM_GCS_BUCKET=${BUCKET_NAME},IAP_ALLOWED_EMAIL_SUFFIX=@${DOMAIN},PUBLIC_BASE_URL=https://${HOSTNAME},COUNTRY=${COUNTRY},DOMAIN=${DOMAIN},SUPERADMIN_EMAILS=${SUPERADMIN_EMAILS}" \
  --set-secrets="POSTGRES_PASSWORD=${SECRET_DB_PASSWORD}:latest" \
  --quiet

# IAP service agent → Cloud Run Invoker
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "# PROJECT_NUMBER=\$(gcloud projects describe ${PROJECT_ID} --format='value(projectNumber)')"
  run gcloud services identity create --service=iap.googleapis.com --project="$PROJECT_ID"
  run gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
    --region="$REGION" \
    --member="serviceAccount:service-PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com" \
    --role="roles/run.invoker" \
    --project="$PROJECT_ID"
else
  run gcloud services identity create --service=iap.googleapis.com --project="$PROJECT_ID" || true
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  run gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
    --region="$REGION" \
    --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" \
    --role="roles/run.invoker" \
    --project="$PROJECT_ID"
fi

# Grant IAP access to Workspace domain/group (sl admins) + geb super-admin
run gcloud iap web add-iam-policy-binding \
  --member="${ADMIN_GROUP}" \
  --role="roles/iap.httpsResourceAccessor" \
  --region="$REGION" \
  --resource-type=cloud-run \
  --service="$SERVICE_NAME" \
  --project="$PROJECT_ID"

if [[ -n "${SUPERADMIN_IAP}" && "${SUPERADMIN_IAP}" != "${ADMIN_GROUP}" ]]; then
  run gcloud iap web add-iam-policy-binding \
    --member="${SUPERADMIN_IAP}" \
    --role="roles/iap.httpsResourceAccessor" \
    --region="$REGION" \
    --resource-type=cloud-run \
    --service="$SERVICE_NAME" \
    --project="$PROJECT_ID"
fi

# --- 8. Optional HTTPS LB + serverless NEG + managed cert --------------------
# Custom domain for production. IAP stays on Cloud Run only (do NOT enable IAP
# on the backend service). Docs: LB traffic is still protected by Cloud Run IAP.
if [[ "${ENABLE_LB}" == "true" ]]; then
  LB_NAME="${LB_NAME:-${COUNTRY}-p4sgi-lb}"
  NEG_NAME="${NEG_NAME:-${COUNTRY}-p4sgi-neg}"
  BACKEND_NAME="${BACKEND_NAME:-${COUNTRY}-p4sgi-backend}"
  URLMAP_NAME="${URLMAP_NAME:-${COUNTRY}-p4sgi-urlmap}"
  CERT_NAME="${CERT_NAME:-${COUNTRY}-p4sgi-cert}"
  HTTPS_PROXY_NAME="${HTTPS_PROXY_NAME:-${COUNTRY}-p4sgi-https-proxy}"
  FWD_RULE_NAME="${FWD_RULE_NAME:-${COUNTRY}-p4sgi-https}"
  IP_NAME="${IP_NAME:-${COUNTRY}-p4sgi-ip}"

  run gcloud compute addresses create "$IP_NAME" --global --project="$PROJECT_ID" || true

  run gcloud compute network-endpoint-groups create "$NEG_NAME" \
    --region="$REGION" \
    --network-endpoint-type=serverless \
    --cloud-run-service="$SERVICE_NAME" \
    --project="$PROJECT_ID" || true

  run gcloud compute backend-services create "$BACKEND_NAME" \
    --global \
    --load-balancing-scheme=EXTERNAL_MANAGED \
    --project="$PROJECT_ID" || true

  run gcloud compute backend-services add-backend "$BACKEND_NAME" \
    --global \
    --network-endpoint-group="$NEG_NAME" \
    --network-endpoint-group-region="$REGION" \
    --project="$PROJECT_ID" || true

  run gcloud compute ssl-certificates create "$CERT_NAME" \
    --domains="$HOSTNAME" \
    --global \
    --project="$PROJECT_ID" || true

  run gcloud compute url-maps create "$URLMAP_NAME" \
    --default-service="$BACKEND_NAME" \
    --global \
    --project="$PROJECT_ID" || true

  run gcloud compute target-https-proxies create "$HTTPS_PROXY_NAME" \
    --ssl-certificates="$CERT_NAME" \
    --url-map="$URLMAP_NAME" \
    --global \
    --project="$PROJECT_ID" || true

  run gcloud compute forwarding-rules create "$FWD_RULE_NAME" \
    --global \
    --target-https-proxy="$HTTPS_PROXY_NAME" \
    --address="$IP_NAME" \
    --ports=443 \
    --project="$PROJECT_ID" || true

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "# LB_IP=\$(gcloud compute addresses describe ${IP_NAME} --global --format='value(address)')"
    echo "# Add at Cloudflare (DNS for p4sgi.com):  A  ${HOSTNAME%.${DOMAIN}}  ->  \$LB_IP  (DNS only / grey cloud)"
  else
    LB_IP="$(gcloud compute addresses describe "$IP_NAME" --global --format='value(address)' --project="$PROJECT_ID")"
    echo "============================================================"
    echo " DNS: at Cloudflare, create an A record:"
    echo "   Name:  dash   (or full ${HOSTNAME})"
    echo "   Type:  A"
    echo "   Value: ${LB_IP}"
    echo "   Proxy: DNS only (grey cloud) until cert is ACTIVE"
    echo "============================================================"
  fi
fi

# Print IAP audience for the app patch
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "# IAP_AUDIENCE=/projects/PROJECT_NUMBER/locations/${REGION}/services/${SERVICE_NAME}"
else
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  echo "IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE_NAME}"
  echo "Set this in Cloud Run env IAP_AUDIENCE after applying app-patches."
fi

echo "Done. Next: apply app-patches, run migrate-db.sh, install gam-sync.sh cron on gbu."

# Deploy sl.p4sgi admin dashboard to Google Cloud

Plain-language plan to put the Sierra Leone GWFE admin dashboard on the internet
so that **only admins in the `sl.p4sgi.com` Google Workspace** can sign in.

This folder is **documentation + scripts only**. Nothing here creates billable
GCP resources until **you** run `bash deploy.sh --apply` after filling `env.sh`.

> **Note:** `/mnt/drive_14tb` is mounted `noexec`. Run scripts as `bash deploy.sh --dry-run` / `bash deploy.sh --apply` rather than `./deploy.sh`.

Parameterisation: everything keys off `COUNTRY` and `DOMAIN` (defaults `sl` /
`sl.p4sgi.com`). A second country is a new `env.sh` + another run of the same
scripts (new service, database, bucket, IAP rule).

---

## Chosen design (summary)

| Topic | Choice | Why |
| --- | --- | --- |
| Auth | **IAP directly on Cloud Run (GA)** | Google’s recommended path; protects `*.run.app` and any load-balancer front door; no need to enable IAP on the LB. Docs: [Configure IAP for Cloud Run](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run) (updated 2026-09-30). |
| Custom hostname | **HTTPS LB + serverless NEG + Google-managed cert** for `dash.sl.p4sgi.com` | Cloud Run domain mapping is Preview / not recommended for production. IAP stays on Cloud Run only (do **not** also enable IAP on the backend service). |
| Region | **`africa-south1` (Johannesburg)** | User is in ZA; both Cloud Run and Cloud SQL for PostgreSQL are available there. Alternative: `europe-west1` (often cheaper Tier-1 Cloud Run pricing). |
| Database | Cloud SQL **PostgreSQL 16**, `db-f1-micro`, unix socket via Cloud Run Cloud SQL connector | Matches local `postgres:16-alpine`. |
| Secrets | Secret Manager for DB password | Injected into Cloud Run as `POSTGRES_PASSWORD`. |
| GAM | **(a) keep GAM on gbu + cron → GCS** | Simplest; GAM OAuth stays off the cloud. Option (b) Cloud Run Job + DWD is a later path. |
| App patches | Under `app-patches/` — **not applied** | Another worker is editing the app; apply when ready. |

---


## Identity (geb@p4sgi.com)

George's decision: **everything** for this dashboard uses `geb@p4sgi.com`:

1. **GAM reports** — host `~/.gam` oauth is already this admin on Workspace customer `C03ac36ui` (primary `p4sgi.com`; `sl.p4sgi.com` is a **secondary** domain in the same tenant).
2. **GCP deploy** — `ACCOUNT=geb@p4sgi.com` in `env.sh`; `deploy.sh` runs `gcloud config set account` when set.
3. **Dashboard super-admin** — `SUPERADMIN_EMAILS=geb@p4sgi.com` and IAP `user:geb@p4sgi.com` alongside `domain:sl.p4sgi.com`.

## Prerequisites (you must do these yourself)

1. **Billing** — A GCP project with a billing account attached.
2. **`gcloud auth login geb@p4sgi.com`** — **Everything** for the sl.p4sgi dashboard (GCP project, deploy, IAP super-admin) must run as `geb@p4sgi.com`. On this host (`gbu`) that account is not yet credentialed; George must sign in himself (do not run interactive login from agents). Then set `PROJECT_ID` in `env.sh` from `gcloud projects list --account=geb@p4sgi.com` (do not invent one). Optional: set `ORG_ID` + `CREATE_PROJECT_IF_MISSING=true` in `env.sh` to create the project under the p4sgi.com organisation.
3. **APIs** — `deploy.sh` enables them; or enable in console: Cloud Run, Cloud SQL Admin, Secret Manager, Artifact Registry, IAP, Compute, Storage, Cloud Build, IAM.
4. **OAuth consent / IAP first enable** — For the **first** IAP enablement in a project (especially without an org, or for out-of-org users), Google recommends turning IAP on once from the **Cloud Run console UI** so the OAuth client / consent screen is created. Branding should allow the Workspace that owns `sl.p4sgi.com`. See [Configure IAP for Cloud Run](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run).
5. **DNS** — `p4sgi.com` is hosted at **Cloudflare** (`jeff.ns.cloudflare.com`, `walk.ns.cloudflare.com`). You will add an **A** record for `dash.sl.p4sgi.com` → LB IP (DNS only / grey cloud until the managed cert is ACTIVE).
6. **Docker** on gbu to build/push the API image.
7. **Apply app patches** (or equivalent) before relying on GCS CSV reads / IAP JWT / Cloud SQL socket URL.

---

## Order of steps

1. Copy `env.example` → `env.sh`. Set real `PROJECT_ID`, confirm `ACCOUNT=geb@p4sgi.com`, `REGION`, `ADMIN_GROUP=domain:sl.p4sgi.com`, `SUPERADMIN_IAP=user:geb@p4sgi.com`.
2. `bash deploy.sh --dry-run` — review commands.
3. Console: billing + (recommended) first IAP enable / consent screen for the project.
4. `bash deploy.sh --apply` — sets gcloud account to `geb@p4sgi.com`, Artifact Registry, SA, Secret, Cloud SQL, GCS bucket, image build/push, Cloud Run + `--iap`, IAP IAM for `domain:sl.p4sgi.com` **plus** `user:geb@p4sgi.com`, HTTPS LB + NEG + cert.
5. At Cloudflare: create the A record printed by deploy (see DNS below). Wait for managed cert = ACTIVE.
6. Apply `app-patches/*` when the other worker is done; rebuild/redeploy image; set `IAP_AUDIENCE` env from deploy output.
7. `bash migrate-db.sh --apply` — `pg_dump` local `sl-p4sgi-db` → Cloud SQL via Auth Proxy.
8. Install `gam-sync.sh` crontab on gbu; run once by hand; confirm objects in the bucket.
9. Verification checklist (below).

---

## DNS record to add (Cloudflare)

After `deploy.sh --apply` prints `LB_IP`:

| Field | Value |
| --- | --- |
| Type | **A** |
| Name | `dash` (→ `dash.sl.p4sgi.com`) |
| IPv4 | *(the global forwarding-rule address from deploy output)* |
| Proxy status | **DNS only** (grey cloud) until Google-managed cert is Active; then you may orange-cloud if desired (often leave grey for IAP/LB). |
| TTL | Auto / 300 |

Confirmed via `dig NS p4sgi.com` → Cloudflare.

---

## GAM: recommendation

**Use (a) now:** keep `gam` on host `gbu`, allowlisted runner as today; cron `gam-sync.sh` uploads `gam-out/runs/*.csv` (+ `.meta.json`) to `gs://$BUCKET/$COUNTRY/gam-out/runs/`. Cloud app only **reads** the bucket (after patch `002`).

**Why not (b) yet:** a Cloud Run Job with GAM + service-account **domain-wide delegation** means moving Workspace OAuth/DWD into GCP, harder secret rotation, more blast radius, and rewriting the allowlisted runner for a headless GAM. Revisit when multi-country scale or gbu downtime becomes a problem.

### Crontab line (gbu)

```cron
*/30 * * * * bash -lc 'set -a; source /mnt/drive_14tb/stacks/sl.p4sgi/deploy/gcp/env.sh; set +a; /mnt/drive_14tb/stacks/sl.p4sgi/deploy/gcp/gam-sync.sh' >> /var/log/sl-p4sgi-gam-sync.log 2>&1
```

Grant the uploading identity `roles/storage.objectCreator` (or objectAdmin) on the bucket.

---

## App patches (unapplied)

| File | Purpose |
| --- | --- |
| `app-patches/001-database-env.patch` | `DATABASE_URL` / Cloud SQL unix socket in `db.py` |
| `app-patches/002-gcs-csv-reading.patch` | GCS → local `gam-out/runs` sync + `google-cloud-storage` dep |
| `app-patches/003-iap-jwt-check.patch` | Verify `x-goog-iap-jwt-assertion`; require email ends with `@sl.p4sgi.com` |

Do **not** apply while the other worker is editing GAM report code. Treat as a reviewable patch set.

---

## Verification checklist

- [ ] `gcloud run services describe $SERVICE_NAME --region=$REGION` shows `Iap Enabled: true`
- [ ] IAP policy lists `domain:sl.p4sgi.com` (or admin group) **and** `user:geb@p4sgi.com` as super-admin (`roles/iap.httpsResourceAccessor`)
- [ ] Incognito visit to `https://dash.sl.p4sgi.com` → Google sign-in → only Workspace users in that allowlist get through
- [ ] Non-`@sl.p4sgi.com` account denied (IAP and/or app JWT domain check), except `geb@p4sgi.com` (super-admin via IAP user binding)
- [ ] `GET https://dash.sl.p4sgi.com/api/v1/health` works after login (or from inside after JWT)
- [ ] Cloud SQL: tables present after migrate; Schools page loads
- [ ] After `gam-sync.sh`, GAM reports page shows latest CSVs
- [ ] Managed SSL cert status ACTIVE for `dash.sl.p4sgi.com`

---

## Rollback

1. Disable public path: `gcloud run services update $SERVICE_NAME --region=$REGION --no-iap` is **not** enough alone — also remove LB forwarding rule or delete the A record so DNS stops pointing at GCP.
2. Safer: delete / disable the HTTPS forwarding rule; keep Cloud Run private (`--no-allow-unauthenticated`).
3. Local stack on gbu (`docker compose` on port 8088) remains the fallback; it was not modified except for this new `deploy/gcp/` folder.
4. Cloud SQL: take a final `pg_dump` via Auth Proxy before deleting the instance.
5. To tear down (when you choose): delete forwarding rule, proxy, URL map, cert, backend, NEG, address, Cloud Run service, SQL instance, bucket, secrets, AR images — **only when you intend to stop billing**.

---

## Rough monthly cost (**estimate**, USD)

Assumes light pilot traffic, min instances **0**, smallest SQL tier, one global HTTPS LB. Region pricing varies; figures below use published list rates (often illustrated for `us-central1`). **Label: estimate only — verify in [Pricing Calculator](https://cloud.google.com/products/calculator).**

| Component | Basis | Rough / month |
| --- | --- | --- |
| Cloud SQL `db-f1-micro` | $0.0105/hour × ~730 h ≈ $7.67 compute ([Cloud SQL pricing](https://cloud.google.com/sql/pricing)); + ~10 GiB SSD | **~$9–12** (Google’s example test instance ~$9.37 in us-central1: [pricing examples](https://cloud.google.com/sql/docs/postgres/pricing-examples)) |
| Cloud Run | min instances 0, request-based; free tier for low traffic ([Cloud Run pricing](https://cloud.google.com/run/pricing)) | **~$0–5** at pilot load (`africa-south1` is Tier 2 — a bit higher than Tier 1 if you exceed free tier) |
| HTTPS Load Balancing | First 5 forwarding rules $0.025/hour ≈ **$18.25**/mo ([LB pricing](https://cloud.google.com/load-balancing/pricing)) + small data processing | **~$18–25** |
| Artifact Registry / Secret Manager / GCS (small CSVs) | pennies at this scale | **~$1–3** |
| IAP | No charge for this standard Google-identity use | **$0** |

**Ballpark total: about $30–45 / month** with the LB.  
Without the LB (temporary `*.run.app` URL only): closer to **~$10–15 / month**, dominated by Cloud SQL.

---

## Multi-country

Copy `env.sh` → e.g. `env-xx.sh` with `COUNTRY=xx`, `DOMAIN=xx.p4sgi.com`, `HOSTNAME=dash.xx.p4sgi.com`, new `ADMIN_GROUP`. Re-run `deploy.sh --apply`. Separate SQL instance, bucket, Cloud Run service, IAP binding, DNS A record.

---

## Files in this folder

- `README.md` — this plan
- `env.example` — variables
- `deploy.sh` — idempotent gcloud orchestration (`--dry-run` / `--apply`)
- `migrate-db.sh` — local dump → Cloud SQL via Auth Proxy
- `gam-sync.sh` — gbu → GCS uploader (+ crontab line above)
- `app-patches/*.patch` — unapplied cloud adaptations

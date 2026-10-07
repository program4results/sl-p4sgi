# scripts/

## Primary — on-demand GAM reports (Phase 3.1)

- **`run_gam_report.sh`** — allowlisted read-only `gam` runner.
  Invoked by `POST /api/v1/gam/reports` (and manually for debugging).
  **Never** passes client-supplied strings to a shell; report name selects a fixed argv.

```bash
# Manual (host; drive is often noexec → use bash):
bash scripts/run_gam_report.sh ous /tmp/ous.csv

# Via API (preferred):
curl -s -X POST http://127.0.0.1:8088/api/v1/gam/reports \
  -H 'Content-Type: application/json' \
  -d '{"report":"users"}'
```

Allowlist: `users`, `enrolled`, `never_logged_in`, `wrong_password`, `data_storage`, `ous`, `admins`,
`groups`, `suspended`, `security_2sv`, `last_login`.

User-style reports request GAM `recoveryEmail` when supported. `never_logged_in` is Directory users whose `lastLoginTime` is empty/`Never` (scoped by selected domain).

Outputs: `docker-data/sl.p4sgi/gam-out/runs/{timestamp}_{report}.csv`

GAM must already be authenticated on the gbu host terminal (`~/.gam`). The web
container mounts `/home/george/bin/gam7` and `/home/george/.gam`. **No CSV drop
into docker-data is required** for routine reports.

## Legacy — CSV ingest (deprecated as primary path)

- **`ingest_gam_csv.py`** (Phase 3): read `docker-data/sl.p4sgi/gam-out/*.csv` →
  `provisioning_jobs`. Still useful offline/fallback; not the dashboard primary path.
- **`ingest_gam_sample.py`** (Phase 2): POST bundled sample for smoke tests.

```bash
# Fallback only — prefer dash "GAM reports" buttons:
python3 scripts/ingest_gam_csv.py
python3 scripts/ingest_gam_csv.py --file users.csv
python3 scripts/ingest_gam_csv.py --approve
```

### Expected CSV columns (legacy ingest)

| Column | Notes |
|--------|--------|
| `primaryEmail` / `email` / `schoolEmail` | School Workspace user |
| `orgUnitPath` / `ou` | e.g. `/sl/pilot/school-110101` |
| `emis` | Or inferred from `school-NNNNN` in OU path |
| `schoolName` / `name` / `name.fullName` | Display name |
| `attendanceDocId` | Required before Approve (non-pilot) |

## Site template provision stub (0.4.11)

- **`provision_school_site_template.stub.sh`** — outline only: shared `SHARED_EXEC` + known-EMIS CSV → future school-config writes + Embed checklist. Does **not** invent SEMIS, does **not** require Cloudflare, does **not** automate Sites Insert→Embed (Google gap). Full runbook: [docs/SCHOOL_SITE_TEMPLATE_PROVISION.md](../docs/SCHOOL_SITE_TEMPLATE_PROVISION.md).

```bash
export SHARED_EXEC='https://script.google.com/macros/s/DEPLOYMENT_ID/exec'
bash scripts/provision_school_site_template.stub.sh /path/to/known-schools.csv
```


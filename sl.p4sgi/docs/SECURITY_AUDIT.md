# Security audit (GAT-style) — 0.4.19

Read-only security triage for the p4sgi dashboard, built from the GAM CSVs the dashboard
**already caches**. It does not call GAM, does not change Google Workspace and does not touch
Postgres (no schema change, no migration).

Code: `apps/api/app/security_audit.py` · Tests: `apps/api/tests/test_security_audit.py`
UI: collapsed **Security audit** panel in `apps/api/static/index.html` (loads only on click).

## What it does

| Module | Source reports (cached GAM runs) | Endpoint |
|---|---|---|
| OAuth app risk | `token_activity` | `GET /api/v1/security/oauth` |
| User hygiene (no 2SV, never logged in, inactive, repeated failed logins, admin without 2SV) | `users_full`, `login_activity`, `admins` | `GET /api/v1/security/users` |
| Device compliance (encryption, password, developer mode, USB debugging, unknown sources, patch age, stale sync, compromised) | `devices_ci`, `devices_mobile`, `devices_cros` (+ `users_full`) | `GET /api/v1/security/devices` |
| Overview | all of the above | `GET /api/v1/security/summary` |
| Status / limits / thresholds | – | `GET /api/v1/security/status` |
| **Dry-run** fix plan (super-admin only) | – | `POST /api/v1/security/actions/plan` |

All list endpoints accept the dashboard's `?domain=` / `?domains=` filter and are scoped
server-side by the same rules as the rest of the API (super-admin sees all; everyone else only
their own email domain). Responses include a `sources` block showing which cached report was used,
when it was produced, or that it is missing and must be run from the GAM reports panel first.

## Safety model (why existing behaviour cannot change)

* Additive only: `main.py` gains one guarded block at the end (25 lines, 0 deletions) plus the
  `APP_VERSION` bump. Route table diff vs `master`: **61 → 67 routes, 0 removed or changed, 6 added.**
* If the module fails to import or build, the API still starts and logs
  `[security_audit] disabled: ...` (tested by simulating a crash).
* Kill switch: `SECURITY_AUDIT_ENABLED=0` in `.env` removes every `/api/v1/security/*` route.
* **No action is executed.** `actions/plan` validates input (strict email / client-id / OU patterns,
  shell-quoted output, refuses super-admin targets), returns the exact GAM commands for a human to
  review and run, and appends the plan to `docker-data/sl.p4sgi/security/audit/plans-YYYYMMDD.jsonl`.
  This follows the ADR non-goal "no direct write to GAM without review/approval". Wiring real
  execution is deliberately a separate, later change that needs a tenant test.
* Tablet / snapait ingest endpoints (`/attendance/ingest`, `/telemetry`, radar, publish-events) are
  untouched. The only visible difference to them is `version` reading `0.4.19` in health/schema JSON.

## NOT done / limits (read before relying on it)

1. **Drive sharing exposure is not built.** Detecting public / external sharing needs a new
   allowlisted read-only GAM report (per-user file permission scan across ~29 domains: slow, rate
   limited, needs `scripts/run_gam_report.sh` changes and a tenant test). `summary.drive.available` is `false`
   on purpose. Proposed Phase 2.
2. **OAuth scoring depends on the `token_activity` CSV having scope columns.** The existing code
   confirms `actor.email`, `id.time`, `name`, `app_name`; this module also reads `client_id` and
   `scope` / `scope.N` / `scope_data.scope_name`, which are **assumed**. Check once on the real file:

   ```bash
   head -1 /home/george/drive_14tb/docker-data/sl.p4sgi/gam-out/runs/*_token_activity.csv | tail -1 | tr ',' '\n' | grep -iE 'client|scope'
   ```

   If nothing prints, apps show as `UNKNOWN` (never a falsely reassuring LOW) and the panel says so.
   Send me that header line and the extractor is a one-line change.
3. `token_activity` is a 30-day window and `login_activity` 180 days; results are only as fresh as the
   last cached run. No "dead app after 90 days" logic for that reason.
4. Scores are triage heuristics (weights in the code, thresholds via env), not a Google rating.
5. `account_type` (student / teacher) is not in the Directory export, so no per-role rules.
6. Plan commands use GAM7 syntax from its docs and have **not** been run against your tenant. Test on
   one account and check `gam help` first. `wipe_mobile_device` needs GAM's mobile `resourceId`
   (from `gam print mobile`), which differs from the `deviceId` shown in the dashboard.

## Tunables (optional `.env`, defaults shown)

`SEC_INACTIVE_DAYS=90` `SEC_NEVER_LOGGED_IN_MIN_AGE_DAYS=30` `SEC_FAILED_LOGIN_7D=5`
`SEC_DEVICE_STALE_DAYS=30` `SEC_PATCH_MAX_AGE_DAYS=365` `SEC_LEVEL_CRITICAL=85`
`SEC_LEVEL_HIGH=60` `SEC_LEVEL_MEDIUM=40` `SECURITY_AUDIT_ENABLED=1`

## Deploy / verify / roll back

```bash
cd /home/george/drive_14tb/stacks/sl.p4sgi
git fetch origin && git checkout feature/gat-security-audit      # or merge the PR first
docker compose up -d --build

curl -s http://127.0.0.1:8088/api/v1/health            # version 0.4.19, status ok
curl -s http://127.0.0.1:8088/api/v1/security/status | python3 -m json.tool | head -20
# dashboard: hard-reload (Ctrl+Shift+R) -> "Security audit" -> Load / Reload
```

Rollback: set `SECURITY_AUDIT_ENABLED=0` and `docker compose up -d`, or `git checkout master` and
rebuild. There is no database change to undo.

## Tests

```bash
cd apps/api && pip install pytest httpx && python -m pytest tests -q     # 33 tests, no DB / GAM / network
```

## Why Grok's compose / Dockerfile / requirements / .env were not applied

They describe a different stack (Postgres 15 DB `p4sgi_security`, Redis, Celery, Nginx on 8088,
SQLAlchemy + Alembic, Python 3.9, containers `sl_p4sgi_*`). Applied to this repo they would replace the
working `sl-p4sgi-web` / `sl-p4sgi-db` pair (Python 3.12, psycopg 3, DB `slp4sgi`, volume
`../../docker-data/sl.p4sgi/postgres`), clash on port 8088, and start an empty second database.
The backend service there also mounts `/var/run/docker.sock`, which gives the container root-equivalent
control of the host. None of Redis / Celery / Alembic is needed for a read-only audit over cached CSVs.

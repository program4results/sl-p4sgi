# Phase 2 checklist — SL-P4SGI Admin Dashboard

ADR: [ADR-sl-p4sgi-admin-dashboard.md](./ADR-sl-p4sgi-admin-dashboard.md)

Phase 2 implements **GAM extract review → approve → publish school-config**, with a publish audit trail. No live GAM/Workspace API calls, no Google OAuth, no RBAC enforcement yet (stubs/comments only). Tablet apps stay in **snapait**.

## In scope

- [x] Postgres schema: `schools`, `ous`, `school_configs` (versioned), `provisioning_jobs`, `publish_events`
- [x] SQL init on first boot (`apps/api/sql/init.sql` → `docker-entrypoint-initdb.d`) + API `ensure_schema` on startup
- [x] API stubs that work:
  - [x] `GET /api/v1/health`
  - [x] `GET /api/v1/schools`
  - [x] `GET /api/v1/provisioning/jobs`
  - [x] `POST /api/v1/provisioning/jobs` (CSV/JSON upload)
  - [x] `POST /api/v1/provisioning/jobs/from-json`
  - [x] `POST /api/v1/provisioning/jobs/{id}/approve` → writes `school-configs/{emis}.json`
  - [x] `GET /api/v1/publish-events`
- [x] `scripts/ingest_gam_sample.py` — reads `docker-data/sl.p4sgi/gam-out` sample, creates a job
- [x] Minimal UI: list jobs, Approve button
- [x] Seed Test Primary (`emis=110101`, `attendanceDocId=1DKQmr6GNGnx1i-f19Tw1w1QGRfUmJr7-l3JlDWv_Fck`)
- [x] README updated; RBAC still stub comments only

## Out of scope (do not implement in Phase 2)

- [ ] Live GAM API calls
- [ ] Google OAuth / sessions
- [ ] RBAC enforcement (Superadmin / District / School)
- [ ] Moving snapait / edge-attendance / Android
- [ ] Sites embed automation
- [ ] LLM warehouse interface writes

## Verify

```bash
cd /home/george/drive_14tb/stacks/sl.p4sgi
docker compose up -d --build

# Health (host)
curl -sv http://127.0.0.1:8088/api/v1/health

# Ingest sample + approve
python3 scripts/ingest_gam_sample.py --approve

# Confirm published file
cat ../../docker-data/sl.p4sgi/school-configs/110101.json

# Or via UI
xdg-open http://127.0.0.1:8088/
```

## Data layout

```
docker-data/sl.p4sgi/
  postgres/pgdata/     Postgres PGDATA (subdir; bind mount root may have helpers)
  gam-out/             Sample / future GAM extracts
  school-configs/      Published {emis}.json after approve
```

## Notes

- Postgres host port: `127.0.0.1:5439` → container `5432`
- App host port: `${APP_PORT:-8088}` → container `8000`
- If 8088 is taken, set `APP_PORT` in `.env` to a free port and update this doc / README

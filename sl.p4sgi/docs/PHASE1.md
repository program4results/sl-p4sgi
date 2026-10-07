# Phase 1 checklist — SL-P4SGI Admin Dashboard

ADR: [ADR-sl-p4sgi-admin-dashboard.md](./ADR-sl-p4sgi-admin-dashboard.md) (Accepted 2026-09-30)

## In scope

- [x] Stack scaffold under `stacks/sl.p4sgi`
- [x] Data dirs under `docker-data/sl.p4sgi` (`postgres/`, `gam-out/`, `school-configs/`)
- [x] `docker-compose.yml` with `web` (API) + `db` (Postgres); Redis deferred
- [x] Volume mounts to `../../docker-data/sl.p4sgi/...`
- [x] FastAPI health: `/health` and `/api/v1/health`
- [x] Placeholder UI: "SL-P4SGI Admin"
- [x] README run instructions
- [x] `.env.example` (no real secrets)
- [x] Role stubs documented (Superadmin / District / School) — not enforced

## Out of scope (do not implement in Phase 1)

- [ ] GAM live API calls / ingest scripts that hit Workspace
- [ ] Google OAuth
- [ ] RBAC enforcement / session auth
- [ ] Approval → publish school-config workflow
- [ ] Publish audit trail
- [ ] LLM warehouse interface
- [ ] Moving snapait / edge-attendance / Android cutover

## Verify

```bash
cd /home/george/drive_14tb/stacks/sl.p4sgi
cp -n .env.example .env   # edit POSTGRES_PASSWORD
docker compose config     # validate
docker compose up -d --build
curl -s http://127.0.0.1:8088/api/v1/health
```

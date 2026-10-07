# Phase 3 checklist — SL-P4SGI Admin Dashboard

ADR: [ADR-sl-p4sgi-admin-dashboard.md](./ADR-sl-p4sgi-admin-dashboard.md)

Phase 3 operates the pilot dash: **publish audit ingest** + GAM path. **Phase 3.1:** on-demand allowlisted GAM reports. **Phase 3.2:** admin UX — domain filter, job approve-preview, site links, GAM preview modal. No Google OAuth / RBAC enforcement yet (Phase 4). Tablet apps stay in **snapait**.

## In scope

- [x] `POST /api/v1/publish-events` — ingest JSON: `emis`, `kind`, `school_email`, `doc_id`, `status`, `payload_preview`, `created_at`
- [x] `GET /api/v1/publish-events?emis=&kind=&domain=` — filter (kind supports `sqao_*` prefix; domain multi)
- [x] UI: Recent publish events (`daily_attendance_parents`, `radar_parents`, `sqao_*`, `school_config_publish`)
- [x] Seed Test Primary Site success: `daily_attendance_parents`, emis `110101`, class Nursery 3 / site note `5103-1-08837`, date `2026-09-28`
- [x] `scripts/ingest_gam_csv.py` — **legacy** CSV → `provisioning_jobs` (not primary)
- [x] **Phase 3.1:** `POST/GET /api/v1/gam/reports` + `scripts/run_gam_report.sh` (allowlisted host `gam`)
- [x] Optional `PUBLISH_AUDIT_TOKEN` in `.env.example`
- [x] Health phase/version → `3` / `0.3.2`
- [x] **Phase 3.2:** Top-of-page domain filter (`All` / `p4sgi.com` / `sl.p4sgi.com` + discovered); filters schools, jobs, events, GAM runs; `localStorage` persistence
- [x] **Phase 3.2:** `GET /api/v1/domains`; schools list includes `site_attendance_url`
- [x] **Phase 3.2:** Job expand/detail before Approve (`GET /api/v1/provisioning/jobs/{id}` → `preview` with payload, schools affected, config diff)
- [x] **Phase 3.2:** Clickable Site link (`siteAttendanceUrl`) in schools + publish events; Test Primary seeded to `https://sites.google.com/p4sgi.com/test-primary-school/home/attendance`
- [x] **Phase 3.2:** GAM Preview modal (CSV table / text primary; Download secondary); preview returns `headers`/`rows`
- [x] **Phase 3.2:** Empty states, loading spinners, error toasts, refresh buttons, job status badges, health/version in header

## Out of scope (Phase 4+)

- [ ] Google OAuth / sessions
- [ ] RBAC enforcement (Superadmin / District / School)
- [x] On-demand host GAM reports (allowlisted runner) — Phase 3.1 / 4a-lite
- [ ] Full live GAM API / service-account automation beyond allowlisted reports
- [ ] Moving snapait / edge-attendance / Android
- [ ] Sites embed automation

## Verify

```bash
cd /home/george/drive_14tb/stacks/sl.p4sgi
docker compose up -d --build

curl -s http://127.0.0.1:8088/api/v1/health
# expect: "phase": 3, "version": "0.3.2", "status": "ok"

curl -s http://127.0.0.1:8088/api/v1/domains
curl -s 'http://127.0.0.1:8088/api/v1/schools'
curl -s 'http://127.0.0.1:8088/api/v1/schools?domain=sl.p4sgi.com'
# Job preview (expand before approve)
curl -s "http://127.0.0.1:8088/api/v1/provisioning/jobs/$(curl -s http://127.0.0.1:8088/api/v1/provisioning/jobs | python3 -c 'import sys,json; print(json.load(sys.stdin)["jobs"][0]["id"])')" | python3 -m json.tool | head -80

# GAM preview (CSV table fields)
RID=$(curl -s http://127.0.0.1:8088/api/v1/gam/reports | python3 -c 'import sys,json; r=json.load(sys.stdin)["runs"]; print(r[0]["id"] if r else "")')
curl -s "http://127.0.0.1:8088/api/v1/gam/reports/$RID" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("format"), len(d.get("headers") or []), len(d.get("rows") or []))'

# UI
xdg-open http://127.0.0.1:8088/
```

### What to click in the UI

1. Open **http://127.0.0.1:8088/**
2. Header shows **Phase 3.2** and health `phase=3 … v0.3.2` (linked)
3. **Domain filter** (first control) — toggle All / p4sgi.com / sl.p4sgi.com; reload sections; preference persists
4. **Schools** — Test Primary with clickable **Site** link to attendance URL
5. **GAM reports** — run or **Preview** → modal CSV/table; Download secondary
6. **Provisioning jobs** — click row to expand payload / schools / config diff; **Approve** only in detail
7. **Publish events** — Site column when URL known; EMIS/kind filters persist

## Data layout

```
docker-data/sl.p4sgi/
  postgres/pgdata/
  gam-out/runs/        On-demand GAM report CSVs ({timestamp}_{report}.csv)
  gam-out/             Legacy CSV drop (deprecated as primary path)
  school-configs/      Published {emis}.json after Approve (siteAttendanceUrl seeded for 110101)
```

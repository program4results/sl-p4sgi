# p4sgi

Administrative dashboard for the SEMIS / p4sgi pilot: GAM extract review, school-config publish, publish audit, and (later) read-only warehouse assistance.

**0.4.21 (experimental):** **Ask the data** chat (local Ollama, read-only curated views, pgvector knowledge base). The db image gains pgvector (same Alpine base). Kill switch `ASK_DATA_ENABLED=0`. See [docs/ASK_DATA.md](docs/ASK_DATA.md).

**0.4.20 (experimental):** **Privacy compliance (PIA) monitor** with a required DPO email, evidence-based radar and go-live gate. Own JSON state, no DB change; kill switch `PRIVACY_MONITOR_ENABLED=0`. See [docs/PRIVACY_MONITOR.md](docs/PRIVACY_MONITOR.md).

**0.4.19:** Read-only **Security audit** (OAuth app risk, user hygiene, device compliance) over cached GAM CSVs + dry-run fix plans. No DB change; kill switch `SECURITY_AUDIT_ENABLED=0`. See [docs/SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md).

**0.4.18:** Drive Doc/Sheet export (`POST /api/v1/export/{section}`) into folder `YYYYMMDD-sl.p4sgi-<section>` (Doc + Sheet `-data`) via GAM as geb@p4sgi.com. Local DOCX/XLSX under `docker-data/sl.p4sgi/exports/`.

**0.4.17:** Attendance radar panel + `POST /api/v1/attendance/ingest` (edge-attendance/1 outbox) + developer_mode/adb/encryption device chips + `GET /api/v1/insights/summary`.

**Phase 4a:** Device registry + telemetry stubs + summary dash cards + health `0.4.18` (0.4.18 Drive exports; 0.4.17: collapsible panels, dense tables, Devices/Active column picker with hardware defaults; 0.4.15: GAM report buttons / Reload fixed, no-cache dashboard, super-admin Raw GAM exports panel; 0.4.14: Devices + Active users GAM reports, server-side domain scoping). Builds on Phase 3 publish audit / GAM reports. No WhatsApp / Play / Google OAuth yet.

| | |
|---|---|
| ADR | [docs/ADR-sl-p4sgi-admin-dashboard.md](docs/ADR-sl-p4sgi-admin-dashboard.md) |
| Phase 1 | [docs/PHASE1.md](docs/PHASE1.md) |
| Phase 2 | [docs/PHASE2.md](docs/PHASE2.md) |
| Phase 3 | [docs/PHASE3.md](docs/PHASE3.md) |
| Phase 4a | [docs/PHASE4a.md](docs/PHASE4a.md) |
| Phase 4 fleet ops | [docs/PHASE4-fleet-ops.md](docs/PHASE4-fleet-ops.md) |
| **Security audit (0.4.19)** | [docs/SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md) |
| **Privacy monitor (0.4.20, experimental)** | [docs/PRIVACY_MONITOR.md](docs/PRIVACY_MONITOR.md) |
| **Ask the data chat (0.4.21, experimental)** | [docs/ASK_DATA.md](docs/ASK_DATA.md) |
| Radar vs Site | [docs/RADAR_VS_SITE.md](docs/RADAR_VS_SITE.md) |
| **Site attendance embed (HQ runbook)** | [docs/SITE_ATTENDANCE_EMBED.md](docs/SITE_ATTENDANCE_EMBED.md) |
| **Site template + GAM provision (~100 schools)** | [docs/SCHOOL_SITE_TEMPLATE_PROVISION.md](docs/SCHOOL_SITE_TEMPLATE_PROVISION.md) |
| Radar JSON schema | [docs/radar_parents.schema.json](docs/radar_parents.schema.json) |
| **Apps Script radar HtmlService (paste this)** | [`docs/apps-script-radar-html.gs`](docs/apps-script-radar-html.gs) — full path: `/home/george/drive_14tb/stacks/sl.p4sgi/docs/apps-script-radar-html.gs` |
| Stack | `/home/george/drive_14tb/stacks/sl.p4sgi` |
| Data | `/home/george/drive_14tb/docker-data/sl.p4sgi` |

Tablet / Android apps stay in **snapait**. This project does **not** move or replace them.

## Layout

```
sl.p4sgi/
  apps/api/          FastAPI (health + provisioning + publish audit + static UI)
  apps/api/sql/      Postgres init schema
  apps/web/          UI source (mirrored into apps/api/static)
  docs/              ADR + PHASE1–4a
  scripts/           run_gam_report.sh (primary), ingest_gam_*.py (legacy)
  docker-compose.yml web + db

../../docker-data/sl.p4sgi/
  postgres/          DB volume (PGDATA in postgres/pgdata/)
  gam-out/runs/      On-demand GAM report CSVs
  gam-out/           Legacy CSV drop (deprecated primary)
  school-configs/    Published {emis}.json
  radar/             Latest radar/{emis}.json + audit/*.jsonl
  attendance/        Latest attendance/{emis}.json + audit/*.jsonl
  attendance/outbox/  Edge Attendance NDJSON ingest dumps (0.4.17)
```

## Roles (planned — still deferred)

- **Superadmin** — full control. Config: `SUPERADMIN_EMAILS` (default `geb@p4sgi.com` only); helper `is_superadmin()`. Enforcement/scoping comes next.
- **District** — district-scoped review / publish
- **School** — school-scoped read / limited actions

GAM for this stack already uses Workspace admin `geb@p4sgi.com` (primary domain `p4sgi.com`; `sl.p4sgi.com` is secondary).

## How to run

```bash
cd /home/george/drive_14tb/stacks/sl.p4sgi
cp -n .env.example .env
docker compose up -d --build

curl -s http://127.0.0.1:8088/api/v1/health
# UI
xdg-open http://127.0.0.1:8088/
```

### GAM reports (primary) → optional Approve

```bash
# Dashboard: http://127.0.0.1:8088/ → GAM reports → click a report
curl -s -X POST http://127.0.0.1:8088/api/v1/gam/reports \
  -H 'Content-Type: application/json' -d '{"report":"ous"}'
# Legacy CSV ingest (not primary): python3 scripts/ingest_gam_csv.py
```

See [scripts/README.md](scripts/README.md) for expected columns.

### Publish audit webhook

```bash
curl -s -X POST http://127.0.0.1:8088/api/v1/publish-events \
  -H 'Content-Type: application/json' \
  -d '{"emis":"110101","kind":"daily_attendance_parents","status":"ok","school_email":"sl-test@sl.p4sgi.com","doc_id":"1DKQmr6GNGnx1i-f19Tw1w1QGRfUmJr7-l3JlDWv_Fck","payload_preview":"Nursery 3 2026-09-28"}'

# If PUBLISH_AUDIT_TOKEN is set in .env:
#   -H "X-Publish-Audit-Token: $PUBLISH_AUDIT_TOKEN"
```

### Ports

| Service | Host | Notes |
|---------|------|-------|
| Web / API | `http://127.0.0.1:8088` | Override with `APP_PORT` |
| Postgres | `127.0.0.1:5439` | Localhost-only |

## API (Phase 4a)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/health` | Liveness + DB; `phase=4`, `version=0.4.18`, `phase_label=4a` |
| POST | `/api/v1/export/{section}?formats=doc,sheet` | 0.4.18 Drive Doc+Sheet (schools, devices, active_users, gam_reports, raw_exports, attendance, insights, all). Super-admin for raw_exports. |
| GET | `/api/v1/export/sections` | Export catalog + naming convention |
| GET/POST/PATCH/DELETE | `/api/v1/devices` | Fleet device registry |
| GET/POST | `/api/v1/telemetry` | Tablet telemetry ingest/list |
| GET | `/api/v1/telemetry/summary` | Dash-card aggregates |
| GET | `/api/v1/domains` | Domain filter options |
| GET | `/api/v1/emis` | EMIS codes from schools / configs / GAM OU paths (no invented SEMIS) |
| GET | `/api/v1/gam/summary` | GAM section summary chips (enrolled / never / suspended) |
| GET | `/api/v1/schools?domain=` | List schools (+ site URL) |
| GET | `/api/v1/provisioning/jobs?domain=` | List jobs (summary; expand via detail) |
| GET | `/api/v1/provisioning/jobs/{id}` | Job detail + approve preview |
| POST | `/api/v1/provisioning/jobs` | Create job (multipart CSV/JSON) |
| POST | `/api/v1/provisioning/jobs/from-json` | Create job (JSON body) |
| POST | `/api/v1/provisioning/jobs/{id}/approve` | Publish `school-configs/{emis}.json` |
| GET | `/api/v1/publish-events?emis=&kind=&domain=` | Publish audit (+ site URL) |
| POST | `/api/v1/publish-events` | Ingest operational publish event |
| GET | `/api/v1/radar/{emis}.json` | Latest radar_parents JSON (no PNG) |
| POST | `/api/v1/radar/{emis}` | Ingest radar JSON + audit (same publish token) |
| GET | `/radar/?emis=&embed=1` | Static spider page (SVG, client-side) |
| GET | `/school/{emis}/attendance?embed=1` | Combined parent page (daily + radar) — prefer Site embed |
| GET | `/api/v1/school/{emis}/attendance-bundle` | Daily digest + config + public URLs |
| GET/POST | `/api/v1/attendance/{emis}` | Daily attendance digest JSON (no PNG) |
| GET | `/api/v1/whoami` | Resolved identity + scope (0.4.14) |
| GET | `/api/v1/reports/devices?domain=&format=csv` | Devices report (Cloud Identity + mobile + CrOS, joined to user/EMIS/region) — cache-first |
| GET | `/api/v1/reports/active-users?domain=&format=csv` | Users with lastLoginTime set: devices, storage, Gmail/Drive 7d, Gemini/NotebookLM 30d, login IP |
| POST | `/api/v1/reports/{devices\|active-users}/refresh` | 202: queue read-only GAM sources; poll `GET /api/v1/reports/refresh/{bundle_id}` |
| GET/POST | `/api/v1/school-regions` | Region override CSV (`emis,region,district[,school_name]`, super-admin POST) → `gam-out/school-regions.csv` |
| GET | `/api/v1/gam/raw-exports` | 0.4.15, super-admin only (403 otherwise): every CSV under `gam-out/` run folders (probe-*/, runs/) with size, rows, mtime |
| GET | `/api/v1/gam/raw-exports/preview?path=probe-20261006e/devices.csv` | 0.4.15, super-admin only: headers + first 2000 rows (`limit=`); traversal / non-CSV = 403 |
| GET | `/api/v1/gam/raw-exports/download?path=…` | 0.4.15, super-admin only: original CSV |


### 0.4.18 notes

- **Naming:** Africa/Johannesburg date. Folder `YYYYMMDD-sl.p4sgi-<section>/` contains Google Doc `YYYYMMDD-sl.p4sgi-<section>` and Sheet `YYYYMMDD-sl.p4sgi-<section>-data`. Master pack: `YYYYMMDD-sl.p4sgi-all/` with one subfolder per section.
- **UI:** compact **Doc** / **Sheet** on each section header; **Export all** on the top bar.
- **Env:** `DRIVE_EXPORT_FOLDER_ID` (default `1r2lyU95j4BpdaLxLL-ayhl2Vsz7LnTYZ`), `GAM_EXPORT_USER=geb@p4sgi.com`.
- **Future exporters:** add to `EXPORT_SECTIONS`, `_EXPORT_COLLECTORS`, `_EXPORT_FOLDER_SLUG` in `apps/api/app/main.py`; builders live in `export_drive.py`.
- Artifacts always written under `docker-data/sl.p4sgi/exports/` even if Drive upload needs a one-time `gam oauth create` scope.

### 0.4.17 notes

- All major dashboard panels collapse like Domain Filter (chevron + `localStorage`). Defaults: Schools + GAM reports expanded; Devices, Active users, Raw exports, and other panels collapsed. Long help text sits behind **Show help**.
- Devices default visible columns prioritize hardware (`model`, `os`, `manufacturer`, `serial`, `imei`, …) so SM-X216B / Android are on-screen without horizontal hunting. Active users defaults include `latest_device_model` / `os` / `type` and hide long `unknown` school/region/district columns behind the column picker.
- Empty hardware fields show **—**; literal `unknown` is reserved for school/region/district that truly are unknown.
- Dense table row padding for a dashboard look.
- Honest limit: Google Workspace / WFE only expose hardware each phone/tablet already reported (model, OS, serial, IMEI when present). No CPU/RAM/disk serial beyond MDM/endpoint data; ChromeOS is 0 in this tenant.

### 0.4.15 notes

- Root cause of "GAM reports just loading": the `#gamMsg` status element was missing from the page, so every report button and Reload threw `Cannot set properties of null` before fetching. Element restored, access made null-safe, and a global error toast added.
- `/` is served with `Cache-Control: no-cache, no-store` and the page compares its UI version with `/api/v1/health` (warns on a stale page).
- "Latest" GAM runs are ordered by the `YYYYMMDDTHHMMSSZ_` timestamp; ad-hoc files like `runs/smoke_ous.csv` are never picked as the cached OUs report.
- gam-out is bind-mounted at `/data/gam-out` (read-write, because report runs write there).

### Domain scoping (0.4.14)

- Identity = `X-Goog-Authenticated-User-Email` (IAP; `accounts.google.com:` prefix stripped). If IAP JWT patch 003 is applied, its verified email must match the header.
- `SUPERADMIN_EMAILS` (default `geb@p4sgi.com`) see all domains. Everyone else: every `?domain=` filter is forced to their own email domain (all endpoints), GAM report rows are filtered by email domain, `/api/v1/domains` lists only their domain, reports without an email column (e.g. OUs) return no rows.
- Local with no header = super-admin. Testing: `?as=ht.test@sl.p4sgi.com` or header `X-As-User` (only honoured locally or when the real identity is a super-admin; `SCOPE_ALLOW_AS_OVERRIDE=0` disables). With `IAP_AUDIENCE` set a missing header is 401.

```bash
curl -s 'http://127.0.0.1:8088/api/v1/reports/active-users?domain=sl.p4sgi.com' | jq '.count,.summary'
curl -s 'http://127.0.0.1:8088/api/v1/reports/devices?as=ht.test@sl.p4sgi.com' | jq '.scope,.summary.by_domain'
curl -s -X POST http://127.0.0.1:8088/api/v1/reports/active-users/refresh   # 202 → poll_url
```


## Seed: Test Primary

| Field | Value |
|-------|-------|
| EMIS | `110101` |
| Site attendance | `https://sites.google.com/p4sgi.com/test-primary-school/home/attendance` |
| Email | `sl-test@sl.p4sgi.com` |
| attendanceDocId | `1DKQmr6GNGnx1i-f19Tw1w1QGRfUmJr7-l3JlDWv_Fck` |
| googleRadarExecUrl (preferred Site embed; **shared** at scale) | `/exec` after HQ Deploy Anyone — same URL for all schools; iframe adds `?emis=` |
| attendancePageUrl (HQ preview only) | `http://127.0.0.1:8088/school/110101/attendance?embed=1` |
| radarPageUrl (HQ preview only) | `http://127.0.0.1:8088/radar/?emis=110101&embed=1` |
| Radar demo | snap 4 / confirmed 4 / SEMIS 6 (Nursery 3) |
| Sample publish | `daily_attendance_parents` 2026-09-28 Nursery 3 / note `5103-1-08837` |


## Embed parent charts on Google Sites (short) — Google-hosted preferred

Full steps: **[docs/SITE_ATTENDANCE_EMBED.md](docs/SITE_ATTENDANCE_EMBED.md)** · [docs/RADAR_VS_SITE.md](docs/RADAR_VS_SITE.md)

**Apps Script file (cannot miss):** [`docs/apps-script-radar-html.gs`](docs/apps-script-radar-html.gs)  
Absolute: `/home/george/drive_14tb/stacks/sl.p4sgi/docs/apps-script-radar-html.gs`

1. Paste `apps-script-radar-html.gs` → run `seedDemoRadar_` → Deploy Web app **Anyone** → copy `/exec`
2. Pattern: `https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec?emis=110101&embed=1`
3. Sites → Test Primary Attendance → **Insert → Embed → By URL** → paste → **Publish**
4. Set school-config `googleRadarExecUrl` to that `/exec` (tablet may POST JSON to same URL)
5. **Never** embed `http://127.0.0.1:8088/...`. Cloudflare **not** required for this pilot.
6. Local dash combined page = HQ preview only. Doc publisher: `apps-script-publish-attendance.gs` (optional separate deploy).


## Scale: Site template + shared /exec (~100 schools)

One HtmlService `/exec` for all schools; Site template iframe `…/exec?emis={{EMIS}}&embed=1`; copy template per school; GAM → `school-configs/{emis}.json` (`googleRadarExecUrl` shared, `siteAttendanceUrl` / `attendanceDocId` per school). Cloudflare not required. Full HQ runbook: **[docs/SCHOOL_SITE_TEMPLATE_PROVISION.md](docs/SCHOOL_SITE_TEMPLATE_PROVISION.md)**. Stub outline: `scripts/provision_school_site_template.stub.sh`.

## Non-goals (see ADR)

- Do not treat Sheets as source of truth
- No Sites API embed automation
- No direct LLM → GAM writes without review/approval
- Do not relocate snapait / edge-attendance / Android

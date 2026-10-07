# Radar + attendance on the Google Site (Google HtmlService path)

**Date:** 2026-09-30 (Africa/Johannesburg)  
**Stack:** `sl.p4sgi` admin · related read-only: `edge-attendance-work/ea-src`  
**Version:** 0.4.11 — Google-hosted HtmlService spiders; Cloudflare **optional / abandoned for pilot**

## Goal

Parents opening the school Site Attendance page should see **progressive radar spiders** (Snap blue / Confirmed yellow / SEMIS green), with daily attendance text via the Attendance Doc embed if desired.

Google Sites **cannot iframe `http://127.0.0.1`**. Local dash radar is **HQ-only preview**.

## Preferred path (0.4.11) — Google HtmlService

**No Cloudflare. No `PUBLIC_BASE_URL` required for parent delivery.**

Full numbered runbook: **[SITE_ATTENDANCE_EMBED.md](./SITE_ATTENDANCE_EMBED.md)**  
Scale / Site template + GAM provision: **[SCHOOL_SITE_TEMPLATE_PROVISION.md](./SCHOOL_SITE_TEMPLATE_PROVISION.md)**

1. Create/open Apps Script → paste `docs/apps-script-radar-html.gs`
2. Run `seedDemoRadar_` (Test Primary 4/4/6 Nursery 3)
3. **Deploy → Web app → Anyone** → copy `/exec` URL
4. Set school-config `googleRadarExecUrl` to that `/exec`
5. Sites editor → Insert → Embed → By URL → `{googleRadarExecUrl}?emis=110101&embed=1`
6. **Publish** → verify off-LAN / incognito
7. Local dash (`:8088`) remains HQ preview only

| Role | URL |
|------|-----|
| **Parent embed (preferred)** | `{googleRadarExecUrl}?emis={emis}&embed=1` |
| HQ combined preview | `http://127.0.0.1:8088/school/{emis}/attendance?embed=1` |
| HQ radar preview | `http://127.0.0.1:8088/radar/?emis={emis}&embed=1` |
| Site page | `siteAttendanceUrl` (already HTTPS) |

School-config fields:

| Field | Purpose |
|-------|---------|
| `googleRadarExecUrl` | Apps Script Web app `/exec` — **preferred Site iframe**; **shared** across schools at scale |
| `siteAttendanceUrl` | Google Site page parents open |
| `attendancePageUrl` | Local/HQ combined dash page (preview; Sites cannot use localhost) |
| `radarPageUrl` | Local/HQ radar-only page |
| `attendanceDocId` | Optional daily text via Doc embed |
| `publishExecUrl` | Separate Doc-publisher Web app (optional) |

## Cloudflare status

| Check | Result |
|-------|--------|
| Cloudflare Tunnel for sl.p4sgi | **Rejected by George / abandoned for pilot** |
| `PUBLIC_BASE_URL` | Still localhost for HQ preview; **not required** for parents |
| Parent spiders | Google HtmlService on `script.google.com` |

Optional later (not blocking): expose dash via some other HTTPS base and mirror JSON — do **not** invent DNS.

## What “real radar” is

On the tablet (**Progressive Tracking Improvement Reports** / `RadarReportScreen`):

- Progressive **spider / radar** charts (not grouped bars when ≥3 axes).
- Three series: **Snap** (blue) · **Confirmed** (yellow) · **SEMIS Enrollment** (green).
- Periods: Daily → Weekly → Monthly → Termly → Annual.

Implemented in-app by `RadarChart.kt`. Grouped bars only when fewer than 3 axes.

## APIs (dash — HQ preview / optional mirror)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/school/{emis}/attendance` | Combined HTML (HQ preview) |
| GET | `/api/v1/school/{emis}/attendance-bundle` | Daily + config + URLs |
| GET/POST | `/api/v1/attendance/{emis}` | Daily digest JSON (no PNG) |
| GET | `/radar/?emis=&embed=1` | Spider HTML (HQ) |
| GET/POST | `/api/v1/radar/{emis}` | Radar series JSON (no PNG) |

**Parent delivery:** Apps Script `doGet` / `doPost` on the deployed `/exec` URL (see `docs/apps-script-radar-html.gs`). Android: **docs/contract only** — no rebuild for 0.4.11.

## Apps Script

| File | Role |
|------|------|
| `docs/apps-script-radar-html.gs` | **HtmlService** spiders + `doPost` JSON store (pilot parent path) |
| `docs/apps-script-radar.html` | Optional HTML stub (not required) |
| `docs/apps-script-publish-attendance.gs` | Attendance Doc body replace (separate deploy) |

## Related files

| Path | Notes |
|------|-------|
| `apps/api/static/school/attendance.html` | Combined HQ preview page |
| `apps/api/static/radar/index.html` | Local SVG spider page |
| `docs/radar_parents.schema.json` | Radar JSON schema v1 |
| `school-configs/110101.json` | Includes `googleRadarExecUrl` placeholder |

## Gaps (honest)

1. George must **Deploy** HtmlService in Google UI and paste `/exec` + Sites Embed — agents cannot.
2. Apps Script cannot POST to `127.0.0.1` — local dash ingest is optional/LAN only.
3. Google Sites has no reliable page-body API — HQ must click Insert → Embed.
4. Android not rebuilt for 0.4.11.

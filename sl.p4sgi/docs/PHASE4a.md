# Phase 4a — Registry + telemetry stubs

ADR: [ADR-sl-p4sgi-admin-dashboard.md](./ADR-sl-p4sgi-admin-dashboard.md) · Fleet backlog: [PHASE4-fleet-ops.md](./PHASE4-fleet-ops.md)

**Status:** Done (stubs). Health `phase=4`, `version=0.4.11`, `phase_label=4a`.

Android / **snapait** are **untouched**. This phase only adds dashboard registry + telemetry ingest/summary, plus an APK outbox JSON contract for later tablet work. No WhatsApp send, no Private Play, no Google OAuth/RBAC.

## In scope

- [x] Schema: `devices` (emis, school_email, tablet_android_id, sim, whatsapp, app_version, last_seen)
- [x] Schema: `telemetry_events` (device_id, kind ∈ skill_run|prompt|publish|llm_local|llm_online|data_mb|radar_point, payload jsonb, created_at)
- [x] API CRUD `/api/v1/devices`
- [x] API `POST/GET /api/v1/telemetry` + `GET /api/v1/telemetry/summary` (dash cards)
- [x] Domain filter on devices / telemetry (via `school_email`)
- [x] UI: Device registry + Telemetry summary cards
- [x] Seed 2 fake Test Primary devices (`demo-android-tp-001/002`) + sample telemetry
- [x] APK outbox JSON contract (documented only; no Android code changes)

## Out of scope (later phases)

- [ ] 4b Charts (Snap vs SEMIS gap, radar trends, attendance charts)
- [ ] 4c HT reassignment workflow
- [ ] 4d WhatsApp broadcast (registry-backed)
- [ ] 4e Private Play versioning (blocked on keystore)
- [ ] Google OAuth / RBAC
- [ ] Moving or modifying snapait / edge-attendance / APK sources

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/devices?domain=&emis=` | List registry rows |
| POST | `/api/v1/devices` | Create device |
| GET | `/api/v1/devices/{id}` | Get one |
| PATCH | `/api/v1/devices/{id}` | Partial update |
| DELETE | `/api/v1/devices/{id}` | Delete |
| GET | `/api/v1/telemetry?device_id=&kind=&domain=&emis=` | List telemetry |
| POST | `/api/v1/telemetry` | Ingest event (`device_id` or `tablet_android_id`) |
| GET | `/api/v1/telemetry/summary?domain=&emis=` | Counts for dash cards |

### Example ingest

```bash
curl -s -X POST http://127.0.0.1:8088/api/v1/telemetry \
  -H 'Content-Type: application/json' \
  -d '{
    "tablet_android_id": "demo-android-tp-001",
    "kind": "skill_run",
    "payload": {"skill": "attendance_sync", "status": "ok"}
  }'

curl -s 'http://127.0.0.1:8088/api/v1/telemetry/summary'
curl -s 'http://127.0.0.1:8088/api/v1/devices?domain=sl.p4sgi.com'
```

## Seed devices (Test Primary)

| tablet_android_id | EMIS | Email | SIM / WhatsApp |
|-------------------|------|-------|----------------|
| `demo-android-tp-001` | 110101 | sl-test@sl.p4sgi.com | +23276110001 |
| `demo-android-tp-002` | 110101 | sl-test@sl.p4sgi.com | +23276110002 |

## APK outbox JSON contract (for later tablet / snapait work)

Tablets queue work offline and POST batched items when online. **Do not implement Android here** — this is the server-side contract only.

### Envelope (POST `/api/v1/telemetry` per item, or future batch endpoint)

```json
{
  "tablet_android_id": "string — stable Android ID / app install id",
  "device_id": "uuid — optional if tablet_android_id known in registry",
  "kind": "skill_run | prompt | publish | llm_local | llm_online | data_mb | radar_point",
  "created_at": "ISO-8601 timestamptz (device clock; server stores as-is)",
  "payload": {}
}
```

### Kind payloads (suggested shapes)

| kind | payload fields |
|------|----------------|
| `skill_run` | `skill` (string), `status` (`ok`\|`failed`), `duration_ms` (int, optional) |
| `prompt` | `prompt_chars` (int), `skill` (optional) |
| `publish` | `kind` (e.g. `daily_attendance_parents`), `doc_id`, `status` |
| `llm_local` | `model` (e.g. `gemma-2b`), `tokens` (int) |
| `llm_online` | `model`, `tokens` |
| `data_mb` | `mb` (number), `period` (e.g. `YYYY-MM-DD`) |
| `radar_point` | `class`, `score` (0–1), `date` (optional) |

### Future batch outbox sync (not implemented in 4a)

```json
{
  "tablet_android_id": "demo-android-tp-001",
  "app_version": "0.4.x",
  "items": [
    {
      "client_id": "ulid-or-uuid",
      "kind": "skill_run",
      "created_at": "2026-09-30T10:16:00Z",
      "payload": {"skill": "attendance_sync", "status": "ok"}
    }
  ]
}
```

Server response (planned): `{ "accepted": ["client_id"...], "rejected": [{"client_id","error"}] }` for pending / synced / failed outbox status on device.

### Registry upsert (planned companion)

Tablets may `PATCH /api/v1/devices/{id}` or `POST /api/v1/devices` with `emis`, `school_email`, `sim`, `whatsapp`, `app_version`, `last_seen` when identity is known. HT moves (EMIS/email reassignment) are Phase 4c.

## Verify

```bash
cd /home/george/drive_14tb/stacks/sl.p4sgi
docker compose up -d --build

curl -s http://127.0.0.1:8088/api/v1/health
# expect: "phase": 4, "version": "0.4.1", "phase_label": "4a", "status": "ok"

curl -s http://127.0.0.1:8088/api/v1/devices | python3 -m json.tool | head -40
curl -s http://127.0.0.1:8088/api/v1/telemetry/summary | python3 -m json.tool
xdg-open http://127.0.0.1:8088/
```

### What to click

1. Header shows **Phase 4a** and health `phase=4 … v0.4.2`
2. **Telemetry summary** — dash cards with non-zero seed counts
3. **Device registry** — two Test Primary demo tablets
4. Domain filter still scopes devices/telemetry via school email domain

## 0.4.2 fixes (GAM + provision copy)

- Domain column / modal domain list: collapsible summary (default collapsed)
- `enrolled` = Workspace users with `creationTime` (label **Users enrolled**); not ChromeOS
- `wrong_password` / `data_storage` / other user reports: pass selected domains into `run_gam_report.sh` (`domain=` or CSV post-filter)
- data_storage preview: click column headers to sort (quota numeric)
- Provisioning job preview: explicit school-config write; does **not** create Google Site / change SEMIS


## 0.4.3 — report preview UX + fleet identity

- Every GAM CSV preview column is sortable (numeric / ISO dates / `Never` as oldest); sort persists for the open modal session.
- Preview totals bar from loaded rows (after domain filter): row count, never/ever logged in, suspended counts, quota sums when columns exist.
- Fleet / devices section: EMIS, school name, serial, Android id, SIM/WhatsApp, device type (`SM_X216B`), data used from telemetry; demo Test Primary + empty-slot pattern for ~100-school scale.
- Device registry columns: `serial`, `device_type`. Enrolled `lastLoginTime=Never` semantics unchanged.

## 0.4.4 — GAM recovery email, never logged in, chips, EMIS multi-select

- User GAM reports include `recoveryEmail` in CSV / preview when Directory returns it.
- New allowlisted report `never_logged_in` (lastLoginTime empty/Never), same preview/download UX.
- GAM section summary chips: enrolled, never logged in, % never of enrolled, ever logged in, suspended (from latest CSV runs + domain filter).
- Recent publish events: EMIS free-text replaced by multi-select from `/api/v1/emis` (schools, school-configs, OU/GAM paths — no invented SEMIS).
- Radar vs Site diagnosis: [RADAR_VS_SITE.md](RADAR_VS_SITE.md); Apps Script copy in `docs/apps-script-publish-attendance.gs`.

## 0.4.5 — radar JSON + static spider (no PNG)

Tablet must not upload chart PNGs. Dash stores tiny `radar_parents` JSON per EMIS and serves a static HTML page that draws progressive spiders (pure SVG) in the browser.

- Schema: `docs/radar_parents.schema.json` (periods Daily/Weekly/Monthly/Termly/Annual; series A snap / B confirmed / C SEMIS).
- Page: `GET /radar/?emis=110101` → `apps/api/static/radar/index.html`.
- API: `GET /api/v1/radar/{emis}.json`, `POST /api/v1/radar/{emis}` (audit jsonl + `publish_events`). Files under docker-data `radar/`.
- School-config field `radarPageUrl` (Test Primary seeded).
- Demo JSON repeats the known Nursery 3 snapshot snap 4 / confirmed 4 / SEMIS 6 on every axis so each period can draw a spider. Not extra schools.
- Apps Script patch (docs only): accept `radar` object, append the page link in the Attendance Doc, return `radarPageUrl`. No Android rebuild.

```bash
curl -s http://127.0.0.1:8088/api/v1/health
# expect version 0.4.5
curl -s http://127.0.0.1:8088/api/v1/radar/110101.json
# open http://127.0.0.1:8088/radar/?emis=110101
```


## 0.4.7 — combined attendance metrics merge + Site embed runbook

- `GET /api/v1/school/{emis}/attendance-bundle` merges daily + radar (class, date, snap/confirmed/semis, gap %, series).
- Combined parent page shows metrics strip; clarifies Site/legacy ref ≠ warehouse EMIS; notes empty `publishExecUrl`.
- HQ runbook: [SITE_ATTENDANCE_EMBED.md](./SITE_ATTENDANCE_EMBED.md). Android: no rebuild.


## 0.4.8 — Google-hosted HtmlService charts (Cloudflare abandoned for pilot)

- Prefer `googleRadarExecUrl` (`script.google.com/…/exec?emis=110101&embed=1`) for Site embed.
- Apps Script: `docs/apps-script-radar-html.gs` — `doGet` HtmlService SVG spiders from Script Properties; `doPost` accepts tiny `radar_parents` JSON (no PNG); seed Test Primary 4/4/6 Nursery 3.
- Optional stub: `docs/apps-script-radar.html` (not required — HTML is embedded in the `.gs`).
- Doc publisher remains separate: `docs/apps-script-publish-attendance.gs`.
- Runbook: [SITE_ATTENDANCE_EMBED.md](./SITE_ATTENDANCE_EMBED.md). Local dash = HQ preview only. `PUBLIC_BASE_URL` not required for parents.
- Android: docs/contract only — no rebuild.

```bash
curl -s http://127.0.0.1:8088/api/v1/health
# expect version 0.4.8
```

## 0.4.9 — radar / attendance layout polish

- Softened table separators (no harsh HR between spider SVG and axis table); more breathing room under chart/legend.
- Embed mode: full-width chart (no empty side columns from `max-width`); period chips as dark cards; taller attendance iframe.
- Mirrored CSS into `docs/apps-script-radar-html.gs` embedded HtmlService page.
- README pointer with full path to the `.gs` file.

```bash
curl -sS http://127.0.0.1:8088/api/v1/health
# expect version 0.4.9
```

## 0.4.10 — Site template + GAM provision docs (~100 schools)

Docs / schema notes only for fleet Site rollout (rebuild if `APP_VERSION` bumped in image):

- HQ runbook: [SCHOOL_SITE_TEMPLATE_PROVISION.md](./SCHOOL_SITE_TEMPLATE_PROVISION.md) — **one** HtmlService `/exec` for all schools; Site template iframe `…/exec?emis={{EMIS}}&embed=1`; copy template per school; GAM Directory → `school-config.json` (`googleRadarExecUrl` shared, `siteAttendanceUrl` / `attendanceDocId` per school); tablet POSTs JSON to same `/exec` with `emis`.
- Example + notes: `docker-data/sl.p4sgi/school-configs/110101.example.json`, `110101.json` `_notes`.
- Stub outline: `scripts/provision_school_site_template.stub.sh` (no invented SEMIS; Cloudflare not required).
- Links from README, [SITE_ATTENDANCE_EMBED.md](./SITE_ATTENDANCE_EMBED.md), [RADAR_VS_SITE.md](./RADAR_VS_SITE.md).

```bash
curl -sS http://127.0.0.1:8088/api/v1/health
# expect version 0.4.10 after image rebuild
```

## 0.4.11 — compact radar HtmlService embed (Sites one-page)

- Dense `embed=1` layout ~650–720px tall: smaller metric chips, period chips, spider SVG (~300px).
- Legend inline under chart; axis table / mode / foot hidden in embed.
- `apps/api/static/radar/index.html` and `docs/apps-script-radar-html.gs` MUST match.
- School attendance iframe defaults: 720px / embed 680px (was 900/860).
- George: re-paste `.gs` → Manage deployments → **New version** so Site picks it up.

```bash
curl -sS http://127.0.0.1:8088/api/v1/health
# expect version 0.4.11 after image rebuild
xdg-open 'http://127.0.0.1:8088/radar/?emis=110101&embed=1'
```


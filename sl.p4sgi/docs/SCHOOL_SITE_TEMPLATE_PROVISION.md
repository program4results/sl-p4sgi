# HQ runbook — Site template + GAM provision (~100 schools)

**Date:** 2026-09-30 (Africa/Johannesburg)  
**Stack:** `sl.p4sgi` · **Version:** 0.4.11  
**Scale target:** ~100 schools · **Pilot SoT school:** EMIS `110101` (Test Primary)  
**Related:** [SITE_ATTENDANCE_EMBED.md](./SITE_ATTENDANCE_EMBED.md) · [RADAR_VS_SITE.md](./RADAR_VS_SITE.md) · [PHASE4a.md](./PHASE4a.md)

Parents open a **per-school Google Site** Attendance page. Charts come from **one shared** Apps Script HtmlService Web app (`/exec`), keyed by `?emis=…`. Cloudflare is **not** required. Do **not** invent SEMIS / EMIS codes — only provision schools already known from GAM Directory / warehouse / school-configs.

---

## Architecture (one /exec, many Sites)

```
┌─────────────────────────────────────────────────────────────┐
│  ONE Apps Script project (HQ)                               │
│  Deploy → Web app → Anyone → single /exec URL               │
│  doGet(?emis=&embed=1)  → SVG spiders from Script Properties│
│  doPost(JSON with emis) → store radar_parents / daily       │
└───────────────────────────┬─────────────────────────────────┘
                            │ shared googleRadarExecUrl
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
   Site school A       Site school B       Site school N
   iframe …/exec?      iframe …/exec?      iframe …/exec?
     emis=AAAA&embed=1   emis=BBBB&embed=1   emis=NNNN&embed=1
        ▲                   ▲                   ▲
        │                   │                   │
   tablet POSTs        tablet POSTs        tablet POSTs
   JSON + emis         JSON + emis         JSON + emis
```

| Piece | Count | Notes |
|-------|-------|-------|
| HtmlService Web app (`/exec`) | **One** (shared) | Paste `docs/apps-script-radar-html.gs`; Deploy once; all schools reuse |
| Google Site (from template) | **One per school** | Copy template; replace `{{EMIS}}` in embed URL; Publish |
| Attendance Doc | **One per school** (optional) | Daily text embed; Doc Id → `attendanceDocId` |
| `school-configs/{emis}.json` | **One per school** | Written by dash Approve / stub script from GAM fields |
| Local dash `:8088` | HQ only | Preview; **never** embed localhost on Sites |

**Iframe pattern (in the Site template):**

```text
{{googleRadarExecUrl}}?emis={{EMIS}}&embed=1
```

Example after fill:

```text
https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec?emis=110101&embed=1
```

---

## One-time (HQ) vs per-school

### One-time (do once for the fleet)

1. Create / open the **shared** Apps Script project; paste `docs/apps-script-radar-html.gs`.
2. Run `seedDemoRadar_` (Test Primary only — validates the path).
3. **Deploy → Web app → Execute as: Me → Who has access: Anyone** → copy the `/exec` URL.
4. Build a **Site template** (new blank Site or duplicate Test Primary Attendance page) whose Attendance page has:
   - **Insert → Embed → By URL** → `{SHARED_EXEC}?emis={{EMIS}}&embed=1`  
     (literal `{{EMIS}}` placeholder, or a known pilot EMIS you will replace after copy)
   - Optional: Attendance Doc embed placeholder / instructions
5. Record the shared `/exec` (no query string) as the fleet default for `googleRadarExecUrl`.
6. Ensure GAM is authenticated on the gbu host (`~/.gam`) for Directory extracts used by provision jobs.
7. Optional: copy `school-configs/110101.example.json` as the field checklist for Approve payloads.

### Per-school (repeat ~100× — only for known EMIS)

1. **Identity from GAM / warehouse** — `emis`, school name, school email (OU path / Directory). Never invent SEMIS.
2. **Copy Site from template** (Sites UI or Drive file copy of the Site) → rename for the school.
3. **Replace `{{EMIS}}`** (or the pilot EMIS) in the Attendance embed URL with this school’s EMIS → **Publish**.
4. Copy / create Attendance Doc if used → note `attendanceDocId`.
5. Write / Approve `school-configs/{emis}.json` with fields below (`googleRadarExecUrl` = **same shared** `/exec`).
6. Tablet / webhook POSTs `radar_parents` JSON to that **same** `/exec` with `"emis":"<this school>"`.

Dash **Approve** still only writes `school-configs/{emis}.json` (+ audit). It does **not** create Sites, Docs, or change SEMIS.

---

## school-config.json fields (GAM Directory → config)

Path: `/home/george/drive_14tb/docker-data/sl.p4sgi/school-configs/{emis}.json`  
Example checklist: `school-configs/110101.example.json`

| Field | Source / rule | Scale note |
|-------|----------------|------------|
| `emis` | GAM OU / Directory / warehouse | Required; never invent |
| `schoolName` | Directory `name` / GAM | Display |
| `schoolEmail` | Directory `primaryEmail` | HT / school Workspace user |
| **`googleRadarExecUrl`** | Shared HtmlService `/exec` (no `?emis`) | **Same URL for all schools** |
| **`siteAttendanceUrl`** | Published Site Attendance page HTTPS | **Per school** after copy + Publish |
| **`attendanceDocId`** | Google Doc Id for daily text | Per school (optional but usual) |
| `websitePackDocId` | Website pack Doc (if used) | Per school / optional |
| `googleSiteId` | Sites resource id if known | Per school / optional |
| `publishExecUrl` | Optional separate Doc-publisher `/exec` | Often empty; tablet may POST radar to `googleRadarExecUrl` instead |
| `attendancePageUrl` | Local dash combined preview | HQ only; `http://127.0.0.1:8088/school/{emis}/attendance?embed=1` |
| `radarPageUrl` | Local dash radar preview | HQ only; `…/radar/?emis={emis}&embed=1` |
| `driveFolderId` | School Drive folder | Optional |
| `configEndpoint` | Future remote config URL | Optional / usually `""` |
| `_notes` | Operator reminders | Optional |

Minimal publish-oriented shape:

```json
{
  "emis": "110101",
  "schoolName": "Test Primary School",
  "schoolEmail": "sl-test@sl.p4sgi.com",
  "googleRadarExecUrl": "https://script.google.com/macros/s/SHARED_DEPLOYMENT_ID/exec",
  "siteAttendanceUrl": "https://sites.google.com/p4sgi.com/test-primary-school/home/attendance",
  "attendanceDocId": "1DKQmr6GNGnx1i-f19Tw1w1QGRfUmJr7-l3JlDWv_Fck",
  "publishExecUrl": "",
  "attendancePageUrl": "http://127.0.0.1:8088/school/110101/attendance?embed=1",
  "radarPageUrl": "http://127.0.0.1:8088/radar/?emis=110101&embed=1"
}
```

Parent iframe = `googleRadarExecUrl + "?emis=" + emis + "&embed=1"`.

---

## Tablet contract (POST same /exec)

Tablets POST tiny JSON (**no PNG / no base64 charts**) to the **shared** `/exec`:

```bash
curl -sS -X POST "$GOOGLE_RADAR_EXEC_URL" \
  -H 'Content-Type: application/json' \
  -d '{"emis":"110101","kind":"radar_parents","radar":{…}}'
```

- Body must include **`emis`** (or `radar.emis`) so Script Properties key per school.
- Schema: [radar_parents.schema.json](./radar_parents.schema.json); demo: `apps/api/static/radar/demo-110101.json`.
- Android / snapait: **docs / contract only** for 0.4.11 — no APK rebuild required by this doc.

---

## Copy template workflow (manual + stub)

### Manual (Sites UI)

1. Open the fleet **Site template** (or Test Primary Site used as template).
2. **Duplicate / copy** Site (or copy via Drive if your Workspace flow uses Drive Site files).
3. Rename to the school’s public name.
4. Edit **Home → Attendance** → select the Embed → set URL to  
   `{sharedExec}?emis={THIS_EMIS}&embed=1`.
5. Optional: swap Attendance Doc embed to this school’s Doc.
6. **Publish** → copy the published Attendance URL into `siteAttendanceUrl`.
7. Approve / write `school-configs/{emis}.json`.

### Stub outline (automation assist — not a full Sites API)

See [scripts/provision_school_site_template.stub.sh](../scripts/provision_school_site_template.stub.sh) and Apps Script notes in that file’s header. The stub:

- Reads a **CSV/JSON list of known EMIS** (from GAM export / dash — no invented rows).
- Writes `school-configs/{emis}.json` with shared `googleRadarExecUrl` + placeholders for Site/Doc Ids.
- Prints the exact Embed URL and checklist for HQ clicks Google cannot automate.

GAM / Drive **file** copy of Docs is often scriptable; **Sites page-body embed** is not reliably automatable (see below).

---

## What Google cannot automate (honest gaps)

| Step | Automatable? | Who does it |
|------|--------------|-------------|
| Deploy Apps Script Web app (Anyone) | **No** (UI / OAuth consent) | HQ in script.google.com |
| Sites **Insert → Embed → By URL** / edit iframe after copy | **No reliable public API** for new Sites page body | HQ (or carefully trained operators) |
| Sites **Publish** | Manual / limited | HQ |
| First-time script authorization | Manual | HQ |
| Write `school-configs/{emis}.json` | **Yes** (dash Approve / stub) | Dash or script |
| GAM Directory read → EMIS/email/name | **Yes** (existing `run_gam_report.sh` / jobs) | Host GAM |
| Drive copy of Attendance **Doc** | Often yes (Drive API / `gam`) | Script / GAM |
| Duplicate Site + fix embed URL at scale | Partially (Drive copy) + **manual embed fix** | Mixed |
| Cloudflare / public dash URL | **Not required** for this pilot | — |

Agents **cannot** complete Deploy or Sites Embed for you.

---

## Cloudflare / PUBLIC_BASE_URL

**Not required.** Parent spiders are Google-hosted. Local dash remains HQ preview. Do not block ~100-school rollout on tunnels or invented DNS.

---

## Verify (pilot, then sample school)

```bash
# Health (after 0.4.11 ship)
curl -sS http://127.0.0.1:8088/api/v1/health
# expect: "version":"0.4.11", "status":"ok"

# Config present
python3 -c "import json;print(json.load(open('/home/george/drive_14tb/docker-data/sl.p4sgi/school-configs/110101.json'))['googleRadarExecUrl'][:60])"

# Parent path (incognito / off-LAN)
# open siteAttendanceUrl → spiders for that emis
# POST demo JSON to googleRadarExecUrl with matching emis
```

Checklist per new school: known EMIS → Site copied → embed `emis=` correct → Published → `siteAttendanceUrl` + shared `googleRadarExecUrl` + `attendanceDocId` in school-config → tablet POST works.

---

## Non-goals

- Do not invent SEMIS / EMIS schools in configs or scripts.
- Do not require Cloudflare for parent delivery.
- Do not embed `http://127.0.0.1` on Sites.
- Do not upload chart PNGs.
- Do not treat dash Approve as “creates Google Site”.

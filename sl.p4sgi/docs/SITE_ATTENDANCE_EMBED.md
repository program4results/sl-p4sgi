# HQ runbook — embed Google-hosted radar on Google Sites

**Date:** 2026-09-30 (Africa/Johannesburg)  
**Stack:** `sl.p4sgi` · **Version:** 0.4.11  
**Target Site page:** https://sites.google.com/p4sgi.com/test-primary-school/home/attendance  
**Preferred parent path:** Google Apps Script **HtmlService** Web app (`googleRadarExecUrl`) — **no Cloudflare required**.

**Scale (~100 schools):** one shared `/exec` for all schools; copy Site template per EMIS — **[SCHOOL_SITE_TEMPLATE_PROVISION.md](./SCHOOL_SITE_TEMPLATE_PROVISION.md)**.

Parents should see **progressive SVG spider charts** (Snap blue / Confirmed yellow / SEMIS green) in a Site embed. The local dash at `http://127.0.0.1:8088` is **HQ-only preview** — Google Sites cannot iframe localhost, and Cloudflare was **rejected / abandoned for this pilot**.

---

## Preferred path — Google HtmlService (numbered steps)

### 1) Create or open an Apps Script project

1. Open https://script.google.com while signed in as the school / HQ account that can edit Test Primary resources (e.g. `@sl.p4sgi.com`).
2. **New project** (or open an existing radar HtmlService project).
3. Rename the project (e.g. `sl.p4sgi radar HtmlService`).

### 2) Paste stack files

1. Open the editor’s `Code.gs` (default).
2. **Select all → delete → paste** the entire contents of:

   `docs/apps-script-radar-html.gs`

   (absolute: `/home/george/drive_14tb/stacks/sl.p4sgi/docs/apps-script-radar-html.gs`)

3. Optional: `docs/apps-script-radar.html` is only a stub reminder for `createTemplateFromFile('Radar')`. **Not required** — the `.gs` file embeds the HTML page as a string.

4. **Save** (Ctrl/Cmd+S).

### 3) Seed Test Primary demo (4 / 4 / 6 · Nursery 3)

1. In the function dropdown, choose **`seedDemoRadar_`**.
2. Click **Run**.
3. Authorize when prompted (Review permissions → Allow).
4. Confirm Execution log: seeded EMIS `110101`, Nursery 3, A/B/C = 4/4/6.

(`doGet` also auto-seeds 110101 on first view if Script Properties are empty.)

### 4) Deploy Web app — Anyone

1. **Deploy → New deployment**.
2. Gear ⚙️ → type **Web app**.
3. Description: e.g. `radar HtmlService 0.4.11`.
4. **Execute as:** Me.
5. **Who has access:** **Anyone**.
6. **Deploy** → Authorize if asked.
7. **Copy** the Web app URL ending in `/exec`  
   (looks like `https://script.google.com/macros/s/…/exec`).

> Agents **cannot** deploy this for you — only a signed-in Google account in the Apps Script UI can.

### 5) Record the `/exec` URL in school-config

Edit (or let seed merge fill the key):

`/home/george/drive_14tb/docker-data/sl.p4sgi/school-configs/110101.json`

Set:

```json
"googleRadarExecUrl": "https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec"
```

Leave `""` until step 4 is done. Parent Site embed URL with query:

```text
https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec?emis=110101&embed=1
```

**Do not** require `PUBLIC_BASE_URL` for parent delivery. Localhost `attendancePageUrl` / `radarPageUrl` stay HQ preview only.

### 6) Google Sites — Insert → Embed on Test Primary Attendance

1. Open Google Sites **editor** for Test Primary.  
   Site id: `17Tr6txpL2F0WfNAklhtcsWfQHKVYBl2v`  
   Public page: https://sites.google.com/p4sgi.com/test-primary-school/home/attendance
2. Navigate to **Home → Attendance**.
3. **Insert** (right panel) → **Embed**.
4. Choose **By URL**.
5. Paste:

   ```text
   https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec?emis=110101&embed=1
   ```

   (Use the real `/exec` from step 4 + `?emis=110101&embed=1`.)

6. **Insert**. Resize for compact single viewport (~650–720 px).
7. Optional: keep the existing Attendance Doc embed (`attendanceDocId` `1DKQmr6GNGnx1i-f19Tw1w1QGRfUmJr7-l3JlDWv_Fck`) above or below for daily text.

### 7) Publish

Click **Publish** in the Sites editor. Confirm:

https://sites.google.com/p4sgi.com/test-primary-school/home/attendance

### 8) Verify as a parent

1. Open the published Attendance page in **incognito** (or a phone off this LAN).
2. Expect: school name, Nursery 3, A/B/C 4/4/6, period chips, SVG spiders.
3. If blank: wrong `/exec`, deployment not “Anyone”, page not Published, or query missing `emis=110101`.

---

## HQ-only local dash preview (not for Site embed)

On the dash host only:

```bash
curl -sS http://127.0.0.1:8088/api/v1/health
# expect: "version":"0.4.11", "status":"ok"

xdg-open 'http://127.0.0.1:8088/school/110101/attendance?embed=1'
xdg-open 'http://127.0.0.1:8088/radar/?emis=110101&embed=1'
```

**Never** paste `http://127.0.0.1:8088/...` into Sites Embed — parents get a blank iframe.

---

## Updating radar JSON later (doPost)

Tablet / webhook / curl can POST tiny `radar_parents` JSON (no PNG) to the same `/exec` URL:

```bash
curl -sS -X POST 'https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec' \
  -H 'Content-Type: application/json' \
  -d @/home/george/drive_14tb/stacks/sl.p4sgi/apps/api/static/radar/demo-110101.json
```

Body may also be `{ "kind":"radar_parents", "radar": { … } }`. PNG / base64 chart fields are rejected.

After code changes in Apps Script (including **0.4.11 compact layout**): re-paste `docs/apps-script-radar-html.gs` into Code.gs → **Deploy → Manage deployments → Edit (pencil) → Version: New version → Deploy** so the Site iframe picks up the tightened HTML. A plain Save without a new version leaves parents on the old embed.

---

## Cloudflare / PUBLIC_BASE_URL (optional — abandoned for pilot)

George **rejected Cloudflare** for this pilot. Do **not** block parent delivery on tunnels or `PUBLIC_BASE_URL`.

| Item | Pilot status |
|------|----------------|
| Cloudflare Tunnel → `:8088` | **Abandoned / optional later** — not required |
| `PUBLIC_BASE_URL` | HQ-local preview / optional future dash mirror only |
| `googleRadarExecUrl` | **Preferred** parent Site embed |
| Local `attendancePageUrl` / `radarPageUrl` | HQ preview on the dash host |

---

## What NOT to do

- **Do not** embed `http://127.0.0.1` or LAN IPs on Sites.
- **Do not** invent DNS or require Cloudflare before parents can see spiders.
- **Do not** upload chart PNGs — HtmlService draws SVG from JSON.
- **Do not** replace warehouse EMIS `110101` with Site/legacy ref `5103-1-08837`.

---

## Quick reference

| Role | URL |
|------|-----|
| **Parent Site embed (preferred)** | `{googleRadarExecUrl}?emis=110101&embed=1` |
| Google Site (parents) | https://sites.google.com/p4sgi.com/test-primary-school/home/attendance |
| HQ local combined preview | `http://127.0.0.1:8088/school/110101/attendance?embed=1` |
| HQ local radar preview | `http://127.0.0.1:8088/radar/?emis=110101&embed=1` |
| Stack Apps Script | `docs/apps-script-radar-html.gs` |
| Attendance Doc publisher (separate) | `docs/apps-script-publish-attendance.gs` |

## Remaining gaps

1. **George must Deploy** the Web app in the Google UI and paste `/exec` into `googleRadarExecUrl` + Sites Embed — agents cannot do this.
2. Google Sites has no reliable page-body API — HQ must click Insert → Embed → Publish.
3. Android / snapait: **docs only** — no rebuild for 0.4.11 (provision docs).
4. Optional later: mirror POSTs to local dash when a public base exists; not needed for parent spiders.
5. **Fleet template copy:** [SCHOOL_SITE_TEMPLATE_PROVISION.md](./SCHOOL_SITE_TEMPLATE_PROVISION.md) for ~100-school Site copy + GAM → school-config.

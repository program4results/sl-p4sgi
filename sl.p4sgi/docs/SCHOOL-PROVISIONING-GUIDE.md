# SL-P4SGI: school provisioning guide (step by step)

Updated 2026-10-14 for dashboard **0.4.27** (`apps/api/app/school_provision.py`) and tablet app 0.4.15.
Where something is **not implemented** or needs a person, it says so.

**One number per school:** the EMIS (e.g. `5103-1-08837`). It is used for the OU, the head-teacher login `ht.{EMIS}@sl.p4sgi.com`,
the school config, the publish events and the school page. Test Primary School was moved from `110101` to `5103-1-08837` on 2026-10-14
(old rows exported to `docker-data/sl.p4sgi/exports/emis-migration-110101-20261014/`).

---

## (a) Create a school — dashboard **Schools → Provision school**

1. Fill **EMIS**, **School name**, **Region / district** (path under `/sl`, e.g. `western/western-rural`), optional **Head teacher** name.
2. **Preview (dry run)** — `POST /api/v1/school-provision/preview`. Nothing is changed. It checks with GAM (as `geb@p4sgi.com`):
   * OU `/sl/<region>/<EMIS>` and `/sl/<region>/<EMIS>/staff`
   * user `ht.<EMIS>@sl.p4sgi.com` (and the school email already on file, e.g. `sl-test@sl.p4sgi.com`)
   * group membership (only if `PROVISION_GROUP` is set; today ht.* accounts are in no group, so this is skipped)
   * Drive: `Schools/<EMIS> - <name>/` under the sl.p4sgi folder, and its three copies
   Each line shows `exists`, `would_create`, `would_share` or `blocked`.
3. **Approve & create** — `POST /api/v1/school-provision/run`, **super-admin only** (`SUPERADMIN_EMAILS`, IAP header; locally no header = super-admin).
   Creates only what is missing:
   * OU + staff OU (the region/district OU must already exist)
   * `ht.<EMIS>` user with a random temporary password, *change at next login*. The password is shown **once** in the panel; it is not stored or logged
     (there is no encrypted credential store in the stack).
   * Drive folder `Schools/<EMIS> - <name>`, copies of the three templates, folder shared (writer) with the school email
   * school config v+1 (`docker-data/sl.p4sgi/school-configs/<EMIS>.json`), Schools row, a `school_config_publish` event
   Every step and its real result is stored in the job (`provisioning_jobs.payload.log`, source `school-provision`), status `approved` or `failed`.
   Running it again is safe: existing items are skipped.
4. **Create templates** (super-admin, once) — `POST /api/v1/school-provision/templates` creates `Templates/` with
   *Daily attendance (parents)* Doc, *Website pack* Doc, *Class register* Sheet (via GAM Drive upload, same auth as the Drive exports). Existing ones are reused.

Existing IDs in a school config are kept (a live Google Site may embed them); new copies only fill empty fields and are listed under `provisionedDocs`.

The older CSV/JSON **Provisioning jobs** upload + Approve still works (writes the config only, no GAM/Drive).

---

## (b) The school's public page (default) and Google Site (optional)

* **Hosted page (default):** `http://<dashboard>/s/<EMIS>` — public, read-only, mobile-friendly. Shows the latest `daily_attendance_parents`
  publishes: class, date, present, absent, enrolled, absent %. No child names or photos.
* **Publish target for the tablet:** `http://<dashboard>/api/v1/school-publish/<EMIS>/exec?compat=script.google.com`
  (the `compat` part is there because the 0.4.15 app only accepts publish URLs containing `script.google.com` and `/exec`).
  Accepted when `schoolEmail` in the body is the school's email or `ht.<EMIS>@sl.p4sgi.com` (or the publish audit token header is sent).
  It is saved as the school's **Publish website URL** (`publishExecUrl`) by Approve & create.
* **Google Site (optional, manual):** copy the template Site in Google Sites, replace `{{EMIS}}`, embed the school page or the Attendance Doc, Publish,
  then paste the Site ID into the config (Google has no API to copy/edit New Sites). See SCHOOL_SITE_TEMPLATE_PROVISION.md.

---

## (c) Dashboard panels

**Schools:** EMIS · Name · Email · Domains (used by the domain filter) · Config (latest version) · Site.
**Provisioning jobs:** ID · Status (pending/approved/failed) · EMIS · School · Source (`school-provision`, `sample_ous.csv`, …) · Domains.
**Publish events:** ID · Kind · Status · EMIS · Email · Site · Preview · When (UTC). Kinds: `school_config_publish` (Approve), `daily_attendance_parents`
(tablet publish), `radar_parents`, `sqao_summary`, `hq_note`. **"Last seen" moves only when an API publish is stored.**

---

## (d) Tablet app Settings — every field

| Field | Value | Source |
| --- | --- | --- |
| SEMIS username / EMIS | `5103-1-08837` | the school's EMIS |
| SEMIS password | set once, stored encrypted on the tablet | the school's SEMIS password |
| Sign in with Google | `ht.<EMIS>@sl.p4sgi.com` (pilot: `sl-test@sl.p4sgi.com`) | account from (a) |
| Grant All Files Access | allow | Android permission |
| AI backend | Local Gemma (offline) or Gemini (online) | choice |
| Gemini API key / model | only for Gemini | Google AI Studio — keep private |
| Gemma model path | e.g. `/sdcard/Download/gemma-4-E2B-it.litertlm` | where the model was copied |
| Sync endpoint URL | dashboard ingest `https://<dashboard>/api/v1/attendance/ingest` | dashboard |
| School config URL | `https://<dashboard>/api/v1/school-config/exec` then **Load school config** | this dashboard (replaces the Apps Script provision /exec) |
| Publish website URL | filled by Load school config (`publishExecUrl`) | (b) |
| Attendance Doc ID, Website pack Doc ID, Google Site ID, School email | filled by Load school config | school config |

`GET /api/v1/school-config?emis=…|email=…` (alias `/api/v1/school-config/exec`) returns the same JSON as the Apps Script provision script
(`ok, emis, schoolName, schoolEmail, attendanceDocId, websitePackDocId, googleSiteId, publishExecUrl, siteAttendanceUrl, driveFolderId` + `schoolPageUrl`).
It is read-only and has no secrets (only Doc/Site IDs and URLs). Behind IAP it needs an IAP exception (or a public path) for tablets.

**Known limit (app 0.4.15):** the app blocks plain `http://` (cleartext) traffic, so on the LAN (`http://192.168.1.247:8088`) *Load school config*
and *Publish* do not reach the dashboard. They need the HTTPS deployment (Cloud Run, `deploy/gcp/`) or an HTTPS proxy, or an app build that allows the LAN host.

---

## (e) Worked example — EMIS 5103-1-08837 (Test Primary School)

Real run 2026-10-14 (job `03c0db06-7993-42bd-afeb-73a5a78e3bff`, approved):
* OU `/sl/western/western-rural/5103-1-08837/staff` — existed. Users `ht.5103-1-08837@` and `sl-test@sl.p4sgi.com` — existed (no password made).
* Created Drive `Schools/5103-1-08837 - Test Primary School` (`1kw7qW26ZBj_qRlqJISU2nqAp7Ad4-sJH`) with copies of the 3 templates; shared with sl-test.
* Config v2: existing pilot Attendance Doc / Website pack / Site ID kept; Publish URL and school page set.
* School page: `http://192.168.1.247:8088/s/5103-1-08837`. Config: `http://192.168.1.247:8088/api/v1/school-config?emis=5103-1-08837`.
* Emulator phone 1: EMIS `5103-1-08837`, school, Doc IDs, Site ID and School config URL set; Google sign-in as sl-test and the SEMIS password are for a person.

---

## (f) Checklist per school

- [ ] Region/district OU exists under `/sl`
- [ ] Preview shows the expected `would_create` lines
- [ ] Approve & create → job `approved`; copy the one-time password if a user was created
- [ ] Schools row + config version; `/s/<EMIS>` opens
- [ ] (optional) Google Site copied, Site ID added
- [ ] Tablet: Google sign-in, SEMIS username/password, All files access, AI backend, School config URL → Load school config
- [ ] Publish from the tablet → new `daily_attendance_parents` event and a card on `/s/<EMIS>`

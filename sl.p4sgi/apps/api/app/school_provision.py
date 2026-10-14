"""
0.4.27 — Automated school provisioning, hosted school page, school-config endpoint.

Routes (all additive):
  POST /api/v1/school-provision/preview     dry-run: what exists / what would be created (any dashboard user)
  POST /api/v1/school-provision/run         Approve & create (super-admin only)
  GET  /api/v1/school-provision/templates   template status
  POST /api/v1/school-provision/templates   create missing template Docs/Sheet (super-admin only)
  GET  /api/v1/school-config?emis=|email=   same JSON as apps-script-provision-school-config.gs doGet
  GET  /api/v1/school-config/exec           alias (the tablet app only accepts URLs containing /exec)
  POST /api/v1/school-publish/{emis}/exec   tablet "Publish to website" target (Apps Script-compatible body)
  GET  /s/{emis}                            public, read-only school attendance page (aggregates only)

GAM runs as GAM_EXPORT_USER (geb@p4sgi.com) through the same binary/config as export_drive.py.
Every GAM/Drive action is written to the provisioning job payload.log with its real result.
Temporary passwords are returned once in the API response and never stored or logged.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import string
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

EMIS_RE = re.compile(r"^[0-9]{4}-[0-9]-[0-9]{5}$|^[0-9]{5,8}$")
SEG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

DOMAIN = os.getenv("PROVISION_DOMAIN", "sl.p4sgi.com").strip()
OU_ROOT = os.getenv("PROVISION_OU_ROOT", "/sl").rstrip("/")
GROUP = os.getenv("PROVISION_GROUP", "").strip()  # empty = no group (current ht.* convention has none)
GAM_USER = os.getenv("GAM_EXPORT_USER", "geb@p4sgi.com").strip() or "geb@p4sgi.com"
GAM_BIN = os.getenv("GAM_BIN", "/home/george/bin/gam7/gam").strip()
DRIVE_ROOT = os.getenv("DRIVE_EXPORT_FOLDER_ID", "1r2lyU95j4BpdaLxLL-ayhl2Vsz7LnTYZ").strip()
GAM_TIMEOUT = int(os.getenv("PROVISION_GAM_TIMEOUT_SEC", "120"))

TEMPLATES_FOLDER = "Templates"
SCHOOLS_FOLDER = "Schools"
TEMPLATES = {
    "attendance": ("SL-P4SGI template - Daily attendance (parents)", "gdoc"),
    "website_pack": ("SL-P4SGI template - Website pack", "gdoc"),
    "register": ("SL-P4SGI template - Class register", "gsheet"),
}
FOLDER_MIME = "application/vnd.google-apps.folder"


class ProvisionIn(BaseModel):
    emis: str
    school_name: str
    region: str  # e.g. "western/western-rural" (province/district under /sl)
    head_teacher: str | None = None
    school_email: str | None = None  # optional existing account to use instead of ht.{emis}


# --------------------------------------------------------------------------- GAM


def _gam(args: list[str], timeout: int | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    try:
        p = subprocess.run([GAM_BIN, *args], capture_output=True, text=True,
                           timeout=timeout or GAM_TIMEOUT, env=env, check=False)
    except FileNotFoundError:
        return 127, f"gam not found at {GAM_BIN}"
    except subprocess.TimeoutExpired:
        return 124, "gam timeout"
    out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    out = "\n".join(ln for ln in out.splitlines() if "Config File:" not in ln)
    return p.returncode, out


def _user_exists(email: str) -> bool:
    rc, _ = _gam(["info", "user", email, "quick"])
    return rc == 0


def _org_exists(path: str) -> bool:
    rc, _ = _gam(["info", "org", path])
    return rc == 0


def _group_member(group: str, email: str) -> bool | None:
    rc, out = _gam(["print", "group-members", "group", group, "fields", "email"])
    if rc != 0:
        return None
    return email.lower() in out.lower()


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _find_child(parent: str, name: str, folder: bool | None = None) -> str | None:
    """Direct children of parent (works in Shared Drives, where 'me in owners' queries return nothing)."""
    import csv as _csv
    import io as _io
    rc, out = _gam(["user", GAM_USER, "print", "filelist", "select", parent, "depth", "0",
                    "fields", "id,name,mimetype,trashed"])
    if rc != 0 and "Got 0 " not in out:
        raise HTTPException(502, f"Drive lookup failed: {out[-400:]}")
    lines = [ln for ln in out.splitlines() if ln.startswith("Owner,") or ln.startswith(GAM_USER + ",")]
    for row in _csv.DictReader(_io.StringIO("\n".join(lines))):
        if row.get("name") != name or str(row.get("trashed", "")).lower() == "true":
            continue
        is_folder = row.get("mimeType") == FOLDER_MIME
        if folder is None or folder == is_folder:
            return row.get("id")
    return None


_ID_RE = re.compile(r"[-\w]{25,}")


def _last_id(text: str, exclude: set[str] | None = None) -> str | None:
    ids = [m for m in _ID_RE.findall(text) if not exclude or m not in exclude]
    return ids[-1] if ids else None


def _create_folder(parent: str, name: str) -> str:
    rc, out = _gam(["user", GAM_USER, "create", "drivefile", "drivefilename", name,
                    "mimetype", "gfolder", "parentid", parent, "returnidonly"])
    fid = _last_id(out) if rc == 0 else None
    if not fid:
        raise HTTPException(502, f"create folder {name} failed: {out[-400:]}")
    return fid


def _ensure_folder(parent: str, name: str, log: list | None, dry: bool) -> str | None:
    fid = _find_child(parent, name, folder=True)
    if fid:
        _log(log, f"folder {name}", "exists", fid)
        return fid
    if dry:
        _log(log, f"folder {name}", "would_create")
        return None
    fid = _create_folder(parent, name)
    _log(log, f"folder {name}", "created", fid)
    return fid


def _log(log: list | None, action: str, status: str, detail: str = "") -> None:
    if log is not None:
        log.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "action": action, "status": status, "detail": detail})


def _temp_password() -> str:
    alpha = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alpha) for _ in range(14))
    return core[:7] + "-" + core[7:] + "9a"


# --------------------------------------------------------------------------- templates


def _template_body(key: str) -> tuple[str, str]:
    if key == "register":
        return ("register.csv",
                "date,class,snap_faces,confirmed_present,semis_enrollment,absent,absenteeism_pct,notes\n")
    if key == "website_pack":
        return ("website-pack.txt",
                "{{SCHOOL_NAME}} (EMIS {{EMIS}}) - Website pack\n\n"
                "About the school\n\nHead teacher: {{HEAD_TEACHER}}\nContact: {{SCHOOL_EMAIL}}\n\n"
                "News and notices\n\n(HQ template - edit per school. Do NOT embed this Doc on the Attendance page.)\n")
    return ("attendance.txt",
            "Daily attendance (parents) - {{SCHOOL_NAME}}\n\nEMIS: {{EMIS}}\n\n"
            "This Doc is replaced by the school tablet's 'Publish to website'.\n"
            "Only class totals are shown (no child names or photos).\n")


def templates_status(create: bool, log: list | None = None) -> dict[str, Any]:
    folder = _ensure_folder(DRIVE_ROOT, TEMPLATES_FOLDER, log, dry=not create)
    out: dict[str, Any] = {"folder_id": folder, "templates": {}}
    for key, (name, mime) in TEMPLATES.items():
        fid = _find_child(folder, name, folder=False) if folder else None
        if fid:
            _log(log, f"template {name}", "exists", fid)
        elif create and folder:
            fname, body = _template_body(key)
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / fname
                p.write_text(body, encoding="utf-8")
                rc, gout = _gam(["user", GAM_USER, "create", "drivefile", "localfile", str(p),
                                 "drivefilename", name, "mimetype", mime, "parentid", folder, "returnidonly"])
            fid = _last_id(gout) if rc == 0 else None
            _log(log, f"template {name}", "created" if fid else "failed", fid or gout[-300:])
        else:
            _log(log, f"template {name}", "missing")
        out["templates"][key] = {"name": name, "id": fid}
    return out


# --------------------------------------------------------------------------- plan / run


def _norm(body: ProvisionIn) -> dict[str, str]:
    emis = body.emis.strip()
    if not EMIS_RE.match(emis):
        raise HTTPException(400, "EMIS must look like 5103-1-08837")
    region = body.region.strip().strip("/").lower()
    segs = [s for s in region.split("/") if s]
    if not segs or any(not SEG_RE.match(s) for s in segs):
        raise HTTPException(400, "region must be like western/western-rural")
    name = re.sub(r"\s+", " ", body.school_name).strip()
    if not name or len(name) > 120:
        raise HTTPException(400, "school_name required (max 120 chars)")
    ht = re.sub(r"[^A-Za-z .'-]", "", (body.head_teacher or "")).strip()[:80]
    ou = f"{OU_ROOT}/{'/'.join(segs)}/{emis}"
    return {"emis": emis, "name": name, "ou": ou, "ou_staff": ou + "/staff",
            "ht_email": f"ht.{emis}@{DOMAIN}", "head_teacher": ht,
            "school_email_in": (body.school_email or "").strip().lower()}


def plan(m: Any, body: ProvisionIn, *, run: bool, log: list) -> dict[str, Any]:
    n = _norm(body)
    dry = not run
    existing = m.db.fetchone("SELECT email FROM schools WHERE emis = %s", (n["emis"],))
    school_email = n["school_email_in"] or ((existing or {}).get("email") or "") or n["ht_email"]
    result: dict[str, Any] = {"emis": n["emis"], "school_name": n["name"], "ou": n["ou_staff"],
                              "ht_email": n["ht_email"], "school_email": school_email,
                              "dry_run": dry, "temp_password": None}

    # OU (school + staff)
    for path in (n["ou"], n["ou_staff"]):
        if _org_exists(path):
            _log(log, f"OU {path}", "exists")
        elif dry:
            _log(log, f"OU {path}", "would_create")
        else:
            parent, leaf = path.rsplit("/", 1)
            if not _org_exists(parent):
                _log(log, f"OU {path}", "failed", f"parent OU {parent} missing - create the region/district OU first")
                raise HTTPException(400, f"parent OU {parent} does not exist")
            rc, out = _gam(["create", "org", leaf, "parent", parent])
            _log(log, f"OU {path}", "created" if rc == 0 else "failed", "" if rc == 0 else out[-300:])
            if rc != 0:
                raise HTTPException(502, f"create OU failed: {out[-300:]}")

    # Users: head-teacher account (ht.{emis}) and school email (may be the same)
    for email in dict.fromkeys([n["ht_email"], school_email]):
        if _user_exists(email):
            _log(log, f"user {email}", "exists")
            continue
        if email != n["ht_email"]:
            _log(log, f"user {email}", "missing", "school email given but account not found - not created")
            continue
        if dry:
            _log(log, f"user {email}", "would_create", f"OU {n['ou_staff']}, temp password, change at next login")
            continue
        first, last = (n["head_teacher"].split(" ", 1) + [""])[:2] if n["head_teacher"] else ("Head", "Teacher")
        pw = _temp_password()
        rc, out = _gam(["create", "user", email, "firstname", first or "Head", "lastname", last or n["emis"],
                        "password", pw, "changepassword", "on", "org", n["ou_staff"]])
        out = out.replace(pw, "***")
        _log(log, f"user {email}", "created" if rc == 0 else "failed", "" if rc == 0 else out[-300:])
        if rc != 0:
            raise HTTPException(502, f"create user failed: {out[-300:]}")
        result["temp_password"] = pw  # shown once in the response; never stored or logged

    if GROUP:
        mem = _group_member(GROUP, n["ht_email"])
        if mem:
            _log(log, f"group {GROUP}", "exists", "already a member")
        elif dry:
            _log(log, f"group {GROUP}", "would_add" if mem is False else "unknown")
        else:
            rc, out = _gam(["update", "group", GROUP, "add", "member", "user", n["ht_email"]])
            _log(log, f"group {GROUP}", "added" if rc == 0 else "failed", "" if rc == 0 else out[-300:])
    else:
        _log(log, "group", "skipped", "PROVISION_GROUP not set (ht.* accounts are in no group today)")

    # Drive: Templates, Schools/<emis> - <name>, copies, share
    tpl = templates_status(create=False, log=None)
    schools_root = _ensure_folder(DRIVE_ROOT, SCHOOLS_FOLDER, log, dry)
    folder_name = f"{n['emis']} - {n['name']}"
    school_folder = _ensure_folder(schools_root, folder_name, log, dry) if schools_root else None
    if not schools_root:
        _log(log, f"folder {folder_name}", "would_create")
    ids: dict[str, str | None] = {}
    for key, (tname, _mime) in TEMPLATES.items():
        cname = f"{n['emis']} - " + tname.replace("SL-P4SGI template - ", "")
        fid = _find_child(school_folder, cname, folder=False) if school_folder else None
        tid = (tpl["templates"].get(key) or {}).get("id")
        if fid:
            _log(log, f"copy {cname}", "exists", fid)
        elif not tid:
            _log(log, f"copy {cname}", "blocked", "template missing - press Create templates")
        elif dry:
            _log(log, f"copy {cname}", "would_create", f"from template {tid}")
        else:
            rc, out = _gam(["user", GAM_USER, "copy", "drivefile", tid, "newfilename", cname,
                            "parentid", school_folder])
            fid = _find_child(school_folder, cname, folder=False) if rc == 0 else None
            _log(log, f"copy {cname}", "created" if fid else "failed", fid or out[-300:])
        ids[key] = fid
    if school_folder and not dry:
        rc, out = _gam(["user", GAM_USER, "add", "drivefileacl", school_folder, "user", school_email,
                        "role", "writer"])
        _log(log, f"share folder with {school_email}", "ok" if rc == 0 else "failed", "" if rc == 0 else out[-300:])
    elif dry:
        _log(log, f"share folder with {school_email}", "would_share", "writer")

    result["drive"] = {"school_folder_id": school_folder, **{f"{k}_id": v for k, v in ids.items()}}
    result["school_page_url"] = page_url(n["emis"])
    result["publish_url"] = publish_url(n["emis"])
    return result


def base_url() -> str:
    return (os.getenv("SCHOOL_PAGE_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "http://127.0.0.1:8088").rstrip("/")


def page_url(emis: str) -> str:
    return f"{base_url()}/s/{emis}"


def publish_url(emis: str) -> str:
    # The 0.4.15 tablet app only accepts a Publish URL containing "script.google.com" and "/exec".
    # This keeps that check satisfied until the app accepts the dashboard URL directly.
    return f"{base_url()}/api/v1/school-publish/{emis}/exec?compat=script.google.com"


def save_config(m: Any, res: dict[str, Any], actor: str, job_id: str) -> dict[str, Any]:
    emis = res["emis"]
    cfg = m._school_config_for_emis(emis)
    d = res.get("drive") or {}
    cfg.update({
        "emis": emis,
        "schoolName": res["school_name"],
        "schoolEmail": res["school_email"],
        "driveFolderId": cfg.get("driveFolderId") or d.get("school_folder_id") or "",
        # Existing IDs win (a live Site may already embed them); copies fill only empty fields.
        "attendanceDocId": cfg.get("attendanceDocId") or d.get("attendance_id") or "",
        "websitePackDocId": cfg.get("websitePackDocId") or d.get("website_pack_id") or "",
        "registerSheetId": cfg.get("registerSheetId") or d.get("register_id") or "",
        "provisionedDocs": {k: v for k, v in d.items() if v},
        "publishExecUrl": res["publish_url"],
        "schoolPageUrl": res["school_page_url"],
        "configEndpoint": f"{base_url()}/api/v1/school-config/exec",
    })
    cfg.setdefault("googleSiteId", "")
    cfg.setdefault("siteAttendanceUrl", "")
    m.SCHOOL_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    (m.SCHOOL_CONFIGS_DIR / f"{emis}.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    m.db.execute(
        """INSERT INTO schools (emis, name, email) VALUES (%s, %s, %s)
           ON CONFLICT (emis) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email, updated_at = now()""",
        (emis, res["school_name"], res["school_email"]))
    school = m.db.fetchone("SELECT id FROM schools WHERE emis = %s", (emis,))
    v = m.db.fetchone("SELECT COALESCE(MAX(version),0) AS v FROM school_configs WHERE emis = %s", (emis,))
    ver = int(v["v"]) + 1
    sc = m.db.fetchone(
        """INSERT INTO school_configs (school_id, emis, version, config, source_job_id)
           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
        (school["id"] if school else None, emis, ver, m.db.jsonify(cfg), job_id))
    m.db.execute(
        """INSERT INTO publish_events (job_id, school_config_id, emis, config_path, config_version, actor,
             meta, kind, school_email, doc_id, status, payload_preview)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'school_config_publish',%s,%s,'ok',%s)""",
        (job_id, sc["id"], emis, f"school-configs/{emis}.json", ver, actor, m.db.jsonify({"via": "school-provision"}),
         res["school_email"], cfg.get("attendanceDocId"), f"Provisioned school-configs/{emis}.json v{ver}"))
    return {"version": ver, "config": cfg}


def config_out(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "emis": str(cfg.get("emis") or ""),
        "schoolName": str(cfg.get("schoolName") or ""),
        "schoolEmail": str(cfg.get("schoolEmail") or ""),
        "attendanceDocId": str(cfg.get("attendanceDocId") or ""),
        "websitePackDocId": str(cfg.get("websitePackDocId") or ""),
        "googleSiteId": str(cfg.get("googleSiteId") or ""),
        "publishExecUrl": str(cfg.get("publishExecUrl") or ""),
        "siteAttendanceUrl": str(cfg.get("siteAttendanceUrl") or cfg.get("schoolPageUrl") or ""),
        "driveFolderId": str(cfg.get("driveFolderId") or ""),
        "schoolPageUrl": str(cfg.get("schoolPageUrl") or ""),
    }


# --------------------------------------------------------------------------- hosted page

_KV_RE = re.compile(r"^\s*-\s*\*\*(.+?):\*\*\s*(.+?)\s*$")
_PAGE_FIELDS = {
    "Class": "class", "Date": "date", "Edge snap (faces)": "snap", "Confirmed present": "present",
    "SEMIS enrollment": "enrolled", "Absent": "absent", "Absenteeism %": "absent_pct",
}


def parse_report(md: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in (md or "").splitlines():
        mm = _KV_RE.match(ln)
        if mm and mm.group(1).strip() in _PAGE_FIELDS:
            out[_PAGE_FIELDS[mm.group(1).strip()]] = mm.group(2).strip()[:40]
    return out


def render_page(school: dict[str, Any] | None, emis: str, rows: list[dict[str, Any]]) -> str:
    name = html.escape((school or {}).get("name") or f"School {emis}")
    e = html.escape(emis)
    cards = []
    for r in rows:
        meta = r.get("meta") or {}
        c = meta.get("counts") or {}
        def g(k: str) -> str:
            return html.escape(str(c.get(k) or "—"))
        cls = html.escape(str(meta.get("class") or c.get("class") or "—"))
        date = html.escape(str(meta.get("date") or c.get("date") or str(r.get("created_at"))[:10]))
        cards.append(
            f'<div class="card"><div class="hd"><b>{cls}</b><span>{date}</span></div>'
            f'<div class="grid"><div><i>{g("present")}</i><small>present</small></div>'
            f'<div><i>{g("absent")}</i><small>absent</small></div>'
            f'<div><i>{g("enrolled")}</i><small>enrolled</small></div>'
            f'<div><i>{g("absent_pct")}</i><small>absent %</small></div></div></div>')
    body = "".join(cards) or '<p class="empty">No attendance has been published yet.</p>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{name} — attendance</title>
<style>
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9;color:#0f172a}}
header{{background:#1e3a8a;color:#fff;padding:18px 16px}} header h1{{margin:0;font-size:1.3rem}}
header p{{margin:4px 0 0;opacity:.85;font-size:.9rem}} main{{max-width:720px;margin:0 auto;padding:12px}}
.card{{background:#fff;border-radius:12px;box-shadow:0 1px 3px #0002;margin:10px 0;padding:12px 14px}}
.hd{{display:flex;justify-content:space-between;margin-bottom:8px}} .hd span{{color:#475569}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;text-align:center}}
.grid i{{display:block;font-style:normal;font-size:1.4rem;font-weight:700;color:#1e3a8a}}
.grid small{{color:#64748b}} .empty{{text-align:center;color:#64748b;padding:30px}}
footer{{text-align:center;color:#64748b;font-size:.8rem;padding:16px}}
</style></head><body><header><h1>{name}</h1><p>EMIS {e} · Daily attendance for parents</p></header>
<main>{body}</main><footer>SL-P4SGI · class totals only, no child names or photos · read-only</footer></body></html>"""


# --------------------------------------------------------------------------- router


def build_router(m: Any) -> APIRouter:
    r = APIRouter(tags=["school-provision"])

    def actor(request: Request) -> tuple[str, bool]:
        sc = m._request_scope(request)
        return str(sc.get("real_email") or sc.get("email") or "local"), bool(sc.get("superadmin"))

    def need_super(request: Request) -> str:
        who, sup = actor(request)
        if not sup:
            raise HTTPException(403, "Super-admin only (SUPERADMIN_EMAILS)")
        return who

    @r.post("/api/v1/school-provision/preview")
    def preview(body: ProvisionIn) -> dict:
        log: list = []
        res = plan(m, body, run=False, log=log)
        return {**res, "log": log}

    @r.post("/api/v1/school-provision/run")
    def run(body: ProvisionIn, request: Request) -> dict:
        who = need_super(request)
        n = _norm(body)
        job = m.db.fetchone(
            """INSERT INTO provisioning_jobs (status, source_filename, source_format, payload, emis, school_name, notes)
               VALUES ('pending','school-provision','api',%s,%s,%s,%s) RETURNING id""",
            (m.db.jsonify({"fields": {"emis": n["emis"], "school_name": n["name"], "ou_path": n["ou_staff"]}, "log": []}),
             n["emis"], n["name"], f"Provision school by {who}"))
        job_id = str(job["id"])
        log: list = []
        status, err, res, saved = "approved", None, None, None
        try:
            res = plan(m, body, run=True, log=log)
            saved = save_config(m, res, who, job_id)
            _log(log, "school config", "saved", f"v{saved['version']}")
        except HTTPException as exc:
            status, err = "failed", str(exc.detail)
            _log(log, "run", "failed", err)
        m.db.execute(
            """UPDATE provisioning_jobs SET status=%s, updated_at=now(), approved_at=CASE WHEN %s='approved' THEN now() END,
               approved_by=%s, payload = jsonb_set(payload, '{log}', %s) WHERE id=%s""",
            (status, status, who, m.db.jsonify(log), job_id))
        out = {"job_id": job_id, "status": status, "error": err, "log": log}
        if res:
            out.update({k: v for k, v in res.items() if k != "dry_run"})
        if saved:
            out["config_version"] = saved["version"]
        return out

    @r.get("/api/v1/school-provision/templates")
    def tpl_status() -> dict:
        log: list = []
        return {**templates_status(create=False, log=log), "log": log}

    @r.post("/api/v1/school-provision/templates")
    def tpl_create(request: Request) -> dict:
        need_super(request)
        log: list = []
        return {**templates_status(create=True, log=log), "log": log}

    def _lookup(emis: str | None, email: str | None) -> dict:
        emis = (emis or "").strip()
        email = (email or "").strip().lower()
        if not emis and not email:
            return {"ok": False, "error": "emis or email required"}
        if not emis:
            row = m.db.fetchone("SELECT emis FROM schools WHERE lower(email) = %s", (email,))
            if not row and email.startswith("ht.") and email.endswith("@" + DOMAIN):
                row = {"emis": email[3:].split("@")[0]}
            if not row:
                return {"ok": False, "error": "not_found"}
            emis = row["emis"]
        if not (m.SCHOOL_CONFIGS_DIR / f"{emis}.json").is_file():
            return {"ok": False, "error": "not_found"}
        return config_out(m._school_config_for_emis(emis))

    @r.get("/api/v1/school-config")
    @r.get("/api/v1/school-config/exec")
    def school_config(emis: str | None = Query(None), email: str | None = Query(None)) -> dict:
        # Read-only, same fields as the Apps Script provision /exec. Contains Doc/Site IDs and URLs only
        # (no passwords, keys or tokens), which the school's own Site already exposes.
        return _lookup(emis, email)

    @r.post("/api/v1/school-publish/{emis}/exec")
    @r.post("/api/v1/school-publish/{emis}")
    async def school_publish(emis: str, request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "JSON body required"}
        if not isinstance(body, dict) or len(json.dumps(body)) > 64000:
            return {"ok": False, "error": "invalid body"}
        emis = emis.strip()
        school = m.db.fetchone("SELECT emis, name, email FROM schools WHERE emis = %s", (emis,))
        if not school:
            raise HTTPException(404, "unknown EMIS")
        sender = str(body.get("schoolEmail") or "").strip().lower()
        allowed = {str(school.get("email") or "").lower(), f"ht.{emis}@{DOMAIN}"}
        token = m.PUBLISH_AUDIT_TOKEN
        tok_ok = bool(token) and request.headers.get("X-Publish-Audit-Token") == token
        if sender not in allowed and not tok_ok:
            raise HTTPException(403, "schoolEmail does not match this school")
        kind = str(body.get("kind") or "daily_attendance_parents")[:40]
        md = str(body.get("reportMarkdown") or "")
        counts = parse_report(md)
        meta = {"class": str(body.get("classLabel") or counts.get("class") or "")[:60],
                "date": str(body.get("attendanceDate") or counts.get("date") or "")[:20],
                "counts": counts, "via": "school-publish"}
        preview = f"{meta['class'] or 'class ?'} {meta['date']}: present {counts.get('present','?')}, absent {counts.get('absent','?')}"
        m.db.execute(
            """INSERT INTO publish_events (emis, kind, school_email, doc_id, status, payload_preview, actor, meta)
               VALUES (%s,%s,%s,%s,'ok',%s,'tablet',%s)""",
            (emis, kind, sender, str(body.get("targetDocId") or "")[:80] or None, preview[:300], m.db.jsonify(meta)))
        return {"ok": True, "message": "Published to " + page_url(emis), "url": page_url(emis)}

    @r.get("/s/{emis}", response_class=HTMLResponse)
    @r.get("/s/{emis}/", response_class=HTMLResponse)
    def school_page(emis: str) -> HTMLResponse:
        emis = emis.strip()
        if not EMIS_RE.match(emis):
            raise HTTPException(404, "not found")
        school = m.db.fetchone("SELECT name, emis FROM schools WHERE emis = %s", (emis,))
        if not school:
            raise HTTPException(404, "unknown school")
        rows = m.db.fetchall(
            """SELECT meta, created_at FROM publish_events WHERE emis = %s AND kind = 'daily_attendance_parents'
               ORDER BY created_at DESC LIMIT 20""", (emis,))
        return HTMLResponse(render_page(school, emis, rows),
                            headers={"Content-Security-Policy": "frame-ancestors *", "Cache-Control": "no-cache"})

    return r

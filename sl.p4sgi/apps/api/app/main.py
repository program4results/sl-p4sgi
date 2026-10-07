"""
p4sgi API — Phase 4a fleet registry + telemetry stubs.

Endpoints: health, schools, provisioning, publish-events, GAM reports,
device registry, telemetry ingest/summary.
GAM runs via allowlisted host runner (scripts/run_gam_report.sh); no arbitrary shell from client.
RBAC / Google OAuth / WhatsApp / Play not enforced yet. Tablet apps remain in snapait.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from . import export_drive as _exdrive

APP_TITLE = "p4sgi"
APP_VERSION = "0.4.21"
APP_PHASE = 4
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SCHOOL_CONFIGS_DIR = DATA_DIR / "school-configs"
GAM_OUT_DIR = DATA_DIR / "gam-out"
RADAR_DIR = DATA_DIR / "radar"
ATTENDANCE_DIR = DATA_DIR / "attendance"
ATTENDANCE_OUTBOX_DIR = ATTENDANCE_DIR / "outbox"
ATTENDANCE_AUDIT_DIR = ATTENDANCE_DIR / "audit"
ATTENDANCE_MAX_BYTES = 64 * 1024
RADAR_AUDIT_DIR = RADAR_DIR / "audit"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8088").rstrip("/")
DRIVE_EXPORT_FOLDER_ID = os.getenv(
    "DRIVE_EXPORT_FOLDER_ID", "1r2lyU95j4BpdaLxLL-ayhl2Vsz7LnTYZ"
).strip()
GAM_EXPORT_USER = os.getenv("GAM_EXPORT_USER", "geb@p4sgi.com").strip() or "geb@p4sgi.com"
EXPORTS_DIR = DATA_DIR / "exports"
RADAR_PERIODS = ("Daily", "Weekly", "Monthly", "Termly", "Annual")
RADAR_MAX_BYTES = 64_000
GAM_RUNS_DIR = GAM_OUT_DIR / "runs"
def _safe_parent_scripts(levels: int) -> Path | None:
    """Return <ancestor>/scripts/run_gam_report.sh if that ancestor exists."""
    try:
        return Path(__file__).resolve().parents[levels] / "scripts" / "run_gam_report.sh"
    except IndexError:
        return None


# Prefer env / image / mounts; never IndexError on short container paths.
_GAM_RUNNER_CANDIDATES = [
    Path(os.getenv("GAM_RUNNER", "")) if os.getenv("GAM_RUNNER") else None,
    Path("/scripts/run_gam_report.sh"),
    Path("/app/scripts/run_gam_report.sh"),
    _safe_parent_scripts(2),
    _safe_parent_scripts(3),
]
_GAM_RUNNER_CANDIDATES = [p for p in _GAM_RUNNER_CANDIDATES if p is not None]
GAM_RUNNER = _GAM_RUNNER_CANDIDATES[0] if _GAM_RUNNER_CANDIDATES else Path("/app/scripts/run_gam_report.sh")
GAM_ALLOWLIST = (
    "users", "enrolled", "never_logged_in", "wrong_password", "data_storage", "ous", "admins", "groups",
    "suspended", "security_2sv", "last_login",
    # 0.4.14 read-only device / activity / usage sources (see scripts/run_gam_report.sh)
    "users_full", "devices_mobile", "devices_ci", "devices_cros", "login_activity", "token_activity",
    "usage_snapshot", "usage_7d", "gemini_activity", "notebooklm_activity", "drive_activity", "drive_filecounts",
)
GAM_TIMEOUT_SEC = int(os.getenv("GAM_TIMEOUT_SEC", "600"))
PUBLISH_AUDIT_TOKEN = os.getenv("PUBLISH_AUDIT_TOKEN", "").strip()

# Super-admin emails (comma-separated). Default geb@p4sgi.com only.
# 0.4.14: domain scoping enforced server-side (DomainScopeMiddleware + report row filters).
_SUPERADMIN_RAW = os.getenv("SUPERADMIN_EMAILS", "geb@p4sgi.com").strip()
SUPERADMIN_EMAILS: frozenset[str] = frozenset(
    e.strip().lower()
    for e in _SUPERADMIN_RAW.split(",")
    if e.strip()
)


def is_superadmin(email: str | None) -> bool:
    """True if email is in SUPERADMIN_EMAILS (case-insensitive)."""
    if not email:
        return False
    return str(email).strip().lower() in SUPERADMIN_EMAILS


# Domains always offered in the UI filter (plus any discovered in data).
KNOWN_DOMAINS = (
    "ao.p4sgi.com",
    "auiped.p4sgi.com",
    "bw.p4sgi.com",
    "cg.p4sgi.com",
    "chikoro.p4sgi.com",
    "esccom.p4sgi.com",
    "gesci.p4sgi.com",
    "gm.p4sgi.com",
    "idrc.p4sgi.com",
    "ls.p4sgi.com",
    "mg.p4sgi.com",
    "mu.p4sgi.com",
    "mw.p4sgi.com",
    "mz.p4sgi.com",
    "na.p4sgi.com",
    "p4rtracker.com",
    "p4sgi.com",
    "sadc.p4sgi.com",
    "sl.p4sgi.com",
    "swazi.school",
    "sz.p4sgi.com",
    "tl.p4sgi.com",
    "tz.p4sgi.com",
    "ug.p4sgi.com",
    "uio.p4sgi.com",
    "uis.p4sgi.com",
    "za.p4sgi.com",
    "zm.p4sgi.com",
    "zw.p4sgi.com",
)


def _email_domain(email: str | None) -> str | None:
    if not email or "@" not in str(email):
        return None
    return str(email).rsplit("@", 1)[-1].strip().lower() or None


def _domains_from_url(url: str | None) -> set[str]:
    """Pull Workspace Sites path domains (sites.google.com/<domain>/...)."""
    out: set[str] = set()
    if not url:
        return out
    m = re.search(r"sites\.google\.com/([^/]+)/", str(url), re.I)
    if m:
        out.add(m.group(1).lower())
    text = str(url).lower()
    for d in KNOWN_DOMAINS:
        # Match a domain as a token, not as a substring of a subdomain
        # (e.g. ao.p4sgi.com must not also match p4sgi.com).
        if re.search(rf"(?<![a-z0-9.-]){re.escape(d.lower())}(?![a-z0-9.-])", text):
            out.add(d.lower())
    return out


def _domains_for_school_row(email: str | None, site_url: str | None = None) -> set[str]:
    found: set[str] = set()
    d = _email_domain(email)
    if d:
        found.add(d)
    found |= _domains_from_url(site_url)
    return found


def _domain_matches(selected: set[str], candidate_domains: set[str]) -> bool:
    """True if no filter (empty selected) or any overlap."""
    if not selected:
        return True
    return bool(selected & candidate_domains)


def _parse_domain_query(domain: str | None, domains: str | None) -> set[str]:
    """Accept ?domain=a&domain=b or ?domains=a,b (ignore 'all')."""
    parts: list[str] = []
    if domain:
        parts.append(domain)
    if domains:
        parts.extend(domains.split(","))
    out = {p.strip().lower() for p in parts if p and p.strip() and p.strip().lower() != "all"}
    return out



# --- Roles (stubs only — Phase 3 still does not enforce RBAC) ---
# Planned roles: Superadmin | District | School
# SUPERADMIN_EMAILS env (default geb@p4sgi.com) + is_superadmin() helper ready;
# Google OAuth + session auth + scoped queries still deferred (Phase 4d+/RBAC).

# Pilot seed matching snapait edge-attendance docs (attendanceDocId per user task).
TEST_PRIMARY_CONFIG: dict[str, Any] = {
    "emis": "110101",
    "schoolName": "Test Primary School",
    "schoolEmail": "sl-test@sl.p4sgi.com",
    "attendanceDocId": "1DKQmr6GNGnx1i-f19Tw1w1QGRfUmJr7-l3JlDWv_Fck",
    "websitePackDocId": "12Gx23QvwXUBGZLz9osggOOCuHFKHRSdtV41SVQ0k9oU",
    "googleSiteId": "17Tr6txpL2F0WfNAklhtcsWfQHKVYBl2v",
    "publishExecUrl": "",
    # Parent Site embed (preferred): Google Apps Script HtmlService /exec after HQ deploy.
    "googleRadarExecUrl": "",
    "siteAttendanceUrl": "https://sites.google.com/p4sgi.com/test-primary-school/home/attendance",
    # Local dash combined page — HQ preview only (Sites cannot iframe localhost).
    "attendancePageUrl": "http://127.0.0.1:8088/school/110101/attendance?embed=1",
    "radarPageUrl": "http://127.0.0.1:8088/radar/?emis=110101&embed=1",
    "driveFolderId": "",
    "configEndpoint": "",
    "_notes": (
        "Prefer googleRadarExecUrl (script.google.com …/exec) for Site Insert→Embed; "
        "at scale one shared /exec for all schools — iframe adds ?emis={{EMIS}}&embed=1 "
        "(docs/SCHOOL_SITE_TEMPLATE_PROVISION.md). "
        "Cloudflare / PUBLIC_BASE_URL not required for this pilot. "
        "attendancePageUrl / radarPageUrl stay localhost for HQ dash preview only. "
        "Tablets POST radar JSON to googleRadarExecUrl with emis; publishExecUrl optional Doc publisher. "
        "Warehouse EMIS is 110101; optional Site/legacy ref 5103-1-08837 is NOT an EMIS."
    ),
}

# Sample operational publish events (Test Primary Site success, 2026-09-28)
SEED_PUBLISH_EVENTS: list[dict[str, Any]] = [
    {
        "emis": "110101",
        "kind": "daily_attendance_parents",
        "school_email": "sl-test@sl.p4sgi.com",
        "doc_id": "1DKQmr6GNGnx1i-f19Tw1w1QGRfUmJr7-l3JlDWv_Fck",
        "status": "ok",
        "payload_preview": (
            "Test Primary Site success — class Nursery 3 (Nusery 3), "
            "date 2026-09-28; note site/ref 5103-1-08837"
        ),
        "created_at": "2026-09-28T14:30:00+00:00",
        "meta": {
            "class": "Nursery 3",
            "class_label_site": "Nusery 3",
            "date": "2026-09-28",
            "site_note": "5103-1-08837",
            "seed": True,
        },
    },
    {
        "emis": "110101",
        "kind": "radar_parents",
        "school_email": "sl-test@sl.p4sgi.com",
        "doc_id": "1DKQmr6GNGnx1i-f19Tw1w1QGRfUmJr7-l3JlDWv_Fck",
        "status": "ok",
        "payload_preview": "Radar parents digest for Nursery 3 — 2026-09-28 (seed)",
        "created_at": "2026-09-28T15:00:00+00:00",
        "meta": {"class": "Nursery 3", "date": "2026-09-28", "seed": True},
    },
    {
        "emis": "110101",
        "kind": "sqao_summary",
        "school_email": "sl-test@sl.p4sgi.com",
        "doc_id": "12Gx23QvwXUBGZLz9osggOOCuHFKHRSdtV41SVQ0k9oU",
        "status": "ok",
        "payload_preview": "SQAO summary published — Test Primary 2026-09-28 (seed)",
        "created_at": "2026-09-28T16:00:00+00:00",
        "meta": {"date": "2026-09-28", "seed": True},
    },
]


# Phase 4a — demo fleet tablets for Test Primary (Android/snapait untouched; stubs only)
SEED_DEVICES: list[dict[str, Any]] = [
    {
        "emis": "110101",
        "school_email": "sl-test@sl.p4sgi.com",
        "tablet_android_id": "demo-android-tp-001",
        "serial": "R9ZY40MFF4T",
        "device_type": "SM_X216B",
        "sim": "+23276110001",
        "whatsapp": "+23276110001",
        "app_version": "0.4.0-demo",
        "last_seen": "2026-09-30T10:15:00+00:00",
    },
    {
        "emis": "110101",
        "school_email": "sl-test@sl.p4sgi.com",
        "tablet_android_id": "demo-android-tp-002",
        "serial": None,
        "device_type": "SM_X216B",
        "sim": "+23276110002",
        "whatsapp": "+23276110002",
        "app_version": "0.4.0-demo",
        "last_seen": "2026-09-29T18:40:00+00:00",
    },
]

SEED_TELEMETRY: list[dict[str, Any]] = [
    {
        "tablet_android_id": "demo-android-tp-001",
        "kind": "skill_run",
        "payload": {"skill": "attendance_sync", "status": "ok", "seed": True},
        "created_at": "2026-09-30T10:16:00+00:00",
    },
    {
        "tablet_android_id": "demo-android-tp-001",
        "kind": "prompt",
        "payload": {"prompt_chars": 420, "seed": True},
        "created_at": "2026-09-30T10:17:00+00:00",
    },
    {
        "tablet_android_id": "demo-android-tp-001",
        "kind": "llm_local",
        "payload": {"model": "gemma-2b", "tokens": 180, "seed": True},
        "created_at": "2026-09-30T10:17:30+00:00",
    },
    {
        "tablet_android_id": "demo-android-tp-001",
        "kind": "publish",
        "payload": {"kind": "daily_attendance_parents", "seed": True},
        "created_at": "2026-09-30T10:18:00+00:00",
    },
    {
        "tablet_android_id": "demo-android-tp-002",
        "kind": "data_mb",
        "payload": {"mb": 12.5, "period": "2026-09-29", "seed": True},
        "created_at": "2026-09-29T19:00:00+00:00",
    },
    {
        "tablet_android_id": "demo-android-tp-002",
        "kind": "radar_point",
        "payload": {"class": "Nursery 3", "score": 0.82, "seed": True},
        "created_at": "2026-09-29T19:05:00+00:00",
    },
    {
        "tablet_android_id": "demo-android-tp-002",
        "kind": "llm_online",
        "payload": {"model": "online", "tokens": 90, "seed": True},
        "created_at": "2026-09-29T19:10:00+00:00",
    },
]

app = FastAPI(
    title=APP_TITLE,
    description="p4sgi administrative dashboard (Phase 4a / 0.4.18: Drive Doc/Sheet export per section + Attendance radar + insights).",
    version=APP_VERSION,
)


# ---------------------------------------------------------------------------
# 0.4.14 — server-side domain scoping (identity → allowed domains)
# ---------------------------------------------------------------------------
# Identity: X-Goog-Authenticated-User-Email (IAP; "accounts.google.com:" prefix stripped).
# If the IAP JWT patch (deploy/gcp/app-patches/003) is applied, its verified
# request.state.iap_email must agree with the header (checked per request).
# Super-admins (SUPERADMIN_EMAILS) see every domain; everyone else only rows whose
# email domain equals their own, and /api/v1/domains lists only that domain.
# Local (no header, IAP_AUDIENCE unset): acts as super-admin.
# Testing override: ?as=<email> or header X-As-User — honoured only locally (no header)
# or when the real identity is a super-admin (cannot be used to escalate).
IDENTITY_HEADER = "x-goog-authenticated-user-email"
AS_OVERRIDE_HEADER = "x-as-user"
SCOPE_ALLOW_AS_OVERRIDE = os.getenv("SCOPE_ALLOW_AS_OVERRIDE", "1").strip().lower() not in ("0", "false", "no", "")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _clean_identity(raw: str | None) -> str | None:
    if not raw:
        return None
    v = str(raw).strip()
    if v.lower().startswith("accounts.google.com:"):
        v = v.split(":", 1)[1]
    v = v.strip().lower()
    return v if _EMAIL_RE.match(v) else None


def _resolve_scope(header_email: str | None, as_raw: str | None) -> dict[str, Any]:
    """Return scope dict. Raises ValueError(status, detail) on denied override / missing identity."""
    real = _clean_identity(header_email)
    cloud = bool(os.getenv("IAP_AUDIENCE", "").strip())
    if cloud and not real:
        raise ValueError(401, "missing_identity: X-Goog-Authenticated-User-Email required behind IAP")
    effective = real
    source = "header" if real else "local_default_superadmin"
    if as_raw:
        as_email = _clean_identity(as_raw)
        if not as_email:
            raise ValueError(400, "invalid_as_override: as= must be an email address")
        if not SCOPE_ALLOW_AS_OVERRIDE:
            raise ValueError(403, "as_override_disabled: SCOPE_ALLOW_AS_OVERRIDE=0")
        if real is not None and not is_superadmin(real):
            raise ValueError(403, "as_override_denied: only super-admins (or local no-header) may use as=")
        effective = as_email
        source = "as_override"
    superadmin = effective is None or is_superadmin(effective)
    own = _email_domain(effective) if effective else None
    return {
        "real_email": real,
        "email": effective,
        "source": source,
        "superadmin": superadmin,
        "domain": own,
        "allowed_domains": None if superadmin else ([own] if own else []),
    }


class DomainScopeMiddleware:
    """Pure-ASGI: resolve identity, store request.state.p4_scope, and for non-super-admins
    force every ?domain=/?domains= filter to the caller's own domain (all endpoints)."""

    def __init__(self, app_: Any) -> None:
        self.app = app_

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        from urllib.parse import parse_qsl, urlencode

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        qs = scope.get("query_string", b"").decode("latin-1")
        pairs = parse_qsl(qs, keep_blank_values=True)
        as_raw = next((v for k, v in pairs if k == "as" and v), None) or headers.get(AS_OVERRIDE_HEADER)
        try:
            p4 = _resolve_scope(headers.get(IDENTITY_HEADER), as_raw)
        except ValueError as exc:
            status, detail = exc.args
            path = scope.get("path") or ""
            if path in ("/health", "/api/v1/health"):
                await self.app(scope, receive, send)
                return
            from starlette.responses import JSONResponse as _JR

            await _JR({"detail": {"error": detail.split(":")[0], "detail": detail}}, status_code=status)(scope, receive, send)
            return
        kept = [(k, v) for k, v in pairs if k != "as"]
        if not p4["superadmin"]:
            requested = {
                d.strip().lower()
                for k, v in kept if k in ("domain", "domains")
                for d in v.split(",") if d.strip() and d.strip().lower() != "all"
            }
            p4["requested_domains"] = sorted(requested)
            kept = [(k, v) for k, v in kept if k not in ("domain", "domains")]
            # Own domain only; impossible token when the caller has no usable domain.
            kept.append(("domain", p4["domain"] or "no-domain.invalid"))
        scope["query_string"] = urlencode(kept, doseq=True).encode("latin-1")
        scope.setdefault("state", {})["p4_scope"] = p4
        await self.app(scope, receive, send)


app.add_middleware(DomainScopeMiddleware)


def _request_scope(request: Request) -> dict[str, Any]:
    p4 = getattr(request.state, "p4_scope", None)
    if not p4:
        p4 = _resolve_scope(None, None)
    return p4


def _check_iap_consistency(request: Request) -> None:
    """If the IAP JWT middleware (patch 003) verified an email, it must match our identity."""
    iap_email = _clean_identity(getattr(request.state, "iap_email", None))
    if not iap_email:
        return
    p4 = _request_scope(request)
    if p4.get("real_email") != iap_email:
        raise HTTPException(401, {"error": "identity_mismatch", "detail": "IAP JWT email != X-Goog-Authenticated-User-Email"})


# Applies to every route declared below (FastAPI copies router dependencies at registration).
app.router.dependencies.append(Depends(_check_iap_consistency))


def _scope_domains_for(request: Request, selected: set[str] | None = None) -> set[str] | None:
    """Effective domain set for row filtering. None = unrestricted (super-admin, no filter)."""
    p4 = _request_scope(request)
    if not p4["superadmin"]:
        return set(p4["allowed_domains"] or ["no-domain.invalid"])
    return set(selected) if selected else None


@app.get("/api/v1/whoami")
def whoami(request: Request) -> dict:
    p4 = _request_scope(request)
    return {k: v for k, v in p4.items()} | {"superadmin_emails_configured": sorted(SUPERADMIN_EMAILS)}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class JobCreateJSON(BaseModel):
    """Create a provisioning job from structured JSON (GAM-out style)."""

    emis: str | None = None
    school_name: str | None = None
    school_email: str | None = None
    ou_path: str | None = None
    attendance_doc_id: str | None = None
    website_pack_doc_id: str | None = None
    google_site_id: str | None = None
    publish_exec_url: str | None = None
    site_attendance_url: str | None = None
    drive_folder_id: str | None = None
    config_endpoint: str | None = None
    notes: str | None = None
    raw: dict[str, Any] | list[Any] | None = None
    source_filename: str | None = "inline.json"


class PublishEventIn(BaseModel):
    """Operational publish audit event (tablet / Apps Script webhook later)."""

    emis: str
    kind: str = Field(
        ...,
        description="e.g. daily_attendance_parents, radar_parents, sqao_*",
    )
    school_email: str | None = None
    doc_id: str | None = None
    status: str = "ok"
    payload_preview: str | None = None
    created_at: datetime | None = None
    meta: dict[str, Any] | None = None


class GamReportIn(BaseModel):
    """On-demand allowlisted GAM report (Phase 3.1 / 0.4.2 domain filter)."""

    report: str = Field(
        ...,
        description="One of: users|enrolled|never_logged_in|wrong_password|data_storage|ous|admins|groups|suspended|security_2sv|last_login",
    )
    domains: list[str] | None = Field(
        None,
        description="Optional Workspace domains to scope the run (e.g. ['sl.p4sgi.com']). Empty/omit = all.",
    )
    refresh: bool = Field(
        True,
        description="When true (default), start an async GAM run. UI uses GET latest for cache-first display.",
    )


class DeviceIn(BaseModel):
    """Fleet device registry row (EMIS ↔ email ↔ tablet ↔ SIM ↔ WhatsApp)."""

    emis: str
    school_email: str | None = None
    tablet_android_id: str | None = None
    serial: str | None = None
    device_type: str | None = None
    sim: str | None = None
    whatsapp: str | None = None
    app_version: str | None = None
    last_seen: datetime | None = None


class DeviceUpdate(BaseModel):
    """Partial update for a device registry row."""

    emis: str | None = None
    school_email: str | None = None
    tablet_android_id: str | None = None
    serial: str | None = None
    device_type: str | None = None
    sim: str | None = None
    whatsapp: str | None = None
    app_version: str | None = None
    last_seen: datetime | None = None


TELEMETRY_KINDS = (
    "skill_run",
    "prompt",
    "publish",
    "llm_local",
    "llm_online",
    "data_mb",
    "radar_point",
)


class TelemetryIn(BaseModel):
    """Tablet outbox telemetry event (Phase 4a stub ingest)."""

    device_id: UUID | None = None
    tablet_android_id: str | None = None
    kind: str = Field(..., description="skill_run|prompt|publish|llm_local|llm_online|data_mb|radar_point")
    payload: dict[str, Any] | None = None
    created_at: datetime | None = None

class HealthOut(BaseModel):
    status: str
    service: str
    phase: int
    version: str
    db: str = "unknown"
    phase_label: str = "4a"


# ---------------------------------------------------------------------------
# Auth helper (optional publish audit token)
# ---------------------------------------------------------------------------


def require_publish_token(
    x_publish_audit_token: str | None = Header(None, alias="X-Publish-Audit-Token"),
    authorization: str | None = Header(None),
) -> None:
    """If PUBLISH_AUDIT_TOKEN is set, require matching X-Publish-Audit-Token or Bearer."""
    if not PUBLISH_AUDIT_TOKEN:
        return
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    provided = (x_publish_audit_token or bearer or "").strip()
    if provided != PUBLISH_AUDIT_TOKEN:
        raise HTTPException(401, "Invalid or missing publish audit token")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.on_event("startup")
def on_startup() -> None:
    SCHOOL_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    GAM_OUT_DIR.mkdir(parents=True, exist_ok=True)
    GAM_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    RADAR_DIR.mkdir(parents=True, exist_ok=True)
    RADAR_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    ATTENDANCE_DIR.mkdir(parents=True, exist_ok=True)
    ATTENDANCE_OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    ATTENDANCE_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for _ in range(30):
        try:
            db.ensure_schema()
            _ensure_attendance_sync_table()
            _seed_test_primary_school()
            _seed_publish_events()
            _seed_fleet_devices()
            _seed_demo_radar()
            _seed_demo_attendance()
            return
        except Exception as exc:  # noqa: BLE001 — startup retry
            last_err = exc
            import time

            time.sleep(1)
    raise RuntimeError(f"DB schema init failed: {last_err}")


def _seed_test_primary_school() -> None:
    """Ensure Test Primary school row + config with siteAttendanceUrl (Phase 3.2)."""
    db.execute(
        """
        INSERT INTO schools (emis, name, email, district, province)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (emis) DO UPDATE
          SET name = EXCLUDED.name,
              email = EXCLUDED.email,
              updated_at = now()
        """,
        (
            TEST_PRIMARY_CONFIG["emis"],
            TEST_PRIMARY_CONFIG["schoolName"],
            TEST_PRIMARY_CONFIG["schoolEmail"],
            "Pilot",
            "Sierra Leone",
        ),
    )
    # Keep on-disk + DB school config siteAttendanceUrl in sync for UI links.
    emis = TEST_PRIMARY_CONFIG["emis"]
    SCHOOL_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCHOOL_CONFIGS_DIR / f"{emis}.json"
    config = dict(TEST_PRIMARY_CONFIG)
    if out_path.is_file():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                # Preserve any operator overrides; always fill empty siteAttendanceUrl.
                merged = dict(existing)
                for k, v in TEST_PRIMARY_CONFIG.items():
                    if k in ("siteAttendanceUrl", "radarPageUrl", "attendancePageUrl") or not merged.get(k):
                        if v not in (None, ""):
                            merged[k] = v
                # Public host may differ from the baked localhost default.
                merged["radarPageUrl"] = _radar_page_url(emis)
                merged["attendancePageUrl"] = _attendance_page_url(emis)
                merged.setdefault("googleRadarExecUrl", "")
                config = merged
        except (json.JSONDecodeError, OSError):
            pass
    rendered = json.dumps(config, indent=2) + "\n"
    # Startup merge is not a publish. Skip the write when bytes match so a
    # container restart does not look like a Site republish (mtime bump).
    if not out_path.is_file() or out_path.read_text(encoding="utf-8") != rendered:
        out_path.write_text(rendered, encoding="utf-8")
    school = db.fetchone("SELECT id FROM schools WHERE emis = %s", (emis,))
    latest = db.fetchone(
        "SELECT id, version, config FROM school_configs WHERE emis = %s ORDER BY version DESC LIMIT 1",
        (emis,),
    )
    if latest:
        cur_cfg = latest.get("config") or {}
        if isinstance(cur_cfg, str):
            try:
                cur_cfg = json.loads(cur_cfg)
            except json.JSONDecodeError:
                cur_cfg = {}
        if not isinstance(cur_cfg, dict):
            cur_cfg = {}
        if (
            not cur_cfg.get("siteAttendanceUrl")
            or not cur_cfg.get("radarPageUrl")
            or not cur_cfg.get("attendancePageUrl")
            or "googleRadarExecUrl" not in cur_cfg
        ):
            cur_cfg = {**cur_cfg, **{k: v for k, v in config.items() if v not in (None, "")}}
            db.execute(
                "UPDATE school_configs SET config = %s WHERE id = %s",
                (db.jsonify(cur_cfg), latest["id"]),
            )
    else:
        db.execute(
            """
            INSERT INTO school_configs (school_id, emis, version, config, source_job_id)
            VALUES (%s, %s, 1, %s, NULL)
            """,
            (school["id"] if school else None, emis, db.jsonify(config)),
        )


def _seed_publish_events() -> None:
    """Idempotent seed of Test Primary Site success publish events (Phase 3)."""
    existing = db.fetchone(
        """
        SELECT COUNT(*) AS n FROM publish_events
        WHERE kind = 'daily_attendance_parents'
          AND emis = %s
          AND meta->>'seed' = 'true'
        """,
        (TEST_PRIMARY_CONFIG["emis"],),
    )
    if existing and int(existing["n"]) > 0:
        return
    for ev in SEED_PUBLISH_EVENTS:
        created = ev.get("created_at")
        db.execute(
            """
            INSERT INTO publish_events
              (emis, kind, school_email, doc_id, status, payload_preview,
               created_at, actor, meta, config_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz, %s, %s, NULL)
            """,
            (
                ev["emis"],
                ev["kind"],
                ev.get("school_email"),
                ev.get("doc_id"),
                ev.get("status", "ok"),
                ev.get("payload_preview"),
                created,
                "seed",
                db.jsonify(ev.get("meta") or {}),
            ),
        )






def _ensure_attendance_sync_table() -> None:
    """0.4.17: Edge Attendance outbox ingest (edge-attendance/1) → Postgres + NDJSON outbox dir."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_sync_events (
            id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id                  TEXT NOT NULL UNIQUE,
            schema_version              TEXT,
            emis_code                   TEXT NOT NULL,
            school_email                TEXT,
            class_label                 TEXT,
            attendance_date             DATE,
            detected_count              INTEGER,
            confirmed_count             INTEGER,
            absent_named                JSONB NOT NULL DEFAULT '[]'::jsonb,
            mean_confidence             DOUBLE PRECISION,
            manual_review_required      BOOLEAN,
            photo_present_at_capture    BOOLEAN,
            photo_retained              BOOLEAN,
            model_version               TEXT,
            device_serial               TEXT,
            synced_at                   TIMESTAMPTZ,
            province                    TEXT,
            district                    TEXT,
            payload                     JSONB NOT NULL DEFAULT '{}'::jsonb,
            received_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_attendance_sync_emis_date "
        "ON attendance_sync_events (emis_code, attendance_date)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_attendance_sync_province "
        "ON attendance_sync_events (province)"
    )


def _ensure_device_columns() -> None:
    """Idempotent Phase 0.4.3 columns: serial + device_type on devices."""
    db.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS serial TEXT")
    db.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS device_type TEXT")


def _school_name_lookup() -> dict[str, str]:
    """EMIS → school name from schools table + school-config JSON files."""
    names: dict[str, str] = {}
    try:
        for row in db.fetchall("SELECT emis, name FROM schools WHERE emis IS NOT NULL"):
            if row.get("emis") and row.get("name"):
                names[str(row["emis"])] = str(row["name"])
    except Exception:  # noqa: BLE001
        pass
    try:
        if SCHOOL_CONFIGS_DIR.is_dir():
            for path in SCHOOL_CONFIGS_DIR.glob("*.json"):
                if path.name.endswith(".example.json"):
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                emis = str(data.get("emis") or path.stem)
                name = data.get("schoolName") or data.get("school_name")
                if emis and name and emis not in names:
                    names[emis] = str(name)
    except Exception:  # noqa: BLE001
        pass
    return names


def _device_data_mb_map(device_ids: list[Any]) -> dict[str, float]:
    """Sum telemetry data_mb payload.mb per device_id."""
    out: dict[str, float] = {}
    if not device_ids:
        return out
    placeholders = ",".join(["%s"] * len(device_ids))
    rows = db.fetchall(
        f"""
        SELECT device_id, payload
        FROM telemetry_events
        WHERE device_id IN ({placeholders}) AND kind = 'data_mb'
        """,
        tuple(str(x) for x in device_ids),
    )
    for r in rows:
        did = str(r.get("device_id") or "")
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if not isinstance(payload, dict):
            continue
        try:
            mb = float(payload.get("mb") or 0)
        except (TypeError, ValueError):
            continue
        out[did] = out.get(did, 0.0) + mb
    return out


def _seed_fleet_devices() -> None:
    """Idempotent seed of 2 Test Primary demo tablets + sample telemetry (Phase 4a)."""
    for dev in SEED_DEVICES:
        existing = db.fetchone(
            "SELECT id FROM devices WHERE tablet_android_id = %s",
            (dev["tablet_android_id"],),
        )
        if existing:
            device_id = existing["id"]
            # Backfill identity fields on existing seed rows (0.4.3)
            db.execute(
                """
                UPDATE devices
                SET serial = COALESCE(NULLIF(serial, ''), %s),
                    device_type = COALESCE(NULLIF(device_type, ''), %s),
                    updated_at = now()
                WHERE id = %s
                  AND (
                    serial IS NULL OR serial = ''
                    OR device_type IS NULL OR device_type = ''
                  )
                """,
                (dev.get("serial"), dev.get("device_type"), str(device_id)),
            )
        else:
            row = db.fetchone(
                """
                INSERT INTO devices
                  (emis, school_email, tablet_android_id, serial, device_type,
                   sim, whatsapp, app_version, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                RETURNING id
                """,
                (
                    dev["emis"],
                    dev.get("school_email"),
                    dev["tablet_android_id"],
                    dev.get("serial"),
                    dev.get("device_type"),
                    dev.get("sim"),
                    dev.get("whatsapp"),
                    dev.get("app_version"),
                    dev.get("last_seen"),
                ),
            )
            device_id = row["id"] if row else None
        if not device_id:
            continue
        for ev in SEED_TELEMETRY:
            if ev["tablet_android_id"] != dev["tablet_android_id"]:
                continue
            already = db.fetchone(
                """
                SELECT COUNT(*) AS n FROM telemetry_events
                WHERE device_id = %s
                  AND kind = %s
                  AND payload->>'seed' = 'true'
                  AND created_at = %s::timestamptz
                """,
                (device_id, ev["kind"], ev.get("created_at")),
            )
            if already and int(already["n"]) > 0:
                continue
            db.execute(
                """
                INSERT INTO telemetry_events (device_id, kind, payload, created_at)
                VALUES (%s, %s, %s, %s::timestamptz)
                """,
                (
                    device_id,
                    ev["kind"],
                    db.jsonify(ev.get("payload") or {}),
                    ev.get("created_at"),
                ),
            )


# ---------------------------------------------------------------------------
# Health / meta
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthOut)
@app.get("/api/v1/health", response_model=HealthOut)
def health() -> dict:
    db_status = "ok"
    try:
        row = db.fetchone("SELECT 1 AS n")
        if not row or row.get("n") != 1:
            db_status = "error"
    except Exception as exc:  # noqa: BLE001
        db_status = f"error:{type(exc).__name__}"
    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "service": "p4sgi",
        "phase": APP_PHASE,
        "version": APP_VERSION,
        "db": db_status,
        "phase_label": "4a",
    }


@app.get("/api/v1/meta")
def meta() -> dict:
    return {
        "name": APP_TITLE,
        "phase": APP_PHASE,
        "version": APP_VERSION,
        "phase_label": "4a",
        "roles_planned": ["superadmin", "district", "school"],
        "superadmin_emails_configured": sorted(SUPERADMIN_EMAILS),
        "auth": "iap_header",  # X-Goog-Authenticated-User-Email (local: none = super-admin)
        "rbac": "domain_scoping",  # super-admin = all domains; others = own email domain only
        "gam": "on_demand_runner",  # POST /api/v1/gam/reports → scripts/run_gam_report.sh
        "gam_reports": list(GAM_ALLOWLIST),
        "publish_audit_token_required": bool(PUBLISH_AUDIT_TOKEN),
    }


# ---------------------------------------------------------------------------
# Schools
# ---------------------------------------------------------------------------


@app.get("/api/v1/domains")
def list_domains(request: Request) -> dict:
    """Domains for the top-of-page filter: known + discovered from data."""
    found: set[str] = set(KNOWN_DOMAINS)
    for row in db.fetchall("SELECT email FROM schools WHERE email IS NOT NULL"):
        d = _email_domain(row.get("email"))
        if d:
            found.add(d)
    for row in db.fetchall(
        """
        SELECT config->>'schoolEmail' AS email,
               config->>'siteAttendanceUrl' AS site_url
        FROM school_configs
        """
    ):
        d = _email_domain(row.get("email"))
        if d:
            found.add(d)
        found |= _domains_from_url(row.get("site_url"))
    for row in db.fetchall(
        """
        SELECT emis, school_name, payload FROM provisioning_jobs
        """
    ):
        payload = row.get("payload") or {}
        fields = payload.get("fields") if isinstance(payload, dict) else {}
        if isinstance(fields, dict):
            d = _email_domain(fields.get("school_email"))
            if d:
                found.add(d)
            found |= _domains_from_url(fields.get("site_attendance_url"))
    for row in db.fetchall(
        "SELECT school_email FROM publish_events WHERE school_email IS NOT NULL"
    ):
        d = _email_domain(row.get("school_email"))
        if d:
            found.add(d)
    for row in db.fetchall(
        "SELECT school_email FROM devices WHERE school_email IS NOT NULL"
    ):
        d = _email_domain(row.get("school_email"))
        if d:
            found.add(d)
    # Return the complete catalog plus any DB-discovered domains alphabetically.
    ordered = sorted(found)
    p4 = _request_scope(request)
    if not p4["superadmin"]:
        own = list(p4["allowed_domains"] or [])
        return {"domains": own, "known": own, "scoped": True, "scope_email": p4.get("email")}
    return {"domains": ordered, "known": sorted(KNOWN_DOMAINS), "scoped": False, "scope_email": p4.get("email")}


@app.get("/api/v1/schools")
def list_schools(
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
) -> dict:
    selected = _parse_domain_query(
        ",".join(domain) if domain else None,
        domains,
    )
    # Flatten repeated ?domain= into set (FastAPI may pass list)
    if domain:
        for d in domain:
            selected |= _parse_domain_query(d, None)
    rows = db.fetchall(
        """
        SELECT s.id, s.emis, s.name, s.email, s.district, s.province,
               s.created_at, s.updated_at,
               (
                 SELECT sc.config->>'siteAttendanceUrl'
                 FROM school_configs sc
                 WHERE sc.emis = s.emis
                 ORDER BY sc.version DESC
                 LIMIT 1
               ) AS site_attendance_url,
               (
                 SELECT sc.config->>'googleSiteId'
                 FROM school_configs sc
                 WHERE sc.emis = s.emis
                 ORDER BY sc.version DESC
                 LIMIT 1
               ) AS google_site_id,
               (
                 SELECT sc.version
                 FROM school_configs sc
                 WHERE sc.emis = s.emis
                 ORDER BY sc.version DESC
                 LIMIT 1
               ) AS config_version
        FROM schools s
        ORDER BY s.name
        """
    )
    schools = []
    for r in rows:
        ser = _serialize(r)
        site = ser.get("site_attendance_url") or ""
        cand = _domains_for_school_row(ser.get("email"), site)
        ser["domains"] = sorted(cand)
        if not _domain_matches(selected, cand):
            continue
        schools.append(ser)
    return {"schools": schools}


# ---------------------------------------------------------------------------
# Provisioning jobs
# ---------------------------------------------------------------------------


@app.get("/api/v1/provisioning/jobs")
def list_jobs(
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
) -> dict:
    selected: set[str] = set()
    if domain:
        for d in domain:
            selected |= _parse_domain_query(d, None)
    selected |= _parse_domain_query(None, domains)
    rows = db.fetchall(
        """
        SELECT id, status, source_filename, source_format, payload, emis,
               school_name, notes, created_at, updated_at, approved_at, approved_by
        FROM provisioning_jobs
        ORDER BY created_at DESC
        """
    )
    jobs = []
    for r in rows:
        ser = _serialize(r)
        cand = _job_domains(r)
        ser["domains"] = sorted(cand)
        if not _domain_matches(selected, cand):
            continue
        # Omit heavy payload from list; detail endpoint has full preview
        payload = ser.get("payload")
        if isinstance(payload, dict):
            fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
            rows_n = len(payload.get("rows") or []) if isinstance(payload.get("rows"), list) else 0
            ser["payload_summary"] = {
                "fields": fields,
                "row_count": rows_n,
                "keys": sorted(payload.keys()),
            }
            ser.pop("payload", None)
        jobs.append(ser)
    return {"jobs": jobs}


@app.get("/api/v1/provisioning/jobs/{job_id}")
def get_job(job_id: UUID) -> dict:
    """Job detail with approve-preview: payload, schools affected, config diff."""
    row = db.fetchone(
        """
        SELECT id, status, source_filename, source_format, payload, emis,
               school_name, notes, created_at, updated_at, approved_at, approved_by
        FROM provisioning_jobs WHERE id = %s
        """,
        (str(job_id),),
    )
    if not row:
        raise HTTPException(404, "Job not found")
    ser = _serialize(row)
    ser["domains"] = sorted(_job_domains(row))
    ser["preview"] = _job_approve_preview(row)
    return ser


@app.post("/api/v1/provisioning/jobs", status_code=201)
async def create_job(
    file: UploadFile | None = File(None),
    body_json: str | None = Form(None),
) -> dict:
    """
    Create a job from uploaded CSV/JSON (multipart) or JSON form field.
    Also accepts application/json via the alternate JSON endpoint below.
    """
    if file is not None:
        raw_bytes = await file.read()
        filename = file.filename or "upload"
        return _create_job_from_bytes(raw_bytes, filename)

    if body_json:
        try:
            data = json.loads(body_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"Invalid JSON in body_json: {exc}") from exc
        return _create_job_from_parsed(data, "form.json", "json")

    raise HTTPException(
        400,
        "Provide multipart file= (CSV/JSON) or form field body_json=, "
        "or POST JSON to /api/v1/provisioning/jobs/from-json",
    )


@app.post("/api/v1/provisioning/jobs/from-json", status_code=201)
def create_job_from_json(body: JobCreateJSON) -> dict:
    data = body.model_dump(exclude_none=True)
    raw = data.pop("raw", None)
    payload = {"fields": data, "raw": raw if raw is not None else data}
    return _insert_job(
        payload=payload,
        emis=body.emis,
        school_name=body.school_name,
        source_filename=body.source_filename or "inline.json",
        source_format="json",
        notes=body.notes,
    )


@app.post("/api/v1/provisioning/jobs/{job_id}/approve")
def approve_job(job_id: UUID) -> dict:
    """
    School provision approve: write school-config.json for EMIS into docker-data
    school-configs (Attendance Doc / publish URL / Site link from payload). Does NOT
    create a Google Site or change SEMIS. Versions school_configs and inserts a
    publish_events row. RBAC not enforced yet (actor=system stub).
    """
    row = db.fetchone(
        "SELECT * FROM provisioning_jobs WHERE id = %s",
        (str(job_id),),
    )
    if not row:
        raise HTTPException(404, "Job not found")
    if row["status"] == "approved":
        events = db.fetchall(
            "SELECT * FROM publish_events WHERE job_id = %s ORDER BY created_at DESC",
            (str(job_id),),
        )
        return {
            "status": "already_approved",
            "job": _serialize(row),
            "publish_events": [_serialize(e) for e in events],
        }
    if row["status"] not in ("pending", "failed"):
        raise HTTPException(400, f"Cannot approve job in status={row['status']}")

    config = _config_from_job(row)
    emis = config["emis"]
    SCHOOL_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCHOOL_CONFIGS_DIR / f"{emis}.json"
    out_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    db.execute(
        """
        INSERT INTO schools (emis, name, email)
        VALUES (%s, %s, %s)
        ON CONFLICT (emis) DO UPDATE
          SET name = EXCLUDED.name,
              email = COALESCE(EXCLUDED.email, schools.email),
              updated_at = now()
        """,
        (emis, config.get("schoolName") or emis, config.get("schoolEmail")),
    )
    school = db.fetchone("SELECT id FROM schools WHERE emis = %s", (emis,))

    ver_row = db.fetchone(
        "SELECT COALESCE(MAX(version), 0) AS v FROM school_configs WHERE emis = %s",
        (emis,),
    )
    version = int(ver_row["v"]) + 1 if ver_row else 1

    sc = db.fetchone(
        """
        INSERT INTO school_configs (school_id, emis, version, config, source_job_id)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, version
        """,
        (
            school["id"] if school else None,
            emis,
            version,
            db.jsonify(config),
            str(job_id),
        ),
    )

    rel_path = f"school-configs/{emis}.json"
    event = db.fetchone(
        """
        INSERT INTO publish_events
          (job_id, school_config_id, emis, config_path, config_version, actor,
           meta, kind, school_email, doc_id, status, payload_preview)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            str(job_id),
            sc["id"],
            emis,
            rel_path,
            version,
            "system",
            db.jsonify({"host_path": str(out_path)}),
            "school_config_publish",
            config.get("schoolEmail"),
            config.get("attendanceDocId"),
            "ok",
            f"Published {rel_path} v{version}",
        ),
    )

    updated = db.fetchone(
        """
        UPDATE provisioning_jobs
        SET status = 'approved',
            updated_at = now(),
            approved_at = now(),
            approved_by = 'system',
            emis = COALESCE(emis, %s),
            school_name = COALESCE(school_name, %s)
        WHERE id = %s
        RETURNING *
        """,
        (emis, config.get("schoolName"), str(job_id)),
    )

    return {
        "status": "approved",
        "job": _serialize(updated),
        "config": config,
        "config_path": rel_path,
        "config_version": version,
        "publish_event": _serialize(event),
    }


# ---------------------------------------------------------------------------
# Publish events (config publishes + operational webhook audit)
# ---------------------------------------------------------------------------


@app.get("/api/v1/publish-events")
def list_publish_events(
    emis: list[str] | None = Query(None),
    kind: str | None = Query(None),
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
) -> dict:
    selected: set[str] = set()
    if domain:
        for d in domain:
            selected |= _parse_domain_query(d, None)
    selected |= _parse_domain_query(None, domains)
    emis_set: set[str] = set()
    if emis:
        for raw in emis:
            for part in str(raw).split(","):
                p = part.strip()
                if p and p.lower() != "all":
                    emis_set.add(p)
    clauses: list[str] = []
    params: list[Any] = []
    if emis_set:
        clauses.append("emis = ANY(%s)")
        params.append(sorted(emis_set))
    if kind:
        # allow prefix filter for sqao_* style: kind=sqao_ or exact
        if kind.endswith("*"):
            clauses.append("kind LIKE %s")
            params.append(kind[:-1] + "%")
        else:
            clauses.append("kind = %s")
            params.append(kind)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.fetchall(
        f"""
        SELECT id, job_id, school_config_id, emis, config_path, config_version,
               actor, created_at, meta, kind, school_email, doc_id, status,
               payload_preview
        FROM publish_events
        {where}
        ORDER BY created_at DESC
        LIMIT 200
        """,
        tuple(params) if params else None,
    )
    # site URL lookup by emis (latest config)
    site_by_emis: dict[str, str] = {}
    for sc in db.fetchall(
        """
        SELECT DISTINCT ON (emis) emis, config->>'siteAttendanceUrl' AS site_url
        FROM school_configs
        ORDER BY emis, version DESC
        """
    ):
        if sc.get("site_url"):
            site_by_emis[str(sc["emis"])] = sc["site_url"]

    events = []
    for r in rows:
        ser = _serialize(r)
        site = site_by_emis.get(str(ser.get("emis") or ""))
        if site:
            ser["site_attendance_url"] = site
        cand = _domains_for_school_row(ser.get("school_email"), site)
        # also from emis school email
        if not cand and ser.get("emis"):
            s = db.fetchone("SELECT email FROM schools WHERE emis = %s", (ser["emis"],))
            if s:
                cand |= _domains_for_school_row(s.get("email"), site)
        ser["domains"] = sorted(cand)
        if not _domain_matches(selected, cand):
            continue
        events.append(ser)
    last_seen = events[0].get("created_at") if events else None
    return {
        "events": events,
        "count": len(events),
        "last_seen": last_seen,
        "ingest": {
            "writes_here": [
                "POST /api/v1/attendance/{emis}",
                "POST /api/v1/radar/{emis}",
                "POST /api/v1/publish-events",
                "POST /api/v1/provisioning/jobs/{id}/approve",
            ],
            "google_site_republish": False,
            "note": (
                "Google Site Republish and an Apps Script redeploy do not insert a row. "
                "Apps Script mirrors here only when ATTENDANCE_INGEST_BASE or RADAR_INGEST_BASE "
                "is a URL Google can reach (127.0.0.1 on this HQ box is not)."
            ),
        },
    }


@app.post("/api/v1/publish-events", status_code=201, dependencies=[Depends(require_publish_token)])
def create_publish_event(body: PublishEventIn) -> dict:
    """
    Ingest an operational publish event (from tablet / Apps Script webhook later).
    If PUBLISH_AUDIT_TOKEN is set in env, require X-Publish-Audit-Token (or Bearer).
    """
    created = body.created_at
    if created is None:
        created = datetime.now(timezone.utc)
    meta = body.meta or {}
    row = db.fetchone(
        """
        INSERT INTO publish_events
          (emis, kind, school_email, doc_id, status, payload_preview,
           created_at, actor, meta, config_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
        RETURNING *
        """,
        (
            body.emis,
            body.kind,
            body.school_email,
            body.doc_id,
            body.status,
            body.payload_preview,
            created,
            "webhook",
            db.jsonify(meta),
        ),
    )
    return {"event": _serialize(row)}



# ---------------------------------------------------------------------------
# EMIS codes (from warehouse SoT — schools / school-configs / GAM OU paths)
# ---------------------------------------------------------------------------


_EMIS_PATH_RE = re.compile(
    r"(?:^|/|school-)(\d{4,}-\d+-\d+|\d{5,})(?=/|$)",
    re.IGNORECASE,
)


def _emis_from_ou_path(path: str | None) -> str | None:
    """Extract SEMIS/EMIS token from an OU path segment (no invented codes)."""
    if not path:
        return None
    # Prefer explicit school-NNNN leaf, then SEMIS-style NNNN-N-NNNNN, then long numeric leaf.
    m = re.search(r"school-(\d{5,})", str(path), re.I)
    if m:
        return m.group(1)
    matches = _EMIS_PATH_RE.findall(str(path))
    if matches:
        # last path segment match is usually the school folder
        return matches[-1]
    return None


def _collect_emis_codes() -> list[dict[str, Any]]:
    """Aggregate known EMIS codes from schools, school-configs, devices, OUs, GAM CSVs."""
    by_code: dict[str, dict[str, Any]] = {}

    def upsert(code: str, name: str | None = None, source: str = "") -> None:
        code = (code or "").strip()
        if not code:
            return
        row = by_code.setdefault(code, {"emis": code, "name": None, "sources": []})
        if name and not row["name"]:
            row["name"] = name
        if source and source not in row["sources"]:
            row["sources"].append(source)

    for row in db.fetchall("SELECT emis, name FROM schools WHERE emis IS NOT NULL"):
        upsert(str(row["emis"]), row.get("name"), "schools")

    for path in sorted(SCHOOL_CONFIGS_DIR.glob("*.json")):
        if path.name.endswith(".example.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        emis = str(data.get("emis") or path.stem).strip()
        upsert(emis, data.get("schoolName") or data.get("name"), "school-configs")

    for row in db.fetchall("SELECT DISTINCT emis FROM devices WHERE emis IS NOT NULL"):
        upsert(str(row["emis"]), None, "devices")

    for row in db.fetchall("SELECT emis, ou_path FROM ous WHERE emis IS NOT NULL OR ou_path IS NOT NULL"):
        if row.get("emis"):
            upsert(str(row["emis"]), None, "ous")
        extracted = _emis_from_ou_path(row.get("ou_path"))
        if extracted:
            upsert(extracted, None, "ous")

    # Latest GAM OU / enrolled / users CSVs — extract from orgUnitPath when present.
    for report_hint in ("ous", "enrolled", "users", "never_logged_in"):
        fpath = _latest_run_file(report_hint)  # 0.4.15: was 4x full _list_gam_runs() scans (~3 s)
        if not fpath or not fpath.is_file():
            continue
        try:
            text_csv = fpath.read_text(encoding="utf-8", errors="replace")
            reader = csv.DictReader(io.StringIO(text_csv))
            for i, crow in enumerate(reader):
                if i > 20000:
                    break
                for key in ("orgUnitPath", "orgUnit", "ou", "ou_path", "name"):
                    extracted = _emis_from_ou_path(crow.get(key) if crow else None)
                    if extracted:
                        upsert(extracted, None, f"gam:{report_hint}")
                        break
                # bare emis column if present
                if crow and crow.get("emis"):
                    upsert(str(crow["emis"]), crow.get("schoolName") or crow.get("name"), f"gam:{report_hint}")
        except (OSError, csv.Error):
            continue

    out = sorted(by_code.values(), key=lambda r: str(r["emis"]))
    return out


@app.get("/api/v1/emis")
def list_emis_codes(
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
) -> dict:
    """
    EMIS / SEMIS codes known to the warehouse (schools, school-configs, devices,
    OU paths, latest GAM extracts). Does not invent schools.
    """
    selected: set[str] = set()
    if domain:
        for d in domain:
            selected |= _parse_domain_query(d, None)
    selected |= _parse_domain_query(None, domains)
    codes = _collect_emis_codes()
    # Optional domain scope via school email / site URL when we have a schools row.
    if selected:
        scoped: list[dict[str, Any]] = []
        for c in codes:
            sch = db.fetchone("SELECT email FROM schools WHERE emis = %s", (c["emis"],))
            site = None
            sc = db.fetchone(
                """
                SELECT config->>'siteAttendanceUrl' AS site_url
                FROM school_configs WHERE emis = %s
                ORDER BY version DESC LIMIT 1
                """,
                (c["emis"],),
            )
            if sc:
                site = sc.get("site_url")
            cand = _domains_for_school_row(sch.get("email") if sch else None, site)
            # Codes discovered only from OU/GAM with no school email stay visible
            # when filter is set only if they have matching domain OR no domain cand.
            if not cand or _domain_matches(selected, cand):
                scoped.append(c)
        codes = scoped
    return {"emis": codes, "count": len(codes)}


def _csv_col_idx(headers: list[str], *names: str) -> int | None:
    lower = [h.lower().replace(" ", "").replace("_", "") for h in headers]
    for name in names:
        norm = name.lower().replace(" ", "").replace("_", "")
        if norm in lower:
            return lower.index(norm)
    return None


def _summarize_user_csv(fpath: Path, domain_selected: set[str]) -> dict[str, int]:
    """Honest counts from a GAM users-style CSV (after optional email-domain filter)."""
    out = {
        "rows": 0,
        "never_logged_in": 0,
        "ever_logged_in": 0,
        "suspended": 0,
        "with_recovery_email": 0,
    }
    if not fpath.is_file():
        return out
    try:
        text_csv = fpath.read_text(encoding="utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text_csv))
        rows = list(reader)
    except (OSError, csv.Error):
        return out
    if not rows:
        return out
    headers = [str(c) for c in rows[0]]
    email_i = _csv_col_idx(headers, "primaryEmail", "email", "user", "account")
    login_i = _csv_col_idx(headers, "lastLoginTime", "lastLogin")
    susp_i = _csv_col_idx(headers, "suspended")
    rec_i = _csv_col_idx(headers, "recoveryEmail", "recoveryemail")
    for r in rows[1:]:
        if email_i is not None and domain_selected and email_i < len(r):
            d = _email_domain(r[email_i])
            if not d or d not in domain_selected:
                continue
        out["rows"] += 1
        if login_i is not None and login_i < len(r):
            val = (r[login_i] or "").strip()
            if val == "" or val.lower() == "never":
                out["never_logged_in"] += 1
            else:
                out["ever_logged_in"] += 1
        if susp_i is not None and susp_i < len(r):
            if str(r[susp_i]).strip().lower() in ("true", "1", "yes"):
                out["suspended"] += 1
        if rec_i is not None and rec_i < len(r) and (r[rec_i] or "").strip():
            out["with_recovery_email"] += 1
    return out


def _latest_run_file(report: str) -> Path | None:
    """Newest completed CSV for report (skips running / empty / errored-without-file).
    0.4.15: meta-only scan (no CSV reads) and timestamped runs only (never smoke_*.csv)."""
    for run in _run_names_newest_first():
        if run.get("report") != report:
            continue
        if not _RUN_STEM_RE.match(run["id"]):
            continue
        status = str(run.get("status") or "").lower()
        if status in ("running", "timeout"):
            continue
        p = GAM_RUNS_DIR / run["filename"]
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


@app.get("/api/v1/gam/summary")
def gam_section_summary(
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
) -> dict:
    """
    Summary chips for GAM/report sections from latest CSV runs (honest to file).
    Prefer enrolled as denominator; fall back to users. never_logged_in from
    dedicated report when present, else derived from enrolled/users lastLoginTime.
    """
    selected: set[str] = set()
    if domain:
        for d in domain:
            selected |= _parse_domain_query(d, None)
    selected |= _parse_domain_query(None, domains)

    enrolled_path = _latest_run_file("enrolled") or _latest_run_file("users")
    never_path = _latest_run_file("never_logged_in")
    susp_path = _latest_run_file("suspended")
    wrong_path = _latest_run_file("wrong_password")

    enrolled_stats = _summarize_user_csv(enrolled_path, selected) if enrolled_path else {
        "rows": 0, "never_logged_in": 0, "ever_logged_in": 0, "suspended": 0, "with_recovery_email": 0
    }
    never_stats = _summarize_user_csv(never_path, selected) if never_path else None
    susp_stats = _summarize_user_csv(susp_path, selected) if susp_path else None
    wrong_stats = _summarize_user_csv(wrong_path, selected) if wrong_path else None

    enrolled = enrolled_stats["rows"]
    never = never_stats["rows"] if never_stats is not None else enrolled_stats["never_logged_in"]
    ever = enrolled_stats["ever_logged_in"] if never_stats is None else max(0, enrolled - never)
    if never_stats is not None and enrolled and ever == 0 and never <= enrolled:
        ever = max(0, enrolled - never)
    suspended = susp_stats["rows"] if susp_stats is not None else enrolled_stats["suspended"]
    # Same source as Wrong password button: users with changePasswordAtNextLogin=true.
    wrong_password = wrong_stats["rows"] if wrong_stats is not None else None
    pct_never = round(100.0 * never / enrolled, 1) if enrolled else None

    return {
        "enrolled": enrolled,
        "never_logged_in": never,
        "ever_logged_in": ever,
        "suspended": suspended,
        "wrong_password": wrong_password,
        "pct_never_of_enrolled": pct_never,
        "with_recovery_email": enrolled_stats.get("with_recovery_email", 0),
        "sources": {
            "enrolled": enrolled_path.name if enrolled_path else None,
            "never_logged_in": never_path.name if never_path else None,
            "suspended": susp_path.name if susp_path else None,
            "wrong_password": wrong_path.name if wrong_path else None,
        },
        "domains": sorted(selected) if selected else ["all"],
        "note": (
            "Counts from latest GAM CSV runs after domain filter; re-run reports to refresh. "
            "Wrong password = Directory users with changePasswordAtNextLogin=true (same as that report button)."
        ),
    }



# ---------------------------------------------------------------------------
# GAM reports (Phase 3.1 / 4a-lite) — allowlisted host runner only
# ---------------------------------------------------------------------------


def _resolve_gam_runner() -> Path | None:
    for cand in _GAM_RUNNER_CANDIDATES:
        if cand and str(cand) and cand.is_file():
            return cand
    return None


def _gam_available() -> tuple[bool, str]:
    """Return (ok, detail). Checks runner script + gam binary on PATH / GAM_BIN."""
    runner = _resolve_gam_runner()
    if runner is None:
        return False, "runner_missing: scripts/run_gam_report.sh not found in image or mounts"
    gam_bin = os.getenv("GAM_BIN", "").strip()
    if gam_bin and Path(gam_bin).is_file() and os.access(gam_bin, os.X_OK):
        return True, gam_bin
    which = shutil.which("gam")
    if which:
        return True, which
    for candidate in (
        "/opt/gam7/gam",
        "/home/george/bin/gam7/gam",
        str(Path.home() / "bin" / "gam7" / "gam"),
    ):
        p = Path(candidate)
        if p.is_file() and os.access(p, os.X_OK):
            return True, str(p)
    return False, (
        "gam_not_in_path: install/authenticate GAM on the host and mount the binary "
        "(GAM_BIN or /home/george/bin/gam7/gam). Dashboard does not require dropping CSVs."
    )


def _run_id_from_name(name: str) -> str:
    # strip extension
    if name.endswith(".csv") or name.endswith(".txt"):
        return name.rsplit(".", 1)[0]
    return name


def _find_run_file(run_id: str) -> Path | None:
    """Resolve run id to a file under GAM_RUNS_DIR (id may be stem or full filename)."""
    GAM_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    # Exact filename
    for ext in ("", ".csv", ".txt", ".json"):
        p = GAM_RUNS_DIR / f"{run_id}{ext}"
        if p.is_file() and not p.name.endswith(".stderr") and not p.name.endswith(".meta.json"):
            return p
    # Prefix match
    matches = sorted(
        p
        for p in GAM_RUNS_DIR.iterdir()
        if p.is_file()
        and not p.name.endswith(".stderr")
        and not p.name.endswith(".meta.json")
        and (p.stem == run_id or p.name.startswith(run_id))
    )
    return matches[0] if matches else None



def _sanitize_gam_domains(raw: list[str] | None) -> list[str]:
    """Allow only host.tld-looking domain tokens for GAM runner argv."""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        for part in str(item or "").split(","):
            d = part.strip().lower()
            if not d or d == "all":
                continue
            if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", d):
                raise HTTPException(
                    400,
                    {"error": "invalid_domain", "detail": f"Rejected domain token: {d}"},
                )
            if d not in seen:
                seen.add(d)
                out.append(d)
    return out

def _domains_from_gam_file(path: Path, max_bytes: int = 64_000) -> set[str]:
    """Best-effort domain discovery from GAM CSV/text (emails + known domain strings)."""
    found: set[str] = set()
    try:
        raw = path.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except OSError:
        return found
    for m in re.finditer(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", raw):
        found.add(m.group(1).lower())
    # Known catalog: match as a full token only (avoid p4sgi.com inside sl.p4sgi.com).
    raw_l = raw.lower()
    for d in KNOWN_DOMAINS:
        dl = d.lower()
        if re.search(rf"(?<![a-z0-9.-]){re.escape(dl)}(?![a-z0-9.-])", raw_l):
            found.add(dl)
    found |= _domains_from_url(raw)
    return found

# 0.4.15: run files are "<YYYYMMDDTHHMMSSZ>_<report>.csv". Plain reverse filename sorting put
# ad-hoc files such as smoke_ous.csv ("s" > "2") ahead of every real run, so "latest ous" opened
# smoke_ous. Sort by the parsed timestamp instead; untimestamped files sort last and never
# count as a cached report (their report name is the whole stem, e.g. "smoke_ous").
_RUN_STEM_RE = re.compile(r"^(\d{8}T\d{6}Z)_(.+)$")


def _run_stem_parts(stem: str) -> tuple[str | None, str]:
    m = _RUN_STEM_RE.match(stem)
    return (m.group(1), m.group(2)) if m else (None, stem)


def _run_sort_key(p: Path) -> tuple:
    ts, _rep = _run_stem_parts(p.stem)
    if ts:
        return (1, ts, p.name)
    try:
        mt = p.stat().st_mtime
    except OSError:
        mt = 0.0
    return (0, f"{mt:020.6f}", p.name)


def _sorted_run_files() -> list[Path]:
    if not GAM_RUNS_DIR.is_dir():
        return []
    return sorted((p for p in GAM_RUNS_DIR.iterdir() if p.is_file()), key=_run_sort_key, reverse=True)


_DOMAIN_FILE_CACHE: dict[tuple[str, int, int], frozenset[str]] = {}


def _domains_from_gam_file_cached(p: Path) -> set[str]:
    """_domains_from_gam_file memoised on (path, mtime, size): the runs list re-read 64 KB of every CSV per call."""
    try:
        st = p.stat()
    except OSError:
        return set()
    key = (str(p), st.st_mtime_ns, st.st_size)
    hit = _DOMAIN_FILE_CACHE.get(key)
    if hit is None:
        hit = frozenset(_domains_from_gam_file(p))
        _DOMAIN_FILE_CACHE[key] = hit
    return set(hit)


def _list_gam_runs() -> list[dict[str, Any]]:
    GAM_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for p in _sorted_run_files():
        if not p.is_file():
            continue
        if p.name.endswith(".stderr") or p.name.endswith(".meta.json"):
            continue
        if p.suffix not in (".csv", ".txt"):
            continue
        stem = p.stem  # timestamp_report (ad-hoc files: report = whole stem)
        created, report = _run_stem_parts(stem)
        meta_path = GAM_RUNS_DIR / f"{stem}.meta.json"
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        meta_domains = meta.get("domains") if isinstance(meta.get("domains"), list) else None
        domains = sorted({str(d).lower() for d in meta_domains}) if meta_domains else sorted(_domains_from_gam_file_cached(p))
        runs.append(
            {
                "id": stem,
                "report": meta.get("report") or report,
                "filename": p.name,
                "path": f"gam-out/runs/{p.name}",
                "size_bytes": p.stat().st_size,
                "created_at": meta.get("created_at") or created,
                "status": meta.get("status") or "ok",
                "exit_code": meta.get("exit_code"),
                "domains": domains,
            }
        )
    return runs



@app.get("/api/v1/gam/reports")
def list_gam_reports(
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
) -> dict:
    """List past on-demand GAM report runs under gam-out/runs/."""
    selected: set[str] = set()
    if domain:
        for d in domain:
            selected |= _parse_domain_query(d, None)
    selected |= _parse_domain_query(None, domains)
    ok, detail = _gam_available()
    runs = _list_gam_runs()
    if selected:
        runs = [
            r for r in runs
            if set(r.get("domains") or []) and _domain_matches(selected, set(r.get("domains") or []))
        ]
    return {
        "reports_allowlist": list(GAM_ALLOWLIST),
        "gam_available": ok,
        "gam_detail": detail,
        "runs": runs,
    }


def _write_gam_meta(meta_path: Path, meta: dict[str, Any]) -> None:
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def _gam_job_env() -> dict[str, str]:
    env = os.environ.copy()
    if os.getenv("GAM_BIN"):
        env["GAM_BIN"] = os.getenv("GAM_BIN", "")
    extra_paths = [
        "/home/george/bin/gam7",
        "/opt/gam7",
        str(Path(os.getenv("GAM_BIN", "/usr/bin/gam")).parent),
    ]
    env["PATH"] = ":".join(extra_paths + [env.get("PATH", "")])
    return env


def _run_gam_job(
    *,
    report: str,
    run_id: str,
    out_path: Path,
    meta_path: Path,
    runner: Path,
    gam_detail: str,
    domain_list: list[str],
) -> None:
    """Background worker: run allowlisted GAM report without blocking the API event loop."""
    domains_csv = ",".join(domain_list)
    cmd = ["bash", str(runner), report, str(out_path)]
    if domains_csv:
        cmd.append(domains_csv)
    env = _gam_job_env()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=GAM_TIMEOUT_SEC,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        meta = {
            "id": run_id,
            "report": report,
            "status": "timeout",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": None,
            "gam": gam_detail,
            "filename": out_path.name,
            "size_bytes": out_path.stat().st_size if out_path.is_file() else 0,
            "error": f"timed out after {GAM_TIMEOUT_SEC}s",
            "domains": domain_list,
        }
        _write_gam_meta(meta_path, meta)
        return
    except Exception as exc:  # noqa: BLE001 — surface any worker failure in meta
        meta = {
            "id": run_id,
            "report": report,
            "status": "error",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": None,
            "gam": gam_detail,
            "filename": out_path.name,
            "size_bytes": out_path.stat().st_size if out_path.is_file() else 0,
            "error": f"worker_exception: {exc}",
            "domains": domain_list,
        }
        _write_gam_meta(meta_path, meta)
        return

    stderr_tail = (proc.stderr or "")[-2000:]
    status = "ok" if proc.returncode == 0 else "error"
    if proc.returncode == 127 or "gam_not_in_path" in (proc.stderr or "") + (proc.stdout or ""):
        status = "error"
        err = "gam_not_in_path"
    else:
        err = "" if proc.returncode == 0 else f"gam exited {proc.returncode}"
    size = out_path.stat().st_size if out_path.is_file() else 0
    meta = {
        "id": run_id,
        "report": report,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": proc.returncode,
        "gam": gam_detail,
        "filename": out_path.name,
        "size_bytes": size,
        "stderr": stderr_tail if proc.returncode != 0 else "",
        "error": err,
        "domains": domain_list,
    }
    _write_gam_meta(meta_path, meta)


@app.post("/api/v1/gam/reports", status_code=202)
def create_gam_report(body: GamReportIn, request: Request) -> dict:
    """
    Start an allowlisted GAM report via scripts/run_gam_report.sh (host-mounted gam).
    Returns immediately with status=running; poll GET /api/v1/gam/reports/{id} until
    status is ok|error|timeout. Never executes client-supplied shell.
    """
    report = (body.report or "").strip().lower()
    if report not in GAM_ALLOWLIST:
        raise HTTPException(
            400,
            {
                "error": "invalid_report",
                "detail": f"report must be one of {list(GAM_ALLOWLIST)}",
                "allowlist": list(GAM_ALLOWLIST),
            },
        )

    ok, gam_detail = _gam_available()
    if not ok:
        raise HTTPException(
            503,
            {
                "error": "gam_not_available",
                "detail": gam_detail,
                "hint": (
                    "GAM is authenticated on the gbu host terminal. Ensure the web "
                    "container mounts the gam binary + ~/.gam, or run the runner on the host."
                ),
            },
        )

    runner = _resolve_gam_runner()
    if runner is None:
        raise HTTPException(
            503,
            {"error": "runner_missing", "detail": "run_gam_report.sh not found"},
        )

    GAM_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{ts}_{report}"
    out_path = GAM_RUNS_DIR / f"{run_id}.csv"
    meta_path = GAM_RUNS_DIR / f"{run_id}.meta.json"

    domain_list = _sanitize_gam_domains(body.domains)
    p4 = _request_scope(request)
    if not p4["superadmin"]:
        own = list(p4["allowed_domains"] or [])
        if not own:
            raise HTTPException(403, {"error": "no_domain", "detail": "caller has no email domain"})
        extra = sorted(set(domain_list) - set(own))
        if extra:
            raise HTTPException(403, {"error": "domain_not_allowed", "detail": f"not allowed: {extra}; you may only run {own}"})
        domain_list = own
    meta = {
        "id": run_id,
        "report": report,
        "status": "running",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": None,
        "gam": gam_detail,
        "filename": out_path.name,
        "size_bytes": 0,
        "domains": domain_list,
    }
    _write_gam_meta(meta_path, meta)

    thread = threading.Thread(
        target=_run_gam_job,
        kwargs={
            "report": report,
            "run_id": run_id,
            "out_path": out_path,
            "meta_path": meta_path,
            "runner": runner,
            "gam_detail": gam_detail,
            "domain_list": domain_list,
        },
        name=f"gam-{run_id}",
        daemon=True,
    )
    thread.start()

    return {
        "id": run_id,
        "report": report,
        "status": "running",
        "filename": out_path.name,
        "path": f"gam-out/runs/{out_path.name}",
        "size_bytes": 0,
        "created_at": meta["created_at"],
        "domains": domain_list,
        "download_url": f"/api/v1/gam/reports/{run_id}/download",
        "view_url": f"/api/v1/gam/reports/{run_id}",
        "poll_url": f"/api/v1/gam/reports/{run_id}",
        "timeout_sec": GAM_TIMEOUT_SEC,
    }


_EMAIL_COL_HINTS = (
    "primaryemail", "email", "user", "actor.email", "users.0.useremail", "owner",
    "adminemail", "account", "annotateduser", "assignedto",
)


def _filter_table_for_scope(
    headers: list[str], rows: list[list[str]], allowed: set[str] | None
) -> tuple[list[list[str]], str | None]:
    """Keep only rows whose email column is in allowed domains. None = unrestricted."""
    if allowed is None:
        return rows, None
    lower = [h.strip().lower() for h in headers]
    col = next((lower.index(h) for h in _EMAIL_COL_HINTS if h in lower), None)
    if col is None:
        for r in rows[:50]:
            for i, cell in enumerate(r):
                if "@" in cell:
                    col = i
                    break
            if col is not None:
                break
    if col is None:
        return [], "restricted: this report has no user email column (customer-wide); super-admin only"
    kept = [r for r in rows if col < len(r) and (_email_domain(r[col].split()[0] if r[col].split() else "") or "") in allowed]
    return kept, f"scoped to {', '.join(sorted(allowed))}"


@app.get("/api/v1/gam/reports/latest/{report}")
def get_latest_gam_report(report: str, request: Request) -> Any:
    """Return the newest cached CSV for an allowlisted report (no GAM call)."""
    report = (report or "").strip().lower()
    if report not in GAM_ALLOWLIST:
        raise HTTPException(
            400,
            {
                "error": "invalid_report",
                "detail": f"report must be one of {list(GAM_ALLOWLIST)}",
                "allowlist": list(GAM_ALLOWLIST),
            },
        )
    allowed = _scope_domains_for(request)
    fpath = _latest_run_file(report) if allowed is None else _latest_run_for_scope(report, allowed)[0]
    if not fpath:
        raise HTTPException(
            404,
            {
                "error": "no_cached_run",
                "detail": f"No cached CSV for report={report}. Use Refresh to run GAM.",
                "report": report,
            },
        )
    # Pass download=False explicitly: default Query(False) is truthy when called in-process.
    return get_gam_report(fpath.stem, request=request, download=False)


@app.get("/api/v1/gam/reports/{run_id}")
def get_gam_report(run_id: str, request: Request, download: bool = Query(False)) -> Any:
    """View report metadata + preview (or download when ?download=1)."""
    from fastapi.responses import PlainTextResponse, Response

    # Prefer meta even when CSV not written yet (async running / timeout / error).
    stem_guess = run_id[:-4] if run_id.endswith((".csv", ".txt")) else run_id
    meta_path = GAM_RUNS_DIR / f"{stem_guess}.meta.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    fpath = _find_run_file(run_id)
    if not fpath:
        status = (meta.get("status") or "").lower()
        if status in ("running", "timeout", "error") or meta:
            return {
                "id": stem_guess,
                "report": meta.get("report") or (stem_guess.split("_", 1)[1] if "_" in stem_guess else "unknown"),
                "filename": meta.get("filename") or f"{stem_guess}.csv",
                "path": f"gam-out/runs/{meta.get('filename') or (stem_guess + '.csv')}",
                "size_bytes": meta.get("size_bytes") or 0,
                "created_at": meta.get("created_at"),
                "status": meta.get("status") or "running",
                "format": "text",
                "headers": [],
                "rows": [],
                "preview": "",
                "preview_lines": 0,
                "error": meta.get("error") or meta.get("stderr") or "",
                "domains": meta.get("domains") or [],
                "download_url": f"/api/v1/gam/reports/{stem_guess}/download",
            }
        raise HTTPException(404, {"error": "not_found", "detail": f"No run id={run_id}"})
    stem = fpath.stem
    meta_path = GAM_RUNS_DIR / f"{stem}.meta.json"
    if meta_path.is_file() and not meta:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    allowed = _scope_domains_for(request)
    if download:
        return download_gam_report(fpath.stem, request)
    text = fpath.read_text(encoding="utf-8", errors="replace")
    preview_lines = text.splitlines()[:5001]
    headers: list[str] = []
    table_rows: list[list[str]] = []
    fmt = "text"
    if fpath.suffix.lower() == ".csv" or (preview_lines and "," in preview_lines[0]):
        try:
            reader = csv.reader(io.StringIO("\n".join(preview_lines)))
            all_rows = list(reader)
            if all_rows:
                headers = [str(c) for c in all_rows[0]]
                table_rows = [[str(c) for c in r] for r in all_rows[1:5000]]
                fmt = "csv"
        except csv.Error:
            fmt = "text"
    scope_note = None
    if allowed is not None:
        table_rows, scope_note = _filter_table_for_scope(headers, table_rows, allowed)
        if fmt != "csv":
            preview_lines = []
        else:
            _b = io.StringIO()
            csv.writer(_b).writerows([headers] + table_rows[:50])
            preview_lines = _b.getvalue().splitlines()
    return {
        "scope_note": scope_note,
        "id": stem,
        "report": meta.get("report") or (stem.split("_", 1)[1] if "_" in stem else "unknown"),
        "filename": fpath.name,
        "path": f"gam-out/runs/{fpath.name}",
        "size_bytes": fpath.stat().st_size,
        "created_at": meta.get("created_at"),
        "status": meta.get("status") or "ok",
        "format": fmt,
        "headers": headers,
        "rows": table_rows,
        "preview": "\n".join(preview_lines[:50]),
        "preview_lines": len(preview_lines),
        "error": meta.get("error") or "",
        "domains": (
            sorted({str(d).lower() for d in meta["domains"]})
            if isinstance(meta.get("domains"), list) and meta.get("domains")
            else sorted(_domains_from_gam_file(fpath))
        ),
        "download_url": f"/api/v1/gam/reports/{stem}/download",
    }


@app.get("/api/v1/gam/reports/{run_id}/download")
def download_gam_report(run_id: str, request: Request) -> Any:
    fpath = _find_run_file(run_id)
    if not fpath:
        raise HTTPException(404, {"error": "not_found", "detail": f"No run id={run_id}"})
    allowed = _scope_domains_for(request)
    if allowed is not None:
        from fastapi.responses import Response

        rows_all = [r for r in csv.reader(io.StringIO(fpath.read_text(encoding="utf-8", errors="replace"))) if r and not r[0].startswith("#")]
        hdr, body = (rows_all[0], rows_all[1:]) if rows_all else ([], [])
        kept, _note = _filter_table_for_scope(hdr, body, allowed)
        buf = io.StringIO()
        csv.writer(buf).writerows([hdr] + kept if hdr else [])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fpath.stem}.scoped.csv"'})
    return FileResponse(
        path=str(fpath),
        filename=fpath.name,
        media_type="text/csv" if fpath.suffix == ".csv" else "text/plain",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_job_from_bytes(raw_bytes: bytes, filename: str) -> dict:
    text = raw_bytes.decode("utf-8-sig")
    lower = filename.lower()
    if lower.endswith(".json") or text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"Invalid JSON: {exc}") from exc
        return _create_job_from_parsed(data, filename, "json")
    if lower.endswith(".csv") or "," in text.split("\n", 1)[0]:
        return _create_job_from_csv(text, filename)
    raise HTTPException(400, "Unsupported file type; use .csv or .json")


def _create_job_from_parsed(data: Any, filename: str, fmt: str) -> dict:
    rows: list[dict]
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        for key in ("schools", "users", "rows", "ous", "items"):
            if isinstance(data.get(key), list):
                rows = [r for r in data[key] if isinstance(r, dict)]
                break
        else:
            rows = [data]
    else:
        raise HTTPException(400, "JSON must be object or array of objects")

    if not rows:
        raise HTTPException(400, "No rows found in JSON")

    primary = rows[0]
    emis, school_name, email, ou_path = _extract_identity(primary)
    payload = {
        "rows": rows,
        "fields": {
            "emis": emis,
            "school_name": school_name,
            "school_email": email,
            "ou_path": ou_path,
        },
    }
    _maybe_insert_ous(rows)
    return _insert_job(
        payload=payload,
        emis=emis,
        school_name=school_name,
        source_filename=filename,
        source_format=fmt,
    )


def _create_job_from_csv(text: str, filename: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    rows = [dict(r) for r in reader]
    if not rows:
        raise HTTPException(400, "CSV has no data rows")
    primary = rows[0]
    emis, school_name, email, ou_path = _extract_identity(primary)
    payload = {
        "rows": rows,
        "fields": {
            "emis": emis,
            "school_name": school_name,
            "school_email": email,
            "ou_path": ou_path,
        },
    }
    _maybe_insert_ous(rows)
    return _insert_job(
        payload=payload,
        emis=emis,
        school_name=school_name,
        source_filename=filename,
        source_format="csv",
    )


def _extract_identity(row: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    def pick(*keys: str) -> str | None:
        lower_map = {str(k).lower().replace(" ", "").replace("_", ""): v for k, v in row.items()}
        for key in keys:
            norm = key.lower().replace(" ", "").replace("_", "")
            for rk, rv in lower_map.items():
                if rk == norm and rv not in (None, ""):
                    return str(rv).strip()
            if key in row and row[key] not in (None, ""):
                return str(row[key]).strip()
        return None

    email = pick("primaryEmail", "email", "schoolEmail", "school_email", "user")
    ou_path = pick("orgUnitPath", "ou", "ou_path", "ouPath", "orgUnit")
    school_name = pick("schoolName", "school_name", "name.fullName", "fullName", "name", "givenName")
    emis = pick("emis", "EMIS", "code", "schoolCode")
    if not emis and ou_path:
        m = re.search(r"school-(\d+)", ou_path, re.I)
        if m:
            emis = m.group(1)
    if not emis and email:
        m = re.search(r"(?:ht\.|school)[.-]?(\d+)@", email, re.I)
        if m:
            emis = m.group(1)
    return emis, school_name, email, ou_path


def _maybe_insert_ous(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        emis, school_name, email, ou_path = _extract_identity(row)
        if not ou_path:
            continue
        school_id = None
        if emis:
            s = db.fetchone("SELECT id FROM schools WHERE emis = %s", (emis,))
            school_id = s["id"] if s else None
        db.execute(
            """
            INSERT INTO ous (school_id, emis, ou_path, ou_name, primary_email, raw)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                school_id,
                emis,
                ou_path,
                school_name,
                email,
                db.jsonify(row),
            ),
        )


def _insert_job(
    *,
    payload: dict,
    emis: str | None,
    school_name: str | None,
    source_filename: str,
    source_format: str,
    notes: str | None = None,
) -> dict:
    row = db.fetchone(
        """
        INSERT INTO provisioning_jobs
          (status, source_filename, source_format, payload, emis, school_name, notes)
        VALUES ('pending', %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            source_filename,
            source_format,
            db.jsonify(payload),
            emis,
            school_name,
            notes,
        ),
    )
    return {"job": _serialize(row)}


def _job_domains(job: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    payload = job.get("payload") or {}
    fields = payload.get("fields") if isinstance(payload, dict) else {}
    if not isinstance(fields, dict):
        fields = {}
    for email_key in ("school_email", "schoolEmail", "primaryEmail", "email"):
        d = _email_domain(fields.get(email_key))
        if d:
            found.add(d)
    found |= _domains_from_url(fields.get("site_attendance_url") or fields.get("siteAttendanceUrl"))
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        for row in rows[:50]:
            if not isinstance(row, dict):
                continue
            _, _, email, _ = _extract_identity(row)
            d = _email_domain(email)
            if d:
                found.add(d)
            found |= _domains_from_url(
                row.get("siteAttendanceUrl") or row.get("site_attendance_url")
            )
    emis = job.get("emis") or fields.get("emis")
    if emis and not found:
        s = db.fetchone("SELECT email FROM schools WHERE emis = %s", (str(emis),))
        if s:
            d = _email_domain(s.get("email"))
            if d:
                found.add(d)
    return found


def _load_current_config(emis: str) -> dict[str, Any] | None:
    row = db.fetchone(
        """
        SELECT config FROM school_configs
        WHERE emis = %s
        ORDER BY version DESC
        LIMIT 1
        """,
        (emis,),
    )
    if row and row.get("config"):
        cfg = row["config"]
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except json.JSONDecodeError:
                cfg = None
        if isinstance(cfg, dict):
            return dict(cfg)
    path = SCHOOL_CONFIGS_DIR / f"{emis}.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _config_diff(current: dict[str, Any] | None, proposed: dict[str, Any]) -> list[dict[str, Any]]:
    cur = current or {}
    keys = sorted(set(cur.keys()) | set(proposed.keys()))
    diffs: list[dict[str, Any]] = []
    for k in keys:
        old = cur.get(k)
        new = proposed.get(k)
        if old != new:
            diffs.append({"key": k, "from": old if old is not None else None, "to": new if new is not None else None})
    return diffs


def _config_from_job_lenient(job: dict[str, Any]) -> dict[str, Any]:
    """Preview-only: allow missing attendanceDocId."""
    payload = job.get("payload") or {}
    fields = dict(payload.get("fields") or {}) if isinstance(payload, dict) else {}
    rows = payload.get("rows") if isinstance(payload, dict) else None
    primary = rows[0] if isinstance(rows, list) and rows else {}
    if not isinstance(primary, dict):
        primary = {}
    emis = str(job.get("emis") or fields.get("emis") or "")
    has_doc = bool(
        fields.get("attendance_doc_id")
        or primary.get("attendanceDocId")
        or primary.get("attendance_doc_id")
        or emis == TEST_PRIMARY_CONFIG["emis"]
    )
    if has_doc:
        return _config_from_job(job)
    fake = dict(job)
    fake_payload = dict(payload) if isinstance(payload, dict) else {"fields": {}, "rows": []}
    fake_fields = dict(fake_payload.get("fields") or {})
    fake_fields["attendance_doc_id"] = fake_fields.get("attendance_doc_id") or "(missing)"
    fake_payload["fields"] = fake_fields
    fake["payload"] = fake_payload
    cfg = _config_from_job(fake)
    if cfg.get("attendanceDocId") == "(missing)":
        cfg["attendanceDocId"] = ""
    return cfg


def _job_approve_preview(job: dict[str, Any]) -> dict[str, Any]:
    """What Approve will do — payload, schools affected, proposed config, diff."""
    proposed: dict[str, Any] | None = None
    propose_error: str | None = None
    try:
        proposed = _config_from_job(job)
    except HTTPException as exc:
        propose_error = str(exc.detail)
        try:
            proposed = _config_from_job_lenient(job)
        except Exception as exc2:  # noqa: BLE001
            proposed = None
            propose_error = f"{propose_error}; partial={exc2}"

    emis = None
    if proposed:
        emis = str(proposed.get("emis") or "") or None
    if not emis:
        emis = str(job.get("emis") or "") or None

    current = _load_current_config(emis) if emis else None
    schools_affected: list[dict[str, Any]] = []
    if emis:
        s = db.fetchone(
            "SELECT id, emis, name, email FROM schools WHERE emis = %s",
            (emis,),
        )
        if s:
            schools_affected.append(_serialize(s))
        else:
            schools_affected.append(
                {
                    "emis": emis,
                    "name": (proposed or {}).get("schoolName") or job.get("school_name"),
                    "email": (proposed or {}).get("schoolEmail"),
                    "new": True,
                }
            )

    payload = job.get("payload") or {}
    fields = payload.get("fields") if isinstance(payload, dict) else {}
    rows = payload.get("rows") if isinstance(payload, dict) else None
    row_count = len(rows) if isinstance(rows, list) else 0

    return {
        "can_approve": bool(
            job.get("status") in ("pending", "failed") and proposed is not None and not propose_error
        ),
        "propose_error": propose_error,
        "emis": emis,
        "schools_affected": schools_affected,
        "payload_fields": fields if isinstance(fields, dict) else {},
        "payload_row_count": row_count,
        "payload_sample_rows": (rows[:5] if isinstance(rows, list) else []),
        "proposed_config": proposed,
        "current_config": current,
        "config_diff": _config_diff(current, proposed) if proposed else [],
        "will_write": f"school-configs/{emis}.json" if emis else None,
        "action": (
            "School provision: write school-config.json for EMIS into docker-data "
            "school-configs (Attendance Doc, publish URL, Site link from the job payload). "
            "Does NOT create a Google Site or change SEMIS. Also versions school_configs "
            "and inserts a school_config_publish audit event."
        ),
    }


def _config_from_job(job: dict[str, Any]) -> dict[str, Any]:
    """Build school-config.json dict from job payload + Test Primary defaults when EMIS matches."""
    payload = job.get("payload") or {}
    fields = payload.get("fields") if isinstance(payload, dict) else {}
    if not isinstance(fields, dict):
        fields = {}
    rows = payload.get("rows") if isinstance(payload, dict) else None
    primary = rows[0] if isinstance(rows, list) and rows else {}
    if not isinstance(primary, dict):
        primary = {}

    emis = (
        job.get("emis")
        or fields.get("emis")
        or _extract_identity(primary)[0]
        or TEST_PRIMARY_CONFIG["emis"]
    )
    emis = str(emis)

    if emis == TEST_PRIMARY_CONFIG["emis"]:
        config = dict(TEST_PRIMARY_CONFIG)
    else:
        config = {
            "emis": emis,
            "schoolName": job.get("school_name") or fields.get("school_name") or emis,
            "schoolEmail": fields.get("school_email") or "",
            "attendanceDocId": "",
            "websitePackDocId": "",
            "googleSiteId": "",
            "publishExecUrl": "",
            "googleRadarExecUrl": "",
            "siteAttendanceUrl": "",
            "radarPageUrl": "",
            "attendancePageUrl": "",
            "driveFolderId": "",
            "configEndpoint": "",
        }

    def override(cfg_key: str, *sources: Any) -> None:
        for src in sources:
            if src not in (None, ""):
                config[cfg_key] = str(src)
                return

    override(
        "schoolName",
        fields.get("school_name"),
        job.get("school_name"),
        primary.get("schoolName"),
        primary.get("name"),
        primary.get("name.fullName"),
    )
    override(
        "schoolEmail",
        fields.get("school_email"),
        primary.get("schoolEmail"),
        primary.get("primaryEmail"),
        primary.get("email"),
    )
    override(
        "attendanceDocId",
        fields.get("attendance_doc_id"),
        primary.get("attendanceDocId"),
        primary.get("attendance_doc_id"),
    )
    override(
        "websitePackDocId",
        fields.get("website_pack_doc_id"),
        primary.get("websitePackDocId"),
    )
    override("googleSiteId", fields.get("google_site_id"), primary.get("googleSiteId"))
    override("publishExecUrl", fields.get("publish_exec_url"), primary.get("publishExecUrl"))
    override(
        "googleRadarExecUrl",
        fields.get("google_radar_exec_url"),
        primary.get("googleRadarExecUrl"),
    )
    override(
        "siteAttendanceUrl",
        fields.get("site_attendance_url"),
        primary.get("siteAttendanceUrl"),
    )
    override(
        "radarPageUrl",
        fields.get("radar_page_url"),
        primary.get("radarPageUrl"),
    )
    if not config.get("radarPageUrl"):
        config["radarPageUrl"] = _radar_page_url(emis)
    override(
        "attendancePageUrl",
        fields.get("attendance_page_url"),
        primary.get("attendancePageUrl"),
    )
    if not config.get("attendancePageUrl"):
        config["attendancePageUrl"] = _attendance_page_url(emis)
    if "googleRadarExecUrl" not in config:
        config["googleRadarExecUrl"] = ""
    override("driveFolderId", fields.get("drive_folder_id"), primary.get("driveFolderId"))
    override("configEndpoint", fields.get("config_endpoint"), primary.get("configEndpoint"))
    config["emis"] = emis

    if not config.get("attendanceDocId"):
        raise HTTPException(
            400,
            "Cannot approve: attendanceDocId is empty. "
            "Include it in the job payload or use the Test Primary (110101) seed.",
        )
    return config


def _serialize(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat()
        else:
            out[k] = v
    return out



# ---------------------------------------------------------------------------
# Phase 4a — Device registry + telemetry
# ---------------------------------------------------------------------------


def _resolve_device_id(
    device_id: UUID | None = None,
    tablet_android_id: str | None = None,
) -> UUID | None:
    if device_id:
        row = db.fetchone("SELECT id FROM devices WHERE id = %s", (str(device_id),))
        if not row:
            raise HTTPException(404, f"device_id not found: {device_id}")
        return row["id"]
    if tablet_android_id:
        row = db.fetchone(
            "SELECT id FROM devices WHERE tablet_android_id = %s",
            (tablet_android_id,),
        )
        if not row:
            raise HTTPException(
                404,
                f"tablet_android_id not found: {tablet_android_id}",
            )
        return row["id"]
    return None


@app.get("/api/v1/devices")
def list_devices(
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
    emis: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    selected = _parse_domain_query(
        ",".join(domain) if domain else None,
        domains,
    )
    # Also accept repeated ?domain=
    if domain:
        for d in domain:
            selected |= _parse_domain_query(d, None)
    rows = db.fetchall(
        """
        SELECT id, emis, school_email, tablet_android_id, serial, device_type,
               sim, whatsapp, app_version, last_seen, created_at, updated_at
        FROM devices
        ORDER BY last_seen DESC NULLS LAST, created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    school_names = _school_name_lookup()
    out: list[dict[str, Any]] = []
    for r in rows:
        if emis and str(r.get("emis") or "") != str(emis):
            continue
        ser = _serialize(r)
        cand = set()
        d = _email_domain(ser.get("school_email"))
        if d:
            cand.add(d)
        ser["domains"] = sorted(cand)
        if not _domain_matches(selected, cand):
            continue
        ser["school_name"] = school_names.get(str(ser.get("emis") or ""), None)
        out.append(ser)
    mb_map = _device_data_mb_map([d["id"] for d in out])
    for ser in out:
        mb = mb_map.get(str(ser.get("id") or ""), None)
        ser["data_mb"] = round(mb, 3) if mb is not None else None
    return {
        "devices": out,
        "count": len(out),
        "scale_hint": "~100 schools (registry grows as tablets enrol)",
    }


@app.post("/api/v1/devices", status_code=201)
def create_device(body: DeviceIn) -> dict:
    if body.tablet_android_id:
        clash = db.fetchone(
            "SELECT id FROM devices WHERE tablet_android_id = %s",
            (body.tablet_android_id,),
        )
        if clash:
            raise HTTPException(409, "tablet_android_id already registered")
    row = db.fetchone(
        """
        INSERT INTO devices
          (emis, school_email, tablet_android_id, serial, device_type,
           sim, whatsapp, app_version, last_seen)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, emis, school_email, tablet_android_id, serial, device_type,
                  sim, whatsapp, app_version, last_seen, created_at, updated_at
        """,
        (
            body.emis,
            body.school_email,
            body.tablet_android_id,
            body.serial,
            body.device_type,
            body.sim,
            body.whatsapp,
            body.app_version,
            body.last_seen,
        ),
    )
    return {"device": _serialize(row)}


@app.get("/api/v1/devices/{device_id}")
def get_device(device_id: UUID) -> dict:
    row = db.fetchone(
        """
        SELECT id, emis, school_email, tablet_android_id, serial, device_type,
               sim, whatsapp, app_version, last_seen, created_at, updated_at
        FROM devices WHERE id = %s
        """,
        (str(device_id),),
    )
    if not row:
        raise HTTPException(404, "device not found")
    ser = _serialize(row)
    d = _email_domain(ser.get("school_email"))
    ser["domains"] = [d] if d else []
    return {"device": ser}


@app.patch("/api/v1/devices/{device_id}")
def update_device(device_id: UUID, body: DeviceUpdate) -> dict:
    existing = db.fetchone("SELECT * FROM devices WHERE id = %s", (str(device_id),))
    if not existing:
        raise HTTPException(404, "device not found")
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "no fields to update")
    if "tablet_android_id" in data and data["tablet_android_id"]:
        clash = db.fetchone(
            "SELECT id FROM devices WHERE tablet_android_id = %s AND id <> %s",
            (data["tablet_android_id"], str(device_id)),
        )
        if clash:
            raise HTTPException(409, "tablet_android_id already registered")
    cols = []
    vals: list[Any] = []
    for key in (
        "emis",
        "school_email",
        "tablet_android_id",
        "serial",
        "device_type",
        "sim",
        "whatsapp",
        "app_version",
        "last_seen",
    ):
        if key in data:
            cols.append(f"{key} = %s")
            vals.append(data[key])
    cols.append("updated_at = now()")
    vals.append(str(device_id))
    row = db.fetchone(
        f"""
        UPDATE devices SET {', '.join(cols)}
        WHERE id = %s
        RETURNING id, emis, school_email, tablet_android_id, serial, device_type,
                  sim, whatsapp, app_version, last_seen, created_at, updated_at
        """,
        tuple(vals),
    )
    return {"device": _serialize(row)}


@app.delete("/api/v1/devices/{device_id}")
def delete_device(device_id: UUID) -> dict:
    row = db.fetchone(
        "DELETE FROM devices WHERE id = %s RETURNING id",
        (str(device_id),),
    )
    if not row:
        raise HTTPException(404, "device not found")
    return {"deleted": str(row["id"])}


@app.get("/api/v1/telemetry/summary")
def telemetry_summary(
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
    emis: str | None = Query(None),
) -> dict:
    """Aggregate counts for dash cards (skills, prompts, publishes, LLM, data_mb, radar)."""
    selected = _parse_domain_query(
        ",".join(domain) if domain else None,
        domains,
    )
    if domain:
        for d in domain:
            selected |= _parse_domain_query(d, None)

    devices = db.fetchall(
        """
        SELECT id, emis, school_email, last_seen
        FROM devices
        """
    )
    device_ids: list[Any] = []
    for drow in devices:
        if emis and str(drow.get("emis") or "") != str(emis):
            continue
        cand: set[str] = set()
        ed = _email_domain(drow.get("school_email"))
        if ed:
            cand.add(ed)
        if not _domain_matches(selected, cand):
            continue
        device_ids.append(drow["id"])

    counts = {k: 0 for k in TELEMETRY_KINDS}
    data_mb_total = 0.0
    if device_ids:
        # Fetch kind counts
        placeholders = ",".join(["%s"] * len(device_ids))
        rows = db.fetchall(
            f"""
            SELECT kind, COUNT(*) AS n
            FROM telemetry_events
            WHERE device_id IN ({placeholders})
            GROUP BY kind
            """,
            tuple(str(x) for x in device_ids),
        )
        for r in rows:
            k = r["kind"]
            if k in counts:
                counts[k] = int(r["n"])
        mb_rows = db.fetchall(
            f"""
            SELECT payload
            FROM telemetry_events
            WHERE device_id IN ({placeholders}) AND kind = 'data_mb'
            """,
            tuple(str(x) for x in device_ids),
        )
        for r in mb_rows:
            payload = r.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            if isinstance(payload, dict):
                try:
                    data_mb_total += float(payload.get("mb") or 0)
                except (TypeError, ValueError):
                    pass

    seen_recent = 0
    for drow in devices:
        if drow["id"] not in device_ids:
            continue
        ls = drow.get("last_seen")
        if ls is None:
            continue
        if isinstance(ls, datetime):
            # within 7 days
            age = datetime.now(timezone.utc) - ls.astimezone(timezone.utc)
            if age.total_seconds() <= 7 * 86400:
                seen_recent += 1

    return {
        "devices": len(device_ids),
        "devices_seen_7d": seen_recent,
        "counts": counts,
        "skills_run": counts["skill_run"],
        "prompts": counts["prompt"],
        "publishes": counts["publish"],
        "llm_local": counts["llm_local"],
        "llm_online": counts["llm_online"],
        "radar_points": counts["radar_point"],
        "data_mb_total": round(data_mb_total, 3),
    }


@app.get("/api/v1/telemetry")
def list_telemetry(
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
    emis: str | None = Query(None),
    device_id: UUID | None = Query(None),
    kind: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    selected = _parse_domain_query(
        ",".join(domain) if domain else None,
        domains,
    )
    if domain:
        for d in domain:
            selected |= _parse_domain_query(d, None)
    if kind and kind not in TELEMETRY_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(TELEMETRY_KINDS)}")

    rows = db.fetchall(
        """
        SELECT t.id, t.device_id, t.kind, t.payload, t.created_at,
               d.emis, d.school_email, d.tablet_android_id
        FROM telemetry_events t
        LEFT JOIN devices d ON d.id = t.device_id
        ORDER BY t.created_at DESC
        LIMIT %s
        """,
        (limit * 3,),  # over-fetch then filter
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if device_id and str(r.get("device_id")) != str(device_id):
            continue
        if kind and r.get("kind") != kind:
            continue
        if emis and str(r.get("emis") or "") != str(emis):
            continue
        cand: set[str] = set()
        ed = _email_domain(r.get("school_email"))
        if ed:
            cand.add(ed)
        if not _domain_matches(selected, cand):
            continue
        ser = _serialize(r)
        ser["domains"] = sorted(cand)
        out.append(ser)
        if len(out) >= limit:
            break
    return {"events": out, "count": len(out)}


@app.post("/api/v1/telemetry", status_code=201)
def create_telemetry(body: TelemetryIn) -> dict:
    if body.kind not in TELEMETRY_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(TELEMETRY_KINDS)}")
    resolved = _resolve_device_id(body.device_id, body.tablet_android_id)
    if resolved is None:
        raise HTTPException(400, "device_id or tablet_android_id is required")
    created = body.created_at or datetime.now(timezone.utc)
    row = db.fetchone(
        """
        INSERT INTO telemetry_events (device_id, kind, payload, created_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id, device_id, kind, payload, created_at
        """,
        (
            str(resolved),
            body.kind,
            db.jsonify(body.payload or {}),
            created,
        ),
    )
    # Bump last_seen on the device
    db.execute(
        "UPDATE devices SET last_seen = %s, updated_at = now() WHERE id = %s",
        (created, str(resolved)),
    )
    return {"event": _serialize(row)}



# ---------------------------------------------------------------------------
# Radar parents JSON (tiny series; spider drawn in static HTML — no PNG)
# ---------------------------------------------------------------------------

_DEMO_RADAR_PATH = STATIC_DIR / "radar" / "demo-110101.json"


def _public_base_is_local(base: str | None = None) -> bool:
    b = (base or PUBLIC_BASE_URL).lower()
    return (
        "127.0.0.1" in b
        or "localhost" in b
        or b.startswith("http://192.168.")
        or b.startswith("http://10.")
        or ".gbu.lan" in b
    )


def _radar_page_url(emis: str, *, embed: bool = True) -> str:
    q = f"emis={emis}"
    if embed:
        q += "&embed=1"
    return f"{PUBLIC_BASE_URL}/radar/?{q}"


def _attendance_page_url(emis: str, *, embed: bool = True) -> str:
    q = "?embed=1" if embed else ""
    return f"{PUBLIC_BASE_URL}/school/{emis}/attendance{q}"


def _radar_path(emis: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,31}", emis):
        raise HTTPException(400, "invalid emis")
    return RADAR_DIR / f"{emis}.json"


def _known_emis(emis: str) -> bool:
    for row in _collect_emis_codes():
        if str(row.get("emis")) == emis:
            return True
    return False


def _axis_num(raw: dict[str, Any], *keys: str) -> float:
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            try:
                val = float(raw[k])
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"axis {k} must be a number") from exc
            if val < 0:
                raise HTTPException(400, "axis values must be >= 0")
            return val
    raise HTTPException(400, "axis needs A/B/C (or snap/confirmed/semis)")


def _normalize_radar(payload: dict[str, Any], emis: str) -> dict[str, Any]:
    """Canonical radar_parents document. Rejects images / oversized bodies."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "radar body must be a JSON object")
    blob = json.dumps(payload)
    if len(blob.encode("utf-8")) > RADAR_MAX_BYTES:
        raise HTTPException(413, "radar JSON too large (max 64KB); PNG is not accepted")
    low = blob.lower()
    if "data:image" in low or '"png"' in low or "base64" in low:
        raise HTTPException(400, "radar ingest is JSON series only; do not send PNG or base64 images")
    body_emis = str(payload.get("emis") or emis).strip()
    if body_emis != emis:
        raise HTTPException(400, "body emis must match path emis")
    periods_in = payload.get("periods")
    if not isinstance(periods_in, dict):
        raise HTTPException(400, "periods object required")
    periods: dict[str, Any] = {}
    for name in RADAR_PERIODS:
        block = periods_in.get(name)
        if not isinstance(block, dict):
            raise HTTPException(400, f"periods.{name} required")
        axes_in = block.get("axes")
        if not isinstance(axes_in, list):
            raise HTTPException(400, f"periods.{name}.axes must be a list")
        if len(axes_in) > 24:
            raise HTTPException(400, f"periods.{name} has too many axes")
        axes = []
        for ax in axes_in:
            if not isinstance(ax, dict):
                raise HTTPException(400, "axis must be an object")
            label = str(ax.get("label") or "").strip()
            if not label or len(label) > 32:
                raise HTTPException(400, "axis label required (max 32 chars)")
            axes.append({
                "label": label,
                "A": _axis_num(ax, "A", "snap", "snapRaw"),
                "B": _axis_num(ax, "B", "confirmed"),
                "C": _axis_num(ax, "C", "semis", "semisExpected"),
            })
        periods[name] = {"axes": axes}
    series = payload.get("series")
    if not isinstance(series, list) or len(series) != 3:
        series = [
            {"id": "A", "key": "snap", "label": "Snap (face count)", "color": "#1E88E5"},
            {"id": "B", "key": "confirmed", "label": "Confirmed", "color": "#FDD835"},
            {"id": "C", "key": "semis", "label": "SEMIS enrollment", "color": "#43A047"},
        ]
    headline = payload.get("headline") if isinstance(payload.get("headline"), dict) else None
    out: dict[str, Any] = {
        "schemaVersion": "1",
        "kind": "radar_parents",
        "emis": emis,
        "schoolName": payload.get("schoolName"),
        "schoolEmail": payload.get("schoolEmail"),
        "domain": payload.get("domain"),
        "classLabel": payload.get("classLabel"),
        "asOf": payload.get("asOf"),
        "source": payload.get("source") or "webhook",
        "demo": bool(payload.get("demo")),
        "headline": headline,
        "valuesNote": payload.get("valuesNote"),
        "series": series,
        "periods": periods,
        "radarPageUrl": _radar_page_url(emis),
    }
    return {k: v for k, v in out.items() if v is not None}


def _seed_demo_radar() -> None:
    """Write Test Primary demo radar JSON once (known snap 4 / confirmed 4 / SEMIS 6)."""
    RADAR_DIR.mkdir(parents=True, exist_ok=True)
    dest = RADAR_DIR / "110101.json"
    if dest.is_file():
        return
    if not _DEMO_RADAR_PATH.is_file():
        return
    raw = json.loads(_DEMO_RADAR_PATH.read_text(encoding="utf-8"))
    doc = _normalize_radar(raw, "110101")
    dest.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    audit = RADAR_AUDIT_DIR / "110101.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "at": datetime.now(timezone.utc).isoformat(),
        "actor": "seed",
        "emis": "110101",
        "source": "demo",
        "bytes": dest.stat().st_size,
    }
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _read_radar(emis: str) -> dict[str, Any]:
    if not _known_emis(emis):
        raise HTTPException(404, f"EMIS {emis} is not in the warehouse (no invented SEMIS schools)")
    path = _radar_path(emis)
    if not path.is_file():
        raise HTTPException(404, f"no radar JSON for {emis}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, "stored radar JSON is invalid") from exc
    if isinstance(data, dict):
        data["radarPageUrl"] = _radar_page_url(emis)
    return data


@app.get("/api/v1/radar/{emis}.json")
@app.get("/api/v1/radar/{emis}")
def get_radar(emis: str) -> dict:
    """Latest radar_parents JSON for a known EMIS. Static page draws the spider.

    Starlette's path param swallows the ``.json`` suffix, so strip it here.
    """
    emis = emis.strip()
    if emis.endswith(".json"):
        emis = emis[: -len(".json")]
    return _read_radar(emis)


@app.post("/api/v1/radar/{emis}", status_code=201, dependencies=[Depends(require_publish_token)])
def put_radar(emis: str, body: dict[str, Any]) -> dict:
    """
    Store latest radar JSON (Apps Script or tablet webhook). Same audit token as
    publish-events when PUBLISH_AUDIT_TOKEN is set. Writes docker-data radar/{emis}.json
    plus an audit jsonl line and a publish_events row. Does not accept PNG.
    """
    emis = emis.strip()
    if not _known_emis(emis):
        raise HTTPException(404, f"EMIS {emis} is not in the warehouse (no invented SEMIS schools)")
    doc = _normalize_radar(body, emis)
    RADAR_DIR.mkdir(parents=True, exist_ok=True)
    RADAR_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = _radar_path(emis)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    stamped = datetime.now(timezone.utc).isoformat()
    audit_path = RADAR_AUDIT_DIR / f"{emis}.jsonl"
    rec = {
        "at": stamped,
        "actor": "webhook",
        "emis": emis,
        "source": doc.get("source"),
        "bytes": path.stat().st_size,
        "classLabel": doc.get("classLabel"),
        "asOf": doc.get("asOf"),
    }
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    preview = None
    headline = doc.get("headline") or {}
    if headline:
        preview = f"snap {headline.get('A')} / confirmed {headline.get('B')} / SEMIS {headline.get('C')}"
    event = db.fetchone(
        """
        INSERT INTO publish_events
          (emis, kind, school_email, doc_id, status, payload_preview,
           created_at, actor, meta, config_path)
        VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s, %s)
        RETURNING id
        """,
        (
            emis,
            "radar_parents",
            doc.get("schoolEmail"),
            None,
            "ok",
            preview or f"radar JSON {emis}",
            "webhook",
            db.jsonify({
                "radarPageUrl": doc["radarPageUrl"],
                "radar_path": f"radar/{emis}.json",
                "source": doc.get("source"),
                "classLabel": doc.get("classLabel"),
                "asOf": doc.get("asOf"),
            }),
            f"radar/{emis}.json",
        ),
    )
    return {
        "ok": True,
        "emis": emis,
        "radarPageUrl": doc["radarPageUrl"],
        "path": f"radar/{emis}.json",
        "audit": rec,
        "publish_event_id": str(event["id"]) if event else None,
    }



# Daily attendance parents JSON (tiny text digest for combined Site page — no PNG)
# ---------------------------------------------------------------------------

_DEMO_DAILY_PATH = STATIC_DIR / "school" / "demo-110101-daily.json"


def _attendance_path(emis: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,31}", emis):
        raise HTTPException(400, "invalid emis")
    return ATTENDANCE_DIR / f"{emis}.json"


def _optional_nonneg_num(raw: Any, field: str) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{field} must be a number") from exc
    if val < 0:
        raise HTTPException(400, f"{field} must be >= 0")
    return val


def _gap_pct(snap: float | None, confirmed: float | None, semis: float | None) -> float | None:
    """Honest gap % vs SEMIS enrollment using confirmed when present, else snap."""
    if semis is None or semis <= 0:
        return None
    present = confirmed if confirmed is not None else snap
    if present is None:
        return None
    return round((semis - present) / semis * 100.0, 1)


def _normalize_daily_attendance(payload: dict[str, Any], emis: str) -> dict[str, Any]:
    """Canonical daily_attendance_parents digest for the combined parent page."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "attendance body must be a JSON object")
    blob = json.dumps(payload)
    if len(blob.encode("utf-8")) > ATTENDANCE_MAX_BYTES:
        raise HTTPException(413, "attendance JSON too large (max 64KB); PNG is not accepted")
    low = blob.lower()
    if "data:image" in low or "base64" in low:
        raise HTTPException(400, "attendance ingest is JSON text only; do not send PNG or base64 images")
    body_emis = str(payload.get("emis") or emis).strip()
    if body_emis != emis:
        raise HTTPException(400, "body emis must match path emis")
    md = payload.get("reportMarkdown") or payload.get("report_markdown") or ""
    if md is not None and not isinstance(md, str):
        raise HTTPException(400, "reportMarkdown must be a string")
    bullets = payload.get("bullets")
    if bullets is not None:
        if not isinstance(bullets, list) or any(not isinstance(x, str) for x in bullets):
            raise HTTPException(400, "bullets must be a list of strings")
        if len(bullets) > 40:
            raise HTTPException(400, "too many bullets")
    snap = _optional_nonneg_num(
        payload.get("snap") if payload.get("snap") is not None else payload.get("A"),
        "snap",
    )
    confirmed = _optional_nonneg_num(
        payload.get("confirmed") if payload.get("confirmed") is not None else payload.get("B"),
        "confirmed",
    )
    semis = _optional_nonneg_num(
        payload.get("semis") if payload.get("semis") is not None else payload.get("C"),
        "semis",
    )
    gap = payload.get("gapPct")
    if gap is None:
        gap = payload.get("gap_pct")
    if gap is None:
        gap = _gap_pct(snap, confirmed, semis)
    else:
        gap = _optional_nonneg_num(gap, "gapPct")
    site_ref = payload.get("siteRef") or payload.get("site_ref")
    if site_ref is not None:
        site_ref = str(site_ref).strip() or None
        # Never treat site/legacy ref as EMIS — warehouse EMIS stays path `emis`.
        if site_ref and site_ref == emis:
            site_ref = None
    out: dict[str, Any] = {
        "schemaVersion": "1",
        "kind": "daily_attendance_parents",
        "emis": emis,
        "schoolName": payload.get("schoolName"),
        "schoolEmail": payload.get("schoolEmail"),
        "domain": payload.get("domain"),
        "classLabel": payload.get("classLabel"),
        "attendanceDate": payload.get("attendanceDate") or payload.get("asOf"),
        "asOf": payload.get("asOf") or payload.get("attendanceDate"),
        "source": payload.get("source") or "webhook",
        "demo": bool(payload.get("demo")),
        "headline": payload.get("headline"),
        "reportMarkdown": md or None,
        "bullets": bullets,
        "payloadPreview": payload.get("payloadPreview") or payload.get("payload_preview"),
        "snap": snap,
        "confirmed": confirmed,
        "semis": semis,
        "gapPct": gap,
        "siteRef": site_ref,
        "siteRefNote": (
            "Optional Site/legacy ref — not warehouse EMIS"
            if site_ref
            else None
        ),
        "attendancePageUrl": _attendance_page_url(emis),
        "radarPageUrl": _radar_page_url(emis),
    }
    return {k: v for k, v in out.items() if v is not None}


def _seed_demo_attendance() -> None:
    """Write Test Primary demo daily attendance JSON once (Nursery 3 scaffold)."""
    ATTENDANCE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ATTENDANCE_DIR / "110101.json"
    if dest.is_file():
        return
    if not _DEMO_DAILY_PATH.is_file():
        return
    raw = json.loads(_DEMO_DAILY_PATH.read_text(encoding="utf-8"))
    doc = _normalize_daily_attendance(raw, "110101")
    dest.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    audit = ATTENDANCE_AUDIT_DIR / "110101.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "at": datetime.now(timezone.utc).isoformat(),
        "actor": "seed",
        "emis": "110101",
        "source": "demo",
        "bytes": dest.stat().st_size,
    }
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _read_daily_attendance(emis: str) -> dict[str, Any] | None:
    path = _attendance_path(emis)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, "stored attendance JSON is invalid") from exc
    if isinstance(data, dict):
        data["attendancePageUrl"] = _attendance_page_url(emis)
        data["radarPageUrl"] = _radar_page_url(emis)
    return data


def _school_config_for_emis(emis: str) -> dict[str, Any]:
    path = SCHOOL_CONFIGS_DIR / f"{emis}.json"
    cfg: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg = raw
        except (json.JSONDecodeError, OSError):
            cfg = {}
    if not cfg.get("radarPageUrl"):
        cfg["radarPageUrl"] = _radar_page_url(emis)
    if not cfg.get("attendancePageUrl"):
        cfg["attendancePageUrl"] = _attendance_page_url(emis)
    return cfg


# ---------------------------------------------------------------------------
# 0.4.17 — Edge Attendance outbox ingest + radar aggregate + AI-ready summary
# Schema: snapait android SessionRepository.buildPayload (edge-attendance/1)
# Destinations on this host:
#   POST /api/v1/attendance/ingest  → Postgres attendance_sync_events
#                                    + /data/attendance/outbox/*.ndjson
# Designed (not on this host yet): Cloud Run HTTPS → BigQuery append-only → Looker Studio
#   see snapait/docs/edge-attendance/05-gwfe-parallel-verification.md
# On-device: Room edge_attendance.db (sessions + sync_outbox); NDJSON under
#   files/edge-attendance/export/outbox-*.ndjson
# ---------------------------------------------------------------------------

ATTENDANCE_INGEST_SCHEMA = {
    "schema_version": "edge-attendance/1 (const)",
    "session_id": "uuid (required, idempotency key)",
    "emis_code": "string (required)",
    "school_email": "email e.g. ht.{emis}@sl.p4sgi.com",
    "class_label": "string|null",
    "attendance_date": "YYYY-MM-DD",
    "detected_count": "int >= 0 (snap / face count)",
    "confirmed_count": "int >= 0 (teacher-confirmed)",
    "absent_named": "string[] (Phase A always [])",
    "mean_confidence": "number|null",
    "manual_review_required": "bool",
    "photo_present_at_capture": "bool",
    "photo_retained": "bool (false after FIFO A)",
    "model_version": "string",
    "device_serial": "string",
    "synced_at": "ISO-8601",
    "notes": "Photos never sync. Accepts one object, {items:[...]}, or NDJSON lines.",
    "landing_paths": {
        "api": "POST /api/v1/attendance/ingest",
        "postgres": "attendance_sync_events",
        "ndjson": "/data/attendance/outbox/ (host: /mnt/drive_14tb/docker-data/sl.p4sgi/attendance/outbox/)",
        "cloud_run_bq_looker": "designed in snapait docs; not deployed on this HQ box yet",
    },
}


def _province_for_emis(emis: str, school_email: str | None = None) -> tuple[str | None, str | None]:
    """Map school → (province/region, district) from OU path northern/southern/western or schools table."""
    emis = (emis or "").strip()
    email = (school_email or "").strip().lower()
    try:
        rows = db.fetchall(
            "SELECT ou_path FROM ous WHERE emis = %s OR lower(coalesce(primary_email,'')) = %s "
            "ORDER BY created_at DESC LIMIT 5",
            (emis, email),
        )
        for r in rows or []:
            region, district, _ = _region_from_ou(r.get("ou_path"))
            if region or district:
                return region, district
    except Exception:  # noqa: BLE001
        pass
    try:
        row = db.fetchone(
            "SELECT province, district FROM schools WHERE emis = %s OR lower(coalesce(email,'')) = %s LIMIT 1",
            (emis, email),
        )
        if row:
            return row.get("province"), row.get("district")
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _normalize_outbox_item(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HTTPException(400, "Each outbox item must be a JSON object")
    session_id = str(raw.get("session_id") or "").strip()
    emis = str(raw.get("emis_code") or raw.get("emis") or "").strip()
    if not session_id:
        raise HTTPException(400, "session_id is required")
    if not emis:
        raise HTTPException(400, "emis_code is required")
    schema = str(raw.get("schema_version") or "edge-attendance/1")
    school_email = raw.get("school_email")
    province, district = _province_for_emis(
        emis, school_email if isinstance(school_email, str) else None
    )

    def _int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    return {
        "session_id": session_id,
        "schema_version": schema,
        "emis_code": emis,
        "school_email": school_email,
        "class_label": raw.get("class_label"),
        "attendance_date": raw.get("attendance_date"),
        "detected_count": _int(raw.get("detected_count"), 0),
        "confirmed_count": _int(raw.get("confirmed_count"), 0),
        "absent_named": raw.get("absent_named") if isinstance(raw.get("absent_named"), list) else [],
        "mean_confidence": raw.get("mean_confidence"),
        "manual_review_required": raw.get("manual_review_required"),
        "photo_present_at_capture": raw.get("photo_present_at_capture"),
        "photo_retained": raw.get("photo_retained"),
        "model_version": raw.get("model_version"),
        "device_serial": raw.get("device_serial"),
        "synced_at": raw.get("synced_at"),
        "province": province or "unknown",
        "district": district or "unknown",
        "payload": raw,
    }


def _upsert_outbox_row(item: dict[str, Any]) -> str:
    row = db.fetchone(
        """
        INSERT INTO attendance_sync_events (
            session_id, schema_version, emis_code, school_email, class_label,
            attendance_date, detected_count, confirmed_count, absent_named,
            mean_confidence, manual_review_required, photo_present_at_capture,
            photo_retained, model_version, device_serial, synced_at,
            province, district, payload
        ) VALUES (
            %(session_id)s, %(schema_version)s, %(emis_code)s, %(school_email)s, %(class_label)s,
            %(attendance_date)s, %(detected_count)s, %(confirmed_count)s, %(absent_named)s,
            %(mean_confidence)s, %(manual_review_required)s, %(photo_present_at_capture)s,
            %(photo_retained)s, %(model_version)s, %(device_serial)s, %(synced_at)s,
            %(province)s, %(district)s, %(payload)s
        )
        ON CONFLICT (session_id) DO UPDATE SET
            confirmed_count = EXCLUDED.confirmed_count,
            detected_count = EXCLUDED.detected_count,
            synced_at = EXCLUDED.synced_at,
            province = EXCLUDED.province,
            district = EXCLUDED.district,
            payload = EXCLUDED.payload,
            received_at = now()
        RETURNING id::text AS id
        """,
        {
            **item,
            "absent_named": db.jsonify(item["absent_named"]),
            "payload": db.jsonify(item["payload"]),
            "attendance_date": item["attendance_date"] or None,
            "synced_at": item["synced_at"] or None,
            "mean_confidence": item["mean_confidence"],
        },
    )
    return str(row["id"]) if row else ""


def _append_outbox_ndjson(items: list[dict[str, Any]]) -> str:
    ATTENDANCE_OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ATTENDANCE_OUTBOX_DIR / f"ingest-{stamp}.ndjson"
    with path.open("a", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it["payload"], ensure_ascii=False) + "\n")
    return f"attendance/outbox/{path.name}"


@app.get("/api/v1/attendance/ingest/schema")
def attendance_ingest_schema() -> dict:
    """Document the Edge Attendance outbox JSON shape tablets should POST."""
    return {
        "ok": True,
        "version": APP_VERSION,
        "schema": ATTENDANCE_INGEST_SCHEMA,
        "example": {
            "schema_version": "edge-attendance/1",
            "session_id": "7c2e9a1b-4f3d-4c8a-9e21-0a1b2c3d4e5f",
            "emis_code": "110101",
            "school_email": "ht.110101@sl.p4sgi.com",
            "class_label": "P3-A",
            "attendance_date": "2026-09-26",
            "detected_count": 49,
            "confirmed_count": 48,
            "absent_named": [],
            "mean_confidence": 0.81,
            "manual_review_required": True,
            "photo_present_at_capture": True,
            "photo_retained": False,
            "model_version": "mlkit-face@bundled-v1",
            "device_serial": "R58M12ABCDE",
            "synced_at": "2026-09-26T14:05:22+02:00",
        },
    }


@app.post("/api/v1/attendance/ingest", status_code=201, dependencies=[Depends(require_publish_token)])
async def attendance_ingest(request: Request) -> dict:
    """
    Accept Edge Attendance tiny-sync outbox JSON (single object, {items:[...]}, or NDJSON body).
    Idempotent on session_id. Does not create Vault/holds or wipe devices.
    """
    raw_body = await request.body()
    if not raw_body:
        raise HTTPException(400, "Empty body")
    text_body = raw_body.decode("utf-8", errors="replace").strip()
    items_raw: list[Any] = []
    if text_body.startswith("{"):
        try:
            parsed = json.loads(text_body)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"Invalid JSON: {e}") from e
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            items_raw = parsed["items"]
        elif isinstance(parsed, dict):
            items_raw = [parsed]
        else:
            raise HTTPException(400, "JSON object or {items:[...]} expected")
    else:
        for line in text_body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items_raw.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise HTTPException(400, f"Invalid NDJSON line: {e}") from e

    if not items_raw:
        raise HTTPException(400, "No outbox items")
    if len(items_raw) > 500:
        raise HTTPException(400, "Max 500 items per request")

    normalized = [_normalize_outbox_item(x) for x in items_raw]
    ids = [_upsert_outbox_row(it) for it in normalized]
    ndjson_path = _append_outbox_ndjson(normalized)
    return {
        "ok": True,
        "accepted": len(normalized),
        "ids": ids,
        "ndjson_path": ndjson_path,
        "schema": "edge-attendance/1",
        "radar_url": "/api/v1/attendance/radar",
    }


def _period_key(iso_date: str | None, period: str) -> str:
    """Match Android RadarAggregator period buckets (day/week/month/term/year)."""
    s = (iso_date or "").strip()[:10]
    parts = s.split("-")
    try:
        y = int(parts[0])
        m = int(parts[1])
        d = int(parts[2])
    except (IndexError, ValueError):
        return s or "unknown"
    period = (period or "day").lower()
    if period in ("day", "daily"):
        return s
    if period in ("week", "weekly"):
        mdays = [
            31,
            29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
        ]
        doy = d + sum(mdays[: max(0, m - 1)])
        week = ((doy - 1) // 7) + 1
        return f"{y:04d}-W{week:02d}"
    if period in ("month", "monthly"):
        return f"{y:04d}-{m:02d}"
    if period in ("term", "termly", "termlly"):
        term = 1 if m <= 4 else (2 if m <= 8 else 3)
        return f"{y:04d}-T{term}"
    if period in ("year", "annual", "yearly"):
        return f"{y:04d}"
    return s


@app.get("/api/v1/attendance/radar")
def attendance_radar(
    grain: str = Query("province", description="school|province|country"),
    period: str = Query("week", description="day|week|month|term|year"),
    emis: str | None = Query(None),
) -> dict:
    """
    Aggregate snap (detected_count) vs confirmed vs SEMIS (when present in payload) by grain.
    Empty state returns landing paths so HQ knows where tablet syncs should POST.
    """
    grain = (grain or "province").lower()
    if grain not in ("school", "province", "country", "emis"):
        raise HTTPException(400, "grain must be school|province|country")
    table_err = None
    try:
        rows = db.fetchall(
            """
            SELECT session_id, emis_code, school_email, class_label,
                   attendance_date::text AS attendance_date,
                   detected_count, confirmed_count, province, district, payload, synced_at
            FROM attendance_sync_events
            ORDER BY attendance_date DESC NULLS LAST, received_at DESC
            LIMIT 5000
            """
        )
    except Exception as exc:  # noqa: BLE001
        rows = []
        table_err = str(exc)

    if emis:
        rows = [r for r in rows if str(r.get("emis_code")) == emis]

    landing = {
        "ingest_api": f"{PUBLIC_BASE_URL}/api/v1/attendance/ingest",
        "schema_api": f"{PUBLIC_BASE_URL}/api/v1/attendance/ingest/schema",
        "ndjson_dir_host": "/mnt/drive_14tb/docker-data/sl.p4sgi/attendance/outbox/",
        "ndjson_dir_container": "/data/attendance/outbox/",
        "postgres_table": "attendance_sync_events",
        "designed_cloud": (
            "Cloud Run → BigQuery → Looker "
            "(snapait/docs/edge-attendance/05-gwfe-parallel-verification.md) — not on this host yet"
        ),
        "on_device": "Room DB edge_attendance.db (sync_outbox) + files/edge-attendance/export/outbox-*.ndjson",
        "android_setting": "Settings → sync_endpoint_url → POST JSON (SyncWorker)",
    }

    series = [
        {"id": "snap", "label": "Snap (detected_count)", "color": "#1E88E5"},
        {"id": "confirmed", "label": "Confirmed", "color": "#FDD835"},
        {"id": "semis", "label": "SEMIS enrollment", "color": "#43A047"},
    ]

    if not rows:
        return {
            "ok": True,
            "empty": True,
            "version": APP_VERSION,
            "grain": grain,
            "period": period,
            "message": (
                "No Edge Attendance sync rows on this host yet. "
                "Point tablet sync_endpoint_url at the ingest API, "
                "or drop NDJSON into the outbox dir."
            ),
            "landing": landing,
            "schema": ATTENDANCE_INGEST_SCHEMA,
            "axes": [],
            "series": series,
            "table_error": table_err,
            "count": 0,
        }

    from collections import defaultdict

    buckets: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"snap": [], "confirmed": [], "semis": []}
    )

    def axis_for(r: dict[str, Any]) -> str:
        if grain in ("school", "emis"):
            return str(r.get("emis_code") or "unknown")
        if grain == "province":
            return str(r.get("province") or "unknown")
        return "Sierra Leone"

    for r in rows:
        pk = _period_key(r.get("attendance_date"), period)
        axis = axis_for(r)
        key = f"{axis}|{pk}"
        snap = float(r.get("detected_count") or 0)
        conf = float(r.get("confirmed_count") or 0)
        semis = None
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if isinstance(payload, dict):
            for k in ("semis_expected", "semis", "semis_enrollment", "enrollment"):
                if payload.get(k) is not None:
                    try:
                        semis = float(payload[k])
                    except (TypeError, ValueError):
                        semis = None
                    break
        buckets[key]["snap"].append(snap)
        buckets[key]["confirmed"].append(conf)
        if semis is not None:
            buckets[key]["semis"].append(semis)

    axes = []
    for key, vals in sorted(buckets.items()):
        axis, pk = key.split("|", 1)
        n = max(len(vals["snap"]), 1)
        axes.append({
            "axis": axis,
            "period_key": pk,
            "snap": round(sum(vals["snap"]) / n, 2),
            "confirmed": round(sum(vals["confirmed"]) / max(len(vals["confirmed"]), 1), 2),
            "semis": round(sum(vals["semis"]) / len(vals["semis"]), 2) if vals["semis"] else None,
            "sessions": len(vals["snap"]),
        })

    return {
        "ok": True,
        "empty": False,
        "version": APP_VERSION,
        "grain": grain,
        "period": period,
        "landing": landing,
        "count": len(rows),
        "axes": axes,
        "series": series,
    }


@app.get("/api/v1/insights/summary")
def insights_summary(
    request: Request,
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
) -> dict:
    """AI-ready JSON facts only (no prose chatbot). Sourced from real warehouse / GAM caches."""
    allowed, p4 = _insight_scope(request, domain, domains)
    facts: dict[str, Any] = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "version": APP_VERSION,
        "scope": {
            "email": p4.get("email"),
            "superadmin": p4["superadmin"],
            "domains": sorted(allowed) if allowed else ["all"],
        },
        "devices": {},
        "users": {},
        "attendance": {},
        "gaps": [],
        "notes": [],
    }

    try:
        data, info = _load_sources(INSIGHT_BUNDLES["devices"], allowed)
        rows = _build_device_rows(data, allowed)
        summary = _summarize_devices(rows)
        facts["devices"] = {
            "count": summary.get("devices"),
            "users_with_devices": summary.get("users_with_devices"),
            "by_type": summary.get("by_type"),
            "by_region": summary.get("by_region"),
            "developer_mode": summary.get("developer_mode"),
            "adb": summary.get("adb"),
            "by_compromised": summary.get("by_compromised"),
            "by_encryption": summary.get("by_encryption"),
            "sources": {
                k: {"rows": v.get("rows"), "error": v.get("error")}
                for k, v in (info or {}).items()
            },
        }
    except Exception as exc:  # noqa: BLE001
        facts["notes"].append(f"devices_unavailable: {exc}")

    try:
        path = _latest_run_file("never_logged_in")
        if path and path.is_file():
            nli_rows = _read_gam_csv(path)
            if allowed is not None:
                nli_rows = [
                    r for r in nli_rows
                    if _in_scope(_row_email(r, "primaryEmail", "email"), allowed)
                ]
            facts["users"]["never_logged_in"] = len(nli_rows)
        else:
            facts["users"]["never_logged_in"] = None
            facts["notes"].append(
                "never_logged_in CSV not in gam-out/runs yet — run GAM report never_logged_in"
            )
    except Exception as exc:  # noqa: BLE001
        facts["notes"].append(f"never_logged_in_unavailable: {exc}")

    try:
        sync_rows = db.fetchall(
            """
            SELECT emis_code, class_label, attendance_date::text AS attendance_date,
                   detected_count, confirmed_count, payload
            FROM attendance_sync_events
            ORDER BY attendance_date DESC NULLS LAST
            LIMIT 2000
            """
        )
        facts["attendance"]["sync_sessions"] = len(sync_rows)
        gaps = []
        for r in sync_rows:
            payload = r.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            semis = None
            if isinstance(payload, dict):
                for k in ("semis_expected", "semis", "semis_enrollment", "enrollment"):
                    if payload.get(k) is not None:
                        try:
                            semis = float(payload[k])
                        except (TypeError, ValueError):
                            semis = None
                        break
            conf = float(r.get("confirmed_count") or 0)
            if semis is not None:
                gap = semis - conf
                if abs(gap) >= 3:
                    gaps.append({
                        "emis_code": r.get("emis_code"),
                        "class_label": r.get("class_label"),
                        "attendance_date": r.get("attendance_date"),
                        "confirmed": conf,
                        "semis": semis,
                        "gap_semis_minus_confirmed": gap,
                    })
        facts["gaps"] = gaps[:50]
        facts["attendance"]["gap_count_abs_ge_3"] = len(gaps)
    except Exception as exc:  # noqa: BLE001
        facts["attendance"]["sync_sessions"] = 0
        facts["notes"].append(f"attendance_sync_unavailable: {exc}")

    facts["notes"].append(
        "traffic_bytes: not available from Google Reports API / GAM "
        "(accounts:*_quota_in_mb is Drive/Gmail storage, not mobile network)."
    )
    return facts




@app.get("/api/v1/attendance/{emis}.json")
@app.get("/api/v1/attendance/{emis}")
def get_daily_attendance(emis: str) -> dict:
    """Latest daily attendance digest for a known EMIS (combined parent page)."""
    emis = emis.strip()
    if emis.endswith(".json"):
        emis = emis[: -len(".json")]
    if emis.lower() in {"ingest", "radar", "outbox", "schema"}:
        raise HTTPException(404, f"Reserved attendance path '{emis}'")
    if not _known_emis(emis):
        raise HTTPException(404, f"EMIS {emis} is not in the warehouse (no invented SEMIS schools)")
    data = _read_daily_attendance(emis)
    if not data:
        raise HTTPException(404, f"no daily attendance JSON for {emis}")
    return data


@app.post("/api/v1/attendance/{emis}", status_code=201, dependencies=[Depends(require_publish_token)])
def put_daily_attendance(emis: str, body: dict[str, Any]) -> dict:
    """
    Store latest daily attendance digest (Apps Script or tablet). Same audit token as
    publish-events when PUBLISH_AUDIT_TOKEN is set. Writes docker-data attendance/{emis}.json.
    JSON text only — no PNG.
    """
    emis = emis.strip()
    if emis.lower() in {"ingest", "radar", "outbox", "schema"}:
        raise HTTPException(404, f"Reserved attendance path '{emis}' — use /api/v1/attendance/ingest or /radar")
    if not _known_emis(emis):
        raise HTTPException(404, f"EMIS {emis} is not in the warehouse (no invented SEMIS schools)")
    doc = _normalize_daily_attendance(body, emis)
    ATTENDANCE_DIR.mkdir(parents=True, exist_ok=True)
    ATTENDANCE_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = _attendance_path(emis)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    stamped = datetime.now(timezone.utc).isoformat()
    audit_path = ATTENDANCE_AUDIT_DIR / f"{emis}.jsonl"
    rec = {
        "at": stamped,
        "actor": "webhook",
        "emis": emis,
        "source": doc.get("source"),
        "bytes": path.stat().st_size,
        "classLabel": doc.get("classLabel"),
        "attendanceDate": doc.get("attendanceDate"),
    }
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    preview = doc.get("headline") or doc.get("payloadPreview") or f"daily attendance {emis}"
    event = db.fetchone(
        """
        INSERT INTO publish_events
          (emis, kind, school_email, doc_id, status, payload_preview,
           created_at, actor, meta, config_path)
        VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s, %s)
        RETURNING id
        """,
        (
            emis,
            "daily_attendance_parents",
            doc.get("schoolEmail"),
            None,
            "ok",
            str(preview)[:240],
            "webhook",
            db.jsonify({
                "attendancePageUrl": doc["attendancePageUrl"],
                "radarPageUrl": doc["radarPageUrl"],
                "attendance_path": f"attendance/{emis}.json",
                "source": doc.get("source"),
                "classLabel": doc.get("classLabel"),
                "attendanceDate": doc.get("attendanceDate"),
            }),
            f"attendance/{emis}.json",
        ),
    )
    return {
        "ok": True,
        "emis": emis,
        "attendancePageUrl": doc["attendancePageUrl"],
        "radarPageUrl": doc["radarPageUrl"],
        "path": f"attendance/{emis}.json",
        "audit": rec,
        "publish_event_id": str(event["id"]) if event else None,
    }


def _safe_read_radar_optional(emis: str) -> dict[str, Any] | None:
    path = _radar_path(emis)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _merge_daily_with_radar(
    daily: dict[str, Any] | None,
    radar: dict[str, Any] | None,
    cfg: dict[str, Any],
    emis: str,
) -> dict[str, Any] | None:
    """Fill honest available fields on the daily digest from radar / school-config.

    Does not invent SEMIS schools or fabricate counts. Only copies values that
    already exist on radar headline / axes or config.
    """
    if daily is None and radar is None:
        return None
    out: dict[str, Any] = dict(daily or {})
    out.setdefault("emis", emis)
    out.setdefault("kind", "daily_attendance_parents")
    if not out.get("schoolName"):
        out["schoolName"] = cfg.get("schoolName") or (radar or {}).get("schoolName")
    if not out.get("schoolEmail"):
        out["schoolEmail"] = cfg.get("schoolEmail") or (radar or {}).get("schoolEmail")
    if not out.get("classLabel") and radar:
        out["classLabel"] = radar.get("classLabel")
    if not out.get("attendanceDate") and not out.get("asOf") and radar and radar.get("asOf"):
        out["attendanceDate"] = radar.get("asOf")
        out["asOf"] = radar.get("asOf")
    elif not out.get("asOf") and out.get("attendanceDate"):
        out["asOf"] = out["attendanceDate"]
    elif not out.get("attendanceDate") and out.get("asOf"):
        out["attendanceDate"] = out["asOf"]

    headline = (radar or {}).get("headline") if isinstance((radar or {}).get("headline"), dict) else {}
    snap = out.get("snap")
    confirmed = out.get("confirmed")
    semis = out.get("semis")
    if snap is None and headline.get("A") is not None:
        snap = headline.get("A")
    if confirmed is None and headline.get("B") is not None:
        confirmed = headline.get("B")
    if semis is None and headline.get("C") is not None:
        semis = headline.get("C")
    # If still missing, try latest Daily axis from radar (same single known snapshot).
    if (snap is None or confirmed is None or semis is None) and radar:
        periods = radar.get("periods") if isinstance(radar.get("periods"), dict) else {}
        daily_block = periods.get("Daily") if isinstance(periods.get("Daily"), dict) else {}
        axes = daily_block.get("axes") if isinstance(daily_block.get("axes"), list) else []
        if axes and isinstance(axes[-1], dict):
            ax = axes[-1]
            if snap is None:
                snap = ax.get("A")
            if confirmed is None:
                confirmed = ax.get("B")
            if semis is None:
                semis = ax.get("C")
    try:
        snap_f = float(snap) if snap is not None and snap != "" else None
        conf_f = float(confirmed) if confirmed is not None and confirmed != "" else None
        semis_f = float(semis) if semis is not None and semis != "" else None
    except (TypeError, ValueError):
        snap_f = conf_f = semis_f = None
    if snap_f is not None:
        out["snap"] = snap_f
    if conf_f is not None:
        out["confirmed"] = conf_f
    if semis_f is not None:
        out["semis"] = semis_f
    gap = out.get("gapPct")
    if gap is None:
        gap = _gap_pct(snap_f, conf_f, semis_f)
    if gap is not None:
        out["gapPct"] = gap

    # Series meta for parent page (A/B/C labels) — from radar when present.
    if radar and isinstance(radar.get("series"), list):
        out["series"] = radar["series"]
    else:
        out.setdefault(
            "series",
            [
                {"id": "A", "key": "snap", "label": "Snap (face count)", "color": "#1E88E5"},
                {"id": "B", "key": "confirmed", "label": "Confirmed", "color": "#FDD835"},
                {"id": "C", "key": "semis", "label": "SEMIS enrollment", "color": "#43A047"},
            ],
        )

    if not out.get("headline") and (snap_f is not None or conf_f is not None or semis_f is not None):
        cls = out.get("classLabel") or "Class"
        date = out.get("attendanceDate") or out.get("asOf") or ""
        out["headline"] = f"{cls} — {date}".strip(" —")

    # Rebuild bullets when empty so the page is not blank when metrics exist.
    if not out.get("bullets") and (snap_f is not None or conf_f is not None or semis_f is not None):
        bullets = []
        if out.get("attendanceDate") or out.get("asOf"):
            bullets.append(f"Date: {out.get('attendanceDate') or out.get('asOf')}")
        if out.get("classLabel"):
            bullets.append(f"Class: {out['classLabel']}")
        if snap_f is not None:
            bullets.append(f"Present (snap): {int(snap_f) if snap_f == int(snap_f) else snap_f}")
        if conf_f is not None:
            bullets.append(f"Confirmed: {int(conf_f) if conf_f == int(conf_f) else conf_f}")
        if semis_f is not None:
            bullets.append(f"SEMIS enrollment: {int(semis_f) if semis_f == int(semis_f) else semis_f}")
        if gap is not None:
            bullets.append(f"Gap vs SEMIS: {gap}%")
        if out.get("siteRef"):
            bullets.append(f"Site/legacy ref: {out['siteRef']} (not EMIS; warehouse EMIS is {emis})")
        out["bullets"] = bullets

    if out.get("siteRef") and not out.get("siteRefNote"):
        out["siteRefNote"] = "Optional Site/legacy ref — not warehouse EMIS"

    out["attendancePageUrl"] = _attendance_page_url(emis)
    out["radarPageUrl"] = _radar_page_url(emis)
    if radar:
        out["radarAsOf"] = radar.get("asOf")
        out["radarValuesNote"] = radar.get("valuesNote")
        if isinstance(radar.get("headline"), dict) and radar["headline"].get("note"):
            out["radarHeadlineNote"] = radar["headline"]["note"]
    return {k: v for k, v in out.items() if v is not None}


@app.get("/api/v1/school/{emis}/attendance-bundle")
def school_attendance_bundle(emis: str) -> dict:
    """Parent combined payload: school config + daily digest merged with radar metrics."""
    emis = emis.strip()
    if not _known_emis(emis):
        raise HTTPException(404, f"EMIS {emis} is not in the warehouse (no invented SEMIS schools)")
    cfg = _school_config_for_emis(emis)
    daily_raw = _read_daily_attendance(emis)
    radar = _safe_read_radar_optional(emis)
    daily = _merge_daily_with_radar(daily_raw, radar, cfg, emis)
    school_name = (
        (daily or {}).get("schoolName")
        or cfg.get("schoolName")
        or (radar or {}).get("schoolName")
    )
    publish_exec = cfg.get("publishExecUrl") or ""
    publish_note = None
    if not publish_exec:
        publish_note = (
            "Empty until HQ deploys Apps Script Web app and pastes /exec into publishExecUrl "
            "(tablet POST). Site charts use googleRadarExecUrl (same deployment + ?embed=1)."
        )
    metrics = {
        "classLabel": (daily or {}).get("classLabel") or (radar or {}).get("classLabel"),
        "attendanceDate": (daily or {}).get("attendanceDate") or (daily or {}).get("asOf"),
        "snap": (daily or {}).get("snap"),
        "confirmed": (daily or {}).get("confirmed"),
        "semis": (daily or {}).get("semis"),
        "gapPct": (daily or {}).get("gapPct"),
        "series": (daily or {}).get("series"),
    }
    return {
        "emis": emis,
        "schoolName": school_name,
        "config": {
            "emis": emis,
            "schoolName": school_name,
            "schoolEmail": cfg.get("schoolEmail"),
            "attendanceDocId": cfg.get("attendanceDocId"),
            "siteAttendanceUrl": cfg.get("siteAttendanceUrl"),
            "radarPageUrl": _radar_page_url(emis),
            "attendancePageUrl": _attendance_page_url(emis),
            "googleRadarExecUrl": cfg.get("googleRadarExecUrl") or "",
            "googleSiteId": cfg.get("googleSiteId"),
            "publishExecUrl": publish_exec,
            "publishExecUrlNote": publish_note,
            "googleRadarExecUrlNote": (
                None
                if (cfg.get("googleRadarExecUrl") or "").strip()
                else (
                    "Empty until HQ deploys apps-script-publish-attendance.gs as a Web app (Anyone) "
                    "and pastes …/exec?embed=1 here. Preferred Site embed — no Cloudflare required."
                )
            ),
            "notes": cfg.get("_notes"),
        },
        "preferredSiteEmbedUrl": (cfg.get("googleRadarExecUrl") or "").strip() or None,
        "daily": daily,
        "radar": {
            "present": radar is not None,
            "schoolName": (radar or {}).get("schoolName"),
            "classLabel": (radar or {}).get("classLabel"),
            "asOf": (radar or {}).get("asOf"),
            "headline": (radar or {}).get("headline"),
            "series": (radar or {}).get("series"),
            "valuesNote": (radar or {}).get("valuesNote"),
        } if radar else {"present": False},
        "metrics": metrics,
        "radarPageUrl": _radar_page_url(emis),
        "attendancePageUrl": _attendance_page_url(emis),
        "publicBaseUrl": PUBLIC_BASE_URL,
        "publicBaseIsLocal": _public_base_is_local(),
        "hasRadar": radar is not None,
    }


@app.get("/school/{emis}/attendance")
@app.get("/school/{emis}/attendance/")
def school_attendance_page(emis: str) -> FileResponse:
    """Combined parent page: daily summary + radar iframe (Site embed target)."""
    emis = emis.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,31}", emis):
        raise HTTPException(400, "invalid emis")
    page = STATIC_DIR / "school" / "attendance.html"
    if not page.is_file():
        raise HTTPException(404, "attendance page missing")
    return FileResponse(
        page,
        media_type="text/html",
        headers={
            # Allow Google Sites / other parents to iframe this page.
            "Content-Security-Policy": "frame-ancestors *",
                    },
    )


@app.get("/radar")
@app.get("/radar/")
def radar_page() -> FileResponse:
    page = STATIC_DIR / "radar" / "index.html"
    if not page.is_file():
        raise HTTPException(404, "radar page missing")
    return FileResponse(
        page,
        media_type="text/html",
        headers={
            "Content-Security-Policy": "frame-ancestors *",
                    },
    )


# ---------------------------------------------------------------------------
# 0.4.14 — Devices + Active-users reports (cache-first; Refresh = async 202 bundle)
# ---------------------------------------------------------------------------
# All sources are read-only GAM print/report commands run by scripts/run_gam_report.sh.
# Raw caches are customer-wide (super-admin refresh) or own-domain (scoped refresh);
# every response is scoped server-side again on read.

INSIGHT_BUNDLES: dict[str, list[str]] = {
    "devices": ["devices_ci", "devices_mobile", "devices_cros", "users_full", "login_activity", "token_activity"],
    "active-users": [
        "users_full", "usage_snapshot", "usage_7d", "login_activity", "devices_ci", "devices_mobile",
        "gemini_activity", "notebooklm_activity", "drive_activity", "token_activity", "drive_filecounts",
    ],
}
INSIGHT_SOURCE_LABELS: dict[str, str] = {
    "devices_ci": "gam print devices (Cloud Identity devices + deviceUsers)",
    "devices_mobile": "gam print mobile allfields",
    "devices_cros": "gam print cros allfields",
    "users_full": "gam print users (Directory)",
    "login_activity": "gam report login start -180d",
    "token_activity": "gam report token start -30d (OAuth apps)",
    "usage_snapshot": "gam report users date <latest of today-2..-4>",
    "usage_7d": "gam report users range <7 days ending latest> aggregatebyuser",
    "gemini_activity": "gam report gemini start -30d (gemini_in_workspace_apps)",
    "notebooklm_activity": "gam report gemininotebook start -30d (gemini_notebook = NotebookLM)",
    "drive_activity": "gam report drive start -30d (Drive audit)",
    "drive_filecounts": "gam csvfile <ever-signed-in users> print filecounts (owned Drive items)",
}
_INSIGHT_LOCK = threading.Lock()
BUNDLES_DIR = GAM_RUNS_DIR / "bundles"
REGIONS_CSV = GAM_OUT_DIR / "school-regions.csv"
NOT_FROM_GOOGLE = "not available from Google"
_EMIS_EMAIL_RE = re.compile(r"^(?:ht|[a-z]{1,3}\d{0,2})\.(\d{4}-\d-\d{5})@", re.I)
_EMIS_TOKEN_RE = re.compile(r"^\d{4}-\d-\d{5}$")


def _read_gam_csv(path: Path | None) -> list[dict[str, str]]:
    """Parse a GAM CSV, skipping '#' comment / error lines appended by the runner."""
    if not path or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines: list[str] = []
    for ln in text.splitlines():
        if not ln:
            continue
        if ln.startswith("#"):
            if lines:  # runner appends "# gam exit=…" + raw stderr after the CSV body — stop there
                break
            continue
        lines.append(ln)
    if not lines:
        return []
    try:
        return [dict(r) for r in csv.DictReader(io.StringIO("\n".join(lines)))]
    except csv.Error:
        return []


def _run_meta(stem: str) -> dict[str, Any]:
    mp = GAM_RUNS_DIR / f"{stem}.meta.json"
    if mp.is_file():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _run_names_newest_first() -> list[dict[str, Any]]:
    """Run id/filename/report/status for every CSV/TXT run, newest first (meta only, no CSV reads)."""
    out = []
    for p in _sorted_run_files():
        if p.suffix in (".csv", ".txt") and not p.name.endswith(".stderr"):
            meta = _run_meta(p.stem)
            out.append({"id": p.stem, "filename": p.name, "report": meta.get("report") or _run_stem_parts(p.stem)[1],
                        "status": meta.get("status") or "ok"})
    return out


def _latest_run_for_scope(report: str, needed: set[str] | None, runs: list[dict[str, Any]] | None = None) -> tuple[Path | None, dict[str, Any]]:
    """Newest OK run whose GAM-side domain filter covers the needed scope (empty run domains = all)."""
    for run in (runs if runs is not None else _run_names_newest_first()):
        if run.get("report") != report:
            continue
        if not _RUN_STEM_RE.match(run["id"]):
            continue
        status = str(run.get("status") or "").lower()
        if status not in ("ok", ""):
            continue
        p = GAM_RUNS_DIR / run["filename"]
        if not p.is_file() or p.stat().st_size == 0:
            continue
        meta = _run_meta(p.stem)
        run_domains = {str(d).lower() for d in (meta.get("domains") or [])}
        if run_domains and (needed is None or not needed <= run_domains):
            continue
        return p, meta
    return None, {}


def _latest_attempt(report: str) -> dict[str, Any]:
    """Most recent run meta of any status (to surface the exact GAM error)."""
    metas = sorted(GAM_RUNS_DIR.glob(f"*_{report}.meta.json"), reverse=True)
    if metas:
        try:
            return json.loads(metas[0].read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _load_sources(reports: list[str], needed: set[str] | None) -> tuple[dict[str, list[dict[str, str]]], dict[str, Any]]:
    data: dict[str, list[dict[str, str]]] = {}
    info: dict[str, Any] = {}
    runs = _run_names_newest_first()
    for rep in reports:
        path, meta = _latest_run_for_scope(rep, needed, runs)
        rows = _read_gam_csv(path)
        data[rep] = rows
        last = _latest_attempt(rep)
        window = {}
        if path:
            wp = path.with_name(path.stem + ".window.json")
            if wp.is_file():
                try:
                    window = json.loads(wp.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    window = {}
        info[rep] = {
            "command": INSIGHT_SOURCE_LABELS.get(rep, rep),
            "cached_file": path.name if path else None,
            "cached_at": meta.get("created_at") if path else None,
            "rows_raw": len(rows),
            "status": "ok" if path else ("no_cache" if not last else str(last.get("status") or "unknown")),
            "last_attempt_status": last.get("status") if last else None,
            "last_attempt_error": ((last.get("stderr") or last.get("error") or "")[-600:] if last and str(last.get("status")) != "ok" else ""),
            "window": window,
        }
    return data, info


def _row_email(row: dict[str, str], *cols: str) -> str:
    for c in cols:
        v = (row.get(c) or "").strip().lower()
        if "@" in v:
            return v.split()[0]
    return ""


def _in_scope(email: str, allowed: set[str] | None) -> bool:
    if allowed is None:
        return True
    d = _email_domain(email)
    return bool(d and d in allowed)


def _load_region_overrides() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for r in _read_gam_csv(REGIONS_CSV):
        e = (r.get("emis") or "").strip()
        if e:
            out[e] = {k: (v or "").strip() for k, v in r.items() if k}
    return out


def _region_from_ou(ou: str | None) -> tuple[str | None, str | None, str | None]:
    """/sl/<region>/<district>/<emis>/... → (region, district, emis) from the Workspace OU tree."""
    parts = [p for p in str(ou or "").split("/") if p]
    if len(parts) >= 4 and _EMIS_TOKEN_RE.match(parts[3]):
        return parts[1], parts[2], parts[3]
    for p in parts:
        if _EMIS_TOKEN_RE.match(p):
            return None, None, p
    return None, None, None


def _school_lookup_maps() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_emis: dict[str, dict[str, Any]] = {}
    by_email: dict[str, dict[str, Any]] = {}
    try:
        for r in db.fetchall("SELECT emis, name, email, district, province FROM schools"):
            if r.get("emis"):
                by_emis[str(r["emis"])] = r
            if r.get("email"):
                by_email[str(r["email"]).lower()] = r
    except Exception:  # noqa: BLE001
        pass
    for path in sorted(SCHOOL_CONFIGS_DIR.glob("*.json")):
        if path.name.endswith(".example.json"):
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(cfg, dict) and cfg.get("emis"):
            e = str(cfg["emis"])
            by_emis.setdefault(e, {"emis": e, "name": cfg.get("schoolName"), "email": cfg.get("schoolEmail")})
            if cfg.get("schoolEmail"):
                by_email.setdefault(str(cfg["schoolEmail"]).lower(), by_emis[e])
    return by_emis, by_email


def _school_context(email: str, ou: str | None, maps: tuple, overrides: dict[str, dict[str, str]]) -> dict[str, Any]:
    by_emis, by_email = maps
    emis, emis_src = None, None
    m = _EMIS_EMAIL_RE.match(email or "")
    if m:
        emis, emis_src = m.group(1), "email"
    ou_region, ou_district, ou_emis = _region_from_ou(ou)
    if not emis and ou_emis:
        emis, emis_src = ou_emis, "ou_path"
    if not emis and email in by_email:
        emis, emis_src = str(by_email[email].get("emis")), "schools_table"
    school = by_emis.get(emis or "") or {}
    ov = overrides.get(emis or "") or {}
    if ov.get("region") or ov.get("district"):
        region, district, rsrc = ov.get("region") or None, ov.get("district") or None, "csv_import"
    elif ou_region or ou_district:
        region, district, rsrc = ou_region, ou_district, "gam_ou_path"
    elif school.get("district") or school.get("province"):
        region, district, rsrc = school.get("province"), school.get("district"), "schools_table"
    else:
        region, district, rsrc = None, None, None
    return {
        "emis": emis or "",
        "emis_source": emis_src or "",
        "school_name": ov.get("school_name") or school.get("name") or "unknown",
        "region": region or "unknown",
        "district": district or "unknown",
        "region_source": rsrc or "unknown",
    }


def _ts_max(a: str | None, b: str | None) -> str:
    a, b = a or "", b or ""
    return a if a >= b else b


def _build_device_rows(
    data: dict[str, list[dict[str, str]]], allowed: set[str] | None
) -> list[dict[str, Any]]:
    users = {(_row_email(u, "primaryEmail")): u for u in data.get("users_full", [])}
    maps = _school_lookup_maps()
    overrides = _load_region_overrides()
    last_login = _last_login_by_user(data.get("login_activity", []))
    apps = _oauth_apps_by_user(data.get("token_activity", []))
    mobile_by_id = {(m.get("deviceId") or ""): m for m in data.get("devices_mobile", []) if m.get("deviceId")}
    rows: list[dict[str, Any]] = []
    seen_mobile: set[str] = set()

    def ctx(email: str) -> dict[str, Any]:
        u = users.get(email) or {}
        c = _school_context(email, u.get("orgUnitPath"), maps, overrides)
        ll = last_login.get(email) or {}
        return {
            "email": email,
            "domain": _email_domain(email) or "",
            "user_name": u.get("name.fullName") or "",
            "ou": u.get("orgUnitPath") or "",
            **c,
            "user_last_login": u.get("lastLoginTime") or "",
            "last_login_ip": ll.get("ip") or "",
            "last_login_region": ll.get("region") or "",
            "oauth_apps_30d": ", ".join(sorted(apps.get(email, set()))),
        }

    for d in data.get("devices_ci", []):
        email = _row_email(d, "users.0.userEmail")
        if not email or not _in_scope(email, allowed):
            continue
        dev_id = d.get("deviceId") or ""
        mob = mobile_by_id.get(dev_id) or {}
        if mob:
            seen_mobile.add(dev_id)
        rows.append({
            **ctx(email),
            "source": "cloud_identity" + ("+mobile" if mob else ""),
            "device_type": d.get("deviceType") or "",
            "os": d.get("osVersion") or mob.get("os") or "",
            "model": d.get("model") or mob.get("model") or "",
            "manufacturer": d.get("manufacturer") or mob.get("manufacturer") or mob.get("brand") or d.get("brand") or "",
            "hostname": d.get("hostname") or "",
            "ownership": d.get("ownerType") or "",
            "management_state": d.get("users.0.managementState") or "",
            "mobile_status": mob.get("status") or "",
            "compromised": d.get("compromisedState") or mob.get("deviceCompromisedStatus") or "",
            "encryption": d.get("encryptionState") or mob.get("encryptionStatus") or "",
            "developer_mode": d.get("enabledDeveloperOptions") or mob.get("developerOptionsStatus") or "",
            "adb": d.get("enabledUsbDebugging") or mob.get("adbStatus") or "",
            "unknown_sources": d.get("androidSpecificAttributes.enabledUnknownSources") or mob.get("unknownSourcesStatus") or "",
            "password_state": d.get("users.0.passwordState") or mob.get("devicePasswordStatus") or "",
            "serial": d.get("serialNumber") or mob.get("serialNumber") or "",
            "imei": d.get("imei") or mob.get("imei") or "",
            "wifi_mac": d.get("wifiMacAddresses.0") or mob.get("wifiMacAddress") or "",
            "network_operator": d.get("networkOperator") or mob.get("networkOperator") or "",
            "security_patch": d.get("securityPatchTime") or mob.get("securityPatchLevel") or "",
            "build": d.get("buildNumber") or mob.get("buildNumber") or "",
            "browser": d.get("endpointVerificationSpecificAttributes.browserAttributes.0.chromeBrowserInfo.browserVersion") or "",
            "user_agent": d.get("users.0.userAgent") or mob.get("userAgent") or "",
            "first_sync": d.get("users.0.firstSyncTime") or mob.get("firstSync") or d.get("createTime") or "",
            "last_sync": d.get("users.0.lastSyncTime") or d.get("lastSyncTime") or mob.get("lastSync") or "",
            "device_id": dev_id,
        })
    for dev_id, mob in mobile_by_id.items():
        if dev_id in seen_mobile:
            continue
        email = _row_email(mob, "email")
        if not email or not _in_scope(email, allowed):
            continue
        rows.append({
            **ctx(email),
            "source": "mobile",
            "device_type": mob.get("type") or "",
            "os": mob.get("os") or "",
            "model": mob.get("model") or "",
            "manufacturer": mob.get("manufacturer") or mob.get("brand") or "",
            "hostname": "",
            "ownership": "",
            "management_state": "",
            "mobile_status": mob.get("status") or "",
            "compromised": mob.get("deviceCompromisedStatus") or "",
            "encryption": mob.get("encryptionStatus") or "",
            "developer_mode": mob.get("developerOptionsStatus") or "",
            "adb": mob.get("adbStatus") or "",
            "unknown_sources": mob.get("unknownSourcesStatus") or "",
            "password_state": mob.get("devicePasswordStatus") or "",
            "serial": mob.get("serialNumber") or "",
            "imei": mob.get("imei") or "",
            "wifi_mac": mob.get("wifiMacAddress") or "",
            "network_operator": mob.get("networkOperator") or "",
            "security_patch": mob.get("securityPatchLevel") or "",
            "build": mob.get("buildNumber") or "",
            "browser": "",
            "user_agent": mob.get("userAgent") or "",
            "first_sync": mob.get("firstSync") or "",
            "last_sync": mob.get("lastSync") or "",
            "device_id": dev_id,
        })
    for c in data.get("devices_cros", []):
        email = _row_email(c, "annotatedUser", "recentUsers.0.email")
        if not email or not _in_scope(email, allowed):
            continue
        rows.append({
            **ctx(email),
            "source": "chromeos",
            "device_type": "CHROME_OS",
            "os": c.get("osVersion") or "",
            "model": c.get("model") or "",
            "manufacturer": c.get("manufacturer") or "",
            "hostname": "", "ownership": "", "management_state": c.get("status") or "",
            "mobile_status": "", "compromised": "", "encryption": "",
            "developer_mode": "", "adb": "", "unknown_sources": "", "password_state": "",
            "serial": c.get("serialNumber") or "", "imei": "", "wifi_mac": c.get("macAddress") or "",
            "network_operator": "", "security_patch": "", "build": c.get("platformVersion") or "",
            "browser": "", "user_agent": "", "first_sync": "",
            "last_sync": c.get("lastSync") or "", "device_id": c.get("deviceId") or "",
        })
    rows.sort(key=lambda r: r.get("last_sync") or "", reverse=True)
    return rows


def _last_login_by_user(events: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("name") != "login_success":
            continue
        email = _row_email(e, "actor.email")
        t = e.get("id.time") or ""
        cur = out.get(email)
        if email and (not cur or t > cur["time"]):
            out[email] = {
                "time": t,
                "ip": e.get("ipAddress") or "",
                "login_type": e.get("login_type") or "",
                "region": "-".join(x for x in (e.get("networkInfo.regionCode"), e.get("networkInfo.subdivisionCode")) if x),
            }
        if email:
            out[email]["count"] = out[email].get("count", 0) + 1
    for e in events:
        if e.get("name") == "login_failure":
            email = _row_email(e, "actor.email")
            if email:
                out.setdefault(email, {"time": "", "ip": "", "login_type": "", "region": "", "count": 0})
                out[email]["failures"] = out[email].get("failures", 0) + 1
    return out


def _oauth_apps_by_user(events: list[dict[str, str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for e in events:
        email = _row_email(e, "actor.email")
        app_name = (e.get("app_name") or "").strip()
        if email and app_name and e.get("name") in ("authorize", "activity"):
            out.setdefault(email, set()).add(app_name)
    return out


def _activity_by_user(events: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        email = _row_email(e, "actor.email")
        if not email:
            continue
        a = out.setdefault(email, {"events": 0, "last": "", "names": {}, "features": set()})
        a["events"] += 1
        a["last"] = _ts_max(a["last"], e.get("id.time"))
        n = e.get("name") or ""
        a["names"][n] = a["names"].get(n, 0) + 1
        for k in ("app_name", "feature_source"):
            if e.get(k):
                a["features"].add(e[k])
    return out


def _num(v: Any) -> float | None:
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _intish(v: Any) -> Any:
    n = _num(v)
    if n is None:
        return ""
    return int(n) if n == int(n) else n


def _build_active_user_rows(
    data: dict[str, list[dict[str, str]]], info: dict[str, Any], allowed: set[str] | None
) -> list[dict[str, Any]]:
    maps = _school_lookup_maps()
    overrides = _load_region_overrides()
    snap = {(_row_email(r, "email")): r for r in data.get("usage_snapshot", [])}
    week = {(_row_email(r, "email")): r for r in data.get("usage_7d", [])}
    logins = _last_login_by_user(data.get("login_activity", []))
    apps = _oauth_apps_by_user(data.get("token_activity", []))
    gem = _activity_by_user(data.get("gemini_activity", []))
    nb = _activity_by_user(data.get("notebooklm_activity", []))
    drv = _activity_by_user(data.get("drive_activity", []))
    fc = {(_row_email(r, "User")): r for r in data.get("drive_filecounts", [])}
    drive_off: set[str] = set()
    fc_file = (info.get("drive_filecounts") or {}).get("cached_file")
    if fc_file:
        log = GAM_RUNS_DIR / (Path(fc_file).stem + ".notes.log")
        if log.is_file():
            for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
                mm = re.search(r"User: ([^,\s]+@[^,\s]+), .*not enabled", ln)
                if mm:
                    drive_off.add(mm.group(1).lower())
    devs: dict[str, list[dict[str, Any]]] = {}
    for d in _build_device_rows(data, allowed):
        devs.setdefault(d["email"], []).append(d)
    have = {k: bool(info.get(k, {}).get("cached_file")) for k in info}
    rows: list[dict[str, Any]] = []
    for u in data.get("users_full", []):
        email = _row_email(u, "primaryEmail")
        if not email or not _in_scope(email, allowed):
            continue
        last = (u.get("lastLoginTime") or "").strip()
        if not last or last.lower() == "never":
            continue
        s = snap.get(email) or {}
        w = week.get(email) or {}
        ll = logins.get(email) or {}
        ud = sorted(devs.get(email, []), key=lambda r: r.get("last_sync") or "", reverse=True)
        g, n, dv = gem.get(email), nb.get(email), drv.get(email)
        fcr = fc.get(email)
        rows.append({
            "email": email,
            "domain": _email_domain(email) or "",
            "name": u.get("name.fullName") or "",
            **_school_context(email, u.get("orgUnitPath"), maps, overrides),
            "ou": u.get("orgUnitPath") or "",
            "created": u.get("creationTime") or "",
            "last_login": last,
            "last_login_ip": ll.get("ip") or "",
            "last_login_type": ll.get("login_type") or "",
            "last_login_geo": ll.get("region") or "",
            "logins_180d": ll.get("count", 0) if have.get("login_activity") else "",
            "login_failures_180d": ll.get("failures", 0) if have.get("login_activity") else "",
            "suspended": u.get("suspended") or "",
            "two_sv_enrolled": u.get("isEnrolledIn2Sv") or "",
            "two_sv_enforced": u.get("isEnforcedIn2Sv") or "",
            "device_count": len(ud),
            "latest_device_model": (ud[0].get("model") if ud else ""),
            "latest_device_os": (ud[0].get("os") if ud else ""),
            "latest_device_type": (ud[0].get("device_type") if ud else ""),
            "latest_device_sync": (ud[0].get("last_sync") if ud else ""),
            "storage_used_mb": _intish(s.get("accounts:used_quota_in_mb")),
            "storage_drive_mb": _intish(s.get("accounts:drive_used_quota_in_mb")),
            "storage_gmail_mb": _intish(s.get("accounts:gmail_used_quota_in_mb")),
            "storage_photos_mb": _intish(s.get("accounts:gplus_photos_used_quota_in_mb")),
            "storage_quota_mb": _intish(s.get("accounts:total_quota_in_mb")),
            "gmail_enabled": s.get("gmail:is_gmail_enabled") or "",
            "gmail_sent_7d": _intish(w.get("gmail:num_emails_sent")),
            "gmail_received_7d": _intish(w.get("gmail:num_emails_received")),
            "gmail_exchanged_7d": _intish(w.get("gmail:num_emails_exchanged")),
            "gmail_last_interaction": s.get("gmail:last_interaction_time") or "",
            "gmail_last_access": s.get("gmail:last_access_time") or "",
            "drive_created_7d": _intish(w.get("drive:num_items_created")),
            "drive_edited_7d": _intish(w.get("drive:num_items_edited")),
            "drive_viewed_7d": _intish(w.get("drive:num_items_viewed")),
            "drive_items_owned": (_intish(fcr.get("Total")) if fcr else ("Drive not enabled" if email in drive_off else "")),
            "drive_last_active": s.get("drive:last_active_usage_time") or "",
            "drive_audit_events_30d": (dv["events"] if dv else (0 if have.get("drive_activity") else "")),
            "drive_downloads_30d": (dv["names"].get("download", 0) if dv else (0 if have.get("drive_activity") else "")),
            "drive_uploads_30d": (dv["names"].get("upload", 0) if dv else (0 if have.get("drive_activity") else "")),
            "gemini_events_30d": (g["events"] if g else (0 if have.get("gemini_activity") else "")),
            "gemini_last": (g["last"] if g else ""),
            "gemini_features": (", ".join(sorted(g["features"])) if g else ""),
            "notebooklm_events_30d": (n["events"] if n else (0 if have.get("notebooklm_activity") else "")),
            "notebooklm_last": (n["last"] if n else ""),
            "notebooklm_actions": (", ".join(f"{k}:{v}" for k, v in sorted(n["names"].items())) if n else ""),
            "oauth_apps_authorized": _intish(s.get("accounts:num_authorized_apps")),
            "oauth_apps_30d": ", ".join(sorted(apps.get(email, set()))),
            "traffic_bytes": NOT_FROM_GOOGLE,
        })
    rows.sort(key=lambda r: r.get("last_login") or "", reverse=True)
    return rows


def _truthy_flag(val: Any) -> bool | None:
    """Map GAM boolean-ish security fields to True/False/None(unknown)."""
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("", "unknown", "none", "null"):
        return None
    if s in ("true", "1", "yes", "enabled", "on"):
        return True
    if s in ("false", "0", "no", "disabled", "off", "undetected"):
        return False
    return None


def _summarize_devices(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    def count_flag(key: str) -> dict[str, int]:
        yes = no = unk = 0
        for r in rows:
            t = _truthy_flag(r.get(key))
            if t is True:
                yes += 1
            elif t is False:
                no += 1
            else:
                unk += 1
        return {"true": yes, "false": no, "unknown": unk}

    enc = Counter()
    for r in rows:
        e = (r.get("encryption") or "").strip() or "unknown"
        enc[e] += 1
    comp = Counter()
    for r in rows:
        c = (r.get("compromised") or "").strip() or "unknown"
        comp[c] += 1

    return {
        "devices": len(rows),
        "users_with_devices": len({r["email"] for r in rows}),
        "by_type": dict(Counter(r.get("device_type") or "unknown" for r in rows).most_common()),
        "by_os": dict(Counter(r.get("os") or "unknown" for r in rows).most_common()),
        "by_model": dict(Counter(r.get("model") or "unknown" for r in rows).most_common(15)),
        "by_domain": dict(Counter(r.get("domain") or "unknown" for r in rows).most_common()),
        "by_region": dict(Counter(r.get("region") or "unknown" for r in rows).most_common()),
        "developer_mode": count_flag("developer_mode"),
        "adb": count_flag("adb"),
        "unknown_sources": count_flag("unknown_sources"),
        "by_compromised": dict(comp.most_common()),
        "by_encryption": dict(enc.most_common()),
    }


def _summarize_active(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def tot(key: str) -> float | None:
        vals = [_num(r.get(key)) for r in rows]
        vals = [v for v in vals if v is not None]
        return round(sum(vals), 2) if vals else None

    return {
        "active_users": len(rows),
        "with_devices": sum(1 for r in rows if r.get("device_count")),
        "total_storage_mb": tot("storage_used_mb"),
        "gmail_sent_7d": tot("gmail_sent_7d"),
        "gmail_received_7d": tot("gmail_received_7d"),
        "drive_edited_7d": tot("drive_edited_7d"),
        "gemini_users_30d": sum(1 for r in rows if (_num(r.get("gemini_events_30d")) or 0) > 0),
        "notebooklm_users_30d": sum(1 for r in rows if (_num(r.get("notebooklm_events_30d")) or 0) > 0),
        "two_sv_enrolled": sum(1 for r in rows if str(r.get("two_sv_enrolled")).lower() == "true"),
        "by_region": {k: sum(1 for r in rows if r.get("region") == k) for k in sorted({r.get("region") for r in rows})},
    }


def _insight_scope(request: Request, domain: list[str] | None, domains: str | None) -> tuple[set[str] | None, dict[str, Any]]:
    selected: set[str] = set()
    for d in domain or []:
        selected |= _parse_domain_query(d, None)
    selected |= _parse_domain_query(None, domains)
    return _scope_domains_for(request, selected), _request_scope(request)


def _csv_response(rows: list[dict[str, Any]], filename: str) -> Any:
    from fastapi.responses import Response

    buf = io.StringIO()
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    w = csv.DictWriter(buf, fieldnames=cols or ["empty"], extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_SOURCE_NOTES = {
    "traffic": "Google Reports API exposes no network-byte counters; traffic_bytes = 'not available from Google'. "
               "Proxies (counts, not bytes): gmail_exchanged_7d, drive_downloads_30d, drive_uploads_30d.",
    "gemini": "Gemini = Reports API audit application gemini_in_workspace_apps (gam report gemini). "
              "No Gemini parameters exist in gam report usageparameters user.",
    "notebooklm": "NotebookLM = Reports API audit application gemini_notebook (gam report gemininotebook).",
    "region": "Region/district: CSV import (gam-out/school-regions.csv) > Workspace OU path /sl/<region>/<district>/<emis> > schools table; else 'unknown'.",
    "drive_items_owned": "Owned Drive items via DwD print filecounts (users who ever signed in).",
}


@app.get("/api/v1/reports/devices")
def report_devices(
    request: Request,
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
    format: str | None = Query(None),
) -> Any:
    allowed, p4 = _insight_scope(request, domain, domains)
    data, info = _load_sources(INSIGHT_BUNDLES["devices"], allowed)
    rows = _build_device_rows(data, allowed)
    if (format or "").lower() == "csv":
        return _csv_response(rows, "devices.csv")
    return {
        "report": "devices",
        "scope": {"email": p4.get("email"), "superadmin": p4["superadmin"], "domains": sorted(allowed) if allowed else ["all"]},
        "count": len(rows),
        "summary": _summarize_devices(rows),
        "columns": list(rows[0].keys()) if rows else [],
        "rows": rows,
        "sources": info,
        "notes": _SOURCE_NOTES,
    }


@app.get("/api/v1/reports/active-users")
def report_active_users(
    request: Request,
    domain: list[str] | None = Query(None),
    domains: str | None = Query(None),
    format: str | None = Query(None),
) -> Any:
    allowed, p4 = _insight_scope(request, domain, domains)
    data, info = _load_sources(INSIGHT_BUNDLES["active-users"], allowed)
    rows = _build_active_user_rows(data, info, allowed)
    if (format or "").lower() == "csv":
        return _csv_response(rows, "active-users.csv")
    usage_window = {
        "snapshot_date": (info.get("usage_snapshot", {}).get("window") or {}).get("date"),
        "seven_day": info.get("usage_7d", {}).get("window") or {},
    }
    return {
        "report": "active-users",
        "scope": {"email": p4.get("email"), "superadmin": p4["superadmin"], "domains": sorted(allowed) if allowed else ["all"]},
        "count": len(rows),
        "summary": _summarize_active(rows),
        "usage_window": usage_window,
        "columns": list(rows[0].keys()) if rows else [],
        "rows": rows,
        "sources": info,
        "notes": _SOURCE_NOTES,
    }


def _run_bundle(bundle_id: str, items: list[dict[str, Any]], domain_list: list[str]) -> None:
    bpath = BUNDLES_DIR / f"{bundle_id}.json"
    with _INSIGHT_LOCK:  # one bundle at a time — avoids GAM rate limits / overlapping runs
        ok, gam_detail = _gam_available()
        runner = _resolve_gam_runner()
        for it in items:
            meta_path = GAM_RUNS_DIR / f"{it['id']}.meta.json"
            meta = _run_meta(it["id"]) | {"status": "running"}
            _write_gam_meta(meta_path, meta)
            if not ok or runner is None:
                _write_gam_meta(meta_path, meta | {"status": "error", "error": gam_detail or "runner_missing"})
                continue
            _run_gam_job(
                report=it["report"], run_id=it["id"],
                out_path=GAM_RUNS_DIR / f"{it['id']}.csv", meta_path=meta_path,
                runner=runner, gam_detail=gam_detail, domain_list=domain_list,
            )
    try:
        b = json.loads(bpath.read_text(encoding="utf-8"))
        b["finished_at"] = datetime.now(timezone.utc).isoformat()
        bpath.write_text(json.dumps(b, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


@app.post("/api/v1/reports/{name}/refresh", status_code=202)
def refresh_insight(name: str, request: Request) -> dict:
    """Async: queue the read-only GAM runs behind a report; poll GET /api/v1/reports/refresh/{bundle_id}."""
    if name not in INSIGHT_BUNDLES:
        raise HTTPException(404, {"error": "unknown_report", "detail": f"one of {list(INSIGHT_BUNDLES)}"})
    p4 = _request_scope(request)
    ok, gam_detail = _gam_available()
    if not ok:
        raise HTTPException(503, {"error": "gam_not_available", "detail": gam_detail})
    # Super-admin: customer-wide cache. Others: own domain only (GAM-side post-filter).
    domain_list = [] if p4["superadmin"] else list(p4["allowed_domains"] or [])
    if not p4["superadmin"] and not domain_list:
        raise HTTPException(403, {"error": "no_domain", "detail": "caller has no email domain"})
    GAM_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle_id = f"{ts}_{name}"
    items = []
    for rep in INSIGHT_BUNDLES[name]:
        run_id = f"{ts}_{rep}"
        _write_gam_meta(GAM_RUNS_DIR / f"{run_id}.meta.json", {
            "id": run_id, "report": rep, "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(), "exit_code": None,
            "filename": f"{run_id}.csv", "size_bytes": 0, "domains": domain_list,
            "bundle": bundle_id, "requested_by": p4.get("email"),
        })
        items.append({"report": rep, "id": run_id})
    (BUNDLES_DIR / f"{bundle_id}.json").write_text(json.dumps({
        "id": bundle_id, "report": name, "items": items, "domains": domain_list,
        "created_at": datetime.now(timezone.utc).isoformat(), "requested_by": p4.get("email"),
    }, indent=2) + "\n", encoding="utf-8")
    threading.Thread(target=_run_bundle, args=(bundle_id, items, domain_list), name=f"bundle-{bundle_id}", daemon=True).start()
    return {"id": bundle_id, "report": name, "status": "running", "items": items,
            "poll_url": f"/api/v1/reports/refresh/{bundle_id}", "domains": domain_list or ["all"]}


@app.get("/api/v1/reports/refresh/{bundle_id}")
def refresh_insight_status(bundle_id: str, request: Request) -> dict:
    if not re.fullmatch(r"[0-9TZ]+_[a-z-]+", bundle_id or ""):
        raise HTTPException(400, {"error": "invalid_bundle_id"})
    bpath = BUNDLES_DIR / f"{bundle_id}.json"
    if not bpath.is_file():
        raise HTTPException(404, {"error": "not_found"})
    b = json.loads(bpath.read_text(encoding="utf-8"))
    p4 = _request_scope(request)
    items = []
    for it in b.get("items", []):
        m = _run_meta(it["id"])
        err = (m.get("stderr") or m.get("error") or "") if str(m.get("status")) != "ok" else ""
        items.append({"report": it["report"], "id": it["id"], "status": m.get("status") or "unknown",
                      "exit_code": m.get("exit_code"), "size_bytes": m.get("size_bytes"),
                      "error": err[-600:] if p4["superadmin"] else ("error" if err else "")})
    states = {i["status"] for i in items}
    status = "running" if states & {"queued", "running"} else ("ok" if states <= {"ok"} else "partial")
    return {"id": bundle_id, "report": b.get("report"), "status": status, "items": items,
            "created_at": b.get("created_at"), "finished_at": b.get("finished_at")}


@app.get("/api/v1/school-regions")
def list_school_regions(request: Request) -> dict:
    ov = _load_region_overrides()
    return {"file": str(REGIONS_CSV.relative_to(DATA_DIR)) if REGIONS_CSV.is_file() else None,
            "count": len(ov), "rows": list(ov.values()),
            "format": "CSV header: emis,region,district[,school_name]"}


@app.post("/api/v1/school-regions", status_code=201)
async def import_school_regions(request: Request, file: UploadFile = File(...)) -> dict:
    """Super-admin: replace gam-out/school-regions.csv (emis,region,district[,school_name]). Old file kept as .bak.<ts>."""
    if not _request_scope(request)["superadmin"]:
        raise HTTPException(403, {"error": "superadmin_only"})
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    hdr = [h.strip().lower() for h in (reader.fieldnames or [])]
    if "emis" not in hdr or not ({"region", "district"} & set(hdr)):
        raise HTTPException(400, {"error": "bad_header", "detail": "need emis + region and/or district"})
    rows = []
    for r in reader:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if r.get("emis"):
            rows.append({k: r.get(k, "") for k in ("emis", "region", "district", "school_name")})
    GAM_OUT_DIR.mkdir(parents=True, exist_ok=True)
    if REGIONS_CSV.is_file():
        shutil.copy2(REGIONS_CSV, REGIONS_CSV.with_name(REGIONS_CSV.name + ".bak." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    with open(REGIONS_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["emis", "region", "district", "school_name"])
        w.writeheader()
        w.writerows(rows)
    return {"imported": len(rows), "file": str(REGIONS_CSV.relative_to(DATA_DIR))}



# ---------------------------------------------------------------------------
# 0.4.15 — Raw GAM exports (super-admin only): every CSV under gam-out/ run folders
# (probe-*/, runs/, ...). These hold IMEIs / MAC addresses / serials, so anyone who is
# not a super-admin gets 403, and any path outside gam-out (traversal, symlink escape,
# non-CSV) is 403 as well. Container path: /data/gam-out (compose bind mount of
# docker-data/sl.p4sgi/gam-out).
# ---------------------------------------------------------------------------
RAW_EXPORT_MAX_DEPTH = 4
RAW_EXPORT_PREVIEW_DEFAULT = 2000
_RAW_ROWCOUNT_CACHE: dict[tuple[str, int, int], int] = {}
csv.field_size_limit(64 * 1024 * 1024)


def _require_superadmin(request: Request) -> dict[str, Any]:
    p4 = _request_scope(request)
    if not p4.get("superadmin"):
        raise HTTPException(
            403,
            {"error": "forbidden", "detail": "Raw GAM exports are super-admin only (they contain IMEIs / MAC addresses)."},
        )
    return p4


def _raw_forbidden() -> HTTPException:
    return HTTPException(403, {"error": "forbidden_path", "detail": "path must name a .csv file inside gam-out/"})


def _resolve_raw_export(rel: str | None) -> Path:
    rel = (rel or "").strip()
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/") or ":" in rel:
        raise _raw_forbidden()
    parts = rel.split("/")
    if len(parts) > RAW_EXPORT_MAX_DEPTH + 1 or any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise _raw_forbidden()
    root = GAM_OUT_DIR.resolve()
    cand = root / rel
    if cand.suffix.lower() != ".csv":
        raise _raw_forbidden()
    try:
        real = cand.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, {"error": "not_found", "detail": f"No export {rel}"})
    if not real.is_relative_to(root) or not real.is_file():
        raise _raw_forbidden()
    return real


def _raw_row_count(p: Path, st: os.stat_result) -> int:
    key = (str(p), st.st_mtime_ns, st.st_size)
    hit = _RAW_ROWCOUNT_CACHE.get(key)
    if hit is not None:
        return hit
    n = 0
    try:
        with open(p, newline="", encoding="utf-8", errors="replace") as fh:
            for _ in csv.reader(fh):
                n += 1
    except csv.Error:
        with open(p, "rb") as fh:
            n = sum(1 for _ in fh)
    except OSError:
        n = 0
    rows = max(0, n - 1)
    _RAW_ROWCOUNT_CACHE[key] = rows
    return rows


def _iso_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _raw_err_text(csv_path: Path) -> str:
    errp = csv_path.with_suffix(".err")
    try:
        if errp.is_file() and errp.stat().st_size:
            return errp.read_text(encoding="utf-8", errors="replace")[:600].strip()
    except OSError:
        pass
    return ""


@app.get("/api/v1/gam/raw-exports")
def list_raw_exports(request: Request) -> dict:
    """List CSV files in every gam-out run folder (probe-*/, runs/, ...). Super-admin only."""
    _require_superadmin(request)
    root = GAM_OUT_DIR.resolve()
    files: list[dict[str, Any]] = []
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            d = Path(dirpath)
            depth = len(d.relative_to(root).parts)
            dirnames[:] = sorted(n for n in dirnames if not n.startswith(".")) if depth < RAW_EXPORT_MAX_DEPTH else []
            for name in filenames:
                if not name.lower().endswith(".csv") or name.startswith("."):
                    continue
                p = d / name
                try:
                    if p.is_symlink() or not p.is_file():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                rel = p.relative_to(root).as_posix()
                folder = p.parent.relative_to(root).as_posix()
                files.append({
                    "folder": folder if folder != "." else "(gam-out root)",
                    "name": name,
                    "path": rel,
                    "size_bytes": st.st_size,
                    "rows": _raw_row_count(p, st),
                    "modified": _iso_mtime(st.st_mtime),
                    "err": _raw_err_text(p),
                })
    folders: dict[str, dict[str, Any]] = {}
    for f in files:
        g = folders.setdefault(f["folder"], {"folder": f["folder"], "files": 0, "size_bytes": 0, "rows": 0, "modified": ""})
        g["files"] += 1
        g["size_bytes"] += f["size_bytes"]
        g["rows"] += f["rows"]
        g["modified"] = max(g["modified"], f["modified"])
    # Probe / ad-hoc folders first (newest first); the routine runs/ cache last.
    folder_list = sorted(folders.values(), key=lambda g: (g["folder"].split("/")[0] == "runs", g["folder"] == "(gam-out root)", -datetime.fromisoformat(g["modified"]).timestamp() if g["modified"] else 0, g["folder"]))
    order = {g["folder"]: i for i, g in enumerate(folder_list)}
    files.sort(key=lambda f: (order[f["folder"]], f["name"]))
    return {"root": "gam-out", "container_path": str(GAM_OUT_DIR), "folders": folder_list, "files": files, "count": len(files)}


@app.get("/api/v1/gam/raw-exports/preview")
def preview_raw_export(
    request: Request,
    path: str = Query(...),
    limit: int = Query(RAW_EXPORT_PREVIEW_DEFAULT, ge=1, le=20000),
) -> dict:
    _require_superadmin(request)
    p = _resolve_raw_export(path)
    st = p.stat()
    headers: list[str] = []
    rows: list[list[str]] = []
    total = 0
    with open(p, newline="", encoding="utf-8", errors="replace") as fh:
        for i, r in enumerate(csv.reader(fh)):
            if i == 0:
                headers = [str(c) for c in r]
                continue
            total += 1
            if len(rows) < limit:
                rows.append([str(c) for c in r])
    return {
        "path": p.relative_to(GAM_OUT_DIR.resolve()).as_posix(),
        "name": p.name,
        "size_bytes": st.st_size,
        "modified": _iso_mtime(st.st_mtime),
        "headers": headers,
        "rows": rows,
        "total_rows": total,
        "shown_rows": len(rows),
        "truncated": total > len(rows),
        "err": _raw_err_text(p),
        "download_url": "/api/v1/gam/raw-exports/download?path=" + quote(path, safe=""),
    }


@app.get("/api/v1/gam/raw-exports/download")
def download_raw_export(request: Request, path: str = Query(...)) -> Any:
    _require_superadmin(request)
    p = _resolve_raw_export(path)
    return FileResponse(path=str(p), filename=p.name, media_type="text/csv",
                        headers={"Cache-Control": "no-store"})




# ---------------------------------------------------------------------------
# Drive exports (0.4.18) — Doc + Sheet per collapsible section
# ---------------------------------------------------------------------------
# Convention (Africa/Johannesburg date):
#   Folder YYYYMMDD-sl.p4sgi-<section>/
#     Doc   YYYYMMDD-sl.p4sgi-<section>
#     Sheet YYYYMMDD-sl.p4sgi-<section>-data
# Master: YYYYMMDD-sl.p4sgi-all/ containing one subfolder per section.
# Register future sections in EXPORT_SECTIONS below (slug → collector + orientation).

EXPORT_SECTIONS: dict[str, dict[str, Any]] = {
    # slug: {title, orientation, superadmin_only, aliases}
    "schools": {"title": "Schools", "orientation": "portrait", "superadmin_only": False},
    "gam_reports": {"title": "GAM reports", "orientation": "portrait", "superadmin_only": False,
                    "aliases": ["gamreports"]},
    "devices": {"title": "Devices", "orientation": "landscape", "superadmin_only": False},
    "active_users": {"title": "Active users", "orientation": "landscape", "superadmin_only": False,
                     "aliases": ["activeusers", "active-users"]},
    "raw_exports": {"title": "Raw GAM exports", "orientation": "landscape", "superadmin_only": True,
                    "aliases": ["rawexports", "googlesources", "gamreports_raw"]},
    "attendance": {"title": "Attendance radar", "orientation": "portrait", "superadmin_only": False},
    "insights": {"title": "Insights summary", "orientation": "portrait", "superadmin_only": False},
    "all": {"title": "All sections", "orientation": "auto", "superadmin_only": False},
}


def _export_resolve_section(section: str) -> str:
    key = (section or "").strip().lower().replace("-", "_")
    if key in EXPORT_SECTIONS:
        return key
    for canon, meta in EXPORT_SECTIONS.items():
        for a in meta.get("aliases") or []:
            if key == a.replace("-", "_"):
                return canon
    raise HTTPException(
        404,
        {"error": "unknown_section", "detail": f"one of {sorted(EXPORT_SECTIONS)}"},
    )


def _export_meta_lines(request: Request, allowed: set[str] | None, section_title: str) -> list[str]:
    p4 = _request_scope(request)
    domains = "all" if allowed is None else (", ".join(sorted(allowed)) or "(none)")
    return [
        f"Section: {section_title}",
        f"Generated at: {_exdrive.johannesburg_now_label()}",
        f"Domain filter applied: {domains}",
        f"Scoped-as (UI identity): {p4.get('email') or 'local super-admin'}",
        f"Drive upload as: {GAM_EXPORT_USER}",
        f"App version: {APP_VERSION}",
    ]


def _collect_export_schools(request: Request, allowed: set[str] | None) -> dict[str, Any]:
    rows_raw = db.fetchall(
        """
        SELECT s.emis, s.name, s.email, s.district, s.province,
               s.created_at::text AS created_at, s.updated_at::text AS updated_at,
               (
                 SELECT sc.config->>'siteAttendanceUrl'
                 FROM school_configs sc WHERE sc.emis = s.emis
                 ORDER BY sc.version DESC LIMIT 1
               ) AS site_attendance_url,
               (
                 SELECT sc.version FROM school_configs sc
                 WHERE sc.emis = s.emis ORDER BY sc.version DESC LIMIT 1
               ) AS config_version
        FROM schools s ORDER BY s.name
        """
    )
    rows: list[dict[str, Any]] = []
    for r in rows_raw:
        ser = _serialize(r)
        cand = _domains_for_school_row(ser.get("email"), ser.get("site_attendance_url"))
        ser["domains"] = ",".join(sorted(cand))
        if allowed is not None and not _domain_matches(allowed, cand):
            continue
        rows.append(ser)
    cols = ["emis", "name", "email", "domains", "district", "province",
            "config_version", "site_attendance_url", "created_at", "updated_at"]
    return {
        "title": "Schools",
        "columns": cols,
        "rows": rows,
        "summary": {"schools": len(rows)},
        "preferred_doc_cols": ["emis", "name", "email", "domains", "district", "province"],
    }


def _collect_export_gam_reports(request: Request, allowed: set[str] | None) -> dict[str, Any]:
    runs = _list_gam_runs()
    if allowed is not None:
        runs = [
            r for r in runs
            if set(r.get("domains") or []) and _domain_matches(allowed, set(r.get("domains") or []))
        ]
    rows = []
    for r in runs:
        rows.append({
            "id": r.get("id"),
            "report": r.get("report"),
            "status": r.get("status"),
            "exit_code": r.get("exit_code"),
            "domains": ",".join(r.get("domains") or []) or "all",
            "filename": r.get("filename"),
            "size_bytes": r.get("size_bytes"),
            "created_at": r.get("created_at"),
        })
    cols = ["id", "report", "status", "exit_code", "domains", "filename", "size_bytes", "created_at"]
    return {
        "title": "GAM reports",
        "columns": cols,
        "rows": rows,
        "summary": {
            "runs": len(rows),
            "ok": sum(1 for r in rows if r.get("status") == "ok"),
            "error": sum(1 for r in rows if r.get("status") not in ("ok", None)),
        },
        "preferred_doc_cols": cols,
    }


def _collect_export_devices(request: Request, allowed: set[str] | None) -> dict[str, Any]:
    data, info = _load_sources(INSIGHT_BUNDLES["devices"], allowed)
    rows = _build_device_rows(data, allowed)
    cols = list(rows[0].keys()) if rows else [
        "email", "domain", "user_name", "device_type", "model", "os", "serial", "imei", "last_sync"
    ]
    return {
        "title": "Devices",
        "columns": cols,
        "rows": rows,
        "summary": _summarize_devices(rows),
        "preferred_doc_cols": [
            "email", "user_name", "emis", "school_name", "region",
            "device_type", "model", "os", "serial", "imei", "last_sync",
            "developer_mode", "compromised", "encryption",
        ],
    }


def _collect_export_active_users(request: Request, allowed: set[str] | None) -> dict[str, Any]:
    data, info = _load_sources(INSIGHT_BUNDLES["active-users"], allowed)
    rows = _build_active_user_rows(data, info, allowed)
    cols = list(rows[0].keys()) if rows else ["email", "name", "last_login", "domain"]
    return {
        "title": "Active users",
        "columns": cols,
        "rows": rows,
        "summary": _summarize_active(rows),
        "preferred_doc_cols": [
            "email", "name", "emis", "school_name", "region",
            "last_login", "storage_used_mb", "device_count",
            "latest_device_model", "latest_device_os",
        ],
    }


def _collect_export_raw_exports(request: Request, allowed: set[str] | None) -> dict[str, Any]:
    _require_superadmin(request)
    listing = list_raw_exports(request)
    files = listing.get("files") or []
    rows = []
    for f in files:
        rows.append({
            "folder": f.get("folder"),
            "name": f.get("name"),
            "path": f.get("path"),
            "size_bytes": f.get("size_bytes"),
            "rows": f.get("rows"),
            "modified": f.get("modified"),
            "err": f.get("err") or "",
        })
    cols = ["folder", "name", "path", "size_bytes", "rows", "modified", "err"]
    return {
        "title": "Raw GAM exports (google sources)",
        "columns": cols,
        "rows": rows,
        "summary": {"files": len(rows), "folders": len(listing.get("folders") or [])},
        "preferred_doc_cols": cols,
    }


def _collect_export_attendance(request: Request, allowed: set[str] | None) -> dict[str, Any]:
    radar = attendance_radar(grain="province", period="week", emis=None)
    axes = radar.get("axes") or []
    rows = []
    for a in axes:
        rows.append({
            "axis": a.get("axis"),
            "period_key": a.get("period_key"),
            "snap": a.get("snap"),
            "confirmed": a.get("confirmed"),
            "semis": a.get("semis"),
            "sessions": a.get("sessions"),
        })
    # Also include recent sync events if present
    try:
        sync = db.fetchall(
            """
            SELECT emis_code, school_email, class_label,
                   attendance_date::text AS attendance_date,
                   detected_count, confirmed_count, province, district,
                   synced_at::text AS synced_at
            FROM attendance_sync_events
            ORDER BY attendance_date DESC NULLS LAST
            LIMIT 2000
            """
        )
    except Exception:  # noqa: BLE001
        sync = []
    sync_rows = []
    for r in sync:
        ser = _serialize(r)
        email = ser.get("school_email")
        if allowed is not None and email and not _in_scope(email, allowed):
            continue
        sync_rows.append(ser)
    # Prefer axes for the Doc/Sheet primary table; append sync as extra columns note in summary
    cols = ["axis", "period_key", "snap", "confirmed", "semis", "sessions"]
    if not rows and sync_rows:
        cols = list(sync_rows[0].keys())
        rows = sync_rows
    return {
        "title": "Attendance radar",
        "columns": cols,
        "rows": rows,
        "summary": {
            "axes": len(axes),
            "sync_sessions": len(sync_rows),
            "empty": bool(radar.get("empty")),
            "grain": radar.get("grain"),
            "period": radar.get("period"),
        },
        "preferred_doc_cols": cols,
        "extra_sync_rows": sync_rows,
    }


def _collect_export_insights(request: Request, allowed: set[str] | None) -> dict[str, Any]:
    facts = insights_summary(request, domain=None, domains=None)
    # Flatten into a readable key/value table + gaps
    rows: list[dict[str, Any]] = []
    def walk(prefix: str, obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v)
        elif isinstance(obj, list):
            rows.append({"key": prefix, "value": f"[{len(obj)} items]"})
        else:
            rows.append({"key": prefix, "value": obj})
    walk("", {k: v for k, v in facts.items() if k != "gaps"})
    for g in (facts.get("gaps") or [])[:100]:
        rows.append({
            "key": "gap",
            "value": g,
            "emis_code": g.get("emis_code") if isinstance(g, dict) else "",
            "gap_semis_minus_confirmed": g.get("gap_semis_minus_confirmed") if isinstance(g, dict) else "",
        })
    # Normalize columns
    flat = []
    for r in rows:
        flat.append({
            "key": r.get("key"),
            "value": r.get("value") if not isinstance(r.get("value"), (dict, list)) else str(r.get("value")),
            "emis_code": r.get("emis_code") or "",
            "gap_semis_minus_confirmed": r.get("gap_semis_minus_confirmed") or "",
        })
    cols = ["key", "value", "emis_code", "gap_semis_minus_confirmed"]
    return {
        "title": "Insights summary",
        "columns": cols,
        "rows": flat,
        "summary": {
            "devices": (facts.get("devices") or {}).get("count"),
            "never_logged_in": (facts.get("users") or {}).get("never_logged_in"),
            "sync_sessions": (facts.get("attendance") or {}).get("sync_sessions"),
            "gaps": len(facts.get("gaps") or []),
        },
        "preferred_doc_cols": cols,
    }


_EXPORT_COLLECTORS = {
    "schools": _collect_export_schools,
    "gam_reports": _collect_export_gam_reports,
    "devices": _collect_export_devices,
    "active_users": _collect_export_active_users,
    "raw_exports": _collect_export_raw_exports,
    "attendance": _collect_export_attendance,
    "insights": _collect_export_insights,
}


# Map internal keys → Drive folder slug (no underscores, matches user request)
_EXPORT_FOLDER_SLUG = {
    "schools": "schools",
    "gam_reports": "gamreports",
    "devices": "devices",
    "active_users": "activeusers",
    "raw_exports": "googlesources",
    "attendance": "attendance",
    "insights": "insights",
}


@app.post("/api/v1/export/{section}")
def export_section(
    section: str,
    request: Request,
    formats: str = Query("doc,sheet", description="Comma list: doc,sheet (also accepts docx,xlsx)"),
    orientation: str = Query("auto", description="portrait|landscape|auto"),
    upload: bool = Query(True, description="Upload to Drive via GAM; local files always written"),
) -> dict:
    """
    Build a professional A4 Google Doc and/or Spreadsheet for a dashboard section,
    upload into DRIVE_EXPORT_FOLDER_ID as folder YYYYMMDD-sl.p4sgi-<section>/.

    Super-admin only for raw_exports. Other sections respect domain scoping.
    Future sections: add to EXPORT_SECTIONS + _EXPORT_COLLECTORS + _EXPORT_FOLDER_SLUG.
    """
    key = _export_resolve_section(section)
    fmt_set = {f.strip().lower() for f in formats.split(",") if f.strip()}
    # normalize aliases
    if "docx" in fmt_set:
        fmt_set.add("doc")
    if "xlsx" in fmt_set or "csv" in fmt_set:
        fmt_set.add("sheet")
    fmt_set &= {"doc", "sheet", "docx", "xlsx"}
    if not fmt_set:
        raise HTTPException(400, {"error": "bad_formats", "detail": "need doc and/or sheet"})

    orient = (orientation or "auto").lower()
    if orient not in ("portrait", "landscape", "auto"):
        raise HTTPException(400, {"error": "bad_orientation"})

    stamp = _exdrive.johannesburg_stamp()
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if key == "all":
        p4 = _request_scope(request)
        # Create master folder first (if upload)
        master_name = _exdrive.section_folder_name(stamp, "all")
        master_drive = None
        master_err = None
        master_hint = None
        parent_for_children = DRIVE_EXPORT_FOLDER_ID
        if upload:
            try:
                master_drive = _exdrive.gam_create_folder(master_name, DRIVE_EXPORT_FOLDER_ID)
                parent_for_children = master_drive["id"]
            except _exdrive.GamUploadError as exc:
                master_err = str(exc)
                master_hint = exc.hint
                upload = False  # still build locals; skip further Drive attempts? keep trying children? no — parent missing
                parent_for_children = None

        children = []
        for child_key, child_meta in EXPORT_SECTIONS.items():
            if child_key == "all":
                continue
            if child_meta.get("superadmin_only") and not p4.get("superadmin"):
                children.append({
                    "section": child_key,
                    "skipped": True,
                    "reason": "superadmin_only",
                })
                continue
            # Patch folder slug via temporary monkey of section name in build
            slug = _EXPORT_FOLDER_SLUG.get(child_key, child_key)
            child_orient = orient if orient != "auto" else (child_meta.get("orientation") or "portrait")
            try:
                # collect + build with slug as section folder label
                selected: set[str] = set()
                for d in request.query_params.getlist("domain"):
                    selected |= _parse_domain_query(d, None)
                selected |= _parse_domain_query(None, request.query_params.get("domains"))
                allowed = _scope_domains_for(request, selected)
                if child_meta.get("superadmin_only"):
                    _require_superadmin(request)
                payload = _EXPORT_COLLECTORS[child_key](request, allowed)
                full_cols = _exdrive.rows_to_columns(payload["rows"], payload.get("preferred_doc_cols") or payload["columns"])
                one = _exdrive.build_and_upload_section(
                    data_dir=DATA_DIR,
                    section=slug,
                    title=f"sl.p4sgi — {payload['title']}",
                    columns=full_cols,
                    rows=payload["rows"],
                    summary=payload.get("summary"),
                    meta_lines=_export_meta_lines(request, allowed, payload["title"]),
                    orientation=child_orient if child_orient in ("portrait", "landscape") else "portrait",
                    formats=fmt_set,
                    parent_folder_id=parent_for_children,
                    stamp=stamp,
                    upload=bool(upload and parent_for_children),
                )
                one["section_key"] = child_key
                one["title"] = payload["title"]
                children.append(one)
            except HTTPException as exc:
                children.append({"section": child_key, "error": exc.detail, "status_code": exc.status_code})
            except Exception as exc:  # noqa: BLE001
                children.append({"section": child_key, "error": str(exc)})

        return {
            "ok": True,
            "version": APP_VERSION,
            "section": "all",
            "stamp": stamp,
            "convention": _exdrive.build_and_upload_section.__doc__,
            "naming": (
                f"Folder {master_name}/ contains per-section subfolders "
                "YYYYMMDD-sl.p4sgi-<section>/ with Doc + Sheet (-data)."
            ),
            "drive_folder_id_config": DRIVE_EXPORT_FOLDER_ID,
            "master": {
                "folder_name": master_name,
                "drive": master_drive,
                "upload_error": master_err,
                "upload_hint": master_hint,
            },
            "children": children,
            "help": (
                "Each section folder holds a Google Doc (A4 summary+table) and a Google Sheet "
                "(-data, full rows). Landscape for Devices / Active users / Raw exports."
            ),
        }

    # Single section
    slug = _EXPORT_FOLDER_SLUG.get(key, key)
    meta = EXPORT_SECTIONS[key]
    if meta.get("superadmin_only"):
        _require_superadmin(request)
    selected: set[str] = set()
    for d in request.query_params.getlist("domain"):
        selected |= _parse_domain_query(d, None)
    selected |= _parse_domain_query(None, request.query_params.get("domains"))
    allowed = _scope_domains_for(request, selected)
    payload = _EXPORT_COLLECTORS[key](request, allowed)
    full_cols = _exdrive.rows_to_columns(payload["rows"], payload.get("preferred_doc_cols") or payload["columns"])
    child_orient = orient if orient != "auto" else (meta.get("orientation") or "portrait")
    if child_orient not in ("portrait", "landscape"):
        child_orient = "portrait"
    result = _exdrive.build_and_upload_section(
        data_dir=DATA_DIR,
        section=slug,
        title=f"sl.p4sgi — {payload['title']}",
        columns=full_cols,
        rows=payload["rows"],
        summary=payload.get("summary"),
        meta_lines=_export_meta_lines(request, allowed, payload["title"]),
        orientation=child_orient,
        formats=fmt_set,
        parent_folder_id=DRIVE_EXPORT_FOLDER_ID,
        stamp=stamp,
        upload=upload,
    )
    result["ok"] = True
    result["version"] = APP_VERSION
    result["section_key"] = key
    result["title"] = payload["title"]
    result["drive_folder_id_config"] = DRIVE_EXPORT_FOLDER_ID
    result["help"] = (
        "Convention: folder YYYYMMDD-sl.p4sgi-<section> contains Doc "
        "YYYYMMDD-sl.p4sgi-<section> + Sheet YYYYMMDD-sl.p4sgi-<section>-data "
        "(Africa/Johannesburg date). Register exporters via EXPORT_SECTIONS + "
        "_EXPORT_COLLECTORS + _EXPORT_FOLDER_SLUG in main.py."
    )
    return result


@app.get("/api/v1/export/sections")
def export_sections_catalog() -> dict:
    """List exportable sections and naming convention (for UI + future registration)."""
    return {
        "version": APP_VERSION,
        "drive_folder_id": DRIVE_EXPORT_FOLDER_ID,
        "gam_export_user": GAM_EXPORT_USER,
        "convention": (
            "Folder YYYYMMDD-sl.p4sgi-<section>/ with Doc YYYYMMDD-sl.p4sgi-<section> "
            "and Sheet YYYYMMDD-sl.p4sgi-<section>-data (Africa/Johannesburg)."
        ),
        "sections": [
            {
                "key": k,
                "slug": _EXPORT_FOLDER_SLUG.get(k, k),
                "title": v["title"],
                "orientation": v.get("orientation"),
                "superadmin_only": bool(v.get("superadmin_only")),
                "aliases": v.get("aliases") or [],
            }
            for k, v in EXPORT_SECTIONS.items()
        ],
        "register_future": (
            "Add entry to EXPORT_SECTIONS, implement _collect_export_<name>, "
            "wire into _EXPORT_COLLECTORS and _EXPORT_FOLDER_SLUG."
        ),
    }


# Static UI
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 0.4.15: never let a browser keep a stale dashboard (the inline script IS the app).
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-App-Version": APP_VERSION,
}


@app.get("/")
@app.get("/index.html")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE_HEADERS)


# 0.4.19 security audit (GAT-style, READ-ONLY). Isolated module: any failure here must never stop the API.
# Disable with SECURITY_AUDIT_ENABLED=0. See docs/SECURITY_AUDIT.md.
if os.getenv("SECURITY_AUDIT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"):
    try:
        from . import security_audit as _secaudit

        app.include_router(
            _secaudit.build_router(
                _secaudit.Helpers(
                    insight_scope=_insight_scope,
                    load_sources=_load_sources,
                    build_device_rows=_build_device_rows,
                    row_email=_row_email,
                    in_scope=_in_scope,
                    request_scope=_request_scope,
                    is_superadmin=is_superadmin,
                    data_dir=DATA_DIR,
                    app_version=APP_VERSION,
                )
            )
        )
    except Exception as _sec_exc:  # noqa: BLE001
        print(f"[security_audit] disabled: {_sec_exc!r}", flush=True)


# 0.4.20 privacy / PIA compliance monitor (EXPERIMENTAL, own JSON state, no schema change). Isolated module:
# any failure here must never stop the API. Disable with PRIVACY_MONITOR_ENABLED=0.
if os.getenv("PRIVACY_MONITOR_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"):
    try:
        from . import privacy_monitor as _privacy

        app.include_router(
            _privacy.build_router(
                _privacy.Helpers(
                    insight_scope=_insight_scope,
                    load_sources=_load_sources,
                    build_device_rows=_build_device_rows,
                    row_email=_row_email,
                    in_scope=_in_scope,
                    request_scope=_request_scope,
                    is_superadmin=is_superadmin,
                    data_dir=DATA_DIR,
                    app_version=APP_VERSION,
                )
            )
        )
    except Exception as _priv_exc:  # noqa: BLE001
        print(f"[privacy_monitor] disabled: {_priv_exc!r}", flush=True)


# 0.4.21 ask-the-data chat (EXPERIMENTAL; local Ollama by default, read-only curated views, pgvector knowledge base).
# Isolated module: any failure here must never stop the API. Disable with ASK_DATA_ENABLED=0.
if os.getenv("ASK_DATA_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"):
    try:
        from . import ask_data as _askdata

        app.include_router(
            _askdata.build_router(
                _askdata.Helpers(request_scope=_request_scope, data_dir=DATA_DIR, app_version=APP_VERSION)
            )
        )
    except Exception as _chat_exc:  # noqa: BLE001
        print(f"[ask_data] disabled: {_chat_exc!r}", flush=True)

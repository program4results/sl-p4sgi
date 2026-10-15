"""
Security audit (GAT-style) for the p4sgi dashboard — 0.4.19.

READ-ONLY. Analyses the GAM CSVs the dashboard already caches (token_activity,
login_activity, users_full, devices_*). It never calls GAM, never writes to
Google Workspace and never touches Postgres.

Action endpoints only produce a reviewable DRY-RUN plan (the exact GAM commands a
human would run) and append it to an audit log. Nothing is executed here.

Wired from main.py with a guarded include_router so a failure in this module
cannot stop the API from starting. Disable with SECURITY_AUDIT_ENABLED=0.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Tunables (env overridable; defaults follow the GAT spec)
# --------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


INACTIVE_DAYS = _env_int("SEC_INACTIVE_DAYS", 90)
NEVER_LOGGED_IN_MIN_AGE_DAYS = _env_int("SEC_NEVER_LOGGED_IN_MIN_AGE_DAYS", 30)
FAILED_LOGIN_7D = _env_int("SEC_FAILED_LOGIN_7D", 5)
DEVICE_STALE_DAYS = _env_int("SEC_DEVICE_STALE_DAYS", 30)
PATCH_MAX_AGE_DAYS = _env_int("SEC_PATCH_MAX_AGE_DAYS", 365)
LEVEL_CRITICAL = _env_int("SEC_LEVEL_CRITICAL", 85)
LEVEL_HIGH = _env_int("SEC_LEVEL_HIGH", 60)
LEVEL_MEDIUM = _env_int("SEC_LEVEL_MEDIUM", 40)

SOURCES_OAUTH = ["token_activity"]
SOURCES_USERS = ["users_full", "login_activity", "admins"]
SOURCES_DEVICES = ["devices_ci", "devices_mobile", "devices_cros", "users_full", "login_activity", "token_activity"]

_AUDIT_LOCK = threading.Lock()
LEVEL_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0, "APPROVED": -1}
APPROVAL_DEFAULT_DAYS = _env_int("SEC_APPROVAL_DAYS", 180)


def level_for(score: int | None) -> str:
    """Risk level from a 0-100 risk score (higher = worse)."""
    if score is None:
        return "UNKNOWN"
    if score >= LEVEL_CRITICAL:
        return "CRITICAL"
    if score >= LEVEL_HIGH:
        return "HIGH"
    if score >= LEVEL_MEDIUM:
        return "MEDIUM"
    return "LOW"


# --------------------------------------------------------------------------
# Small parsing helpers (pure)
# --------------------------------------------------------------------------


def parse_ts(value: Any) -> datetime | None:
    """Parse GAM timestamps: ISO-8601 (Z or offset), date-only, or epoch ms/seconds."""
    s = str(value or "").strip()
    if not s or s.lower() in ("never", "unknown", "none", "null"):
        return None
    if s.isdigit():
        try:
            n = int(s)
            if n <= 0:
                return None
            return datetime.fromtimestamp(n / 1000 if n > 10_000_000_000 else n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def days_since(dt: datetime | None, now: datetime) -> int | None:
    if dt is None:
        return None
    return max(0, (now - dt).days)


def truthy(value: Any) -> bool | None:
    """GAM boolean-ish -> True / False / None (unknown). Mirrors main._truthy_flag."""
    s = str(value if value is not None else "").strip().lower()
    if s in ("", "unknown", "none", "null"):
        return None
    if s in ("true", "1", "yes", "enabled", "on"):
        return True
    if s in ("false", "0", "no", "disabled", "off", "undetected"):
        return False
    return None


# --------------------------------------------------------------------------
# Module 1 — OAuth app risk (from token_activity)
# --------------------------------------------------------------------------
# Heuristic weights, NOT a Google risk rating. Tune with review. Sum of distinct
# scopes, capped at 100. Full Gmail + full Drive => CRITICAL.
_SCOPE_RULES: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"^https://mail\.google\.com/?$"), 60, "Full Gmail access"),
    (re.compile(r"/auth/gmail\.(modify|send|compose|insert|settings)"), 45, "Gmail send/modify"),
    (re.compile(r"/auth/gmail(\.readonly|\.metadata)?$"), 25, "Gmail read"),
    (re.compile(r"/auth/drive$"), 60, "Full Drive access"),
    (re.compile(r"/auth/drive\.(readonly|metadata)(\.readonly)?$"), 20, "Drive read"),
    (re.compile(r"/auth/drive\.(file|appdata)$"), 5, "Drive per-file/app data"),
    (re.compile(r"/auth/drive\."), 20, "Drive (other)"),
    (re.compile(r"/auth/admin\.directory\.[a-z.]*readonly$"), 20, "Directory read"),
    (re.compile(r"/auth/admin\.directory"), 60, "Directory admin (write)"),
    (re.compile(r"/auth/admin\."), 20, "Admin API (other)"),
    (re.compile(r"/auth/calendar$"), 25, "Full Calendar access"),
    (re.compile(r"/auth/calendar\."), 10, "Calendar (limited)"),
    (re.compile(r"(/auth/contacts$|/m8/feeds)"), 25, "Full Contacts access"),
    (re.compile(r"/auth/contacts\."), 10, "Contacts (limited)"),
    (re.compile(r"/auth/cloud-platform"), 45, "Google Cloud (broad)"),
    (re.compile(r"(^openid$|/auth/userinfo\.|^profile$|^email$)"), 0, "Sign-in identity only"),
]


def scope_points(scope: str) -> tuple[int, str]:
    s = scope.strip()
    for pat, pts, label in _SCOPE_RULES:
        if pat.search(s):
            return pts, label
    return 5, "Other scope"


def score_oauth_app(scopes: list[str], user_count: int) -> tuple[int | None, list[str]]:
    """(risk_score, reasons). score None => cannot score (no scope data in the CSV)."""
    if not scopes:
        return None, ["No scope data in token_activity CSV for this app"]
    pts_by_label: dict[str, int] = {}
    for sc in scopes:
        pts, label = scope_points(sc)
        if pts > pts_by_label.get(label, -1):
            pts_by_label[label] = pts
    total = sum(pts_by_label.values())
    reasons = [f"{lbl} (+{p})" for lbl, p in sorted(pts_by_label.items(), key=lambda kv: -kv[1]) if p > 0]
    if total >= 20 and user_count >= 10:
        total += 10
        reasons.append(f"Broad reach: {user_count} users (+10)")
    return min(100, total), reasons or ["Sign-in identity only"]


_SCOPE_COL_RE = re.compile(r"^(scope(\.\d+)?|scope_data\.scope_name(\.\d+)?)$", re.I)
_SCOPE_SPLIT_RE = re.compile(r"[\s,;|]+")


def extract_scopes(row: dict[str, str]) -> list[str]:
    out: list[str] = []
    for col, val in row.items():
        if not val or not _SCOPE_COL_RE.match(col or ""):
            continue
        for tok in _SCOPE_SPLIT_RE.split(val.strip()):
            if tok and (tok.startswith("http") or tok in ("openid", "email", "profile")):
                out.append(tok)
    return out


def analyse_oauth(
    events: list[dict[str, str]],
    in_scope: Callable[[str], bool],
    row_email: Callable[..., str],
) -> dict[str, Any]:
    apps: dict[str, dict[str, Any]] = {}
    scope_cols_seen = False
    for e in events:
        email = row_email(e, "actor.email")
        if not email or not in_scope(email):
            continue
        app_name = (e.get("app_name") or "").strip()
        client_id = (e.get("client_id") or "").strip()
        if not (app_name or client_id):
            continue
        key = client_id or app_name
        a = apps.setdefault(key, {
            "app_name": app_name, "client_id": client_id, "users": set(), "events": 0,
            "revoke_events": 0, "last_seen": "", "scopes": set(),
        })
        a["app_name"] = a["app_name"] or app_name
        a["client_id"] = a["client_id"] or client_id
        a["users"].add(email)
        a["events"] += 1
        if (e.get("name") or "").lower() == "revoke":
            a["revoke_events"] += 1
        t = e.get("id.time") or ""
        if t > a["last_seen"]:
            a["last_seen"] = t
        sc = extract_scopes(e)
        if sc:
            scope_cols_seen = True
            a["scopes"].update(sc)
    rows: list[dict[str, Any]] = []
    for a in apps.values():
        scopes = sorted(a["scopes"])
        score, reasons = score_oauth_app(scopes, len(a["users"]))
        rows.append({
            "app_name": a["app_name"] or "(unnamed)",
            "client_id": a["client_id"],
            "user_count": len(a["users"]),
            "users": sorted(a["users"])[:50],
            "events_30d": a["events"],
            "revoke_events_30d": a["revoke_events"],
            "last_seen": a["last_seen"],
            "scopes": scopes,
            "risk_score": score,
            "risk_level": level_for(score),
            "reasons": reasons,
        })
    rows.sort(key=lambda r: (-LEVEL_ORDER[r["risk_level"]], -(r["risk_score"] or 0), -r["user_count"]))
    return {"rows": rows, "scope_data_present": scope_cols_seen}


# --------------------------------------------------------------------------
# Known / approved apps (0.4.22). An approval is bound to the app's scope set: if the app later asks for
# different permissions, or the review date passes, it stops being approved and shows its real risk again.
# --------------------------------------------------------------------------
def app_key(row: dict[str, Any]) -> str:
    return str(row.get("client_id") or row.get("app_name") or "")


def scope_fingerprint(scopes: list[str]) -> str:
    import hashlib

    return hashlib.sha256("\n".join(sorted(set(scopes))).encode()).hexdigest()[:16]


def load_approvals(path: Path) -> dict[str, dict[str, Any]]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def apply_approvals(rows: list[dict[str, Any]], approvals: dict[str, dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Mark approved apps. Never hides a row; keeps the raw level; flags stale approvals."""
    for r in rows:
        a = approvals.get(app_key(r))
        if not a:
            continue
        review = parse_ts(a.get("review_by"))
        if a.get("scope_fp") != scope_fingerprint(r.get("scopes") or []):
            r["approval_stale"] = "permissions changed since this app was approved: review again"
        elif review and review < now:
            r["approval_stale"] = "approval expired on " + str(a.get("review_by"))[:10]
        else:
            r["risk_level_raw"] = r["risk_level"]
            r["risk_level"] = "APPROVED"
            r["approved"] = {k: a.get(k) for k in ("note", "approved_by", "approved_at", "review_by")}
    rows.sort(key=lambda r: (-LEVEL_ORDER[r["risk_level"]], -(r["risk_score"] or 0), -r["user_count"]))
    return rows


def record_approval(approvals_path: Path, audit_dir: Path, key: str, row: dict[str, Any], note: str, who: str,
                    review_days: int, now: datetime) -> dict[str, Any]:
    """Persist an approval bound to the app's CURRENT scope set. Caller has already checked super-admin."""
    rec = {"app_name": row["app_name"], "client_id": row["client_id"], "note": note, "approved_by": who,
           "approved_at": now.isoformat(timespec="seconds"),
           "review_by": (now + timedelta(days=review_days)).isoformat(timespec="seconds"),
           "scope_fp": scope_fingerprint(row["scopes"]), "level_when_approved": row["risk_level"], "score_when_approved": row["risk_score"]}
    with _AUDIT_LOCK:
        cur = load_approvals(approvals_path)
        cur[key] = rec
        approvals_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = approvals_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, approvals_path)
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / "approvals.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now.isoformat(), "action": "approve", "key": key, **rec}) + "\n")
    return rec


# --------------------------------------------------------------------------
# Module 3 — user activity / hygiene (users_full + login_activity + admins)
# --------------------------------------------------------------------------
# Single flags land where a reviewer would expect: no 2SV = MEDIUM, admin without 2SV = HIGH,
# repeated failed logins = HIGH, idle / never-used = LOW-MEDIUM. Flags add up (cap 100).
_USER_FLAG_POINTS = {"NO_2SV": 45, "ADMIN_NO_2SV": 70, "NEVER_LOGGED_IN": 25, "INACTIVE": 40, "SUSPICIOUS_LOGINS": 65}


def analyse_users(
    users: list[dict[str, str]],
    logins: list[dict[str, str]],
    admins: list[dict[str, str]],
    in_scope: Callable[[str], bool],
    row_email: Callable[..., str],
    now: datetime,
) -> dict[str, Any]:
    admin_emails = {row_email(a, "primaryEmail", "email", "user", "User") for a in admins} - {""}
    fails: Counter[str] = Counter()
    cutoff = now - timedelta(days=7)
    for e in logins:
        if (e.get("name") or "") != "login_failure":
            continue
        email = row_email(e, "actor.email")
        t = parse_ts(e.get("id.time"))
        if email and t and t >= cutoff:
            fails[email] += 1

    rows: list[dict[str, Any]] = []
    for u in users:
        email = row_email(u, "primaryEmail")
        if not email or not in_scope(email):
            continue
        suspended = truthy(u.get("suspended")) is True
        last_raw = (u.get("lastLoginTime") or "").strip()
        last = parse_ts(last_raw)
        never = (not last_raw) or last_raw.lower() == "never"
        created = parse_ts(u.get("creationTime"))
        age = days_since(created, now)
        idle = days_since(last, now)
        flags: list[str] = []
        if not suspended:
            if never and (age is None or age >= NEVER_LOGGED_IN_MIN_AGE_DAYS):
                flags.append("NEVER_LOGGED_IN")
            if not never and truthy(u.get("isEnrolledIn2Sv")) is False:
                flags.append("ADMIN_NO_2SV" if email in admin_emails else "NO_2SV")
            if idle is not None and idle >= INACTIVE_DAYS:
                flags.append("INACTIVE")
            if fails.get(email, 0) >= FAILED_LOGIN_7D:
                flags.append("SUSPICIOUS_LOGINS")
        if not flags:
            continue
        score = min(100, sum(_USER_FLAG_POINTS[f] for f in flags))
        rows.append({
            "email": email,
            "name": u.get("name.fullName") or "",
            "ou": u.get("orgUnitPath") or "",
            "is_admin": email in admin_emails,
            "last_login": "" if never else last_raw,
            "days_since_login": idle,
            "account_age_days": age,
            "two_sv_enrolled": truthy(u.get("isEnrolledIn2Sv")),
            "failed_logins_7d": fails.get(email, 0),
            "flags": flags,
            "risk_score": score,
            "risk_level": level_for(score),
        })
    rows.sort(key=lambda r: (-r["risk_score"], r["email"]))
    return {"rows": rows, "admins_known": bool(admin_emails)}


# --------------------------------------------------------------------------
# Module 4 — device compliance (rows from main._build_device_rows)
# --------------------------------------------------------------------------
_DEVICE_FLAG_POINTS = {
    "COMPROMISED": 60, "UNENCRYPTED": 30, "UNKNOWN_SOURCES": 20, "NO_PASSWORD": 25,
    "OUTDATED_PATCH": 20, "DEVELOPER_MODE": 15, "USB_DEBUGGING": 15, "STALE_SYNC": 15,
}


def _is_unencrypted(v: Any) -> bool:
    s = str(v or "").strip().upper()
    return bool(s) and (("UNENCRYPTED" in s) or ("NOT_ENCRYPTED" in s) or s in ("DISABLED", "OFF", "FALSE"))


def _is_compromised(v: Any) -> bool:
    return str(v or "").strip().lower() in ("compromised", "true", "1", "yes")


def _no_password(v: Any) -> bool:
    s = str(v or "").strip().upper()
    return ("NOT_SET" in s) or s in ("NO_PASSWORD", "NONE", "OFF", "FALSE", "DISABLED")


def device_flags(d: dict[str, Any], now: datetime) -> list[str]:
    flags: list[str] = []
    if _is_compromised(d.get("compromised")):
        flags.append("COMPROMISED")
    if _is_unencrypted(d.get("encryption")):
        flags.append("UNENCRYPTED")
    if truthy(d.get("unknown_sources")) is True:
        flags.append("UNKNOWN_SOURCES")
    if _no_password(d.get("password_state")):
        flags.append("NO_PASSWORD")
    patch = days_since(parse_ts(d.get("security_patch")), now)
    if patch is not None and patch > PATCH_MAX_AGE_DAYS:
        flags.append("OUTDATED_PATCH")
    if truthy(d.get("developer_mode")) is True:
        flags.append("DEVELOPER_MODE")
    if truthy(d.get("adb")) is True:
        flags.append("USB_DEBUGGING")
    sync = days_since(parse_ts(d.get("last_sync")), now)
    if sync is not None and sync > DEVICE_STALE_DAYS:
        flags.append("STALE_SYNC")
    return flags


def device_status(compliance_score: int) -> str:
    if compliance_score >= 85:
        return "COMPLIANT"
    if compliance_score >= 60:
        return "WARNING"
    return "CRITICAL"


def analyse_devices(device_rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    for d in device_rows:
        flags = device_flags(d, now)
        score = max(0, 100 - sum(_DEVICE_FLAG_POINTS[f] for f in flags))
        status = device_status(score)
        status_counts[status] += 1
        flag_counts.update(flags)
        out.append({
            "device_id": d.get("device_id") or "",
            "email": d.get("email") or "",
            "domain": d.get("domain") or "",
            "school_name": d.get("school_name") or "",
            "device_type": d.get("device_type") or "",
            "model": d.get("model") or "",
            "os": d.get("os") or "",
            "serial": d.get("serial") or "",
            "last_sync": d.get("last_sync") or "",
            "security_patch": d.get("security_patch") or "",
            "flags": flags,
            "compliance_score": score,
            "compliance_status": status,
        })
    out.sort(key=lambda r: (r["compliance_score"], r["email"]))
    return {"rows": out, "by_status": dict(status_counts), "by_flag": dict(flag_counts.most_common())}


# --------------------------------------------------------------------------
# Action PLANS (never executed)
# --------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{3,256}$")
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_:.-]{3,128}$")
_OU_RE = re.compile(r"^/[A-Za-z0-9 _./-]{0,200}$")
PLAN_NOTE = (
    "DRY-RUN PLAN ONLY. Nothing was executed. Review, then run the commands yourself in a terminal "
    "with the geb@p4sgi.com GAM config. Syntax follows GAM7 docs: confirm with `gam help` for your version "
    "and test on one account first."
)


class ApproveAppIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=256, description="client_id (or app name when no client id)")
    note: str = Field(..., min_length=3, max_length=500, description="why this app is approved")
    review_days: int = Field(APPROVAL_DEFAULT_DAYS, ge=7, le=730)


class ActionPlanIn(BaseModel):
    action: Literal["revoke_token", "offboard_user", "wipe_mobile_device"]
    email: str | None = Field(None, max_length=254)
    client_id: str | None = Field(None, max_length=256)
    transfer_drive_to: str | None = Field(None, max_length=254)
    archive_ou: str | None = Field("/Archived", max_length=200)
    resource_id: str | None = Field(None, max_length=128)
    full_wipe: bool = False
    reason: str = Field("", max_length=500)


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise HTTPException(422, {"error": "invalid_plan_request", "detail": msg})


def build_plan(body: ActionPlanIn, is_protected: Callable[[str | None], bool]) -> list[dict[str, str]]:
    q = shlex.quote
    steps: list[dict[str, str]] = []
    if body.action in ("revoke_token", "offboard_user"):
        _need(bool(body.email and _EMAIL_RE.match(body.email)), "email is required and must be a valid address")
        _need(not is_protected(body.email), "refusing to plan actions against a super-admin account")
    if body.action == "revoke_token":
        _need(bool(body.client_id and _CLIENT_ID_RE.match(body.client_id)), "client_id is required (see /api/v1/security/oauth)")
        steps.append({"step": "Revoke the OAuth grant for this user and app",
                      "command": f"gam user {q(body.email)} delete token clientid {q(body.client_id)}"})
    elif body.action == "offboard_user":
        steps.append({"step": "1. Suspend (reversible)", "command": f"gam update user {q(body.email)} suspended on"})
        steps.append({"step": "2. Sign out all sessions", "command": f"gam user {q(body.email)} signout"})
        if body.transfer_drive_to:
            _need(bool(_EMAIL_RE.match(body.transfer_drive_to)), "transfer_drive_to must be a valid address")
            steps.append({"step": "3. Transfer Drive ownership", "command": f"gam user {q(body.email)} transfer drive {q(body.transfer_drive_to)}"})
        if body.archive_ou:
            _need(bool(_OU_RE.match(body.archive_ou)), "archive_ou must be an OU path like /Archived")
            steps.append({"step": "4. Move to archive OU", "command": f"gam update user {q(body.email)} ou {q(body.archive_ou)}"})
    else:
        _need(bool(body.resource_id and _RESOURCE_ID_RE.match(body.resource_id)),
              "resource_id (GAM mobile resourceId) is required; /security/devices shows device_id, map it via `gam print mobile`")
        act = "admin_remote_wipe" if body.full_wipe else "admin_account_wipe"
        steps.append({"step": "Remote full wipe" if body.full_wipe else "Wipe managed account data only",
                      "command": f"gam update mobile {q(body.resource_id)} action {act}"})
    return steps


# --------------------------------------------------------------------------
# Router factory
# --------------------------------------------------------------------------


@dataclass
class Helpers:
    """Callables supplied by main.py (avoids a circular import)."""
    insight_scope: Callable[..., tuple[set[str] | None, dict[str, Any]]]
    load_sources: Callable[[list[str], set[str] | None], tuple[dict[str, list[dict[str, str]]], dict[str, Any]]]
    build_device_rows: Callable[[dict[str, list[dict[str, str]]], set[str] | None], list[dict[str, Any]]]
    row_email: Callable[..., str]
    in_scope: Callable[[str, set[str] | None], bool]
    request_scope: Callable[[Request], dict[str, Any]]
    is_superadmin: Callable[[str | None], bool]
    data_dir: Path
    app_version: str




def build_router(h: Helpers) -> APIRouter:
    router = APIRouter(prefix="/api/v1/security", tags=["security-audit"])
    audit_dir = h.data_dir / "security" / "audit"
    approvals_path = h.data_dir / "security" / "approved_apps.json"

    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _sources_info(info: dict[str, Any]) -> dict[str, Any]:
        return {
            k: {
                "status": v.get("status"),
                "cached_file": v.get("cached_file"),
                "cached_at": v.get("cached_at"),
                "rows": v.get("rows_raw"),
                "hint": None if v.get("status") == "ok" else f"No usable cache: run the '{k}' GAM report from the dashboard first",
            }
            for k, v in info.items()
        }

    def _load(reports: list[str], allowed: set[str] | None):
        data, info = h.load_sources(reports, allowed)
        return data, _sources_info(info)

    def _scope_fn(allowed: set[str] | None) -> Callable[[str], bool]:
        return lambda email: h.in_scope(email, allowed)

    def _filter(rows: list[dict[str, Any]], key: str, wanted: str | None) -> list[dict[str, Any]]:
        if not wanted:
            return rows
        w = wanted.strip().upper()
        return [r for r in rows if str(r.get(key, "")).upper() == w]

    def _min_level(rows: list[dict[str, Any]], min_level: str | None) -> list[dict[str, Any]]:
        if not min_level:
            return rows
        floor = LEVEL_ORDER.get(min_level.strip().upper())
        if floor is None:
            raise HTTPException(400, {"error": "invalid_level", "allowed": list(LEVEL_ORDER)})
        return [r for r in rows if LEVEL_ORDER[r["risk_level"]] >= floor]

    @router.get("/status")
    def module_status(request: Request) -> dict:
        scope = h.request_scope(request)
        return {
            "module": "security_audit",
            "version": h.app_version,
            "mode": "read_only",
            "actions": {"mode": "plan_only", "executes_gam": False,
                        "plan_endpoint": "POST /api/v1/security/actions/plan (super-admin only)"},
            "superadmin": bool(scope.get("superadmin")),
            "thresholds": {
                "critical": LEVEL_CRITICAL, "high": LEVEL_HIGH, "medium": LEVEL_MEDIUM,
                "inactive_days": INACTIVE_DAYS, "failed_login_7d": FAILED_LOGIN_7D,
                "device_stale_days": DEVICE_STALE_DAYS, "patch_max_age_days": PATCH_MAX_AGE_DAYS,
            },
            "limits": [
                "Drive exposure (public / external sharing) is NOT available: it needs a new allowlisted read-only GAM report. See docs/SECURITY_AUDIT.md.",
                "OAuth scoring needs scope columns in the token_activity CSV; if absent apps are reported as UNKNOWN (see scope_data_present).",
                "token_activity covers 30 days and login_activity 180 days; results are only as fresh as the cached runs (see sources.*.cached_at).",
                "Scores are heuristics for triage, not a Google risk rating.",
            ],
        }

    @router.get("/oauth")
    def oauth(
        request: Request,
        domain: list[str] | None = Query(None),
        domains: str | None = Query(None),
        min_level: str | None = Query(None),
        limit: int = Query(200, ge=1, le=2000),
    ) -> dict:
        allowed, _ = h.insight_scope(request, domain, domains)
        data, src = _load(SOURCES_OAUTH, allowed)
        res = analyse_oauth(data.get("token_activity", []), _scope_fn(allowed), h.row_email)
        apply_approvals(res["rows"], load_approvals(approvals_path), _now())
        rows = _min_level(res["rows"], min_level)
        return {
            "as_of": _now().isoformat(), "scope": sorted(allowed) if allowed else ["all"],
            "scope_data_present": res["scope_data_present"], "total": len(rows),
            "by_level": dict(Counter(r["risk_level"] for r in rows)), "rows": rows[:limit], "sources": src,
        }

    @router.get("/users")
    def users(
        request: Request,
        domain: list[str] | None = Query(None),
        domains: str | None = Query(None),
        flag: str | None = Query(None),
        limit: int = Query(500, ge=1, le=5000),
    ) -> dict:
        allowed, _ = h.insight_scope(request, domain, domains)
        data, src = _load(SOURCES_USERS, allowed)
        res = analyse_users(data.get("users_full", []), data.get("login_activity", []), data.get("admins", []),
                            _scope_fn(allowed), h.row_email, _now())
        rows = res["rows"]
        if flag:
            f = flag.strip().upper()
            rows = [r for r in rows if f in r["flags"]]
        all_flags = Counter(f for r in res["rows"] for f in r["flags"])
        return {
            "as_of": _now().isoformat(), "scope": sorted(allowed) if allowed else ["all"],
            "admins_known": res["admins_known"], "total": len(rows), "by_flag": dict(all_flags),
            "by_level": dict(Counter(r["risk_level"] for r in rows)), "rows": rows[:limit], "sources": src,
        }

    @router.get("/devices")
    def devices(
        request: Request,
        domain: list[str] | None = Query(None),
        domains: str | None = Query(None),
        status: str | None = Query(None),
        limit: int = Query(500, ge=1, le=5000),
    ) -> dict:
        allowed, _ = h.insight_scope(request, domain, domains)
        data, src = _load(SOURCES_DEVICES, allowed)
        res = analyse_devices(h.build_device_rows(data, allowed), _now())
        rows = _filter(res["rows"], "compliance_status", status)
        return {
            "as_of": _now().isoformat(), "scope": sorted(allowed) if allowed else ["all"],
            "total": len(rows), "all_devices": len(res["rows"]), "by_status": res["by_status"],
            "by_flag": res["by_flag"], "rows": rows[:limit], "sources": src,
        }

    @router.get("/summary")
    def summary(
        request: Request,
        domain: list[str] | None = Query(None),
        domains: str | None = Query(None),
    ) -> dict:
        allowed, _ = h.insight_scope(request, domain, domains)
        now = _now()
        reports = sorted(set(SOURCES_OAUTH + SOURCES_USERS + SOURCES_DEVICES))
        data, src = _load(reports, allowed)
        o = analyse_oauth(data.get("token_activity", []), _scope_fn(allowed), h.row_email)
        apply_approvals(o["rows"], load_approvals(approvals_path), _now())
        u = analyse_users(data.get("users_full", []), data.get("login_activity", []), data.get("admins", []),
                          _scope_fn(allowed), h.row_email, now)
        d = analyse_devices(h.build_device_rows(data, allowed), now)
        return {
            "as_of": now.isoformat(), "scope": sorted(allowed) if allowed else ["all"],
            "oauth": {"apps": len(o["rows"]), "by_level": dict(Counter(r["risk_level"] for r in o["rows"])),
                      "scope_data_present": o["scope_data_present"]},
            "users": {"flagged": len(u["rows"]),
                      "by_flag": dict(Counter(f for r in u["rows"] for f in r["flags"])),
                      "admins_known": u["admins_known"]},
            "devices": {"total": len(d["rows"]), "by_status": d["by_status"], "by_flag": d["by_flag"]},
            "drive": {"available": False, "reason": "needs a new allowlisted read-only GAM report (see docs/SECURITY_AUDIT.md)"},
            "sources": src,
        }

    @router.get("/approved-apps")
    def approved_apps(request: Request) -> dict:
        scope = h.request_scope(request)
        a = load_approvals(approvals_path)
        return {"can_edit": bool(scope.get("superadmin")), "default_review_days": APPROVAL_DEFAULT_DAYS,
                "rows": [{"key": k, **v} for k, v in sorted(a.items())]}

    @router.post("/approved-apps")
    def approve_app(body: ApproveAppIn, request: Request) -> dict:
        scope = h.request_scope(request)
        if not scope.get("superadmin"):
            raise HTTPException(403, {"error": "superadmin_only"})
        allowed, _ = h.insight_scope(request, None, None)
        data, _src = _load(SOURCES_OAUTH, allowed)
        res = analyse_oauth(data.get("token_activity", []), _scope_fn(allowed), h.row_email)
        row = next((r for r in res["rows"] if app_key(r) == body.key), None)
        if not row:
            raise HTTPException(404, {"error": "unknown_app", "detail": "app not seen in the cached token_activity report"})
        if row["risk_level"] == "UNKNOWN":
            raise HTTPException(422, {"error": "scopes_unknown", "detail": "cannot approve an app whose permissions are not visible in the data"})
        who = str(scope.get("real_email") or scope.get("email") or "local")
        rec = record_approval(approvals_path, audit_dir, body.key, row, body.note, who, body.review_days, _now())
        return {"ok": True, "approved": {"key": body.key, **rec}}

    @router.delete("/approved-apps")
    def unapprove_app(request: Request, key: str = Query(..., min_length=1, max_length=256)) -> dict:
        scope = h.request_scope(request)
        if not scope.get("superadmin"):
            raise HTTPException(403, {"error": "superadmin_only"})
        with _AUDIT_LOCK:
            cur = load_approvals(approvals_path)
            if key not in cur:
                raise HTTPException(404, {"error": "not_approved"})
            cur.pop(key)
            tmp = approvals_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cur, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, approvals_path)
            audit_dir.mkdir(parents=True, exist_ok=True)
            with (audit_dir / "approvals.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": _now().isoformat(), "action": "remove", "key": key,
                                     "by": scope.get("real_email") or scope.get("email") or "local"}) + "\n")
        return {"ok": True}

    @router.post("/actions/plan")
    def action_plan(body: ActionPlanIn, request: Request) -> dict:
        scope = h.request_scope(request)
        if not scope.get("superadmin"):
            raise HTTPException(403, {"error": "superadmin_only", "detail": "Action plans are restricted to super-admins"})
        steps = build_plan(body, h.is_superadmin)
        record = {
            "ts": _now().isoformat(), "actor": scope.get("real_email") or scope.get("email") or "local",
            "request": body.model_dump(), "steps": steps, "executed": False,
        }
        try:
            with _AUDIT_LOCK:
                audit_dir.mkdir(parents=True, exist_ok=True)
                with (audit_dir / f"plans-{_now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            logged = True
        except OSError:
            logged = False
        return {"executed": False, "dry_run": True, "audit_logged": logged, "note": PLAN_NOTE, "steps": steps}

    return router

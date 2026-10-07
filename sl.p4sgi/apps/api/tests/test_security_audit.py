"""
Tests for app/security_audit.py (0.4.19). No Postgres, no GAM, no network.

Run (from sl.p4sgi/apps/api):
    pip install pytest httpx
    python -m pytest tests -q
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# DATA_DIR is read at import time by app.main -> point it at a temp dir BEFORE importing.
_TMP = Path(tempfile.mkdtemp(prefix="p4sgi-sec-test-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ.setdefault("SUPERADMIN_EMAILS", "geb@p4sgi.com")
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "1")  # nothing listens: DB lookups fail fast and are swallowed by the app
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import security_audit as sa  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _email(row, *cols):
    for c in cols:
        v = (row.get(c) or "").strip().lower()
        if "@" in v:
            return v.split()[0]
    return ""


ALL = lambda e: True  # noqa: E731


# ---------------------------------------------------------------- scoring ---
def test_levels():
    assert sa.level_for(None) == "UNKNOWN"
    assert sa.level_for(0) == "LOW" and sa.level_for(39) == "LOW"
    assert sa.level_for(40) == "MEDIUM" and sa.level_for(59) == "MEDIUM"
    assert sa.level_for(60) == "HIGH" and sa.level_for(84) == "HIGH"
    assert sa.level_for(85) == "CRITICAL" and sa.level_for(100) == "CRITICAL"


def test_full_gmail_plus_full_drive_is_critical():
    score, reasons = sa.score_oauth_app(["https://mail.google.com/", "https://www.googleapis.com/auth/drive"], 2)
    assert sa.level_for(score) == "CRITICAL"
    assert any("Full Gmail" in r for r in reasons)


def test_limited_drive_file_is_low_and_identity_only_is_zero():
    s1, _ = sa.score_oauth_app(["https://www.googleapis.com/auth/drive.file"], 3)
    assert sa.level_for(s1) == "LOW"
    s2, _ = sa.score_oauth_app(["openid", "https://www.googleapis.com/auth/userinfo.email"], 3)
    assert s2 == 0


def test_drive_full_is_not_confused_with_drive_file():
    assert sa.scope_points("https://www.googleapis.com/auth/drive")[0] == 60
    assert sa.scope_points("https://www.googleapis.com/auth/drive.file")[0] == 5
    assert sa.scope_points("https://www.googleapis.com/auth/drive.readonly")[0] == 20


def test_directory_write_vs_readonly():
    assert sa.scope_points("https://www.googleapis.com/auth/admin.directory.user")[0] == 60
    assert sa.scope_points("https://www.googleapis.com/auth/admin.directory.user.readonly")[0] == 20


def test_no_scopes_is_unknown_not_low():
    score, _ = sa.score_oauth_app([], 5)
    assert score is None and sa.level_for(score) == "UNKNOWN"


def test_broad_reach_bonus_only_when_already_risky():
    base, _ = sa.score_oauth_app(["https://www.googleapis.com/auth/calendar"], 1)
    wide, _ = sa.score_oauth_app(["https://www.googleapis.com/auth/calendar"], 20)
    assert wide == base + 10
    tiny, _ = sa.score_oauth_app(["https://www.googleapis.com/auth/drive.file"], 50)
    assert tiny == 5


def test_extract_scopes_handles_multiple_column_shapes():
    row = {"scope": "https://mail.google.com/ openid", "scope.1": "https://www.googleapis.com/auth/drive", "other": "http://x"}
    assert set(sa.extract_scopes(row)) == {"https://mail.google.com/", "openid", "https://www.googleapis.com/auth/drive"}


# ---------------------------------------------------------------- parsing ---
def test_parse_ts_variants():
    assert sa.parse_ts("Never") is None and sa.parse_ts("") is None and sa.parse_ts("garbage") is None
    assert sa.parse_ts("2026-09-30T10:16:00.000Z").year == 2026
    assert sa.parse_ts("2026-09-30").month == 9
    assert sa.parse_ts("1788000000000").year >= 2026  # epoch ms
    assert sa.parse_ts("0") is None


# ------------------------------------------------------------------ oauth ---
def test_analyse_oauth_aggregates_scopes_and_respects_scope_filter():
    ev = [
        {"actor.email": "a@sl.p4sgi.com", "app_name": "MailTool", "client_id": "c1", "name": "authorize",
         "id.time": iso(2), "scope": "https://mail.google.com/ https://www.googleapis.com/auth/drive"},
        {"actor.email": "b@zw.p4sgi.com", "app_name": "MailTool", "client_id": "c1", "name": "activity", "id.time": iso(1),
         "scope": "https://mail.google.com/"},
        {"actor.email": "a@sl.p4sgi.com", "app_name": "Quiz", "client_id": "c2", "name": "activity", "id.time": iso(5),
         "scope": "https://www.googleapis.com/auth/drive.file"},
    ]
    allowed = {"sl.p4sgi.com"}
    res = sa.analyse_oauth(ev, lambda e: e.split("@")[1] in allowed, _email)
    assert res["scope_data_present"] is True
    names = [r["app_name"] for r in res["rows"]]
    assert names == ["MailTool", "Quiz"]  # worst first
    mail = res["rows"][0]
    assert mail["user_count"] == 1 and "b@zw.p4sgi.com" not in mail["users"]  # out-of-scope user not leaked
    assert mail["risk_level"] == "CRITICAL"


def test_analyse_oauth_without_scope_columns_is_unknown():
    ev = [{"actor.email": "a@sl.p4sgi.com", "app_name": "X", "client_id": "c", "name": "authorize", "id.time": iso(1)}]
    res = sa.analyse_oauth(ev, ALL, _email)
    assert res["scope_data_present"] is False
    assert res["rows"][0]["risk_level"] == "UNKNOWN" and res["rows"][0]["risk_score"] is None


# ------------------------------------------------------------------ users ---
def test_analyse_users_flags():
    users = [
        {"primaryEmail": "never@sl.p4sgi.com", "creationTime": iso(100), "lastLoginTime": "Never", "suspended": "False", "isEnrolledIn2Sv": "False"},
        {"primaryEmail": "newacct@sl.p4sgi.com", "creationTime": iso(5), "lastLoginTime": "Never", "suspended": "False", "isEnrolledIn2Sv": "False"},
        {"primaryEmail": "no2sv@sl.p4sgi.com", "creationTime": iso(400), "lastLoginTime": iso(3), "suspended": "False", "isEnrolledIn2Sv": "False"},
        {"primaryEmail": "adm@sl.p4sgi.com", "creationTime": iso(400), "lastLoginTime": iso(3), "suspended": "False", "isEnrolledIn2Sv": "False"},
        {"primaryEmail": "idle@sl.p4sgi.com", "creationTime": iso(400), "lastLoginTime": iso(120), "suspended": "False", "isEnrolledIn2Sv": "True"},
        {"primaryEmail": "susp@sl.p4sgi.com", "creationTime": iso(400), "lastLoginTime": iso(200), "suspended": "True", "isEnrolledIn2Sv": "False"},
        {"primaryEmail": "fine@sl.p4sgi.com", "creationTime": iso(400), "lastLoginTime": iso(2), "suspended": "False", "isEnrolledIn2Sv": "True"},
        {"primaryEmail": "brute@sl.p4sgi.com", "creationTime": iso(400), "lastLoginTime": iso(1), "suspended": "False", "isEnrolledIn2Sv": "True"},
    ]
    logins = [{"name": "login_failure", "actor.email": "brute@sl.p4sgi.com", "id.time": iso(1)} for _ in range(6)]
    logins.append({"name": "login_failure", "actor.email": "fine@sl.p4sgi.com", "id.time": iso(40)})  # old: ignored
    admins = [{"primaryEmail": "adm@sl.p4sgi.com"}]
    res = sa.analyse_users(users, logins, admins, ALL, _email, NOW)
    by = {r["email"]: r for r in res["rows"]}
    assert by["never@sl.p4sgi.com"]["flags"] == ["NEVER_LOGGED_IN"]
    assert "newacct@sl.p4sgi.com" not in by  # younger than the grace period
    assert by["no2sv@sl.p4sgi.com"]["flags"] == ["NO_2SV"]
    assert by["adm@sl.p4sgi.com"]["flags"] == ["ADMIN_NO_2SV"]
    assert by["idle@sl.p4sgi.com"]["flags"] == ["INACTIVE"]
    assert "susp@sl.p4sgi.com" not in by  # suspended accounts are not re-flagged
    assert "fine@sl.p4sgi.com" not in by
    assert by["brute@sl.p4sgi.com"]["flags"] == ["SUSPICIOUS_LOGINS"] and by["brute@sl.p4sgi.com"]["failed_logins_7d"] == 6
    assert by["no2sv@sl.p4sgi.com"]["risk_level"] == "MEDIUM"
    assert by["adm@sl.p4sgi.com"]["risk_level"] == "HIGH"
    assert by["brute@sl.p4sgi.com"]["risk_level"] == "HIGH"
    assert by["never@sl.p4sgi.com"]["risk_level"] == "LOW"
    assert by["idle@sl.p4sgi.com"]["risk_level"] == "MEDIUM"
    assert res["admins_known"] is True
    assert res["rows"][0]["risk_score"] >= res["rows"][-1]["risk_score"]


# ---------------------------------------------------------------- devices ---
def test_device_flags_and_compliance():
    bad = {"encryption": "NOT_ENCRYPTED", "developer_mode": "true", "adb": "true", "unknown_sources": "true",
           "password_state": "PASSWORD_NOT_SET", "compromised": "Compromised", "security_patch": "2023-01-05",
           "last_sync": iso(90)}
    flags = sa.device_flags(bad, NOW)
    assert set(flags) == set(sa._DEVICE_FLAG_POINTS)
    good = {"encryption": "ENCRYPTED", "developer_mode": "false", "adb": "false", "unknown_sources": "false",
            "password_state": "PASSWORD_SET", "compromised": "Undetected", "security_patch": iso(30), "last_sync": iso(1)}
    assert sa.device_flags(good, NOW) == []
    unknown = {"encryption": "", "developer_mode": "", "adb": "", "password_state": "", "compromised": ""}
    assert sa.device_flags(unknown, NOW) == []  # unknown is not treated as non-compliant


def test_analyse_devices_status_buckets():
    rows = [
        {"email": "a@x.com", "device_id": "1", "encryption": "ENCRYPTED"},
        {"email": "b@x.com", "device_id": "2", "encryption": "UNENCRYPTED"},  # 100-30 = 70
        {"email": "c@x.com", "device_id": "3", "compromised": "COMPROMISED", "encryption": "UNENCRYPTED"},  # 10
        {"email": "d@x.com", "device_id": "4", "encryption": "UNENCRYPTED", "developer_mode": "true"},  # 55 -> below 60
    ]
    res = sa.analyse_devices(rows, NOW)
    st = {r["device_id"]: r["compliance_status"] for r in res["rows"]}
    assert st == {"1": "COMPLIANT", "2": "WARNING", "3": "CRITICAL", "4": "CRITICAL"}
    assert res["rows"][0]["device_id"] == "3"  # worst first


# ---------------------------------------------------------------- planning ---
def _plan(**kw):
    return sa.build_plan(sa.ActionPlanIn(**kw), lambda e: (e or "").lower() == "geb@p4sgi.com")


def test_plan_revoke_token_quotes_arguments():
    steps = _plan(action="revoke_token", email="jane@sl.p4sgi.com", client_id="123-abc.apps.googleusercontent.com")
    assert steps[0]["command"] == "gam user jane@sl.p4sgi.com delete token clientid 123-abc.apps.googleusercontent.com"


@pytest.mark.parametrize("bad", ["x@y.com; rm -rf /", "x@y.com $(id)", "a b@y.com", "no-at-sign", "x@y.com`id`", "x@y.com\nid"])
def test_plan_rejects_injection_in_email(bad):
    with pytest.raises(Exception) as ei:
        _plan(action="offboard_user", email=bad)
    assert getattr(ei.value, "status_code", None) == 422


def test_plan_rejects_bad_client_id_and_ou_and_protected_account():
    with pytest.raises(Exception):
        _plan(action="revoke_token", email="a@b.com", client_id="x; reboot")
    with pytest.raises(Exception):
        _plan(action="offboard_user", email="a@b.com", archive_ou="/Archived'; drop")
    with pytest.raises(Exception) as ei:
        _plan(action="offboard_user", email="geb@p4sgi.com")
    assert "super-admin" in str(ei.value.detail)


def test_plan_offboard_steps_in_order():
    steps = _plan(action="offboard_user", email="jane@sl.p4sgi.com", transfer_drive_to="boss@sl.p4sgi.com")
    cmds = [s["command"] for s in steps]
    assert cmds[0].endswith("suspended on") and "signout" in cmds[1] and "transfer drive" in cmds[2] and cmds[3].endswith("ou /Archived")


def test_plan_wipe_defaults_to_account_wipe_only():
    assert _plan(action="wipe_mobile_device", resource_id="abc123")[0]["command"].endswith("action admin_account_wipe")
    assert _plan(action="wipe_mobile_device", resource_id="abc123", full_wipe=True)[0]["command"].endswith("action admin_remote_wipe")
    with pytest.raises(Exception):
        _plan(action="wipe_mobile_device")


# ------------------------------------------------- HTTP integration (app) ---
def _write_run(report: str, csv_text: str, stamp: str = "20261007T080000Z") -> None:
    runs = _TMP / "gam-out" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{stamp}_{report}.csv").write_text(csv_text, encoding="utf-8")
    (runs / f"{stamp}_{report}.meta.json").write_text(json.dumps(
        {"report": report, "status": "ok", "created_at": "2026-10-07T08:00:00Z", "domains": [],
         "filename": f"{stamp}_{report}.csv"}), encoding="utf-8")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    _write_run("token_activity",
               "actor.email,id.time,name,app_name,client_id,scope\n"
               f"a@sl.p4sgi.com,{iso(2)},authorize,MailTool,c1,https://mail.google.com/ https://www.googleapis.com/auth/drive\n"
               f"b@zw.p4sgi.com,{iso(2)},authorize,ZwApp,c9,https://www.googleapis.com/auth/calendar\n")
    _write_run("users_full",
               "primaryEmail,name.fullName,orgUnitPath,creationTime,lastLoginTime,suspended,isEnrolledIn2Sv,isEnforcedIn2Sv\n"
               f"a@sl.p4sgi.com,A,/SL,{iso(300)},{iso(2)},False,False,False\n"
               f"b@zw.p4sgi.com,B,/ZW,{iso(300)},{iso(2)},False,False,False\n")
    _write_run("devices_mobile",
               "deviceId,email,model,os,type,status,lastSync,encryptionStatus,developerOptionsStatus,adbStatus,"
               "unknownSourcesStatus,devicePasswordStatus,securityPatchLevel,serialNumber\n"
               f"d1,a@sl.p4sgi.com,SM-X216B,Android 14,ANDROID,APPROVED,{iso(1)},NOT_ENCRYPTED,true,false,false,PASSWORD_SET,2024-01-01,SER1\n"
               f"d2,b@zw.p4sgi.com,SM-X216B,Android 14,ANDROID,APPROVED,{iso(1)},ENCRYPTED,false,false,false,PASSWORD_SET,{iso(10)[:10]},SER2\n")
    from app.main import app

    return TestClient(app)  # no context manager: startup (DB seeding) is intentionally not run


SL = {"X-Goog-Authenticated-User-Email": "accounts.google.com:ht@sl.p4sgi.com"}
SUPER = {"X-Goog-Authenticated-User-Email": "accounts.google.com:geb@p4sgi.com"}


def test_status_endpoint_declares_read_only(client):
    r = client.get("/api/v1/security/status", headers=SL)
    assert r.status_code == 200
    j = r.json()
    assert j["mode"] == "read_only" and j["actions"]["executes_gam"] is False and j["superadmin"] is False
    assert any("Drive exposure" in s for s in j["limits"])


def test_oauth_superadmin_sees_all_domains_scoped_user_sees_own(client):
    allr = client.get("/api/v1/security/oauth", headers=SUPER).json()
    assert {x["app_name"] for x in allr["rows"]} == {"MailTool", "ZwApp"}
    own = client.get("/api/v1/security/oauth", headers=SL).json()
    assert [x["app_name"] for x in own["rows"]] == ["MailTool"]
    assert own["rows"][0]["risk_level"] == "CRITICAL"
    assert own["sources"]["token_activity"]["status"] == "ok"


def test_users_and_devices_are_domain_scoped(client):
    u = client.get("/api/v1/security/users", headers=SL).json()
    assert [r["email"] for r in u["rows"]] == ["a@sl.p4sgi.com"] and u["rows"][0]["flags"] == ["NO_2SV"]
    d = client.get("/api/v1/security/devices", headers=SL).json()
    assert [r["device_id"] for r in d["rows"]] == ["d1"]
    assert {"UNENCRYPTED", "DEVELOPER_MODE"} <= set(d["rows"][0]["flags"])
    d_all = client.get("/api/v1/security/devices", headers=SUPER).json()
    assert d_all["all_devices"] == 2


def test_summary_reports_drive_unavailable_and_missing_sources(client):
    j = client.get("/api/v1/security/summary", headers=SUPER).json()
    assert j["drive"]["available"] is False
    assert j["sources"]["login_activity"]["status"] != "ok" and "run the 'login_activity'" in j["sources"]["login_activity"]["hint"]
    assert j["oauth"]["apps"] == 2 and j["devices"]["total"] == 2


def test_missing_cache_degrades_gracefully(client):
    r = client.get("/api/v1/security/users?flag=NOPE", headers=SUPER)
    assert r.status_code == 200 and r.json()["rows"] == []


def test_bad_params_rejected(client):
    assert client.get("/api/v1/security/oauth?min_level=bogus", headers=SUPER).status_code == 400
    assert client.get("/api/v1/security/oauth?limit=0", headers=SUPER).status_code == 422


def test_action_plan_is_superadmin_only_and_never_executes(client):
    cid = "1234567890-abcdef.apps.googleusercontent.com"
    body = {"action": "revoke_token", "email": "a@sl.p4sgi.com", "client_id": cid, "reason": "test"}
    assert client.post("/api/v1/security/actions/plan", json=body, headers=SL).status_code == 403
    r = client.post("/api/v1/security/actions/plan", json=body, headers=SUPER)
    assert r.status_code == 200
    j = r.json()
    assert j["executed"] is False and j["dry_run"] is True and j["audit_logged"] is True
    assert j["steps"][0]["command"] == f"gam user a@sl.p4sgi.com delete token clientid {cid}"
    logs = list((_TMP / "security" / "audit").glob("plans-*.jsonl"))
    assert logs and json.loads(logs[0].read_text().splitlines()[0])["executed"] is False


def test_action_plan_rejects_malicious_input_over_http(client):
    r = client.post("/api/v1/security/actions/plan",
                    json={"action": "offboard_user", "email": "x@y.com; rm -rf /"}, headers=SUPER)
    assert r.status_code == 422


def test_existing_routes_still_present(client):
    paths = {getattr(r, "path", "") for r in client.app.routes}
    for p in ("/api/v1/health", "/api/v1/devices", "/api/v1/telemetry", "/api/v1/gam/reports",
              "/api/v1/whoami", "/api/v1/insights/summary", "/api/v1/export/{section}"):
        assert p in paths, p
    assert client.get("/api/v1/whoami", headers=SL).status_code == 200

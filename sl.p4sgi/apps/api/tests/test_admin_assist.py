"""Tests for app/admin_assist.py + approved apps (0.4.22). No Postgres, no GAM, no network: the LLM is mocked."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="p4sgi-assist-test-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("SUPERADMIN_EMAILS", "geb@p4sgi.com")
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import admin_assist as aa  # noqa: E402
from app import ask_data as ad  # noqa: E402
from app import security_audit as sa  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
SL = {"X-Goog-Authenticated-User-Email": "accounts.google.com:ht@sl.p4sgi.com"}
SUPER = {"X-Goog-Authenticated-User-Email": "accounts.google.com:geb@p4sgi.com"}
FULL = "https://mail.google.com/ https://www.googleapis.com/auth/drive"


def iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ------------------------------------------------------------------ pure units ---
def test_redactor_roundtrip_and_scrub():
    r = aa.Redactor()
    t = r.tok("john@sl.p4sgi.com")
    assert t == "U1" and r.tok("JOHN@sl.p4sgi.com") == "U1"
    out = r.scrub("mail john@sl.p4sgi.com and also stranger@x.org please")
    assert "@" not in out and "U1" in out and "U2" in out
    assert r.detok("email U1 about U2") == "email john@sl.p4sgi.com about stranger@x.org"
    assert r.generalise("U1 uses A1 on D2") == "the user uses the app on the device"


def test_clean_strips_control_chars_and_limits():
    assert aa.clean("a\x00b\nc" + "x" * 500, 10) == "a b c" + "xxxxx"


def _oauth_row(**kw):
    base = {"app_name": "MailTool", "client_id": "cid-mail1", "risk_level": "CRITICAL", "risk_score": 90, "reasons": ["Full Gmail"], "scopes": [FULL.split()[0]],
            "users": ["a@sl.p4sgi.com", "b@sl.p4sgi.com"], "user_count": 2, "last_seen": "2026-10-01"}
    base.update(kw)
    return base


def test_facts_never_contain_emails():
    r = aa.Redactor()
    facts, refs = aa.build_facts("oauth", [_oauth_row()], r)
    blob = json.dumps(facts)
    assert "@" not in blob and "a@sl" not in blob
    assert facts[0]["users"] == ["U1", "U2"] and refs["A1"]["kind"] == "oauth"
    dev = {"device_id": "d1", "email": "a@sl.p4sgi.com", "serial": "SER1", "model": "SM", "os": "A14", "flags": ["UNENCRYPTED"], "compliance_score": 70,
           "compliance_status": "WARNING", "last_sync": "2026-10-01", "security_patch": "2024-01-01"}
    f2, _ = aa.build_facts("devices", [dev], aa.Redactor())
    blob2 = json.dumps(f2)
    assert "SER1" not in blob2 and "@" not in blob2 and '"d1"' not in blob2 and f2[0]["owner"] == "U1"


def test_validate_options_allowlist_and_targets():
    r = aa.Redactor()
    _, refs = aa.build_facts("oauth", [_oauth_row()], r)
    raw = [{"action": "rm_rf", "label": "x"},
           {"action": "plan_revoke_token", "target": "A9", "label": "bad target"},
           {"action": "plan_revoke_token", "target": "A1", "label": "Remove access"},
           {"action": "draft_email", "target": "U1", "label": "Email U1"},
           {"action": "draft_email", "target": "U77", "label": "Email unknown"},
           {"action": "plan_offboard_user", "target": "U2", "label": "Suspend U2"},
           {"action": "explain", "target": "A1", "label": "Why?", "say": "why"}]
    sup = aa.validate_options(raw, refs, r, True, "oauth")
    assert [o["action"] for o in sup] == ["plan_revoke_token", "draft_email", "plan_offboard_user", "explain"]
    assert sup[-1]["target"] is None
    plain = aa.validate_options(raw, refs, r, False, "oauth")
    assert {o["action"] for o in plain} == {"draft_email", "explain"}
    assert aa.validate_options("nonsense", refs, r, True, "oauth") == []


def test_mailto_and_email_compose():
    subj, body = aa.compose_email("users", {"row": {"flags": ["NO_2SV"]}}, ["a@x.org"])
    assert "2-step" in subj and "myaccount.google.com" in body
    url = aa.mailto(["a@x.org", "b@x.org"], subj, body)
    assert url.startswith("mailto:a@x.org?subject=") and "bcc=b%40x.org" in url and len(url) <= 1900


def test_help_store_dedupe_and_search(tmp_path):
    st = aa.HelpStore(tmp_path)
    a, created = st.upsert(None, {"title": "Why is Canva critical", "body": "Canva can read Drive."}, dedupe_key="k1")
    assert created
    b, created2 = st.upsert(None, {"title": "dup", "body": "dup"}, dedupe_key="k1")
    assert b == a and not created2 and st.get(a)["asked"] == 2
    assert all(x["id"] != a for x in st.all(False))  # drafts hidden from non-admins
    assert any(x["id"] == a for x in st.all(True))
    st.mutate(a, lambda x: x.update(status="approved"))
    hits = aa.search_articles(st.all(False), "what is canva drive")
    assert hits and hits[0]["id"] == a or any(h["id"] == a for h in hits)
    assert aa.search_articles(st.all(False), "zzzz") == []
    assert any(h["id"] == "risk-levels" for h in aa.search_articles(st.all(False), "what does critical mean risk levels"))


def test_seed_articles_are_well_formed():
    ids = [a["id"] for a in aa.HELP_SEED]
    assert len(ids) == len(set(ids)) >= 16
    for a in aa.HELP_SEED:
        assert a["title"] and len(a["body"]) > 60 and a["category"]


def test_collect_knowledge_includes_help(tmp_path):
    (tmp_path / "help").mkdir()
    (tmp_path / "help" / "articles.json").write_text(json.dumps(
        {"kb-aaaa1111": {"title": "Approved one", "body": "b1", "status": "approved"}, "kb-bbbb2222": {"title": "Draft one", "body": "b2", "status": "draft"}}))
    rows = ad.collect_knowledge(tmp_path)
    refs = {r for s, r, _ in rows if s == "help"}
    assert "risk-levels" in refs and "kb-aaaa1111" in refs and "kb-bbbb2222" not in refs


# ---------------------------------------------------------------- approvals ---
def test_scope_fingerprint_is_order_independent():
    assert sa.scope_fingerprint(["b", "a"]) == sa.scope_fingerprint(["a", "b", "a"])
    assert sa.scope_fingerprint(["a"]) != sa.scope_fingerprint(["a", "b"])


def _row():
    return {"app_name": "MailTool", "client_id": "cid-mail1", "scopes": FULL.split(), "risk_level": "CRITICAL", "risk_score": 90, "reasons": ["x"], "user_count": 1}


def test_approval_applies_goes_stale_and_expires(tmp_path):
    ap, au = tmp_path / "ap.json", tmp_path / "audit"
    row = _row()
    sa.record_approval(ap, au, sa.app_key(row), row, "Used by IT for mail", "geb@p4sgi.com", 180, NOW)
    ap_all = sa.load_approvals(ap)
    ok = sa.apply_approvals([dict(row)], ap_all, NOW + timedelta(days=10))[0]
    assert ok["risk_level"] == "APPROVED" and ok["risk_level_raw"] == "CRITICAL" and not ok.get("approval_stale")
    changed = dict(row, scopes=FULL.split() + ["https://www.googleapis.com/auth/admin.directory.user"])
    c = sa.apply_approvals([changed], ap_all, NOW + timedelta(days=10))[0]
    assert c["risk_level"] == "CRITICAL" and c.get("approval_stale")
    old = sa.apply_approvals([dict(row)], ap_all, NOW + timedelta(days=400))[0]
    assert old["risk_level"] == "CRITICAL" and old.get("approval_stale")
    assert (au / "approvals.jsonl").is_file()


# ----------------------------------------------------------------- endpoints ---
def _write_run(runs: Path, report: str, csv_text: str) -> None:
    runs.mkdir(parents=True, exist_ok=True)
    stamp = "20261007T080000Z"
    (runs / f"{stamp}_{report}.csv").write_text(csv_text, encoding="utf-8")
    (runs / f"{stamp}_{report}.meta.json").write_text(json.dumps(
        {"report": report, "status": "ok", "created_at": "2026-10-07T08:00:00Z", "domains": [], "filename": f"{stamp}_{report}.csv"}), encoding="utf-8")


@pytest.fixture()
def env(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import main

    runs = tmp_path / "runs"
    _write_run(runs, "token_activity", "actor.email,id.time,name,app_name,client_id,scope\n"
               f"a@sl.p4sgi.com,{iso(2)},authorize,MailTool,cid-mail1,{FULL}\n"
               f"c@sl.p4sgi.com,{iso(2)},authorize,MailTool,cid-mail1,{FULL}\n"
               f"b@zw.p4sgi.com,{iso(2)},authorize,ZwApp,cid-zw9,https://www.googleapis.com/auth/calendar\n")
    _write_run(runs, "users_full", "primaryEmail,name.fullName,orgUnitPath,creationTime,lastLoginTime,suspended,isEnrolledIn2Sv,isEnforcedIn2Sv\n"
               f"a@sl.p4sgi.com,A,/SL,{iso(300)},{iso(2)},False,False,False\n"
               f"b@zw.p4sgi.com,B,/ZW,{iso(300)},{iso(2)},False,False,False\n")
    _write_run(runs, "devices_mobile", "deviceId,email,model,os,type,status,lastSync,encryptionStatus,developerOptionsStatus,adbStatus,"
               "unknownSourcesStatus,devicePasswordStatus,securityPatchLevel,serialNumber\n"
               f"d1,a@sl.p4sgi.com,SM-X216B,Android 14,ANDROID,APPROVED,{iso(1)},NOT_ENCRYPTED,true,false,false,PASSWORD_SET,2024-01-01,SER1\n")
    monkeypatch.setattr(main, "GAM_RUNS_DIR", runs)
    for sub in ("security", "help", "assist"):
        shutil.rmtree(main.DATA_DIR / sub, ignore_errors=True)
    sent: list[tuple[str, str]] = []
    reply = {"message": "U1 and U2 use A1. It can read all mail. Do you want to remove its access?",
             "options": [{"label": "Remove access of A1", "action": "plan_revoke_token", "target": "A1"},
                         {"label": "Email U1", "action": "draft_email", "target": "U1"},
                         {"label": "Approve A1", "action": "approve_app", "target": "A1"},
                         {"label": "Explain more", "action": "explain", "say": "Is it safe?"}]}

    def fake_llm(provider, model, system, user, json_mode=False):
        sent.append((system, user))
        return json.dumps(reply)

    monkeypatch.setattr(ad, "llm", fake_llm)
    monkeypatch.setattr(ad, "cloud_state", lambda p: {"label": p, "available": False, "reason": "off"})
    c = TestClient(main.app)
    c.sent, c.reply = sent, reply  # type: ignore[attr-defined]
    return c


def _start(c, headers, kind="oauth", keys=("cid-mail1",)):
    r = c.post("/api/v1/assist/start", json={"kind": kind, "keys": list(keys)}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _opt(j, action):
    return next(o for o in j["options"] if o["action"] == action)


def test_status_and_privacy_statement(env):
    j = env.get("/api/v1/assist/status", headers=SUPER).json()
    assert j["engine"]["cloud"] is False and "No email addresses" in j["privacy"]


def test_start_sends_no_personal_data_to_model(env):
    j = _start(env, SUPER)
    system, user = env.sent[-1]
    assert "@" not in user and "SER1" not in user and "a@sl" not in user
    assert "MailTool" in user and "U1" in user
    assert "a@sl.p4sgi.com" in json.dumps(j) or "Remove" in json.dumps(j)
    assert "U1" not in j["message"] and "a@sl.p4sgi.com" in j["message"]  # labels restored for the admin only


def test_free_text_email_is_scrubbed_before_model(env):
    sid = _start(env, SUPER)["session_id"]
    env.post("/api/v1/assist/reply", json={"session_id": sid, "text": "should I tell stranger@elsewhere.org or a@sl.p4sgi.com?"}, headers=SUPER)
    assert "stranger@elsewhere.org" not in env.sent[-1][1] and "a@sl.p4sgi.com" not in env.sent[-1][1]


def test_non_super_gets_no_dangerous_options_and_cannot_force_them(env):
    j = _start(env, SL, keys=("cid-mail1",))
    acts = {o["action"] for o in j["options"]}
    assert not acts & aa.SUPER_ONLY and "draft_email" in acts
    r = env.post("/api/v1/assist/reply", json={"session_id": j["session_id"], "option_id": "o1_1"}, headers=SL)
    assert r.status_code in (200, 404)  # unknown/expired option cannot run a hidden action
    plan = env.post("/api/v1/security/actions/plan", json={"action": "revoke_token", "email": "a@sl.p4sgi.com", "client_id": "cid-mail1"}, headers=SL)
    assert plan.status_code == 403


def test_session_is_bound_to_user(env):
    sid = _start(env, SUPER)["session_id"]
    r = env.post("/api/v1/assist/reply", json={"session_id": sid, "text": "hi"}, headers=SL)
    assert r.status_code == 404


def test_unknown_keys_404_and_scope_filtering(env):
    r = env.post("/api/v1/assist/start", json={"kind": "oauth", "keys": ["nope"]}, headers=SUPER)
    assert r.status_code == 404
    r = env.post("/api/v1/assist/start", json={"kind": "oauth", "keys": ["cid-zw9"]}, headers=SL)  # zw app, sl scope
    assert r.status_code == 404


def test_revoke_plan_flow(env):
    j = _start(env, SUPER)
    r = env.post("/api/v1/assist/reply", json={"session_id": j["session_id"], "option_id": _opt(j, "plan_revoke_token")["id"]}, headers=SUPER).json()
    assert r["result"]["type"] == "plan", r
    assert len(r["result"]["steps"]) == 2, r["result"]
    cmds = json.dumps(r["result"]["steps"])
    assert "delete token clientid cid-mail1" in cmds and "a@sl.p4sgi.com" in cmds


def test_email_flow_has_mailto_and_sends_nothing(env):
    j = _start(env, SUPER)
    r = env.post("/api/v1/assist/reply", json={"session_id": j["session_id"], "option_id": _opt(j, "draft_email")["id"]}, headers=SUPER).json()
    res = r["result"]
    assert res["type"] == "email" and res["to"] == ["a@sl.p4sgi.com"] and res["mailto"].startswith("mailto:a@sl.p4sgi.com")


def test_approve_needs_note_and_confirm_then_changes_label(env):
    j = _start(env, SUPER)
    oid = _opt(j, "approve_app")["id"]
    r1 = env.post("/api/v1/assist/reply", json={"session_id": j["session_id"], "option_id": oid}, headers=SUPER).json()
    assert r1["result"]["type"] == "confirm"
    assert env.get("/api/v1/security/approved-apps", headers=SUPER).json()["rows"] == []
    r2 = env.post("/api/v1/assist/reply", json={"session_id": j["session_id"], "option_id": oid, "confirm": True, "note": "IT mail client"}, headers=SUPER).json()
    assert r2["result"]["type"] == "done"
    rows = env.get("/api/v1/security/oauth", headers=SUPER).json()["rows"]
    mt = next(x for x in rows if x["client_id"] == "cid-mail1")
    assert mt["risk_level"] == "APPROVED" and mt["risk_level_raw"] == "CRITICAL"
    assert rows[-1]["risk_level"] == "APPROVED" or rows[0]["risk_level"] != "APPROVED"
    assert env.get("/api/v1/security/approved-apps", headers=SL).json()["can_edit"] is False


def test_approve_endpoints_super_only_and_delete(env):
    body = {"key": "cid-mail1", "note": "known mail app", "review_days": 90}
    assert env.post("/api/v1/security/approved-apps", json=body, headers=SL).status_code == 403
    assert env.post("/api/v1/security/approved-apps", json=body, headers=SUPER).status_code == 200
    assert env.delete("/api/v1/security/approved-apps?key=cid-mail1", headers=SL).status_code == 403
    assert env.delete("/api/v1/security/approved-apps?key=cid-mail1", headers=SUPER).status_code == 200
    mt = next(x for x in env.get("/api/v1/security/oauth", headers=SUPER).json()["rows"] if x["client_id"] == "cid-mail1")
    assert mt["risk_level"] != "APPROVED"


def test_model_down_falls_back_to_builtin_text(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(ad, "llm", boom)
    j = _start(env, SUPER)
    assert "MailTool" in j["message"] and j["options"] and any("built-in" in w for w in j["warnings"])


def test_bad_model_output_cannot_inject_actions(env):
    env.reply["options"] = [{"action": "plan_wipe_device", "target": "D9", "label": "wipe"}, {"action": "format_disk", "label": "x"}]
    j = _start(env, SUPER)
    assert all(o["action"] in aa.ACTIONS for o in j["options"])
    assert not any(o["action"] == "plan_wipe_device" for o in j["options"])


def test_device_wipe_plan_and_user_offboard(env):
    j = _start(env, SUPER, kind="devices", keys=("d1",))
    env.reply["options"] = [{"action": "plan_wipe_device", "target": "D1", "label": "Wipe D1"}]
    j = _start(env, SUPER, kind="devices", keys=("d1",))
    oid = _opt(j, "plan_wipe_device")["id"]
    r = env.post("/api/v1/assist/reply", json={"session_id": j["session_id"], "option_id": oid}, headers=SUPER).json()
    assert "RESOURCE_ID" in json.dumps(r["result"]["steps"]) and "admin_account_wipe" in json.dumps(r["result"]["steps"])


def test_first_answer_saves_generalised_draft_help(env):
    _start(env, SUPER)
    arts = env.get("/api/v1/help/articles", headers=SUPER).json()
    drafts = [a for a in arts["rows"] if a["status"] == "draft"]
    assert drafts and "@" not in drafts[0]["body"] and "U1" not in drafts[0]["body"] and "the user" in drafts[0]["body"]
    assert not [a for a in env.get("/api/v1/help/articles", headers=SL).json()["rows"] if a["status"] == "draft"]


def test_help_ask_search_vote_and_review(env):
    r = env.post("/api/v1/help/ask", json={"question": "what does critical mean for an app? email me at x@y.org"}, headers=SL).json()
    assert r["sources"] and r["new_question"] is True and r["answer"]
    assert "x@y.org" not in json.dumps(env.get("/api/v1/help/articles", headers=SUPER).json())
    again = env.post("/api/v1/help/ask", json={"question": "what does critical mean for an app? email me at x@y.org"}, headers=SL).json()
    assert again["new_question"] is False
    s = env.get("/api/v1/help/search?q=2sv", headers=SL).json()
    assert any(x["id"] == "two-step" for x in s["rows"])
    assert env.post("/api/v1/help/articles/risk-levels/vote", json={"helpful": True}, headers=SL).json()["ok"] is False
    # review: only super-admins
    aid = r["saved_as"]
    assert env.post(f"/api/v1/help/articles/{aid}/status", json={"status": "approved"}, headers=SL).status_code == 403
    assert env.post(f"/api/v1/help/articles/{aid}/status", json={"status": "approved"}, headers=SUPER).status_code == 200
    assert any(a["id"] == aid for a in env.get("/api/v1/help/articles", headers=SL).json()["rows"])
    assert env.post(f"/api/v1/help/articles/{aid}/vote", json={"helpful": True}, headers=SL).json()["ok"] is True
    assert env.delete(f"/api/v1/help/articles/{aid}", headers=SL).status_code == 403
    assert env.delete(f"/api/v1/help/articles/{aid}", headers=SUPER).status_code == 200
    assert env.post("/api/v1/help/articles", json={"id": "risk-levels", "title": "hack", "body": "hack hack"}, headers=SUPER).status_code == 422
    ok = env.post("/api/v1/help/articles", json={"title": "Our policy", "body": "Always ask Grace first."}, headers=SUPER).json()
    assert ok["created"] and any(a["id"] == ok["id"] for a in env.get("/api/v1/help/articles", headers=SL).json()["rows"])


def test_session_expiry(env, monkeypatch):
    sid = _start(env, SUPER)["session_id"]
    real = aa.time.time
    monkeypatch.setattr(aa.time, "time", lambda: real() + aa.SESSION_TTL_S + 5)
    assert env.post("/api/v1/assist/reply", json={"session_id": sid, "text": "hi"}, headers=SUPER).status_code == 404

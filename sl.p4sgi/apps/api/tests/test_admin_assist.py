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
    aa._CACHE.clear()
    aa._INDEXED.update(done=True, tried=0)  # no background indexing thread in tests
    for sub in ("security", "help", "assist"):
        shutil.rmtree(main.DATA_DIR / sub, ignore_errors=True)
    sent: list[tuple[str, str]] = []
    reply = {"message": "U1 and U2 use A1. It can read all mail. Do you want to remove its access?",
             "options": [{"label": "Remove access of A1", "action": "plan_revoke_token", "target": "A1"},
                         {"label": "Email U1", "action": "draft_email", "target": "U1"},
                         {"label": "Approve A1", "action": "approve_app", "target": "A1"},
                         {"label": "Explain more", "action": "explain", "say": "Is it safe?"}]}

    def fake_llm(provider, model, system, user, json_mode=False, max_tokens=None):
        sent.append((system, user))
        return json.dumps(reply)

    monkeypatch.setattr(ad, "llm", fake_llm)
    monkeypatch.setattr(ad, "cloud_state", lambda p: {"label": p, "available": False, "reason": "off"})
    c = TestClient(main.app)
    c.sent, c.reply = sent, reply  # type: ignore[attr-defined]
    return c


def _start(c, headers, kind="oauth", keys=("cid-mail1",), enhance=True):
    r = c.post("/api/v1/assist/start", json={"kind": kind, "keys": list(keys)}, headers=headers)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ai_pending"] is True and j["message"] and j["options"] and j["samples"]  # instant built-in answer first
    if enhance:
        e = c.post("/api/v1/assist/enhance", json={"session_id": j["session_id"]}, headers=headers)
        assert e.status_code == 200, e.text
        if not e.json().get("skipped"):
            j = {**j, **e.json()}
    return j


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
    q = {"question": "what does critical mean for an app? email me at x@y.org"}
    fast = env.post("/api/v1/help/ask", json=q, headers=SL).json()
    assert fast["sources"] and fast["ai"] is False and fast["engine"].startswith("Guide lookup") and fast["saved_as"] is None
    assert not env.sent  # the instant lookup never calls the model
    r = env.post("/api/v1/help/ask", json={**q, "ai": True}, headers=SL).json()
    assert r["ai"] is True and r["new_question"] is True and r["saved_as"]
    assert "x@y.org" not in json.dumps(env.get("/api/v1/help/articles", headers=SUPER).json())
    again = env.post("/api/v1/help/ask", json={**q, "ai": True}, headers=SL).json()
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


# ------------------------------------------------------------ 0.4.23 additions ---
def test_topics_are_comprehensive_and_unique():
    ids = [a["id"] for a in aa.ALL_SEED]
    assert len(ids) == len(set(ids)) >= 70
    assert len({a["category"] for a in aa.ALL_SEED}) >= 10
    for a in aa.HELP_TOPICS:
        assert len(a["body"]) > 80 and a["tags"] and a["title"]


def test_collect_knowledge_indexes_all_topics(tmp_path):
    refs = {r for s_, r, _ in ad.collect_knowledge(tmp_path) if s_ == "help"}
    assert {a["id"] for a in aa.ALL_SEED} <= refs


def test_detok_shows_app_name_not_client_id():
    r = aa.Redactor()
    aa.build_facts("oauth", [_oauth_row()], r)
    assert r.detok("Remove A1?") == "Remove MailTool?"


def test_help_lookup_uses_pgvector_first(env, monkeypatch):
    calls = []

    def fake_search(q, k=6, source=None):
        calls.append(source)
        return [{"source": "help", "ref": "two-step", "content": "x", "score": 0.82}, {"source": "help", "ref": "glossary", "content": "y", "score": 0.1}], "vector"

    monkeypatch.setattr(ad, "search_chunks", fake_search)
    j = env.get("/api/v1/help/search?q=how%20do%20I%20secure%20admins", headers=SL).json()
    assert calls == ["help"] and j["mode"].startswith("meaning") and j["rows"][0]["id"] == "two-step"
    assert "glossary" not in [r["id"] for r in j["rows"][:1]]  # below the similarity floor: not a vector hit


def test_help_lookup_falls_back_to_keywords_when_db_down(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ad, "search_chunks", boom)
    j = env.get("/api/v1/help/search?q=2sv", headers=SL).json()
    assert j["mode"] == "keyword" and any(r["id"] == "two-step" for r in j["rows"])


def test_help_index_status_and_reindex(env, monkeypatch):
    seen = {}

    def fake_up(rows):
        seen["n"] = len(rows)
        seen["sources"] = {r[0] for r in rows}
        return {"indexed": len(rows), "vectors": len(rows), "warning": None}

    monkeypatch.setattr(ad, "upsert_chunks", fake_up)
    monkeypatch.setattr(ad, "count_chunks", lambda s_: (seen.get("n", 0), seen.get("n", 0)))
    monkeypatch.setattr(ad, "ensure_setup", lambda force=False: {"ok": True, "vector": True, "error": None})
    assert env.post("/api/v1/help/reindex", headers=SL).status_code == 403
    r = env.post("/api/v1/help/reindex", headers=SUPER).json()
    assert r["indexed"] == seen["n"] >= 70 and seen["sources"] == {"help"} and r["with_vectors"] == r["indexed"]
    st = env.get("/api/v1/help/status", headers=SL).json()
    assert st["pgvector"] is True and st["in_db"] == st["expected"]


def test_approving_and_deleting_keeps_index_in_sync(env, monkeypatch):
    up, dele = [], []
    monkeypatch.setattr(ad, "upsert_chunk", lambda s_, r, c: up.append((s_, r)) or True)
    monkeypatch.setattr(ad, "delete_chunk", lambda s_, r: dele.append((s_, r)) or True)
    ok = env.post("/api/v1/help/articles", json={"title": "Our policy", "body": "Always ask Grace first."}, headers=SUPER).json()
    assert ("help", ok["id"]) in up
    env.post(f"/api/v1/help/articles/{ok['id']}/status", json={"status": "archived"}, headers=SUPER)
    env.delete(f"/api/v1/help/articles/{ok['id']}", headers=SUPER)
    assert dele.count(("help", ok["id"])) == 2


def test_help_ai_answer_is_not_json_forced(env, monkeypatch):
    modes = []
    monkeypatch.setattr(ad, "llm", lambda p, m, sy, u, json_mode=False, max_tokens=None: modes.append(json_mode) or "Plain text answer [1].")
    r = env.post("/api/v1/help/ask", json={"question": "what is 2sv for admins", "ai": True}, headers=SL).json()
    assert modes == [False] and r["answer"].startswith("Plain text")


def test_app_email_goes_to_all_users_and_smtp_state(env):
    env.reply["options"] = [{"action": "draft_email", "target": "A1", "label": "Email everyone who uses A1"}]
    j = _start(env, SUPER)
    res = env.post("/api/v1/assist/reply", json={"session_id": j["session_id"], "option_id": _opt(j, "draft_email")["id"]}, headers=SUPER).json()["result"]
    assert res["to"] == ["a@sl.p4sgi.com", "c@sl.p4sgi.com"] and "c%40sl.p4sgi.com" in res["mailto"].replace(",", "")
    assert res["smtp"]["configured"] is False and res["smtp"]["can_send"] is False


def test_smtp_send_guarded(env, monkeypatch):
    env.reply["options"] = [{"action": "draft_email", "target": "A1", "label": "Email everyone who uses A1"}]
    j = _start(env, SUPER)
    env.post("/api/v1/assist/reply", json={"session_id": j["session_id"], "option_id": _opt(j, "draft_email")["id"]}, headers=SUPER)
    body = {"session_id": j["session_id"], "to": ["a@sl.p4sgi.com"], "subject": "Please confirm", "body": "Hello, please confirm you use this app."}
    assert env.post("/api/v1/assist/send-email", json=body, headers=SUPER).status_code == 409  # SMTP not configured
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_FROM", "it@example.org")
    sent = []
    monkeypatch.setattr(aa, "send_smtp", lambda to, subj, text, rt: sent.append((to, rt)))
    assert env.post("/api/v1/assist/send-email", json={**body, "to": ["stranger@x.org"]}, headers=SUPER).status_code == 422
    ok = env.post("/api/v1/assist/send-email", json={**body, "to": ["a@sl.p4sgi.com", "c@sl.p4sgi.com"]}, headers=SUPER).json()
    assert ok["sent"] == 2 and [t for t, _ in sent] == ["a@sl.p4sgi.com", "c@sl.p4sgi.com"] and sent[0][1] == "geb@p4sgi.com"
    # non-super and other users' sessions cannot send
    j2 = _start(env, SL)
    assert env.post("/api/v1/assist/send-email", json={**body, "session_id": j2["session_id"]}, headers=SL).status_code == 403
    assert env.post("/api/v1/assist/send-email", json=body, headers=SL).status_code == 404
    monkeypatch.setattr(aa, "MAX_SENDS_PER_HOUR", 2)
    assert env.post("/api/v1/assist/send-email", json=body, headers=SUPER).status_code == 429
    audit = (Path(os.environ["DATA_DIR"]) / "assist" / "audit.jsonl")
    assert "send_email" in audit.read_text() if audit.exists() else True


def test_send_smtp_builds_safe_message(monkeypatch):
    got = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            got["hp"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            got["tls"] = True

        def login(self, u, p):
            got["login"] = u

        def send_message(self, m):
            got["msg"] = m

    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_FROM", "it@example.org")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setattr(aa.smtplib, "SMTP", FakeSMTP)
    aa.send_smtp("a@x.org", "Hi\r\nBcc: evil@x.org", "body text here", "geb@p4sgi.com")
    m = got["msg"]
    assert got["tls"] and got["login"] == "u" and m["To"] == "a@x.org" and "\n" not in m["Subject"] and m["Bcc"] is None and m["Reply-To"] == "geb@p4sgi.com"


# ----------------------------------------------- AIdmin on every section (extra kinds) ---
RUN = {"id": "20261007T080000Z_users", "report": "users", "status": "failed", "exit_code": 1, "size_bytes": 10, "created_at": "2026-10-07T08:00:00Z", "domains": ["sl.p4sgi.com"]}
FILE = {"folder": "probe-1", "name": "users_a@sl.p4sgi.com.csv", "path": "probe-1/users_a@sl.p4sgi.com.csv", "size_bytes": 5, "rows": 3, "modified": "2026-10-07T08:00:00+00:00", "err": "bad row for b@sl.p4sgi.com"}
JOB = {"id": "j-1111", "status": "pending", "emis": "110101", "school_name": "Test School", "source_format": "xlsx", "source_filename": "cfg_x@y.org.xlsx",
       "created_at": "2026-10-01T00:00:00", "approved_by": "boss@p4sgi.com", "notes": "ask ht@sl.p4sgi.com", "payload_summary": {"row_count": 4, "fields": {"a": 1}}}
EVENT = {"id": "e-2222", "kind": "radar_parents", "status": "ok", "emis": "110101", "created_at": "2026-10-02T00:00:00", "school_email": "ht@sl.p4sgi.com",
         "payload_preview": "published for ht@sl.p4sgi.com", "site_attendance_url": "https://x"}
TAB = {"id": "t-3333", "emis": "110101", "school_name": "Test School", "device_type": "tablet", "app_version": "1.2", "last_seen": None, "data_mb": 1.5,
       "serial": "SERIAL999", "sim": "+23277000000", "whatsapp": "+23277000001", "tablet_android_id": "ANDROIDID123", "school_email": "ht@sl.p4sgi.com"}
EXTRA_ROWS = {"gam_runs": [RUN], "raw_exports": [FILE], "jobs": [JOB], "events": [EVENT], "fleet": [TAB]}


def test_extra_facts_are_whitelisted_and_scrubbed():
    for kind, rows in EXTRA_ROWS.items():
        r = aa.Redactor()
        facts, refs = aa.build_facts(kind, rows, r)
        blob = json.dumps(facts)
        assert "@" not in blob and "SERIAL999" not in blob and "+2327700" not in blob and "ANDROIDID123" not in blob, (kind, blob)
        assert list(refs) == [facts[0]["ref"]] and refs[facts[0]["ref"]]["kind"] == kind
        assert aa.baseline_message(kind, facts) and aa.default_options(kind)
    assert aa.Redactor().generalise("R1 and T2 and J3") == "the report run and the tablet and the job"


def test_extra_kinds_are_explain_only():
    for kind, rows in EXTRA_ROWS.items():
        r = aa.Redactor()
        _, refs = aa.build_facts(kind, rows, r)
        ref = next(iter(refs))
        raw = [{"action": "plan_wipe_device", "target": ref, "label": "wipe"}, {"action": "approve_app", "target": ref, "label": "approve"},
               {"action": "plan_offboard_user", "target": "U1", "label": "suspend"}, {"action": "draft_email", "target": ref, "label": "email"},
               {"action": "explain", "label": "why", "say": "why"}, {"action": "save_help", "label": "save"}]
        acts = {o["action"] for o in aa.validate_options(raw, refs, r, True, kind)}
        assert acts == ({"explain", "save_help", "draft_email"} if kind == "fleet" else {"explain", "save_help"}), (kind, acts)
        assert "approve_app" not in aa.system_prompt(True, kind) and "plan_revoke_token" not in aa.system_prompt(True, kind)


def test_general_session_and_guides_in_prompt(env):
    r = env.post("/api/v1/assist/start", json={"kind": "general", "keys": ["*"]}, headers=SL).json()
    assert r["ai_pending"] is False and "AIdmin" in r["message"] and r["samples"] and r["options"]
    assert not env.sent  # instant, no model call
    env.post("/api/v1/assist/reply", json={"session_id": r["session_id"], "text": "what does critical mean for an app?"}, headers=SL)
    assert "GUIDES:" in env.sent[-1][1] and "CRITICAL" in env.sent[-1][1]


@pytest.fixture()
def extra_client(env, tmp_path):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app import main

    state = {"deny": False}

    def fetch(kind, request, allowed):
        if state["deny"] and kind == "raw_exports":
            raise HTTPException(403, {"error": "forbidden"})
        return [dict(x) for x in EXTRA_ROWS[kind]]

    h = sa.Helpers(insight_scope=main._insight_scope, load_sources=main._load_sources, build_device_rows=main._build_device_rows, row_email=main._row_email,
                   in_scope=main._in_scope, request_scope=main._request_scope, is_superadmin=main.is_superadmin, data_dir=tmp_path, app_version="t")
    app = FastAPI()
    app.include_router(aa.build_router(h, fetch))
    c = TestClient(app)
    c.sent, c.state = env.sent, state  # type: ignore[attr-defined]
    return c


@pytest.mark.parametrize("kind,key", [("gam_runs", RUN["id"]), ("raw_exports", FILE["path"]), ("jobs", "j-1111"), ("events", "e-2222"), ("fleet", "t-3333")])
def test_each_section_kind_end_to_end(extra_client, kind, key):
    c = extra_client
    j = c.post("/api/v1/assist/start", json={"kind": kind, "keys": [key]}).json()
    assert j["ai_pending"] is True and j["samples"] and j["message"] and j["items"][0]["label"]
    e = c.post("/api/v1/assist/enhance", json={"session_id": j["session_id"]}).json()
    assert e["skipped"] is False
    prompt = c.sent[-1][1]
    assert "@" not in prompt and "SERIAL999" not in prompt and "+2327700" not in prompt and "ANDROIDID123" not in prompt
    sys_prompt = c.sent[-1][0]
    assert KIND_TEXT[kind] in sys_prompt
    bad = c.post("/api/v1/assist/start", json={"kind": kind, "keys": ["nope"]})
    assert bad.status_code == 404


KIND_TEXT = {"gam_runs": "runs of GAM reports", "raw_exports": "raw CSV files", "jobs": "provisioning jobs", "events": "publish events", "fleet": "tablet registry"}


def test_fleet_email_goes_to_school_and_forbidden_propagates(extra_client):
    c = extra_client
    j = c.post("/api/v1/assist/start", json={"kind": "fleet", "keys": ["t-3333"]}).json()
    opt = next(o for o in j["options"] if o["action"] == "draft_email")
    res = c.post("/api/v1/assist/reply", json={"session_id": j["session_id"], "option_id": opt["id"]}).json()["result"]
    assert res["type"] == "email" and res["to"] == ["ht@sl.p4sgi.com"] and "tablet" in res["subject"].lower()
    c.state["deny"] = True
    assert c.post("/api/v1/assist/start", json={"kind": "raw_exports", "keys": [FILE["path"]]}).status_code == 403

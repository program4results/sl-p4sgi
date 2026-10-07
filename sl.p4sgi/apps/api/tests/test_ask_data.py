"""Tests for app/ask_data.py (0.4.21). No Postgres, no Ollama, no network: LLM / DB calls are monkeypatched."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p4sgi-chat-test-"))
os.environ.setdefault("SUPERADMIN_EMAILS", "geb@p4sgi.com")
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ask_data as ad  # noqa: E402

GOOD = [
    "SELECT district, COUNT(*) AS n FROM chat_devices JOIN chat_schools USING (emis) GROUP BY district ORDER BY n DESC",
    "select count(*) from chat_attendance where attendance_date >= CURRENT_DATE - 7;",
    "WITH d AS (SELECT emis FROM chat_devices WHERE last_seen < now() - interval '30 days') SELECT COUNT(*) FROM d",
    "SELECT EXTRACT(YEAR FROM created_at) AS y, COUNT(*) FROM chat_jobs GROUP BY 1",
    "SELECT name FROM chat_schools WHERE name ILIKE '%update%'",  # keyword inside a string literal is fine
    "SELECT * FROM (SELECT emis FROM chat_schools) s",
]
BAD = [
    "",
    "DELETE FROM schools",
    "SELECT * FROM schools",                                  # raw table
    "SELECT absent_named FROM attendance_sync_events",
    "SELECT * FROM devices",
    "SELECT 1; DROP TABLE schools",
    "SELECT * FROM chat_schools; SELECT 1",
    "SELECT * FROM chat_schools -- hi",
    "SELECT * FROM chat_schools /* x */",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_sleep(100)",
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM chat_schools, schools",                    # comma join
    "SELECT * INTO newt FROM chat_schools",
    "SELECT * FROM chat_schools FOR UPDATE",
    'SELECT * FROM "schools"',
    "SELECT current_setting('x')",
    "SELECT version()",
    "WITH x AS (DELETE FROM schools RETURNING *) SELECT * FROM x",
    "SELECT * FROM chat_schools WHERE emis = (SELECT emis FROM schools LIMIT 1)",
    "COPY chat_schools TO '/tmp/x'",
    "SET statement_timeout = 0",
    "SELECT lo_import('/etc/passwd')",
    "SELECT 1",                                               # no relation
    "SELECT * FROM public.chat_schools",
]


@pytest.mark.parametrize("sql", GOOD)
def test_good_sql_is_accepted(sql):
    assert ad.validate_sql(sql)


@pytest.mark.parametrize("sql", BAD)
def test_bad_sql_is_rejected(sql):
    with pytest.raises(ad.SqlRejected):
        ad.validate_sql(sql)


def test_views_never_select_personal_columns():
    for name, v in ad.VIEWS.items():
        ddl = v["ddl"].lower()
        for bad in ("serial", "sim", "whatsapp", "tablet_android_id", "school_email", "payload", "device_serial", "approved_by"):
            assert not __import__("re").search(rf"\b{bad}\b", ddl), (name, bad)
        assert "absent_named" not in ddl.replace("jsonb_array_length(absent_named)", "")
        assert "absent_named" not in v["cols"]


def test_prompt_lists_only_views():
    p = ad.sql_system_prompt()
    assert all(n in p for n in ad.ALLOWED_VIEWS) and "absent_named" not in p and "serial" not in p


def test_chunking_and_knowledge_has_no_personal_data(tmp_path):
    ch = ad._chunk("a" * 2000 + "\n\npara two\n\npara three")
    assert all(len(c) <= 900 for c in ch) and len(ch) >= 3
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "note.md").write_text("Retention policy is three years.", encoding="utf-8")
    rows = ad.collect_knowledge(tmp_path)
    srcs = {r[0] for r in rows}
    assert {"pia", "schema", "doc:note.md"} <= srcs
    assert sum(1 for r in rows if r[0] == "pia" and __import__("re").fullmatch(r"A\d{2}", r[1])) == 40
    assert any(r[0] == "pia" and r[1] == "R16" for r in rows)


def test_json_object_extraction():
    assert ad._json_obj('noise {"mode":"sql","sql":"SELECT 1"} tail')["mode"] == "sql"
    with pytest.raises(ValueError):
        ad._json_obj("no json here")


def test_cloud_adapters_build_expected_requests(monkeypatch):
    calls = []

    def fake(url, payload, headers=None, timeout=0):
        calls.append((url, payload, headers))
        return {"choices": [{"message": {"content": "ok-openai"}}], "content": [{"text": "ok-claude"}],
                "candidates": [{"content": {"parts": [{"text": "ok-gemini"}]}}]}
    monkeypatch.setattr(ad, "_post_json", fake)
    for k in ("XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(k, "KEY")
    assert ad.llm("xai", "m", "sys", "q") == "ok-openai" and calls[-1][0].startswith("https://api.x.ai/")
    assert ad.llm("openai", "m", "sys", "q") == "ok-openai" and calls[-1][2]["Authorization"] == "Bearer KEY"
    assert ad.llm("anthropic", "m", "sys", "q") == "ok-claude" and calls[-1][2]["x-api-key"] == "KEY"
    assert ad.llm("gemini", "gm", "sys", "q") == "ok-gemini" and "gm:generateContent" in calls[-1][0]
    assert "KEY" not in calls[-1][0]  # key travels in a header, not the URL


# ------------------------------------------------------------------- HTTP ---
@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(ad, "ensure_setup", lambda force=False: {"ok": True, "vector": True, "error": None})
    monkeypatch.setattr(ad, "ollama_models", lambda: (True, ["qwen2.5-coder:14b", "nomic-embed-text:latest"], None))
    return TestClient(app)


SL = {"X-Goog-Authenticated-User-Email": "accounts.google.com:ht@sl.p4sgi.com"}
SUPER = {"X-Goog-Authenticated-User-Email": "accounts.google.com:geb@p4sgi.com"}


def test_status_shows_cloud_off_by_default(client):
    j = client.get("/api/v1/chat/status", headers=SL).json()
    assert j["experimental"] and j["ollama"]["reachable"] and j["embed_model_installed"]
    assert all(not c["available"] for c in j["cloud"].values()) and "off" in j["cloud"]["xai"]["reason"]
    assert "chat_devices" in j["database"]["views"]


def test_cloud_provider_is_refused_when_disabled(client):
    r = client.post("/api/v1/chat/ask", json={"question": "how many tablets", "provider": "openai"}, headers=SUPER)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "cloud_disabled"


def test_sql_mode_is_super_admin_only(client):
    r = client.post("/api/v1/chat/ask", json={"question": "how many tablets", "mode": "sql"}, headers=SL)
    assert r.status_code == 403


def test_sql_flow_validates_executes_and_summarises_locally(client, monkeypatch):
    seen = {}

    def fake_llm(provider, model, system, user, json_mode=False):
        seen.setdefault("calls", []).append((provider, json_mode))
        if json_mode:
            return json.dumps({"mode": "sql", "sql": "SELECT district, COUNT(*) AS n FROM chat_schools GROUP BY district", "explanation": "schools per district"})
        return "Kono has 12 schools."
    monkeypatch.setattr(ad, "llm", fake_llm)
    monkeypatch.setattr(ad, "run_readonly", lambda sql, *a, **k: (["district", "n"], [["Kono", 12]], False))
    r = client.post("/api/v1/chat/ask", json={"question": "schools per district?"}, headers=SUPER).json()
    assert r["mode"] == "sql" and r["rows"] == [["Kono", 12]] and r["answer"].startswith("Kono") and r["provider"] == "ollama"
    assert seen["calls"][-1][0] == "ollama"
    audit = (Path(os.environ["DATA_DIR"]) / "chat" / "audit.jsonl").read_text()
    assert "schools per district" in audit and "Kono" not in audit  # rows are never logged


def test_unsafe_model_sql_is_never_executed(client, monkeypatch):
    ran = []
    monkeypatch.setattr(ad, "llm", lambda *a, **k: json.dumps({"mode": "sql", "sql": "SELECT absent_named FROM attendance_sync_events"}))
    monkeypatch.setattr(ad, "run_readonly", lambda *a, **k: ran.append(1) or ([], [], False))
    r = client.post("/api/v1/chat/ask", json={"question": "who was absent?", "mode": "sql"}, headers=SUPER).json()
    assert not ran and r["error"].startswith("rejected") and r["rows"] == []


def test_one_repair_attempt_after_a_rejection(client, monkeypatch):
    replies = iter([json.dumps({"mode": "sql", "sql": "SELECT * FROM schools"}),
                    json.dumps({"mode": "sql", "sql": "SELECT COUNT(*) FROM chat_schools"})])
    monkeypatch.setattr(ad, "llm", lambda p, m, s, u, json_mode=False: next(replies) if json_mode else "42 schools.")
    monkeypatch.setattr(ad, "run_readonly", lambda sql, *a, **k: (["count"], [[42]], False))
    r = client.post("/api/v1/chat/ask", json={"question": "how many schools?", "mode": "sql"}, headers=SUPER).json()
    assert r["row_count"] == 1 and "error" not in r or not r.get("error")


def test_docs_mode_cites_sources_for_any_user(client, monkeypatch):
    monkeypatch.setattr(ad, "search_chunks", lambda q, k=6: ([{"source": "pia", "ref": "A02", "content": "DPO designation control", "score": 0.9}], "vector"))
    monkeypatch.setattr(ad, "llm", lambda *a, **k: "A DPO must be designated [1].")
    r = client.post("/api/v1/chat/ask", json={"question": "do we need a DPO?", "mode": "docs"}, headers=SL).json()
    assert r["mode"] == "docs" and r["sources"][0]["ref"] == "A02" and "[1]" in r["answer"] and r["retrieval"] == "vector"


def test_auto_mode_for_non_super_goes_to_docs_without_calling_the_router(client, monkeypatch):
    monkeypatch.setattr(ad, "search_chunks", lambda q, k=6: ([], "fulltext"))
    monkeypatch.setattr(ad, "llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("router must not run")))
    r = client.post("/api/v1/chat/ask", json={"question": "what is the retention rule?"}, headers=SL).json()
    assert r["mode"] == "docs" and "nothing relevant" in r["answer"]


def test_ollama_down_gives_clear_502(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ollama not reachable: refused")
    monkeypatch.setattr(ad, "llm", boom)
    r = client.post("/api/v1/chat/ask", json={"question": "how many tablets?"}, headers=SUPER)
    assert r.status_code == 502 and "not reachable" in r.json()["detail"]["message"]


def test_setup_and_reindex_need_super_admin(client):
    assert client.post("/api/v1/chat/setup", headers=SL).status_code == 403
    assert client.post("/api/v1/chat/reindex", headers=SL).status_code == 403


def test_input_validation(client):
    assert client.post("/api/v1/chat/ask", json={"question": "x"}, headers=SUPER).status_code == 422
    assert client.post("/api/v1/chat/ask", json={"question": "valid question", "model": "a b;rm"}, headers=SUPER).status_code == 422
    assert client.post("/api/v1/chat/ask", json={"question": "valid question", "provider": "evil"}, headers=SUPER).status_code == 422

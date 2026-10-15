"""Tests for app/rustadmin.py (0.4.26). No Postgres, no network, no adb binary: subprocess is mocked."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="p4sgi-rustadmin-test-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("SUPERADMIN_EMAILS", "geb@p4sgi.com")
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import rustadmin as ra  # noqa: E402

SL = {"X-Goog-Authenticated-User-Email": "accounts.google.com:ht@sl.p4sgi.com"}
SUPER = {"X-Goog-Authenticated-User-Email": "accounts.google.com:geb@p4sgi.com"}
TGT = "100.64.1.20:5555"


@pytest.fixture()
def client(monkeypatch):
    for k in ("RUSTADMIN_ADB_ENABLED", "RUSTADMIN_ADB_ALLOW_CHANGES", "RUSTADMIN_ADB_NETS", "RUSTDESK_HOST", "RUSTDESK_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    from app.main import app  # lazy: other test modules set DATA_DIR before app.main is first imported

    return TestClient(app)


def fake_run(calls):
    def _run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
    return _run


def test_route_registered_and_catalog_shape(client):
    r = client.get("/api/v1/rustadmin/adb/catalog", headers=SL)
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == len(ra.CATALOG) >= 30
    ids = [c["id"] for cat in j["categories"].values() for c in cat]
    assert len(ids) == len(set(ids))
    assert {"getprop", "usagestats", "pm_list_inst", "pm_perm", "dev_options", "adb_enabled", "nonmarket", "netstats", "getenforce", "dump_account", "ps", "logcat_err"} <= set(ids)


def test_every_catalog_entry_has_valid_risk_and_no_shell_metachar():
    for c in ra.CATALOG:
        assert c["risk"] in (ra.RISK_READ, ra.RISK_CHANGE, ra.RISK_DANGER)
        for a in c["argv"]:
            assert not any(ch in a for ch in ";|&`$<>\n"), (c["id"], a)


def test_target_validation(monkeypatch):
    monkeypatch.delenv("RUSTADMIN_ADB_NETS", raising=False)
    assert ra.valid_target("100.64.1.20:5555") == "100.64.1.20:5555"
    assert ra.valid_target("192.168.1.5") == "192.168.1.5:5555"
    assert ra.valid_target("8.8.8.8:5555") is None            # public address refused
    assert ra.valid_target("10.0.0.1:99999") is None
    assert ra.valid_target("300.1.1.1:5555") is None
    assert ra.valid_target("R58M123ABCD") == "R58M123ABCD"      # USB serial
    assert ra.valid_target("x; reboot") is None
    assert ra.valid_target("") is None
    monkeypatch.setenv("RUSTADMIN_ADB_NETS", "192.168.0.0/16")
    assert ra.valid_target("100.64.1.20:5555") is None


def test_run_requires_super(client):
    r = client.post("/api/v1/rustadmin/adb/run", json={"target": TGT, "command": "getprop"}, headers=SL)
    assert r.status_code == 403


def test_adb_off_by_default_returns_plan(client, monkeypatch):
    calls = []
    monkeypatch.setattr(ra.subprocess, "run", fake_run(calls))
    r = client.post("/api/v1/rustadmin/adb/run", json={"target": TGT, "command": "getprop"}, headers=SUPER)
    j = r.json()
    assert r.status_code == 200 and j["executed"] is False and "switched off" in j["reason"]
    assert j["shown"] == "adb -s 100.64.1.20:5555 shell getprop"
    assert calls == []


def test_read_command_runs_without_shell(client, monkeypatch):
    calls = []
    monkeypatch.setenv("RUSTADMIN_ADB_ENABLED", "1")
    monkeypatch.setattr(ra.shutil, "which", lambda b: "/usr/bin/adb")
    monkeypatch.setattr(ra.subprocess, "run", fake_run(calls))
    r = client.post("/api/v1/rustadmin/adb/run", json={"target": TGT, "command": "dev_options"}, headers=SUPER)
    j = r.json()
    assert j["executed"] is True and j["output"].startswith("ok")
    argv, kw = calls[0]
    assert argv == ["adb", "-s", TGT, "shell", "settings", "get", "global", "development_settings_enabled"]
    assert kw["shell"] is False and kw["timeout"] == ra.TIMEOUT


def test_change_command_is_plan_only_by_default_and_needs_typed_confirm(client, monkeypatch):
    calls = []
    monkeypatch.setenv("RUSTADMIN_ADB_ENABLED", "1")
    monkeypatch.setattr(ra.shutil, "which", lambda b: "/usr/bin/adb")
    monkeypatch.setattr(ra.subprocess, "run", fake_run(calls))
    body = {"target": TGT, "command": "force_stop", "args": {"pkg": "com.example.app"}}
    assert client.post("/api/v1/rustadmin/adb/run", json=body, headers=SUPER).json()["executed"] is False
    monkeypatch.setenv("RUSTADMIN_ADB_ALLOW_CHANGES", "1")
    j = client.post("/api/v1/rustadmin/adb/run", json=body, headers=SUPER).json()
    assert j["executed"] is False and "Type the target" in j["reason"]
    j = client.post("/api/v1/rustadmin/adb/run", json={**body, "confirm": TGT}, headers=SUPER).json()
    assert j["executed"] is True and len(calls) == 1
    assert calls[0][0][-3:] == ["am", "force-stop", "com.example.app"] or calls[0][0][-1] == "com.example.app"


def test_bad_arguments_and_unknown_commands_rejected(client, monkeypatch):
    for args in ({"pkg": "com.example; reboot"}, {"pkg": "$(id)"}, {"pkg": ""}, {}):
        r = client.post("/api/v1/rustadmin/adb/run", json={"target": TGT, "command": "pm_path", "args": args}, headers=SUPER)
        assert r.status_code == 400, args
    assert client.post("/api/v1/rustadmin/adb/run", json={"target": TGT, "command": "rm -rf /"}, headers=SUPER).status_code == 400
    assert client.post("/api/v1/rustadmin/adb/run", json={"target": "8.8.8.8:5555", "command": "getprop"}, headers=SUPER).status_code == 400
    assert client.post("/api/v1/rustadmin/adb/run", json={"command": "getprop"}, headers=SUPER).status_code == 400
    r = client.post("/api/v1/rustadmin/adb/run", json={"target": TGT, "command": "rm", "args": {"path": "/sdcard/../data/x"}}, headers=SUPER)
    assert r.status_code == 400 or r.json()["executed"] is False


def test_path_traversal_blocked_in_build_argv(tmp_path):
    cmd = ra.BY_ID["rm"]
    with pytest.raises(ValueError):
        ra.build_argv(cmd, TGT, {"path": "/system/bin/sh"}, tmp_path)
    assert ra.build_argv(cmd, TGT, {"path": "/sdcard/Download/a.txt"}, tmp_path)[-1] == "/sdcard/Download/a.txt"
    pull = ra.build_argv(ra.BY_ID["pull"], TGT, {"path": "/sdcard/Download/a b.txt".replace(" ", "_")}, tmp_path)
    assert str(tmp_path / "rustadmin" / "pulled") in pull[-1]


def test_audit_written_and_readable_by_super_only(client, monkeypatch):
    client.post("/api/v1/rustadmin/adb/run", json={"target": TGT, "command": "getprop"}, headers=SUPER)
    assert client.get("/api/v1/rustadmin/adb/audit", headers=SL).status_code == 403
    rows = client.get("/api/v1/rustadmin/adb/audit", headers=SUPER).json()["rows"]
    assert rows and rows[0]["command"] == "getprop" and rows[0]["by"].endswith("geb@p4sgi.com")


def test_status_unconfigured_and_configured(client, monkeypatch, tmp_path):
    j = client.get("/api/v1/rustadmin/status", headers=SL).json()
    assert j["rustdesk"]["configured"] is False and j["adb"]["enabled"] is False and j["is_super"] is False
    key = tmp_path / "id_ed25519.pub"
    key.write_text("PUBKEY123=\n")
    monkeypatch.setenv("RUSTDESK_HOST", "rd.example.org")
    monkeypatch.setenv("RUSTDESK_KEY_FILE", str(key))
    j = client.get("/api/v1/rustadmin/status", headers=SUPER).json()
    assert j["rustdesk"]["key_found"] is True and "PUBKEY123=" in j["rustdesk"]["config"]["text"] and j["is_super"] is True
    assert "1.2.3" in j["rustdesk"]["client_note"]


def test_ui_has_section_after_domain_filter_and_version_consistent():
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()
    assert html.index('id="domainFilter"') < html.index('id="rustAidmin"') < html.index('id="uiSettings"')
    assert "rustAidmin: false" in html
    from app.main import APP_VERSION
    assert APP_VERSION in html and html.count(APP_VERSION) >= 3

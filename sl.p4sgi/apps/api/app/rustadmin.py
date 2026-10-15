"""RUSTAiDMIN (0.4.26): remote-support helper for the field tablets. EXPERIMENTAL, additive, own JSON/JSONL state.

Two independent parts, both optional:

1. RustDesk status/config helper. The dashboard does NOT contain a RustDesk server. The server (hbbs/hbbr) and an admin
   console run as separate containers from upstream images (docker compose --profile rustdesk). This module only reads the
   server's PUBLIC key file and builds the client settings string, so staff can configure tablets and open the console.
2. ADB helper (off unless RUSTADMIN_ADB_ENABLED=1 and an `adb` binary exists). A fixed catalogue of commands, run without a
   shell, super-admin only, with an audit log. Read-only diagnostics run directly; commands that change a device are plan-only
   unless RUSTADMIN_ADB_ALLOW_CHANGES=1, and then need a typed confirmation of the target. RustDesk does NOT carry ADB: the
   adb host must have its own network path to the tablet (USB, same LAN, or a VPN).

Isolated module: any failure here must never stop the API. Kill switch: RUSTADMIN_ENABLED=0.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

OUT_CAP = 24000
TIMEOUT = int(os.getenv("RUSTADMIN_ADB_TIMEOUT", "25") or 25)
DEFAULT_NETS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,127.0.0.0/8"

RISK_READ, RISK_CHANGE, RISK_DANGER = "read", "change", "danger"

PKG = r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"
PERM = r"[A-Za-z][A-Za-z0-9_.]*"
SETKEY = r"[a-z][a-z0-9_]{1,63}"
SETVAL = r"[A-Za-z0-9_.:-]{1,64}"
SDPATH = r"/sdcard/[A-Za-z0-9_./-]{1,120}"
KEYCODE = r"KEYCODE_[A-Z0-9_]{2,30}|[0-9]{1,3}"
INT = r"[0-9]{1,4}"


def _c(id_: str, cat: str, label: str, risk: str, argv: list[str], args: dict[str, str] | None = None, *, host: bool = False, note: str = "") -> dict[str, Any]:
    return {"id": id_, "category": cat, "label": label, "risk": risk, "argv": argv, "args": args or {}, "host": host, "note": note}


# argv is what follows `adb -s <target>` (or `adb` alone for host commands). {name} is replaced by a validated argument.
CATALOG: list[dict[str, Any]] = [
    # --- host
    _c("devices", "Connection", "List devices seen by this adb host", RISK_READ, ["devices", "-l"], host=True),
    _c("connect", "Connection", "adb connect (wireless)", RISK_READ, ["connect", "{target}"], host=True, note="Needs wireless debugging on the tablet."),
    _c("disconnect", "Connection", "adb disconnect", RISK_READ, ["disconnect", "{target}"], host=True),
    # --- diagnostics (read-only)
    _c("getprop", "Device / system", "Device properties (model, Android version, patch level)", RISK_READ, ["shell", "getprop"]),
    _c("battery", "Device / system", "Battery state", RISK_READ, ["shell", "dumpsys", "battery"]),
    _c("df", "Device / system", "Storage free/used", RISK_READ, ["shell", "df", "-h"]),
    _c("top", "Device / system", "Top processes (one snapshot)", RISK_READ, ["shell", "top", "-n", "1", "-b", "-m", "20"]),
    _c("ps", "Process / security", "All processes (ps -A)", RISK_READ, ["shell", "ps", "-A"]),
    _c("getenforce", "Process / security", "SELinux mode (getenforce)", RISK_READ, ["shell", "getenforce"]),
    _c("dmesg", "Process / security", "Kernel log (often needs root; may be refused)", RISK_READ, ["shell", "dmesg"]),
    _c("dev_options", "Process / security", "Developer options enabled?", RISK_READ, ["shell", "settings", "get", "global", "development_settings_enabled"]),
    _c("adb_enabled", "Process / security", "USB debugging enabled?", RISK_READ, ["shell", "settings", "get", "global", "adb_enabled"]),
    _c("nonmarket", "Process / security", "Install from unknown sources?", RISK_READ, ["shell", "settings", "get", "secure", "install_non_market_apps"]),
    _c("dump_account", "Process / security", "Accounts on the device (dumpsys account)", RISK_READ, ["shell", "dumpsys", "account"], note="Contains account names: personal data."),
    _c("logcat_err", "Logs", "Recent errors (logcat -d, errors only, last 1500 lines)", RISK_READ, ["shell", "logcat", "-d", "-t", "1500", "-v", "threadtime", "*:E"]),
    _c("pm_list", "Packages", "Installed packages", RISK_READ, ["shell", "pm", "list", "packages"]),
    _c("pm_list_inst", "Packages", "Packages with installer, incl. uninstalled-with-data (-i -u)", RISK_READ, ["shell", "pm", "list", "packages", "-i", "-u"]),
    _c("pm_perm", "Packages", "Permissions by group (-g -u)", RISK_READ, ["shell", "pm", "list", "permissions", "-g", "-u"]),
    _c("pm_path", "Packages", "APK path of a package", RISK_READ, ["shell", "pm", "path", "{pkg}"], {"pkg": PKG}),
    _c("usagestats", "Telemetry", "App usage statistics (dumpsys usagestats)", RISK_READ, ["shell", "dumpsys", "usagestats"], note="Contains app-use history: personal data."),
    _c("netstats", "Network", "Per-app data use (dumpsys netstats detail)", RISK_READ, ["shell", "dumpsys", "netstats", "detail"]),
    _c("ip_addr", "Network", "IP addresses (ip addr)", RISK_READ, ["shell", "ip", "addr"]),
    _c("netstat", "Network", "Open connections (netstat)", RISK_READ, ["shell", "netstat", "-tun"]),
    _c("ping", "Network", "Ping 8.8.8.8 four times", RISK_READ, ["shell", "ping", "-c", "4", "8.8.8.8"]),
    # --- changes to a device
    _c("am_start", "Apps", "Start an app (monkey launcher)", RISK_CHANGE, ["shell", "monkey", "-p", "{pkg}", "-c", "android.intent.category.LAUNCHER", "1"], {"pkg": PKG}),
    _c("force_stop", "Apps", "Force-stop an app", RISK_CHANGE, ["shell", "am", "force-stop", "{pkg}"], {"pkg": PKG}),
    _c("pm_grant", "Apps", "Grant a runtime permission", RISK_CHANGE, ["shell", "pm", "grant", "{pkg}", "{perm}"], {"pkg": PKG, "perm": PERM}),
    _c("pm_revoke", "Apps", "Revoke a runtime permission", RISK_CHANGE, ["shell", "pm", "revoke", "{pkg}", "{perm}"], {"pkg": PKG, "perm": PERM}),
    _c("key", "UI input", "Press a key (e.g. KEYCODE_HOME)", RISK_CHANGE, ["shell", "input", "keyevent", "{code}"], {"code": KEYCODE}),
    _c("tap", "UI input", "Tap at x y", RISK_CHANGE, ["shell", "input", "tap", "{x}", "{y}"], {"x": INT, "y": INT}),
    _c("screencap", "UI input", "Screenshot to /sdcard/p4sgi-screen.png", RISK_CHANGE, ["shell", "screencap", "-p", "/sdcard/p4sgi-screen.png"], note="Captures whatever is on screen."),
    _c("pull", "Files", "Copy a file from /sdcard to the server (data/rustadmin/pulled)", RISK_CHANGE, ["pull", "{path}", "{dest}"], {"path": SDPATH}, note="Copies device data to this server."),
    _c("settings_put", "Settings", "settings put <namespace> <key> <value>", RISK_DANGER, ["shell", "settings", "put", "{ns}", "{key}", "{val}"], {"ns": "system|secure|global", "key": SETKEY, "val": SETVAL}),
    _c("pm_clear", "Apps", "Clear an app's data (cannot be undone)", RISK_DANGER, ["shell", "pm", "clear", "{pkg}"], {"pkg": PKG}),
    _c("uninstall", "Apps", "Uninstall an app", RISK_DANGER, ["uninstall", "{pkg}"], {"pkg": PKG}),
    _c("push", "Files", "Copy a file from data/rustadmin/push to /sdcard", RISK_DANGER, ["push", "{src}", "{path}"], {"src": r"[A-Za-z0-9_.-]{1,80}", "path": SDPATH}),
    _c("rm", "Files", "Delete a file under /sdcard", RISK_DANGER, ["shell", "rm", "{path}"], {"path": SDPATH}),
    _c("reboot", "Device / system", "Reboot the tablet", RISK_DANGER, ["reboot"]),
]
BY_ID = {c["id"]: c for c in CATALOG}


class AdbRunIn(BaseModel):
    target: str = Field("", max_length=64)
    command: str = Field(..., max_length=40)
    args: dict[str, str] = Field(default_factory=dict)
    confirm: str = Field("", max_length=80)  # for change/danger: must equal the target exactly
    dry_run: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def allowed_nets() -> list[Any]:
    out = []
    for part in (os.getenv("RUSTADMIN_ADB_NETS", "") or DEFAULT_NETS).split(","):
        part = part.strip()
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
    return out


def valid_target(target: str) -> str | None:
    """Return the normalised target if it is an IP:port inside the allowed networks or a plain USB serial; else None."""
    t = (target or "").strip()
    m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{2,5}))?", t)
    if m:
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            return None
        port = int(m.group(2) or 5555)
        if not 1 <= port <= 65535 or not any(ip in n for n in allowed_nets()):
            return None
        return f"{ip}:{port}"
    if re.fullmatch(r"[A-Za-z0-9]{6,32}", t):
        return t
    return None


def build_argv(cmd: dict[str, Any], target: str | None, args: dict[str, str], data_dir: Path) -> list[str]:
    """Substitute validated arguments. Raises ValueError on any bad input. Never uses a shell."""
    values: dict[str, str] = {}
    for name, rx in cmd["args"].items():
        v = (args.get(name) or "").strip()
        if not re.fullmatch(rx, v):
            raise ValueError(f"argument '{name}' is missing or not allowed")
        values[name] = v
    if "{target}" in cmd["argv"]:
        if not target:
            raise ValueError("a valid target is required")
        values["target"] = target
    if "{dest}" in cmd["argv"]:
        values["dest"] = str(data_dir / "rustadmin" / "pulled" / (re.sub(r"[^A-Za-z0-9_.-]", "_", Path(values["path"]).name) or "file"))
    if "{src}" in cmd["argv"]:
        values["src"] = str(data_dir / "rustadmin" / "push" / values["src"])
    out = []
    for a in cmd["argv"]:
        m = re.fullmatch(r"\{(\w+)\}", a)
        out.append(values[m.group(1)] if m else a)
    return out


def adb_state() -> dict[str, Any]:
    binary = os.getenv("ADB_BIN", "adb")
    found = shutil.which(binary)
    return {
        "enabled": _flag("RUSTADMIN_ADB_ENABLED"),
        "binary_found": bool(found),
        "allow_changes": _flag("RUSTADMIN_ADB_ALLOW_CHANGES"),
        "networks": [str(n) for n in allowed_nets()],
        "timeout_sec": TIMEOUT,
    }


def read_key(data_dir: Path) -> str:
    p = Path(os.getenv("RUSTDESK_KEY_FILE", "") or str(data_dir / "rustdesk" / "id_ed25519.pub"))
    try:
        return p.read_text(encoding="utf-8").strip()[:200]
    except OSError:
        return ""


def client_config(host: str, relay: str, key: str) -> dict[str, str]:
    cfg = {"host": host, "relay": relay or "", "key": key}
    cfg["text"] = "\n".join([f"ID server: {host}", f"Relay server: {relay or host}", f"Key: {key or '(public key not found yet)'}", "API server: leave empty"])
    return cfg


def build_router(h: Any) -> APIRouter:
    router = APIRouter(prefix="/api/v1/rustadmin", tags=["rustadmin"])
    audit_path = h.data_dir / "rustadmin" / "audit.jsonl"

    def audit(rec: dict[str, Any]) -> None:
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": _now(), **rec}) + "\n")
        except OSError:
            pass

    def who(request: Request) -> tuple[str, bool]:
        sc = h.request_scope(request)
        email = str(sc.get("real_email") or sc.get("email") or "local")
        return email, bool(sc.get("superadmin") or sc.get("is_super") or h.is_superadmin(sc.get("real_email") or sc.get("email")))

    def need_super(request: Request) -> str:
        email, sup = who(request)
        if not sup:
            raise HTTPException(status_code=403, detail="Super-admin only")
        return email

    @router.get("/status")
    def status(request: Request) -> dict[str, Any]:
        _email, sup = who(request)
        host = os.getenv("RUSTDESK_HOST", "").strip()
        relay = os.getenv("RUSTDESK_RELAY", "").strip()
        key = read_key(h.data_dir)
        console = os.getenv("RUSTDESK_CONSOLE_URL", "").strip()
        return {
            "version": h.app_version,
            "is_super": sup,
            "rustdesk": {
                "configured": bool(host),
                "key_found": bool(key),
                "console_url": console,
                "config": client_config(host, relay, key) if host else None,
                "ports": {"tcp": "21115-21117 (and 21118-21119 only for the web client)", "udp": "21116"},
                "client_note": "Field tablets run RustDesk Android 1.2.3 (2023-10-13). Test one tablet against the server before relying on it; the old client lacks later security fixes.",
            },
            "adb": adb_state(),
        }

    @router.get("/adb/catalog")
    def catalog() -> dict[str, Any]:
        cats: dict[str, list[dict[str, Any]]] = {}
        for c in CATALOG:
            cats.setdefault(c["category"], []).append({k: c[k] for k in ("id", "label", "risk", "host", "note")} | {"args": list(c["args"])})
        return {"categories": cats, "count": len(CATALOG)}

    @router.post("/adb/run")
    def run(body: AdbRunIn, request: Request) -> dict[str, Any]:
        email = need_super(request)
        cmd = BY_ID.get(body.command)
        if not cmd:
            raise HTTPException(status_code=400, detail="Unknown command")
        st = adb_state()
        target = valid_target(body.target) if body.target.strip() else None
        if body.target.strip() and not target:
            raise HTTPException(status_code=400, detail="Target must be IP[:port] inside the allowed networks, or a USB serial")
        if not cmd["host"] and not target:
            raise HTTPException(status_code=400, detail="A target tablet is required")
        try:
            argv = build_argv(cmd, target, body.args, h.data_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        full = [os.getenv("ADB_BIN", "adb")] + ([] if cmd["host"] else ["-s", target or ""]) + argv
        shown = " ".join(["adb"] + full[1:])
        base = {"command": cmd["id"], "risk": cmd["risk"], "target": target or "", "shown": shown}

        plan_only = body.dry_run or not st["enabled"] or not st["binary_found"]
        reason = ""
        if not st["enabled"]:
            reason = "ADB is switched off (RUSTADMIN_ADB_ENABLED=0)."
        elif not st["binary_found"]:
            reason = "No adb binary in this container (build with INSTALL_ADB=1)."
        if cmd["risk"] != RISK_READ and not plan_only:
            if not st["allow_changes"]:
                plan_only, reason = True, "Commands that change a device are plan-only (RUSTADMIN_ADB_ALLOW_CHANGES=0)."
            elif body.confirm.strip() != (target or ""):
                plan_only, reason = True, "Type the target exactly in the confirm box to run a command that changes the device."
        if plan_only:
            audit({"by": email, "result": "plan", **base, "why": reason})
            return {**base, "executed": False, "plan": True, "reason": reason or "Dry run.", "output": ""}
        try:
            cp = subprocess.run(full, capture_output=True, text=True, timeout=TIMEOUT, shell=False, check=False)  # noqa: S603
            out = ((cp.stdout or "") + (("\n[stderr]\n" + cp.stderr) if cp.stderr.strip() else ""))
            rc = cp.returncode
        except subprocess.TimeoutExpired:
            out, rc = f"Timed out after {TIMEOUT}s.", -1
        except OSError as exc:
            out, rc = f"Could not run adb: {exc}", -1
        trunc = len(out) > OUT_CAP
        audit({"by": email, "result": "ran", "rc": rc, "bytes": len(out), **base})
        return {**base, "executed": True, "plan": False, "rc": rc, "output": out[:OUT_CAP], "truncated": trunc}

    @router.get("/adb/audit")
    def audit_tail(request: Request, limit: int = 50) -> dict[str, Any]:
        need_super(request)
        rows: list[dict[str, Any]] = []
        try:
            for line in audit_path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 200)):]:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass
        return {"rows": rows[::-1]}

    return router

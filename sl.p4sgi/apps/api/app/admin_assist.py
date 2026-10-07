"""
Admin assist + Help database for the p4sgi dashboard — 0.4.22 (EXPERIMENTAL).

* /api/v1/assist/*  A guided, plain-language chat about rows an admin selected in the Security audit
  (OAuth apps, users, devices). It proposes next steps as buttons; the SERVER validates every step.
* /api/v1/help/*    Searchable help (built-in articles + admin-reviewed AI answers that grow over time).

Safety model
* Pseudonymised: the AI never receives email addresses, names or serials. People become labels (U1, U2...),
  devices D1.., apps A1... Labels are put back only in the response to the signed-in admin.
* Gemini is used only if CHAT_CLOUD_ENABLED=1 + GEMINI_API_KEY + GEMINI_MODEL are set (see ask_data.py);
  otherwise the local Ollama model is used and nothing leaves the server. ASSIST_ENGINE=local forces local.
* The model can only choose from a fixed list of actions and only targets the admin selected. Nothing is executed:
  plans are dry-run text (security_audit.build_plan), emails are mailto drafts, and the only write is
  'approve app' which needs a super-admin, a written reason and an explicit confirm click.
* Scope-safe: items are re-read server-side with the caller's domain scope; the client sends keys only.
* Prompt-injection note: app names and other fields come from outside; they are length-limited, stripped of
  control characters, and cannot cause any action by themselves (see above).

Disable with ASSIST_ENABLED=0.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import ask_data as ad
from . import security_audit as sa
from .help_seed import HELP_SEED
from .help_topics import HELP_TOPICS

ALL_SEED: list[dict[str, Any]] = [*HELP_SEED, *HELP_TOPICS]  # type: ignore[list-item]

MAX_ITEMS = 25
MAX_USERS_PER_APP = 20
MAX_TURNS = 12
SESSION_TTL_S = 3600
MAX_SESSIONS = 200
MAX_SENDS_PER_HOUR = int(os.getenv("ASSIST_MAX_EMAILS_PER_HOUR", "30") or 30)
_SENDS: dict[str, list[float]] = {}
ASSIST_MODEL = os.getenv("ASSIST_MODEL", "").strip()  # optional smaller/faster local model for Admin assist
MAX_TOKENS = int(os.getenv("ASSIST_MAX_TOKENS", "450") or 450)
CACHE_TTL_S = 3600
_CACHE: dict[str, tuple[float, str]] = {}  # facts signature -> raw model reply (first turn only)
_INDEX_LOCK = threading.Lock()
_INDEXED = {"done": False}

ACTIONS = {"explain", "approve_app", "plan_revoke_token", "plan_offboard_user", "plan_wipe_device", "draft_email", "save_help", "close"}
SUPER_ONLY = {"approve_app", "plan_revoke_token", "plan_offboard_user", "plan_wipe_device"}
_EMAIL_ANY = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_TOKEN = re.compile(r"\b([UAD]\d{1,3})\b")
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean(text: Any, n: int = 120) -> str:
    return _CTRL.sub(" ", str(text or "")).strip()[:n]


# ------------------------------------------------------------ pseudonymisation ---
class Redactor:
    """Two-way label map for one session. Real values never go to the model."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}  # label -> friendly text shown to the admin (e.g. app name) instead of the raw key
        self.fwd: dict[str, str] = {}
        self.rev: dict[str, str] = {}
        self.n: dict[str, int] = {"U": 0, "A": 0, "D": 0}

    def tok(self, value: str, kind: str = "U") -> str:
        v = value.strip()
        if v.lower() in self.fwd:
            return self.fwd[v.lower()]
        self.n[kind] += 1
        t = f"{kind}{self.n[kind]}"
        self.fwd[v.lower()] = t
        self.rev[t] = v
        return t

    def scrub(self, text: str) -> str:
        """Replace known real values, then any remaining email address, with labels."""
        out = text
        for real in sorted(self.fwd, key=len, reverse=True):
            out = re.sub(re.escape(real), self.fwd[real], out, flags=re.I)
        return _EMAIL_ANY.sub(lambda m: self.tok(m.group(0), "U"), out)

    def detok(self, text: str) -> str:
        return _TOKEN.sub(lambda m: self.names.get(m.group(1)) or self.rev.get(m.group(1), m.group(1)), text)

    def generalise(self, text: str) -> str:
        """For the shared help database: labels become generic words, so no one is identifiable."""
        names = {"U": "the user", "A": "the app", "D": "the device"}
        return _TOKEN.sub(lambda m: names[m.group(1)[0]], text)


# --------------------------------------------------------------------- items ---
def _key(kind: str, row: dict[str, Any]) -> str:
    return sa.app_key(row) if kind == "oauth" else (row["email"] if kind == "users" else row["device_id"])


def _label(kind: str, row: dict[str, Any]) -> str:
    if kind == "oauth":
        return clean(row.get("app_name"), 80)
    if kind == "users":
        return row["email"]
    return f"{row.get('model') or 'device'} · {row.get('email') or ''}"


def load_items(h: sa.Helpers, request: Request, kind: str, keys: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    allowed, _ = h.insight_scope(request, None, None)
    now = _now()
    scope_fn = lambda e: h.in_scope(e, allowed)  # noqa: E731
    approvals = sa.load_approvals(h.data_dir / "security" / "approved_apps.json")
    if kind == "oauth":
        data, _ = h.load_sources(sa.SOURCES_OAUTH, allowed)
        rows = sa.apply_approvals(sa.analyse_oauth(data.get("token_activity", []), scope_fn, h.row_email)["rows"], approvals, now)
    elif kind == "users":
        data, _ = h.load_sources(sa.SOURCES_USERS, allowed)
        rows = sa.analyse_users(data.get("users_full", []), data.get("login_activity", []), data.get("admins", []), scope_fn, h.row_email, now)["rows"]
    else:
        data, _ = h.load_sources(sa.SOURCES_DEVICES, allowed)
        rows = sa.analyse_devices(h.build_device_rows(data, allowed), now)["rows"]
    idx = {_key(kind, r): r for r in rows}
    found = [idx[k] for k in dict.fromkeys(keys) if k in idx]
    return found, [k for k in keys if k not in idx]


def build_facts(kind: str, rows: list[dict[str, Any]], red: Redactor) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (facts for the model, ref -> {kind,row} map for the server)."""
    facts, refs = [], {}
    for r in rows:
        if kind == "oauth":
            ref = red.tok(_key(kind, r), "A")
            red.names[ref] = clean(r.get("app_name"), 60) or red.rev[ref]
            users = [red.tok(u, "U") for u in r.get("users", [])[:MAX_USERS_PER_APP]]
            facts.append({"ref": ref, "type": "google_app", "name": clean(r["app_name"], 80), "client_id": clean(r["client_id"], 90),
                          "risk": r["risk_level_raw" if r.get("approved") else "risk_level"], "score": r["risk_score"], "reasons": [clean(x, 60) for x in r.get("reasons", [])][:8],
                          "permissions": [x.rsplit("/", 1)[-1] for x in r.get("scopes", [])][:12], "users": users, "user_count": r["user_count"],
                          "last_seen": str(r.get("last_seen", ""))[:10], "already_approved": bool(r.get("approved")), "approval_problem": r.get("approval_stale")})
        elif kind == "users":
            ref = red.tok(r["email"], "U")
            facts.append({"ref": ref, "type": "account", "flags": r["flags"], "risk": r["risk_level"], "days_since_login": r.get("days_since_login"),
                          "two_step_enrolled": r.get("two_sv_enrolled"), "is_admin": r.get("is_admin"), "failed_logins_7d": r.get("failed_logins_7d")})
        else:
            ref = red.tok(r["device_id"], "D")
            red.names[ref] = f"{clean(r.get('model'), 30) or 'device'} of {r.get('email') or 'unknown owner'}"
            owner = red.tok(r["email"], "U") if r.get("email") else None
            facts.append({"ref": ref, "type": "device", "owner": owner, "model": clean(r.get("model"), 40), "os": clean(r.get("os"), 40),
                          "flags": r["flags"], "compliance_score": r["compliance_score"], "status": r["compliance_status"],
                          "last_sync": str(r.get("last_sync", ""))[:10], "security_patch": str(r.get("security_patch", ""))[:10]})
        refs[ref] = {"kind": kind, "row": r}
    return facts, refs


# ----------------------------------------------------------------- text bits ---
FLAG_TEXT = {
    "NO_2SV": "has no 2-step verification, so a stolen password is enough to get in",
    "ADMIN_NO_2SV": "is an administrator without 2-step verification (high risk: that account can change everything)",
    "NEVER_LOGGED_IN": "has never signed in",
    "INACTIVE": "has not signed in for a long time",
    "SUSPICIOUS_LOGINS": "has had many failed sign-ins recently",
    "UNENCRYPTED": "is not encrypted, so a lost device exposes its data",
    "NO_PASSWORD": "has no screen lock",
    "STALE_SYNC": "has not contacted Google for over 30 days",
    "OUTDATED_PATCH": "has an old security update",
    "DEVELOPER_MODE": "has developer mode on", "USB_DEBUGGING": "has USB debugging on", "UNKNOWN_SOURCES": "allows apps from unknown sources",
    "COMPROMISED": "is reported as compromised (rooted or tampered with)",
}


def baseline_message(kind: str, facts: list[dict[str, Any]]) -> str:
    """Deterministic explanation used when no AI model is reachable."""
    lines = []
    for f in facts[:8]:
        if kind == "oauth":
            lines.append(f"{f['name']} is rated {f['risk']} ({f['score']}) because: {'; '.join(f['reasons']) or 'its permissions are not visible'}. {f['user_count']} account(s) use it.")
        else:
            why = "; ".join(FLAG_TEXT.get(x, x.lower()) for x in f["flags"])
            lines.append(f"{f['ref']} {why or 'has no flags'}.")
    more = f" (+{len(facts) - 8} more)" if len(facts) > 8 else ""
    return " ".join(lines) + more + " Choose a next step below."


def default_options(kind: str) -> list[dict[str, Any]]:
    if kind == "oauth":
        return [{"action": "explain", "label": "Is this app safe? Explain simply", "say": "Is this app safe and what should I check?"},
                {"action": "draft_email", "label": "Email the people who use it"},
                {"action": "approve_app", "label": "It is a known, wanted app: approve it"},
                {"action": "plan_revoke_token", "label": "Prepare steps to remove its access"}]
    if kind == "users":
        return [{"action": "explain", "label": "What does this mean for the school?", "say": "Explain in simple terms what this means and what is the safest next step."},
                {"action": "draft_email", "label": "Email the person"},
                {"action": "plan_offboard_user", "label": "Prepare steps to suspend the account"}]
    return [{"action": "explain", "label": "What should I do about this device?", "say": "Explain simply what is wrong and what the school should do."},
            {"action": "draft_email", "label": "Email the owner"},
            {"action": "plan_wipe_device", "label": "Prepare steps to wipe it (lost or retired)"}]


def system_prompt(is_super: bool) -> str:
    acts = [("explain", "answer a follow-up question in plain language"), ("draft_email", "prepare an email draft to the people involved (target = a label)"),
            ("save_help", "save this advice to the shared help database")]
    if is_super:
        acts += [("approve_app", "mark an app as known and approved (target = app label)"), ("plan_revoke_token", "prepare GAM steps to remove an app's access (target = app label)"),
                 ("plan_offboard_user", "prepare steps to suspend an account (target = user label)"), ("plan_wipe_device", "prepare steps to wipe a device (target = device label)")]
    return (
        "You are the built-in IT helper for a school-system administrator in Sierra Leone who may not be technical. "
        "Explain clearly in plain, friendly language, short sentences, no jargon (say 'a third-party app' not 'OAuth client'). "
        "You are given FACTS about the rows the admin selected. People are labelled U1, U2; apps A1; devices D1. Use only the labels, never invent names or addresses, never ask for personal data. "
        "Be honest about uncertainty: a high score is a reason to look, not proof of harm. You cannot run anything: you only propose steps the admin may choose.\n"
        "Reply with ONE JSON object: {\"message\": \"<=110 words\", \"options\": [{\"label\": \"short button text\", \"action\": \"<one of the actions>\", \"target\": \"<label or null>\", \"say\": \"question text for explain, else null\"}]}. "
        "Give 2 to 5 options, most sensible first, phrased as questions or choices an admin understands (for example: 'Do you want to remove this app's access?', 'Do you want me to draft an email to U1?'). "
        "Allowed actions: " + "; ".join(f"{a} = {d}" for a, d in acts) + "."
    )


def validate_options(raw: Any, refs: dict[str, Any], red: Redactor, is_super: bool, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    users_known = {t for t in red.rev if t.startswith("U")}
    for o in raw[:8]:
        if not isinstance(o, dict):
            continue
        act = str(o.get("action") or "")
        if act not in ACTIONS or (act in SUPER_ONLY and not is_super):
            continue
        target = o.get("target")
        target = str(target).strip() if target else None
        if act == "explain":
            target = None
        elif act in ("approve_app", "plan_revoke_token"):
            if target not in refs or refs[target]["kind"] != "oauth":
                continue
        elif act == "plan_wipe_device":
            if target not in refs or refs[target]["kind"] != "devices":
                continue
        elif act == "plan_offboard_user":
            if not (target in users_known):
                continue
        elif act == "draft_email":
            if target is not None and target not in refs and target not in users_known:
                continue
        label = clean(o.get("label"), 90)
        if not label:
            continue
        out.append({"action": act, "target": target, "label": label, "say": clean(o.get("say"), 300) or None})
        if len(out) >= 5:
            break
    return out


# --------------------------------------------------------------- email drafts ---
def compose_email(kind: str, item: dict[str, Any], recipients: list[str]) -> tuple[str, str]:
    r = item["row"]
    if kind == "oauth":
        subj = f"Please confirm you use \"{clean(r['app_name'], 60)}\" with your school Google account"
        body = (f"Hello,\n\nOur security check shows that the app \"{clean(r['app_name'], 60)}\" has been given access to your school Google account "
                f"(it can: {', '.join(re.sub(r'\s*\(\+\d+\)', '', x) for x in r.get('reasons', [])[:4]) or 'see parts of your account'}).\n\n"
                "If you installed it on purpose and still need it, just reply \"yes, I use it\". "
                "If you do not recognise it or no longer need it, reply \"remove it\" and we will remove its access. No action is needed from you to remove it.\n\nThank you.")
    elif kind == "users":
        fl = r.get("flags", [])
        if "NO_2SV" in fl or "ADMIN_NO_2SV" in fl:
            subj, body = ("Please turn on 2-step verification for your school Google account",
                          "Hello,\n\nTo protect your account if your password is ever stolen, please turn on 2-step verification:\n1. Open myaccount.google.com/security while signed in.\n2. Choose \"2-Step Verification\" and follow the steps (a phone prompt is simplest).\n\nIf you need help, reply to this email.\n\nThank you.")
        elif "NEVER_LOGGED_IN" in fl:
            subj, body = ("Is your school Google account working?",
                          "Hello,\n\nOur records show this account has not been used yet. Please try to sign in at accounts.google.com. If you cannot sign in, or you no longer need the account, reply to this email and we will help or close it.\n\nThank you.")
        elif "SUSPICIOUS_LOGINS" in fl:
            subj, body = ("Several failed sign-in attempts on your account",
                          "Hello,\n\nWe saw several failed sign-in attempts on your account in the last week. If that was you forgetting the password, no problem; please reply and we will reset it. If it was not you, please reply straight away.\n\nThank you.")
        else:
            subj, body = ("Do you still use your school Google account?",
                          "Hello,\n\nThis account has not been used for a long time. If you still need it, please sign in once to keep it active. If not, reply and we will close it safely.\n\nThank you.")
    else:
        fl = ", ".join(FLAG_TEXT.get(x, x.lower()) for x in r.get("flags", [])[:4])
        subj = f"Please check the school device ({clean(r.get('model'), 40)})"
        body = (f"Hello,\n\nOur check shows the device ({clean(r.get('model'), 40)}) {fl or 'needs attention'}.\n\nPlease: (1) connect it to Wi-Fi and sign in so it can update, "
                "(2) set a screen lock, (3) tell us if it is lost or no longer used so we can protect the school data on it.\n\nThank you.")
    return subj, body


def mailto(to: list[str], subject: str, body: str) -> str:
    first, rest = to[:1], to[1:12]
    url = f"mailto:{','.join(first)}?subject={quote(subject)}&body={quote(body)}"
    if rest:
        url += f"&bcc={quote(','.join(rest))}"
    return url[:1900]


# ------------------------------------------------------------- help database ---
class HelpStore:
    def __init__(self, base: Path):
        self.base = base
        self.lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            d = json.loads((self.base / "articles.json").read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, d: dict[str, Any]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.base / "articles.json.tmp"
        tmp.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.base / "articles.json")

    def all(self, include_drafts: bool) -> list[dict[str, Any]]:
        out = [{**a, "source": "seed", "status": "approved", "helpful": 0, "unhelpful": 0, "asked": 0} for a in ALL_SEED]
        for aid, a in self._read().items():
            if include_drafts or a.get("status") == "approved":
                out.append({"id": aid, **a})
        return out

    def get(self, aid: str) -> dict[str, Any] | None:
        for a in self.all(True):
            if a["id"] == aid:
                return a
        return None

    def upsert(self, aid: str | None, fields: dict[str, Any], dedupe_key: str | None = None) -> tuple[str, bool]:
        """Create or update a stored article. Returns (id, created)."""
        with self.lock:
            d = self._read()
            if dedupe_key:
                for k, a in d.items():
                    if a.get("dedupe") == dedupe_key:
                        a["asked"] = int(a.get("asked", 0)) + 1
                        a["updated_at"] = _now().isoformat(timespec="seconds")
                        self._write(d)
                        return k, False
            if aid and aid in d:
                d[aid].update(fields)
                d[aid]["updated_at"] = _now().isoformat(timespec="seconds")
                self._write(d)
                return aid, False
            nid = aid or "kb-" + hashlib.sha1((fields.get("title", "") + str(time.time())).encode()).hexdigest()[:8]
            d[nid] = {"category": "Q&A from admins", "tags": [], "status": "draft", "source": "ai", "asked": 1, "helpful": 0, "unhelpful": 0,
                      "created_at": _now().isoformat(timespec="seconds"), "updated_at": _now().isoformat(timespec="seconds"),
                      **({"dedupe": dedupe_key} if dedupe_key else {}), **fields}
            self._write(d)
            return nid, True

    def mutate(self, aid: str, fn: Callable[[dict[str, Any]], None]) -> bool:
        with self.lock:
            d = self._read()
            if aid not in d:
                return False
            fn(d[aid])
            self._write(d)
            return True

    def delete(self, aid: str) -> bool:
        with self.lock:
            d = self._read()
            if aid not in d:
                return False
            d.pop(aid)
            self._write(d)
            return True


_WORD = re.compile(r"[a-z0-9_]{3,}")


def search_articles(arts: list[dict[str, Any]], q: str, k: int = 6) -> list[dict[str, Any]]:
    qs = set(_WORD.findall(q.lower()))
    if not qs:
        return []
    scored = []
    for a in arts:
        title = set(_WORD.findall(a["title"].lower()))
        tags = set(_WORD.findall(" ".join(a.get("tags", [])).lower()))
        body = _WORD.findall(a["body"].lower())
        s = 3 * len(qs & title) + 2 * len(qs & tags) + sum(1 for w in body if w in qs) * 0.3
        s += 0.2 * (int(a.get("helpful", 0)) - int(a.get("unhelpful", 0)))
        if s > 0:
            scored.append((s, a))
    scored.sort(key=lambda t: -t[0])
    return [a for _, a in scored[:k]]


# ----------------------------------------------------------------- request models ---
class StartIn(BaseModel):
    kind: str = Field(..., pattern=r"^(oauth|users|devices)$")
    keys: list[str] = Field(..., min_length=1, max_length=MAX_ITEMS)


class ReplyIn(BaseModel):
    session_id: str = Field(..., min_length=8, max_length=64)
    option_id: str | None = Field(None, max_length=20)
    text: str | None = Field(None, max_length=600)
    note: str | None = Field(None, max_length=500)
    confirm: bool = False


class AskHelpIn(BaseModel):
    question: str = Field(..., min_length=3, max_length=600)
    ai: bool = False  # False = instant answer from the guides (pgvector lookup); True = also let the AI phrase it


class SendEmailIn(BaseModel):
    session_id: str = Field(..., min_length=8, max_length=64)
    to: list[str] = Field(..., min_length=1, max_length=MAX_USERS_PER_APP)
    subject: str = Field(..., min_length=3, max_length=200)
    body: str = Field(..., min_length=10, max_length=4000)


class ArticleIn(BaseModel):
    id: str | None = Field(None, pattern=r"^kb-[a-z0-9]{4,12}$")
    title: str = Field(..., min_length=3, max_length=200)
    body: str = Field(..., min_length=3, max_length=6000)
    category: str = Field("Q&A from admins", max_length=60)
    tags: list[str] = Field(default_factory=list, max_length=12)


class StatusIn(BaseModel):
    status: str = Field(..., pattern=r"^(approved|draft|archived)$")


class VoteIn(BaseModel):
    helpful: bool


# ------------------------------------------------------------ help lookup (pgvector) ---
VECTOR_MIN_SCORE = 0.40


def chunk_text(a: dict[str, Any]) -> str:
    return f"Help: {a.get('title', '')}. {a.get('body', '')}"


def smtp_state() -> dict[str, Any]:
    host, frm = os.getenv("SMTP_HOST", "").strip(), os.getenv("SMTP_FROM", "").strip()
    return {"configured": bool(host and frm), "from": frm if host and frm else None}


def send_smtp(to: str, subject: str, body: str, reply_to: str | None) -> None:
    """Send ONE message through the configured SMTP server. Raises RuntimeError with a short reason."""
    host, port = os.getenv("SMTP_HOST", "").strip(), int(os.getenv("SMTP_PORT", "587") or 587)
    user, pw, frm = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASSWORD", ""), os.getenv("SMTP_FROM", "").strip()
    mode = os.getenv("SMTP_SECURITY", "starttls").strip().lower()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = frm, to, re.sub(r"[\r\n]+", " ", subject)[:200]
    if reply_to and _EMAIL_ANY.fullmatch(reply_to):
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    try:
        cls = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
        with cls(host, port, timeout=20) as c:
            if mode == "starttls":
                c.starttls()
            if user:
                c.login(user, pw)
            c.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{type(exc).__name__}: {exc}"[:160]) from exc


SAMPLES = {
    "oauth": ["Is this app safe for a school?", "Who uses it and do they still need it?", "What happens if I remove its access?", "Which of these should I look at first?"],
    "users": ["Why is this account flagged?", "What is the safest next step?", "Should this account be suspended?", "Which of these should I look at first?"],
    "devices": ["Is this device probably lost?", "What should the school do about it?", "Should we wipe it?", "Which of these should I look at first?"],
}


# -------------------------------------------------------------------- router ---
def build_router(h: sa.Helpers) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["admin-assist"])
    store = HelpStore(h.data_dir / "help")
    audit_path = h.data_dir / "assist" / "audit.jsonl"
    approvals_path = h.data_dir / "security" / "approved_apps.json"
    sec_audit_dir = h.data_dir / "security" / "audit"
    sessions: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def audit(rec: dict[str, Any]) -> None:
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": _now().isoformat(timespec="seconds"), **rec}) + "\n")
        except OSError:
            pass

    def who(request: Request) -> tuple[dict[str, Any], str]:
        sc = h.request_scope(request)
        return sc, str(sc.get("real_email") or sc.get("email") or "local")

    def engine() -> dict[str, Any]:
        if os.getenv("ASSIST_ENGINE", "").strip().lower() != "local":
            cs = ad.cloud_state("gemini")
            if cs["available"]:
                return {"provider": "gemini", "model": cs["model"], "label": "Gemini (anonymised facts only)", "cloud": True}
        return {"provider": "ollama", "model": ASSIST_MODEL or ad.CHAT_MODEL, "label": "Local model (nothing leaves this server)", "cloud": False}

    def call_model(eng: dict[str, Any], system: str, user: str, warnings: list[str], json_out: bool = True) -> str | None:
        for e in ([eng] + ([{"provider": "ollama", "model": ASSIST_MODEL or ad.CHAT_MODEL, "label": "Local model", "cloud": False}] if eng["cloud"] else [])):
            try:
                text = ad.llm(e["provider"], e["model"], system, user, json_mode=(json_out and e["provider"] == "ollama"), max_tokens=MAX_TOKENS)
                if e is not eng:
                    warnings.append("Gemini was unreachable; answered by the local model.")
                    eng.update(provider=e["provider"], model=e["model"], label=e["label"], cloud=False)
                return text
            except RuntimeError as exc:
                warnings.append(str(exc)[:160])
        return None

    def new_session(request: Request, kind: str, keys: list[str]) -> tuple[str, dict[str, Any]]:
        sc, user = who(request)
        rows, missing = load_items(h, request, kind, keys)
        if not rows:
            raise HTTPException(404, {"error": "items_not_found", "detail": "those rows are not in your current data or scope; reload the table"})
        red = Redactor()
        facts, refs = build_facts(kind, rows, red)
        sid = uuid.uuid4().hex
        s = {"id": sid, "by": user, "super": bool(sc.get("superadmin")), "kind": kind, "red": red, "facts": facts, "refs": refs,
             "history": [], "pending": {}, "t": time.time(), "missing": missing, "engine": engine(), "question_for_help": None}
        with lock:
            now = time.time()
            for k in [k for k, v in sessions.items() if now - v["t"] > SESSION_TTL_S]:
                sessions.pop(k, None)
            while len(sessions) >= MAX_SESSIONS:
                sessions.pop(min(sessions, key=lambda k: sessions[k]["t"]))
            sessions[sid] = s
        return sid, s

    def get_session(sid: str, request: Request) -> dict[str, Any]:
        _, user = who(request)
        s = sessions.get(sid)
        if not s or s["by"] != user or time.time() - s["t"] > SESSION_TTL_S:
            raise HTTPException(404, {"error": "session_expired", "detail": "this assist session has expired; select the rows and start again"})
        s["t"] = time.time()
        return s

    def ask_model(s: dict[str, Any], admin_text: str | None) -> dict[str, Any]:
        warnings: list[str] = []
        red: Redactor = s["red"]
        convo = "\n".join(f"{t['role']}: {t['text']}" for t in s["history"][-MAX_TURNS:])
        user_prompt = ("FACTS (JSON):\n" + json.dumps(s["facts"], ensure_ascii=False) + "\n\nCONVERSATION SO FAR:\n" + (convo or "(none)")
                       + "\n\nADMIN: " + (red.scrub(admin_text) if admin_text else "Please explain these items and suggest what to do next.") + "\n\nReply with the JSON object only.")
        ckey = hashlib.sha1((system_prompt(s["super"]) + user_prompt).encode()).hexdigest() if not admin_text and not s["history"] else None
        hit = _CACHE.get(ckey) if ckey else None
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            raw = hit[1]
        else:
            raw = call_model(s["engine"], system_prompt(s["super"]), user_prompt, warnings)
            if raw and ckey:
                if len(_CACHE) > 300:
                    _CACHE.clear()
                _CACHE[ckey] = (time.time(), raw)
        msg, opts = None, []
        if raw:
            try:
                j = ad._json_obj(raw)
                msg = clean(j.get("message"), 1200) or None
                opts = validate_options(j.get("options"), s["refs"], red, s["super"], s["kind"])
            except (ValueError, AttributeError):
                msg = clean(raw, 1200)
                warnings.append("The AI reply was not in the expected format; showing it as text.")
        if not msg:
            msg = baseline_message(s["kind"], s["facts"])
            warnings.append("No AI model answered, so this is the built-in explanation.")
        if not opts:
            opts = validate_options(default_options(s["kind"]), s["refs"], red, s["super"], s["kind"]) or []
            # default options carry no target: bind them to the first matching item
            first = next(iter(s["refs"]), None)
            for o in opts:
                if o["action"] in ("approve_app", "plan_revoke_token", "plan_wipe_device", "plan_offboard_user", "draft_email") and not o["target"]:
                    o["target"] = first
            opts = [o for o in opts if o["action"] not in ("approve_app", "plan_revoke_token", "plan_wipe_device") or o["target"] in s["refs"]]
            if s["kind"] == "users" and s["super"]:
                opts = [o for o in opts if o["action"] != "plan_offboard_user" or o["target"] in s["red"].rev]
        s["history"].append({"role": "assistant", "text": msg})
        s["pending"] = {}
        out_opts = []
        for i, o in enumerate(opts, 1):
            oid = f"o{len(s['history'])}_{i}"
            s["pending"][oid] = o
            out_opts.append({"id": oid, "label": red.detok(o["label"]), "action": o["action"], "danger": o["action"].startswith("plan_") or o["action"] == "approve_app"})
        msg_out = red.detok(msg)
        # grow the shared help database from the first answer of a session (generalised, no personal data)
        if len(s["history"]) == 1 and not any("No AI model" in w for w in warnings):
            sig = s["kind"] + ":" + ",".join(sorted({f for x in s["facts"] for f in (x.get("flags") or x.get("reasons", [])[:2])}))[:160]
            title = {"oauth": "Reviewing a third-party app with access to Google accounts", "users": "Handling flagged user accounts", "devices": "Handling flagged devices"}[s["kind"]]
            store.upsert(None, {"title": f"{title} ({sig.split(':', 1)[1][:60] or 'general'})", "body": red.generalise(msg), "tags": [s["kind"], "assist"],
                                "category": "Q&A from admins"}, dedupe_key=hashlib.sha1(sig.encode()).hexdigest()[:12])
        return {"message": msg_out, "options": out_opts, "warnings": warnings,
                "engine": {"label": s["engine"]["label"], "cloud": s["engine"]["cloud"]}}

    def baseline_turn(s: dict[str, Any]) -> dict[str, Any]:
        """Instant first answer without the model (built-in explanation + default buttons); the UI then asks /assist/enhance."""
        red: Redactor = s["red"]
        msg = baseline_message(s["kind"], s["facts"])
        opts = validate_options(default_options(s["kind"]), s["refs"], red, s["super"], s["kind"])
        first = next(iter(s["refs"]), None)
        for o in opts:
            if not o["target"] and o["action"] in ("approve_app", "plan_revoke_token", "plan_wipe_device", "plan_offboard_user", "draft_email"):
                o["target"] = first
        opts = [o for o in opts if not (o["action"] in ("approve_app", "plan_revoke_token", "plan_wipe_device") and o["target"] not in s["refs"])]
        opts = [o for o in opts if not (o["action"] == "plan_offboard_user" and o["target"] not in red.rev)]
        s["history"].append({"role": "assistant", "text": msg})
        s["pending"] = {}
        out = []
        for i, o in enumerate(opts, 1):
            oid = f"o{len(s['history'])}_{i}"
            s["pending"][oid] = o
            out.append({"id": oid, "label": red.detok(o["label"]), "action": o["action"], "danger": o["action"].startswith("plan_") or o["action"] == "approve_app"})
        return {"message": red.detok(msg), "options": out, "warnings": [], "ai_pending": True,
                "engine": {"label": s["engine"]["label"], "cloud": s["engine"]["cloud"]}}

    # ------------------------------------------------------------ assist endpoints
    @router.get("/assist/status")
    def assist_status(request: Request) -> dict:
        sc, _ = who(request)
        e = engine()
        return {"module": "admin_assist", "version": h.app_version, "experimental": True, "superadmin": bool(sc.get("superadmin")),
                "engine": {"label": e["label"], "cloud": e["cloud"], "model": e["model"]}, "gemini": ad.cloud_state("gemini"),
                "privacy": "The AI receives labels (U1, A1, D1), risk flags and counts only. No email addresses, names or serial numbers.",
                "actions": sorted(ACTIONS), "max_items": MAX_ITEMS, "smtp": smtp_state(), "samples": SAMPLES}

    @router.post("/assist/start")
    def assist_start(body: StartIn, request: Request) -> dict:
        sid, s = new_session(request, body.kind, [k.strip()[:256] for k in body.keys])
        r = baseline_turn(s)
        audit({"by": s["by"], "event": "start", "kind": body.kind, "items": len(s["facts"]), "engine": s["engine"]["label"]})
        return {"session_id": sid, "kind": body.kind, "items": [{"ref": f["ref"], "label": _label(body.kind, s["refs"][f["ref"]]["row"])} for f in s["facts"]],
                "skipped": s["missing"], "samples": SAMPLES[body.kind], **r}

    @router.post("/assist/enhance")
    def assist_enhance(body: ReplyIn, request: Request) -> dict:
        """Second step of a session start: let the AI replace the instant built-in explanation (only if nothing was chosen meanwhile)."""
        s = get_session(body.session_id, request)
        if len(s["history"]) != 1 or s.get("enhanced"):
            return {"skipped": True}
        s["enhanced"] = True
        epoch = s.get("epoch", 0)
        t = {**s, "history": [], "pending": {}}  # work on a copy: the admin may click a button while the AI is thinking
        r = ask_model(t, None)
        if s.get("epoch", 0) != epoch:
            return {"skipped": True}
        s["history"], s["pending"] = t["history"], t["pending"]
        r["engine"] = {"label": s["engine"]["label"], "cloud": s["engine"]["cloud"]}
        return {"skipped": False, **r}

    def _targets_for(s: dict[str, Any], target: str | None) -> dict[str, Any]:
        if target in s["refs"]:
            return s["refs"][target]
        raise HTTPException(422, {"error": "bad_target"})

    def _users_of(s: dict[str, Any], target: str) -> list[str]:
        """Real emails for a target label (an app's users, a user label, or a device owner)."""
        red: Redactor = s["red"]
        if target in s["refs"]:
            it = s["refs"][target]
            if it["kind"] == "oauth":
                return list(it["row"].get("users", []))[:MAX_USERS_PER_APP]
            return [it["row"]["email"]] if it["row"].get("email") else []
        if target in red.rev and target.startswith("U"):
            return [red.rev[target]]
        return []

    def run_action(s: dict[str, Any], o: dict[str, Any], body: ReplyIn, request: Request) -> dict[str, Any]:
        act, target = o["action"], o.get("target")
        red: Redactor = s["red"]
        if act in SUPER_ONLY and not s["super"]:
            raise HTTPException(403, {"error": "superadmin_required"})
        if act == "close":
            return {"type": "closed"}
        if act == "save_help":
            last = next((t["text"] for t in reversed(s["history"]) if t["role"] == "assistant"), "")
            aid, _ = store.upsert(None, {"title": clean(s.get("question_for_help") or f"Advice: {s['kind']} review", 120), "body": red.generalise(last), "tags": [s["kind"], "assist"]},
                                  dedupe_key=hashlib.sha1((s.get("question_for_help") or last).encode()).hexdigest()[:12])
            return {"type": "saved", "id": aid, "text": "Saved to the help database as a draft for an administrator to review."}
        if act == "draft_email":
            if target:
                emails = _users_of(s, target)
            else:
                emails = [e for ref in s["refs"] for e in _users_of(s, ref)][:MAX_USERS_PER_APP]
            emails = list(dict.fromkeys(emails))
            if not emails:
                return {"type": "info", "text": "I could not find an email address for that item."}
            item = s["refs"][target] if target in s["refs"] else next(iter(s["refs"].values()))
            subj, text = compose_email(item["kind"], item, emails)
            s["email_allowed"] = set(emails)
            sm = smtp_state()
            return {"type": "email", "to": emails, "subject": subj, "body": text, "mailto": mailto(emails, subj, text),
                    "smtp": {"configured": sm["configured"], "from": sm["from"], "can_send": sm["configured"] and s["super"]},
                    "note": "Nothing has been sent. Check the addresses and wording, then open it in your mail program"
                            + (" or send it from here." if sm["configured"] and s["super"] else ".")}

        if act == "approve_app":
            it = _targets_for(s, target)
            if it["row"]["risk_level"] == "UNKNOWN":
                return {"type": "info", "text": "This app's permissions are not visible in the data, so it cannot be approved."}
            if not body.confirm or not (body.note or "").strip() or len(body.note.strip()) < 3:
                return {"type": "confirm", "option_id": body.option_id, "needs_note": True,
                        "text": f"Mark \"{clean(it['row']['app_name'], 60)}\" as a known, approved app for {sa.APPROVAL_DEFAULT_DAYS} days? It will show as APPROVED instead of {it['row'].get('risk_level_raw') or it['row']['risk_level']}. "
                                "This only changes the label in this dashboard. It is cancelled automatically if the app asks for new permissions. Write the reason:"}
            rec = sa.record_approval(approvals_path, sec_audit_dir, sa.app_key(it["row"]), it["row"], body.note.strip()[:500], s["by"], sa.APPROVAL_DEFAULT_DAYS, _now())
            audit({"by": s["by"], "event": "approve_app", "key": sa.app_key(it["row"])})
            return {"type": "done", "text": f"Approved until {rec['review_by'][:10]}. Reload the table to see it."}
        if act == "plan_revoke_token":
            it = _targets_for(s, target)
            row = it["row"]
            if not row.get("client_id"):
                return {"type": "info", "text": "This app has no client ID in the data, so a precise revoke command cannot be built. Remove it in Admin console > Security > API controls."}
            steps, skipped = [], []
            for e in _users_of(s, target):
                try:
                    steps += sa.build_plan(sa.ActionPlanIn(action="revoke_token", email=e, client_id=row["client_id"]), h.is_superadmin)
                except HTTPException as exc:
                    skipped.append({"who": e, "reason": str(exc.detail.get("detail") if isinstance(exc.detail, dict) else exc.detail)})
            audit({"by": s["by"], "event": "plan_revoke_token", "app": sa.app_key(row), "steps": len(steps), "skipped": len(skipped)})
            return {"type": "plan", "title": f"Remove access of \"{clean(row['app_name'], 60)}\" for {len(steps)} account(s)", "steps": steps, "skipped": skipped,
                    "alternative": "To block the app for everyone: Admin console > Security > Access and data control > API controls.",
                    "note": sa.PLAN_NOTE}
        if act == "plan_offboard_user":
            emails = _users_of(s, target)
            if not emails:
                raise HTTPException(422, {"error": "bad_target"})
            try:
                steps = sa.build_plan(sa.ActionPlanIn(action="offboard_user", email=emails[0]), h.is_superadmin)
            except HTTPException as exc:
                return {"type": "info", "text": str(exc.detail.get("detail") if isinstance(exc.detail, dict) else exc.detail)}
            audit({"by": s["by"], "event": "plan_offboard_user", "steps": len(steps)})
            return {"type": "plan", "title": "Suspend this account safely (reversible)", "steps": steps, "skipped": [], "note": sa.PLAN_NOTE}
        if act == "plan_wipe_device":
            it = _targets_for(s, target)
            email = it["row"].get("email") or ""
            import shlex

            steps = [{"step": "1. Find the device's GAM resourceId", "command": f"gam print mobile query {shlex.quote('email:' + email)} fields resourceId,deviceId,model,status"},
                     {"step": "2. Wipe ONLY the school account data (replace RESOURCE_ID with the value from step 1)", "command": "gam update mobile RESOURCE_ID action admin_account_wipe"}]
            audit({"by": s["by"], "event": "plan_wipe_device"})
            return {"type": "plan", "title": "Wipe school data from this device (irreversible: confirm it is lost or retired first)", "steps": steps, "skipped": [], "note": sa.PLAN_NOTE}
        raise HTTPException(422, {"error": "unknown_action"})

    @router.post("/assist/send-email")
    def assist_send_email(body: SendEmailIn, request: Request) -> dict:
        s = get_session(body.session_id, request)
        if not s["super"]:
            raise HTTPException(403, {"error": "superadmin_required"})
        if not smtp_state()["configured"]:
            raise HTTPException(409, {"error": "smtp_not_configured", "detail": "Sending from the dashboard is not set up. Use 'Open in my mail program', or ask the server administrator to set SMTP_HOST and SMTP_FROM."})
        allowed = s.get("email_allowed") or set()
        to = list(dict.fromkeys(e.strip() for e in body.to))
        bad = [e for e in to if e not in allowed]
        if not allowed or bad:
            raise HTTPException(422, {"error": "recipient_not_allowed", "detail": "You can only send to the people on the rows you selected."})
        with lock:
            now = time.time()
            hist = [t for t in _SENDS.get(s["by"], []) if now - t < 3600]
            if len(hist) + len(to) > MAX_SENDS_PER_HOUR:
                raise HTTPException(429, {"error": "send_limit", "detail": f"At most {MAX_SENDS_PER_HOUR} emails per hour from the dashboard."})
            _SENDS[s["by"]] = hist + [now] * len(to)
        sc, _ = who(request)
        reply_to = str(sc.get("real_email") or "") or None
        footer = "\n\n--\nSent by the school IT administrator through the dashboard" + (f" (reply to {reply_to})." if reply_to else ".")
        results = []
        for e in to:
            try:
                send_smtp(e, body.subject, body.body.strip() + footer, reply_to)
                results.append({"to": e, "ok": True})
            except RuntimeError as exc:
                results.append({"to": e, "ok": False, "error": str(exc)})
        audit({"by": s["by"], "event": "send_email", "to": to, "ok": sum(1 for r in results if r["ok"]), "subject": body.subject[:80]})
        return {"results": results, "sent": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}

    @router.post("/assist/reply")
    def assist_reply(body: ReplyIn, request: Request) -> dict:
        s = get_session(body.session_id, request)
        s["epoch"] = s.get("epoch", 0) + 1
        if len(s["history"]) > 2 * MAX_TURNS:
            raise HTTPException(429, {"error": "session_too_long", "detail": "this conversation is long: start a new one"})
        red: Redactor = s["red"]
        if body.option_id:
            o = s["pending"].get(body.option_id)
            if not o:
                raise HTTPException(404, {"error": "option_expired"})
            s["history"].append({"role": "admin", "text": f"(chose) {o['label']}"})
            if o["action"] == "explain":
                s["question_for_help"] = o.get("say") or o["label"]
                r = ask_model(s, o.get("say") or o["label"])
                return {"result": None, **r}
            res = run_action(s, o, body, request)
            if res["type"] == "confirm":
                s["pending"][body.option_id] = o  # keep it for the confirm click
                return {"result": res, "message": None, "options": [], "warnings": [], "engine": {"label": s["engine"]["label"], "cloud": s["engine"]["cloud"]}}
            s["pending"].pop(body.option_id, None)
            follow = {"message": {"plan": "Here are the steps. Nothing has been run.", "email": "Here is the draft. Nothing has been sent.", "done": res.get("text"),
                                  "saved": res.get("text"), "info": res.get("text"), "closed": "Closed."}.get(res["type"], ""),
                      "options": [{"id": "x_more", "label": "Ask another question", "action": "explain", "danger": False}] if res["type"] != "closed" else [],
                      "warnings": [], "engine": {"label": s["engine"]["label"], "cloud": s["engine"]["cloud"]}}
            s["pending"]["x_more"] = {"action": "explain", "target": None, "label": "Ask another question", "say": "What else should I check about these items?"}
            return {"result": res, **follow}
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(422, {"error": "empty_reply"})
        s["history"].append({"role": "admin", "text": red.scrub(text)})
        s["question_for_help"] = red.generalise(red.scrub(text))[:200]
        r = ask_model(s, text)
        audit({"by": s["by"], "event": "free_text", "chars": len(text), "engine": s["engine"]["label"]})
        return {"result": None, **r}

    # --------------------------------------------------------------- help endpoints
    def index_help(force: bool = False) -> dict[str, Any]:
        """Load built-in + approved articles into pgvector (source 'help'). Best effort, never raises."""
        rows = [("help", a["id"], chunk_text(a)) for a in store.all(False)]
        res = ad.upsert_chunks(rows)
        n, v = ad.count_chunks("help")
        res.update(expected=len(rows), in_db=n, with_vectors=v, at=_now().isoformat(timespec="seconds"))
        _INDEXED.update(done=res["indexed"] > 0, tried=time.time(), result=res)
        return res

    def maybe_index() -> None:
        """First help use after a restart: index in the background so lookups are vector-based, without making the user wait."""
        if _INDEXED.get("done") or time.time() - float(_INDEXED.get("tried", 0)) < 600:
            return
        with _INDEX_LOCK:
            if _INDEXED.get("done") or time.time() - float(_INDEXED.get("tried", 0)) < 600:
                return
            _INDEXED["tried"] = time.time()
        threading.Thread(target=index_help, name="help-index", daemon=True).start()

    def lookup(q: str, include_drafts: bool, k: int = 6) -> tuple[list[dict[str, Any]], str]:
        """Meaning search in pgvector first (fast, local), keyword fallback. Returns (articles, mode)."""
        arts = {a["id"]: a for a in store.all(include_drafts)}
        hits: list[dict[str, Any]] = []
        mode = "keyword"
        try:
            chunks, m = ad.search_chunks(q, k * 2, source="help")
            for c in chunks:
                a = arts.get(c.get("ref"))
                if not a or a in hits:
                    continue
                if m == "vector" and float(c.get("score") or 0) < VECTOR_MIN_SCORE:
                    continue
                hits.append(a)
            if hits:
                mode = "meaning (pgvector)" if m == "vector" else "full-text (database)"
        except Exception:  # noqa: BLE001
            hits = []
        for a in search_articles(list(arts.values()), q, k):  # fill / fall back (also finds unreviewed drafts for super-admins)
            if a not in hits:
                hits.append(a)
        return hits[:k], mode

    @router.get("/help/articles")
    def help_articles(request: Request, category: str | None = Query(None, max_length=60)) -> dict:
        sc, _ = who(request)
        maybe_index()
        arts = store.all(bool(sc.get("superadmin")))
        if category:
            arts = [a for a in arts if a.get("category") == category]
        cats = sorted({a.get("category", "") for a in store.all(bool(sc.get("superadmin")))})
        return {"can_edit": bool(sc.get("superadmin")), "categories": cats, "total": len(arts),
                "rows": [{k: a.get(k) for k in ("id", "title", "body", "category", "tags", "source", "status", "asked", "helpful", "unhelpful", "updated_at")} for a in arts]}

    @router.get("/help/status")
    def help_index_status(request: Request) -> dict:
        sc, _ = who(request)
        n, v = ad.count_chunks("help")
        expected = len(store.all(False))
        return {"expected": expected, "in_db": n, "with_vectors": v, "pgvector": bool(ad.ensure_setup().get("vector")),
                "last_index": _INDEXED.get("result"), "can_edit": bool(sc.get("superadmin"))}

    @router.post("/help/reindex")
    def help_reindex(request: Request) -> dict:
        user = need_super(request)
        res = index_help(force=True)
        audit({"by": user, "event": "help_reindex", "indexed": res.get("indexed"), "vectors": res.get("vectors")})
        return res

    @router.get("/help/search")
    def help_search(request: Request, q: str = Query(..., min_length=2, max_length=200)) -> dict:
        sc, _ = who(request)
        maybe_index()
        hits, mode = lookup(q, bool(sc.get("superadmin")))
        return {"q": q, "mode": mode, "rows": [{k: a.get(k) for k in ("id", "title", "body", "category", "source", "status")} for a in hits]}

    @router.post("/help/ask")
    def help_ask(body: AskHelpIn, request: Request) -> dict:
        sc, user = who(request)
        maybe_index()
        q = _EMAIL_ANY.sub("[email]", body.question.strip())
        arts, mode = lookup(q, bool(sc.get("superadmin")), 4)
        warnings: list[str] = []
        eng = engine()
        used_ai = False
        ans = None
        if arts and body.ai:
            ctx = "\n\n".join(f"[{i + 1}] {a['title']}: {a['body']}" for i, a in enumerate(arts))
            ans = call_model(eng, "You are the help assistant of a school-system dashboard. Answer ONLY from the numbered help notes, in plain friendly language for a non-technical administrator, "
                             "max 120 words, cite like [1]. If the notes do not answer it, say so and suggest asking a super-admin.", f"Notes:\n{ctx}\n\nQuestion: {q}", warnings, json_out=False)
            used_ai = bool(ans)
            if not ans:
                warnings.append("No AI model answered; showing the best matching guide.")
        if not ans:
            ans = arts[0]["body"] if arts else "I do not have a guide on that yet. Your question has been saved so an administrator can write one."
        created, aid = False, None
        if used_ai or not arts:  # only save what adds knowledge: an AI-phrased answer, or a gap nobody has answered yet
            aid, created = store.upsert(None, {"title": clean(q, 140), "body": clean(ans if used_ai else "(no guide yet: needs an answer)", 1500),
                                               "tags": ["question"], "category": "Q&A from admins"},
                                        dedupe_key=hashlib.sha1(re.sub(r"\W+", " ", q.lower()).strip().encode()).hexdigest()[:12])
        audit({"by": user, "event": "help_ask", "chars": len(q), "engine": eng["label"] if used_ai else "lookup", "hits": len(arts), "mode": mode})
        return {"answer": ans.strip(), "sources": [{"n": i + 1, "id": a["id"], "title": a["title"]} for i, a in enumerate(arts)],
                "saved_as": aid, "new_question": created, "mode": mode, "ai": used_ai, "engine": eng["label"] if used_ai else "Guide lookup (no AI needed)",
                "warnings": warnings}

    def need_super(request: Request) -> str:
        sc, user = who(request)
        if not sc.get("superadmin"):
            raise HTTPException(403, {"error": "superadmin_required"})
        return user

    @router.post("/help/articles")
    def help_save(body: ArticleIn, request: Request) -> dict:
        user = need_super(request)
        if body.id and body.id in {a["id"] for a in ALL_SEED}:
            raise HTTPException(422, {"error": "seed_readonly"})
        aid, created = store.upsert(body.id, {"title": body.title.strip(), "body": body.body.strip(), "category": body.category.strip(),
                                              "tags": [t.strip()[:30] for t in body.tags][:12], "source": "admin", "status": "approved", "reviewed_by": user})
        try:
            ad.upsert_chunk("help", aid, chunk_text(store.get(aid) or {}))
        except Exception:  # noqa: BLE001
            pass
        audit({"by": user, "event": "help_save", "id": aid})
        return {"ok": True, "id": aid, "created": created}

    @router.post("/help/articles/{aid}/status")
    def help_status(aid: str, body: StatusIn, request: Request) -> dict:
        user = need_super(request)
        ok = store.mutate(aid, lambda a: a.update(status=body.status, reviewed_by=user, updated_at=_now().isoformat(timespec="seconds")))
        if not ok:
            raise HTTPException(404, {"error": "unknown_article", "detail": "built-in articles cannot be changed"})
        if body.status == "approved":
            try:  # best effort: make it searchable by the Ask-the-data chat (pgvector) too
                a = store.get(aid) or {}
                ad.upsert_chunk("help", aid, chunk_text(a))
            except Exception:  # noqa: BLE001
                pass
        else:
            ad.delete_chunk("help", aid)
        audit({"by": user, "event": "help_status", "id": aid, "status": body.status})
        return {"ok": True}

    @router.delete("/help/articles/{aid}")
    def help_delete(aid: str, request: Request) -> dict:
        user = need_super(request)
        if not store.delete(aid):
            raise HTTPException(404, {"error": "unknown_article"})
        ad.delete_chunk("help", aid)
        audit({"by": user, "event": "help_delete", "id": aid})
        return {"ok": True}

    @router.post("/help/articles/{aid}/vote")
    def help_vote(aid: str, body: VoteIn, request: Request) -> dict:
        who(request)
        key = "helpful" if body.helpful else "unhelpful"
        if not store.mutate(aid, lambda a: a.__setitem__(key, int(a.get(key, 0)) + 1)):
            return {"ok": False, "note": "built-in articles are not voted on"}
        return {"ok": True}

    return router

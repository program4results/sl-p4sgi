"""
Ask-the-data chat assistant (EXPERIMENTAL) for the p4sgi dashboard — 0.4.21.

Natural-language questions over (a) the Postgres database, via a SAFE read-only SQL path, and
(b) a knowledge base (PIA workbook, docs) via pgvector similarity search (full-text fallback).

Safety model
* SQL runs ONLY against curated chat_* VIEWS that omit personal columns (no learner names, serials,
  SIM/WhatsApp numbers, tablet IDs, telemetry payloads). The raw tables are never exposed.
* Model-written SQL is validated (single SELECT, view allow-list, no comments/DDL/DML/functions
  that read the system) and executed in a READ ONLY transaction with a statement timeout and row cap.
* SQL mode is super-admin only. Docs mode is open to any signed-in user.
* Local Ollama is the default. Cloud providers (Grok, ChatGPT, Claude, Gemini) are OFF unless
  CHAT_CLOUD_ENABLED=1 and a key + model are configured; even then a cloud model only ever sees the
  question, the view schema and (docs mode) PIA text. Result rows never leave this server.
* Every question is logged (question, SQL, provider, row count, who) to DATA_DIR/chat/audit.jsonl; rows are not logged.

Wired from main.py with a guarded include_router. Disable with ASK_DATA_ENABLED=0.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import db

# ---------------------------------------------------------------- config ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen2.5-coder:14b")
EMBED_MODEL = os.getenv("CHAT_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = 768
MAX_ROWS = int(os.getenv("CHAT_MAX_ROWS", "200") or 200)
SQL_TIMEOUT_MS = int(os.getenv("CHAT_SQL_TIMEOUT_MS", "8000") or 8000)
KNOWLEDGE_DIRS = [Path(p) for p in os.getenv("CHAT_KNOWLEDGE_DIRS", "/knowledge/docs").split(",") if p.strip()]

CLOUD = {  # provider -> (key env, model env, label)
    "xai": ("XAI_API_KEY", "XAI_MODEL", "Grok (xAI)"),
    "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", "ChatGPT (OpenAI)"),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "Claude (Anthropic)"),
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL", "Gemini (Google)"),
}


def cloud_enabled() -> bool:
    return os.getenv("CHAT_CLOUD_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def cloud_state(provider: str) -> dict[str, Any]:
    key_env, model_env, label = CLOUD[provider]
    if not cloud_enabled():
        return {"label": label, "available": False, "reason": "cloud models are off (CHAT_CLOUD_ENABLED=0)"}
    if not os.getenv(key_env):
        return {"label": label, "available": False, "reason": f"{key_env} is not set"}
    if not os.getenv(model_env):
        return {"label": label, "available": False, "reason": f"{model_env} is not set"}
    return {"label": label, "available": True, "model": os.getenv(model_env), "reason": None}


# ------------------------------------------------- safe views (the only SQL surface) ---
VIEWS: dict[str, dict[str, Any]] = {
    "chat_schools": {
        "doc": "One row per school.",
        "cols": "emis, name, district, province, created_at",
        "ddl": "SELECT emis, name, district, province, created_at FROM schools",
    },
    "chat_devices": {
        "doc": "Registered tablets (device identifiers and phone numbers are withheld).",
        "cols": "emis, device_type, app_version, last_seen, created_at",
        "ddl": "SELECT emis, device_type, app_version, last_seen, created_at FROM devices",
    },
    "chat_telemetry": {
        "doc": "Tablet telemetry events by kind, without payload content.",
        "cols": "emis, kind (skill_run|prompt|publish|llm_local|llm_online|data_mb|radar_point), created_at",
        "ddl": "SELECT d.emis AS emis, t.kind AS kind, t.created_at AS created_at FROM telemetry_events t LEFT JOIN devices d ON d.id = t.device_id",
    },
    "chat_attendance": {
        "doc": "Attendance syncs per class and day. Counts only; no learner names.",
        "cols": "emis_code, class_label, attendance_date, detected_count, confirmed_count, absent_count, mean_confidence, manual_review_required, province, district, synced_at",
        "ddl": ("SELECT emis_code, class_label, attendance_date, detected_count, confirmed_count, "
                "jsonb_array_length(absent_named) AS absent_count, mean_confidence, manual_review_required, "
                "province, district, synced_at FROM attendance_sync_events"),
    },
    "chat_publish_events": {
        "doc": "School config publish / tablet webhook events.",
        "cols": "emis, kind, status, created_at",
        "ddl": "SELECT emis, kind, status, created_at FROM publish_events",
    },
    "chat_jobs": {
        "doc": "Provisioning jobs.",
        "cols": "status (pending|approved|rejected|failed), emis, school_name, created_at, approved_at",
        "ddl": "SELECT status, emis, school_name, created_at, approved_at FROM provisioning_jobs",
    },
}
ALLOWED_VIEWS = set(VIEWS)

# ------------------------------------------------------------- SQL validation ---
_DENY_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|execute|vacuum|analyze|set|reset|show|"
    r"listen|notify|prepare|deallocate|declare|fetch|move|lock|comment|refresh|reindex|cluster|merge|into|returning|do|"
    r"regclass|regproc|regtype|oid)\b", re.I)
_DENY_FUNCS = re.compile(
    r"\b(pg_\w*|lo_\w+|dblink\w*|information_schema|set_config|current_setting|current_database|current_user|"
    r"session_user|version|txid_\w+|inet_\w+)\b", re.I)
_LOCKING = re.compile(r"\bfor\s+(update|share|no\s+key|key)\b", re.I)


class SqlRejected(ValueError):
    pass


def _strip_literals(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate_sql(sql: str) -> str:
    """Return a cleaned single SELECT statement or raise SqlRejected."""
    s = (sql or "").strip()
    if not s:
        raise SqlRejected("empty query")
    if len(s) > 4000:
        raise SqlRejected("query too long")
    s = s.rstrip().rstrip(";").strip()
    if ";" in _strip_literals(s):
        raise SqlRejected("only a single statement is allowed")
    bare = _strip_literals(s)
    if "--" in bare or "/*" in bare or "*/" in bare:
        raise SqlRejected("comments are not allowed")
    if '"' in bare or "$$" in bare or "\\" in bare:
        raise SqlRejected("quoted identifiers, dollar quoting and backslashes are not allowed")
    low = bare.lower().lstrip("( \n\t")
    if not (low.startswith("select") or low.startswith("with")):
        raise SqlRejected("only SELECT queries are allowed")
    m = _DENY_WORDS.search(bare)
    if m:
        raise SqlRejected(f"keyword not allowed: {m.group(1).lower()}")
    m = _DENY_FUNCS.search(bare)
    if m:
        raise SqlRejected(f"function or schema not allowed: {m.group(1).lower()}")
    if _LOCKING.search(bare):
        raise SqlRejected("row locking is not allowed")
    # relations: strip FROM-bearing constructs that are not table references
    rel = re.sub(r"\bis\s+(not\s+)?distinct\s+from\b", " ", bare, flags=re.I)
    for _ in range(4):
        rel = re.sub(r"\b(extract|substring|trim|overlay|position)\s*\([^()]*\)", " ", rel, flags=re.I)
    ctes = {c.lower() for c in re.findall(r"(?:\bwith\s+(?:recursive\s+)?|,\s*)([a-z_]\w*)\s+as\s*\(", rel, flags=re.I)}
    tables = [t.lower() for t in re.findall(r"\b(?:from|join)\s+([A-Za-z_][\w\.]*)", rel, flags=re.I)]
    if not tables:
        raise SqlRejected("query must read from one of the chat_* views")
    for t in tables:
        if "." in t:
            raise SqlRejected("schema-qualified names are not allowed")
        if t not in ALLOWED_VIEWS and t not in ctes:
            raise SqlRejected(f"table not allowed: {t}. Use only: {', '.join(sorted(ALLOWED_VIEWS))}")
    if re.search(r"\bfrom\s+\w+(\s+(as\s+)?\w+)?\s*,", rel, flags=re.I):
        raise SqlRejected("comma joins are not allowed; use JOIN")
    return s


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (Decimal, UUID)):
        return float(v) if isinstance(v, Decimal) else str(v)
    if isinstance(v, (dict, list)):
        return v
    return str(v)


def run_readonly(sql: str, max_rows: int = MAX_ROWS, timeout_ms: int = SQL_TIMEOUT_MS) -> tuple[list[str], list[list[Any]], bool]:
    import psycopg
    from psycopg.rows import tuple_row

    with psycopg.connect(db.database_url(), row_factory=tuple_row) as conn:
        conn.read_only = True
        conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        cur = conn.execute(f"SELECT * FROM ({sql}) AS q LIMIT {int(max_rows) + 1}")
        cols = [d.name for d in cur.description or []]
        rows = cur.fetchmany(int(max_rows) + 1)
    truncated = len(rows) > max_rows
    return cols, [[_jsonable(c) for c in r] for r in rows[:max_rows]], truncated


# ----------------------------------------------------------- DB set-up (additive) ---
_SETUP_DONE: dict[str, Any] = {"ok": False, "vector": False, "error": None}


def ensure_setup(force: bool = False) -> dict[str, Any]:
    """Create chat_* views, chat_chunks (+pgvector if installed). Idempotent, additive, no existing object touched."""
    if _SETUP_DONE["ok"] and not force:
        return _SETUP_DONE
    vector = False
    try:
        with db.get_conn() as conn:
            for name, v in VIEWS.items():
                conn.execute(f"CREATE OR REPLACE VIEW {name} AS {v['ddl']}")
        try:
            with db.get_conn() as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            vector = True
        except Exception:  # noqa: BLE001  (image without pgvector: keep going with full-text search)
            vector = False
        with db.get_conn() as conn:
            emb = f", embedding vector({EMBED_DIM})" if vector else ""
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chat_chunks ("
                "id BIGSERIAL PRIMARY KEY, source TEXT NOT NULL, ref TEXT NOT NULL, content TEXT NOT NULL, "
                "model TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                f"tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED{emb}, UNIQUE (source, ref))")
            conn.execute("CREATE INDEX IF NOT EXISTS chat_chunks_tsv_idx ON chat_chunks USING gin (tsv)")
            if vector:
                conn.execute("CREATE INDEX IF NOT EXISTS chat_chunks_vec_idx ON chat_chunks USING hnsw (embedding vector_cosine_ops)")
        _SETUP_DONE.update(ok=True, vector=vector, error=None)
    except Exception as exc:  # noqa: BLE001
        _SETUP_DONE.update(ok=False, vector=vector, error=str(exc)[:300])
    return _SETUP_DONE


# ------------------------------------------------------------------ LLM calls ---
def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 180) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https/http URLs from config)
        return json.loads(r.read().decode())


def _get_json(url: str, timeout: int = 5) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def ollama_models() -> tuple[bool, list[str], str | None]:
    try:
        d = _get_json(f"{OLLAMA_URL}/api/tags")
        return True, sorted(m["name"] for m in d.get("models", [])), None
    except Exception as exc:  # noqa: BLE001
        return False, [], f"{type(exc).__name__}: {exc}"[:200]


def llm(provider: str, model: str, system: str, user: str, json_mode: bool = False, max_tokens: int | None = None) -> str:
    """One chat completion. Returns text. Raises RuntimeError with a short reason."""
    try:
        if provider == "ollama":
            body: dict[str, Any] = {"model": model, "stream": False, "keep_alive": "30m", "options": {"temperature": 0},
                                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if json_mode:
                body["format"] = "json"
            if max_tokens:  # 0.4.22: short answers are much faster on a local model
                body["options"]["num_predict"] = int(max_tokens)
                body["think"] = False  # thinking models (qwen3...) otherwise spend most of the time "thinking"
            return _post_json(f"{OLLAMA_URL}/api/chat", body, timeout=300)["message"]["content"]
        key = os.getenv(CLOUD[provider][0], "")
        if provider in ("xai", "openai"):
            url = "https://api.x.ai/v1/chat/completions" if provider == "xai" else "https://api.openai.com/v1/chat/completions"
            d = _post_json(url, {"model": model, "temperature": 0, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                           {"Authorization": f"Bearer {key}"})
            return d["choices"][0]["message"]["content"]
        if provider == "anthropic":
            d = _post_json("https://api.anthropic.com/v1/messages", {"model": model, "max_tokens": 1024, "system": system,
                           "messages": [{"role": "user", "content": user}]}, {"x-api-key": key, "anthropic-version": "2023-06-01"})
            return d["content"][0]["text"]
        if provider == "gemini":
            d = _post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}]},
                           {"x-goog-api-key": key})
            return d["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{provider} not reachable: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{provider} call failed: {type(exc).__name__}: {exc}"[:240]) from exc
    raise RuntimeError(f"unknown provider {provider}")


def embed(texts: list[str]) -> list[list[float]]:
    d = _post_json(f"{OLLAMA_URL}/api/embed", {"model": EMBED_MODEL, "input": texts}, timeout=300)
    vecs = d.get("embeddings") or []
    if len(vecs) != len(texts) or any(len(v) != EMBED_DIM for v in vecs):
        raise RuntimeError(f"embedding model {EMBED_MODEL} did not return {EMBED_DIM}-dim vectors")
    return vecs


def _json_obj(text: str) -> dict[str, Any]:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in model output")
    return json.loads(text[a:b + 1])


# ------------------------------------------------------------------- prompts ---
def schema_prompt() -> str:
    lines = [f"- {n}({v['cols']}): {v['doc']}" for n, v in VIEWS.items()]
    return "\n".join(lines)


def sql_system_prompt() -> str:
    return (
        "You turn questions about Sierra Leone MBSSE school tablets and attendance into ONE PostgreSQL 16 SELECT.\n"
        "Use ONLY these views (no other tables, no schema prefixes):\n" + schema_prompt() + "\n"
        "Rules: single SELECT (CTEs allowed), no comments, no semicolons inside, no functions that read the system, "
        "always aggregate when the question asks for counts or averages, prefer ILIKE for text matching, "
        "dates are relative to now() (use CURRENT_DATE), keep results small.\n"
        "If the question is NOT answerable from these views (policy, privacy, PIA, how-to), reply {\"mode\":\"docs\"}.\n"
        "Reply with JSON only: {\"mode\":\"sql\",\"sql\":\"SELECT ...\",\"explanation\":\"one sentence\"}."
    )


# --------------------------------------------------------------- knowledge base ---
def _chunk(text: str, size: int = 900) -> list[str]:
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(cur) + len(para) + 2 > size and cur:
            out.append(cur)
            cur = ""
        while len(para) > size:
            out.append(para[:size])
            para = para[size:]
        cur = (cur + "\n\n" + para).strip() if cur else para
    if cur:
        out.append(cur)
    return out


def collect_knowledge(data_dir: Path) -> list[tuple[str, str, str]]:
    """(source, ref, content) rows. No personal data: PIA text, docs and user-supplied knowledge files."""
    rows: list[tuple[str, str, str]] = []
    try:
        from . import privacy_monitor as pm

        wb = {}
        try:
            wb = json.loads((data_dir / "privacy" / "workbook.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        if not wb.get("controls"):
            wb = pm.load_seed()
        cat = {c[0]: c for c in pm.CONTROLS}
        for c in wb.get("controls", []):
            m = cat.get(c["id"])
            rows.append(("pia", c["id"], f"PIA control {c['id']} ({c.get('area', '')}): {c['question']} "
                         + (f"Monitor measures: {m[5]}. Evidence from: {m[2]}. Bill ref: {m[4]}." if m else "")))
        for r in wb.get("risks", []):
            rows.append(("pia", r["id"], f"PIA risk {r['id']} ({r['category']}): {r['description']} Inherent score {r['inherent']} {r['inherent_band']}. "
                         f"Mitigation: {r['mitigation']}"))
        for a in wb.get("actions", []):
            rows.append(("pia", a["id"], f"PIA action {a['id']} for risks {', '.join(a['risks'])}: {a['action']} Owner {a['owner']}. Priority {a['priority']}."))
        for s in wb.get("stakeholders", []):
            rows.append(("pia", s["id"], f"PIA stakeholder {s['name']}: {s['role']}. Consultation required: {s['required']}. Method: {s['method']}."))
        for i, x in enumerate(wb.get("legal", [])):
            rows.append(("pia-legal", f"L{i + 1:02d}", f"{x['topic']}: {x['text']}"))
    except Exception:  # noqa: BLE001
        pass
    try:  # 0.4.22: built-in help articles + approved admin-assist articles (no personal data: generalised text only)
        from .help_seed import HELP_SEED
        from .help_topics import HELP_TOPICS

        for a in HELP_SEED + HELP_TOPICS:
            rows.append(("help", str(a["id"]), f"Help: {a['title']}. {a['body']}"))
        hp = data_dir / "help" / "articles.json"
        if hp.is_file():
            for aid, a in json.loads(hp.read_text(encoding="utf-8")).items():
                if isinstance(a, dict) and a.get("status") == "approved":
                    rows.append(("help", str(aid), f"Help: {a.get('title', '')}. {a.get('body', '')}"))
    except Exception:  # noqa: BLE001
        pass
    for n, v in VIEWS.items():
        rows.append(("schema", n, f"Database view {n}: {v['doc']} Columns: {v['cols']}."))
    dirs = [data_dir / "knowledge", *KNOWLEDGE_DIRS]
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*")):
            if p.suffix.lower() not in (".md", ".txt") or not p.is_file():
                continue
            for i, ch in enumerate(_chunk(p.read_text(encoding="utf-8", errors="replace"))):
                rows.append((f"doc:{p.name}", f"{i + 1:03d}", ch))
    return [(s, r, c[:1200]) for s, r, c in rows if c.strip()]


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def reindex(data_dir: Path) -> dict[str, Any]:
    st = ensure_setup()
    if not st["ok"]:
        raise RuntimeError("database set-up failed: " + str(st["error"]))
    rows = collect_knowledge(data_dir)
    vec_ok, warn = st["vector"], None
    done = 0
    embedded = 0
    for i in range(0, len(rows), 16):
        batch = rows[i:i + 16]
        vecs: list[list[float]] | None = None
        if vec_ok:
            try:
                vecs = embed([c for _, _, c in batch])
                embedded += len(batch)
            except Exception as exc:  # noqa: BLE001
                warn, vec_ok = f"embeddings unavailable ({str(exc)[:120]}); stored for full-text search only", False
        with db.get_conn() as conn:
            for j, (s, r, c) in enumerate(batch):
                if vecs:
                    conn.execute(
                        "INSERT INTO chat_chunks (source, ref, content, model, embedding) VALUES (%s,%s,%s,%s,%s::vector) "
                        "ON CONFLICT (source, ref) DO UPDATE SET content=EXCLUDED.content, model=EXCLUDED.model, embedding=EXCLUDED.embedding, created_at=now()",
                        (s, r, c, EMBED_MODEL, _vec_literal(vecs[j])))
                else:
                    conn.execute(
                        "INSERT INTO chat_chunks (source, ref, content, model) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (source, ref) DO UPDATE SET content=EXCLUDED.content, model=EXCLUDED.model, created_at=now()",
                        (s, r, c, None))
        done += len(batch)
    keep = {(s, r) for s, r, _ in rows}
    with db.get_conn() as conn:  # drop chunks whose source row no longer exists
        have = conn.execute("SELECT source, ref FROM chat_chunks").fetchall()
        stale = [h for h in have if (h["source"], h["ref"]) not in keep]
        for h in stale:
            conn.execute("DELETE FROM chat_chunks WHERE source=%s AND ref=%s", (h["source"], h["ref"]))
    return {"chunks": done, "removed_stale": len(stale), "vectors": embedded > 0, "embedded": embedded, "warning": warn}


def upsert_chunk(source: str, ref: str, content: str) -> bool:
    """Best-effort single-chunk index (used when a help article is approved). Never raises."""
    try:
        st = ensure_setup()
        if not st["ok"]:
            return False
        content = content[:1200]
        vec = None
        if st["vector"]:
            try:
                vec = embed([content])[0]
            except Exception:  # noqa: BLE001
                vec = None
        with db.get_conn() as conn:
            if vec:
                conn.execute(
                    "INSERT INTO chat_chunks (source, ref, content, model, embedding) VALUES (%s,%s,%s,%s,%s::vector) "
                    "ON CONFLICT (source, ref) DO UPDATE SET content=EXCLUDED.content, model=EXCLUDED.model, embedding=EXCLUDED.embedding, created_at=now()",
                    (source, ref, content, EMBED_MODEL, _vec_literal(vec)))
            else:
                conn.execute(
                    "INSERT INTO chat_chunks (source, ref, content, model) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (source, ref) DO UPDATE SET content=EXCLUDED.content, model=EXCLUDED.model, created_at=now()",
                    (source, ref, content, None))
        return True
    except Exception:  # noqa: BLE001
        return False


def upsert_chunks(rows: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Batch index (source, ref, content). Vectors if the embedding model answers, else full-text only. Never raises."""
    out: dict[str, Any] = {"indexed": 0, "vectors": 0, "warning": None}
    try:
        st = ensure_setup()
        if not st["ok"]:
            out["warning"] = "database set-up failed: " + str(st.get("error"))[:120]
            return out
        vec_ok = bool(st["vector"])
        for i in range(0, len(rows), 16):
            batch = [(s, r, c[:1200]) for s, r, c in rows[i:i + 16]]
            vecs = None
            if vec_ok:
                try:
                    vecs = embed([c for _, _, c in batch])
                except Exception as exc:  # noqa: BLE001
                    vec_ok = False
                    out["warning"] = f"embeddings unavailable ({str(exc)[:100]}); stored for full-text search only"
            with db.get_conn() as conn:
                for j, (s_, r_, c_) in enumerate(batch):
                    if vecs:
                        conn.execute(
                            "INSERT INTO chat_chunks (source, ref, content, model, embedding) VALUES (%s,%s,%s,%s,%s::vector) "
                            "ON CONFLICT (source, ref) DO UPDATE SET content=EXCLUDED.content, model=EXCLUDED.model, embedding=EXCLUDED.embedding, created_at=now()",
                            (s_, r_, c_, EMBED_MODEL, _vec_literal(vecs[j])))
                    else:  # no embedding column when pgvector is not installed
                        conn.execute(
                            "INSERT INTO chat_chunks (source, ref, content, model) VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT (source, ref) DO UPDATE SET content=EXCLUDED.content, model=EXCLUDED.model, created_at=now()",
                            (s_, r_, c_, None))
            out["indexed"] += len(batch)
            out["vectors"] += len(batch) if vecs else 0
    except Exception as exc:  # noqa: BLE001
        out["warning"] = f"{type(exc).__name__}: {exc}"[:160]
    return out


def count_chunks(source: str) -> tuple[int, int]:
    """(chunks, chunks with a vector) for a source. (0, 0) if the table does not exist or the DB is down."""
    try:
        with db.get_conn() as conn:
            if _SETUP_DONE.get("vector"):
                r = conn.execute("SELECT count(*) AS n, count(embedding) AS v FROM chat_chunks WHERE source=%s", (source,)).fetchone()
                return int(r["n"]), int(r["v"])
            r = conn.execute("SELECT count(*) AS n FROM chat_chunks WHERE source=%s", (source,)).fetchone()
            return int(r["n"]), 0
    except Exception:  # noqa: BLE001
        return 0, 0


def delete_chunk(source: str, ref: str) -> bool:
    """Best-effort removal of one indexed chunk. Never raises."""
    try:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM chat_chunks WHERE source=%s AND ref=%s", (source, ref))
        return True
    except Exception:  # noqa: BLE001
        return False


def search_chunks(question: str, k: int = 6, source: str | None = None) -> tuple[list[dict[str, Any]], str]:
    st = ensure_setup()
    if not st["ok"]:
        return [], "none"
    if st["vector"]:
        try:
            qv = embed([question])[0]
            with db.get_conn() as conn:
                rows = conn.execute(
                    "SELECT source, ref, content, 1 - (embedding <=> %s::vector) AS score FROM chat_chunks "
                    "WHERE embedding IS NOT NULL AND (%s::text IS NULL OR source = %s) ORDER BY embedding <=> %s::vector LIMIT %s",
                    (_vec_literal(qv), source, source, _vec_literal(qv), k)).fetchall()
            if rows:
                return [dict(r) for r in rows], "vector"
        except Exception:  # noqa: BLE001
            pass
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT source, ref, content, ts_rank(tsv, q) AS score FROM chat_chunks, plainto_tsquery('simple', %s) q "
            "WHERE tsv @@ q AND (%s::text IS NULL OR source = %s) ORDER BY score DESC LIMIT %s", (question, source, source, k)).fetchall()
        if not rows:
            words = [w for w in re.findall(r"[A-Za-z0-9]{4,}", question)][:4]
            if words:
                rows = conn.execute("SELECT source, ref, content, 0.0 AS score FROM chat_chunks WHERE (" + " OR ".join(["content ILIKE %s"] * len(words)) + ") AND (%s::text IS NULL OR source = %s) LIMIT %s",
                                    tuple(f"%{w}%" for w in words) + (source, source, k)).fetchall()
    return [dict(r) for r in rows], "fulltext"


# -------------------------------------------------------------------- request ---
class AskIn(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)
    provider: Literal["ollama", "xai", "openai", "anthropic", "gemini"] = "ollama"
    model: str | None = Field(None, max_length=120, pattern=r"^[A-Za-z0-9._:/-]*$")
    mode: Literal["auto", "sql", "docs"] = "auto"


@dataclass
class Helpers:
    request_scope: Callable[[Request], dict[str, Any]]
    data_dir: Path
    app_version: str


def build_router(h: Helpers) -> APIRouter:
    router = APIRouter(prefix="/api/v1/chat", tags=["ask-data"])
    audit_dir = h.data_dir / "chat"

    def _audit(rec: dict[str, Any]) -> None:
        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
            with (audit_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **rec}) + "\n")
        except OSError:
            pass

    def _who(request: Request) -> tuple[dict[str, Any], str]:
        sc = h.request_scope(request)
        return sc, str(sc.get("real_email") or sc.get("email") or "local")

    @router.get("/status")
    def status(request: Request) -> dict:
        sc, _ = _who(request)
        up, models, err = ollama_models()
        st = _SETUP_DONE
        return {
            "module": "ask_data", "version": h.app_version, "experimental": True, "superadmin": bool(sc.get("superadmin")),
            "ollama": {"url": OLLAMA_URL, "reachable": up, "models": models, "error": err,
                       "hint": None if up else "Ollama must listen on 0.0.0.0 (OLLAMA_HOST=0.0.0.0) so the container can reach host.docker.internal:11434"},
            "default_model": CHAT_MODEL, "embed_model": EMBED_MODEL,
            "embed_model_installed": any(m.split(":")[0] == EMBED_MODEL.split(":")[0] for m in models),
            "cloud": {p: cloud_state(p) for p in CLOUD},
            "database": {"setup_done": st["ok"], "pgvector": st["vector"], "error": st["error"],
                         "views": sorted(ALLOWED_VIEWS)},
            "limits": ["SQL mode is super-admin only and reads only the curated chat_* views (no names, serials, SIM or WhatsApp numbers).",
                       "Cloud models are OFF by default; when on they never receive result rows.",
                       f"Results are capped at {MAX_ROWS} rows and {SQL_TIMEOUT_MS // 1000}s.",
                       "GAM report data (users, devices, logins) is not queryable yet; use the Security audit panel."],
        }

    @router.post("/setup")
    def setup(request: Request) -> dict:
        sc, who = _who(request)
        if not sc.get("superadmin"):
            raise HTTPException(403, {"error": "superadmin_required"})
        r = ensure_setup(force=True)
        _audit({"by": who, "action": "setup", "ok": r["ok"], "vector": r["vector"]})
        if not r["ok"]:
            raise HTTPException(500, {"error": "setup_failed", "message": r["error"]})
        return {"ok": True, "pgvector": r["vector"], "views": sorted(ALLOWED_VIEWS)}

    @router.post("/reindex")
    def do_reindex(request: Request) -> dict:
        sc, who = _who(request)
        if not sc.get("superadmin"):
            raise HTTPException(403, {"error": "superadmin_required"})
        try:
            r = reindex(h.data_dir)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, {"error": "reindex_failed", "message": str(exc)[:300]}) from exc
        _audit({"by": who, "action": "reindex", **{k: r[k] for k in ("chunks", "vectors")}})
        return {"ok": True, **r}

    @router.post("/ask")
    def ask(body: AskIn, request: Request) -> dict:
        t0 = time.time()
        sc, who = _who(request)
        is_super = bool(sc.get("superadmin"))
        provider = body.provider
        if provider != "ollama":
            cs = cloud_state(provider)
            if not cs["available"]:
                raise HTTPException(403, {"error": "cloud_disabled", "message": cs["reason"]})
            model = os.getenv(CLOUD[provider][1], "")
        else:
            model = body.model or CHAT_MODEL
        warnings: list[str] = []
        if provider != "ollama":
            warnings.append("Cloud model used: it saw your question and the view schema, never result rows.")
        mode = body.mode
        if mode == "sql" and not is_super:
            raise HTTPException(403, {"error": "superadmin_required", "message": "Database questions are super-admin only. Try Docs mode."})
        question = body.question.strip()

        def finish(res: dict[str, Any]) -> dict[str, Any]:
            res.update(provider=provider, model=model, elapsed_ms=int((time.time() - t0) * 1000), warnings=warnings)
            _audit({"by": who, "q": question[:500], "mode": res.get("mode"), "sql": res.get("sql"), "provider": provider,
                    "model": model, "rows": res.get("row_count"), "error": res.get("error")})
            return res

        # ---- route
        plan: dict[str, Any] = {"mode": "docs"}
        if mode in ("auto", "sql") and is_super:
            st = ensure_setup()
            if not st["ok"]:
                raise HTTPException(503, {"error": "db_setup_failed", "message": st["error"]})
            try:
                plan = _json_obj(llm(provider, model, sql_system_prompt(), question, json_mode=True))
            except RuntimeError as exc:
                raise HTTPException(502, {"error": "llm_unavailable", "message": str(exc)}) from exc
            except ValueError:
                plan = {"mode": "docs"} if mode == "auto" else {"mode": "sql", "sql": ""}
            if mode == "sql":
                plan["mode"] = "sql"
        if plan.get("mode") == "sql":
            return finish(_answer_sql(provider, model, question, plan, warnings))
        return finish(_answer_docs(provider, model, question, warnings))

    return router


def _answer_sql(provider: str, model: str, question: str, plan: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    sql_text = str(plan.get("sql") or "")
    last_err = ""
    cols: list[str] = []
    rows: list[list[Any]] = []
    truncated = False
    for attempt in (1, 2):
        try:
            clean = validate_sql(sql_text)
            cols, rows, truncated = run_readonly(clean)
            sql_text = clean
            last_err = ""
            break
        except SqlRejected as exc:
            last_err = f"rejected: {exc}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"database error: {str(exc).splitlines()[0][:200]}"
        if attempt == 1:
            try:
                plan = _json_obj(llm(provider, model, sql_system_prompt(),
                                     f"{question}\n\nYour previous SQL failed ({last_err}). Previous SQL:\n{sql_text}\nReturn corrected JSON.", json_mode=True))
                sql_text = str(plan.get("sql") or "")
            except Exception:  # noqa: BLE001
                break
    if last_err:
        return {"mode": "sql", "answer": "I could not produce a safe query for that question. " + last_err, "sql": sql_text,
                "columns": [], "rows": [], "row_count": 0, "truncated": False, "sources": [], "error": last_err}
    answer = f"{len(rows)} row(s) returned."
    try:  # summary is always generated locally so rows never leave the server
        up, _, _ = ollama_models()
        if up and rows:
            sample = json.dumps({"columns": cols, "rows": rows[:30]}, default=str)
            answer = llm("ollama", CHAT_MODEL if provider != "ollama" else model,
                         "Answer the question in at most 4 short sentences using ONLY the data given. Do not invent numbers.",
                         f"Question: {question}\nSQL: {sql_text}\nData (first 30 rows of {len(rows)}): {sample}")
        elif not rows:
            answer = "No matching records."
    except RuntimeError as exc:
        warnings.append(f"summary unavailable: {exc}")
    return {"mode": "sql", "answer": answer.strip(), "sql": sql_text, "explanation": plan.get("explanation"),
            "columns": cols, "rows": rows[:100], "row_count": len(rows), "truncated": truncated or len(rows) > 100, "sources": []}


def _answer_docs(provider: str, model: str, question: str, warnings: list[str]) -> dict[str, Any]:
    chunks, how = search_chunks(question)
    if not chunks:
        return {"mode": "docs", "answer": "I found nothing relevant in the knowledge base. Ask a super-admin to run Reindex, or rephrase.",
                "sql": None, "columns": [], "rows": [], "row_count": 0, "truncated": False, "sources": [], "retrieval": how}
    ctx = "\n\n".join(f"[{i + 1}] ({c['source']} {c['ref']}) {c['content']}" for i, c in enumerate(chunks))
    try:
        ans = llm(provider, model, "Answer ONLY from the numbered context. Cite sources like [1]. If the context does not contain the answer, say you do not know. "
                  "Be concise. The Data Protection Bill is a draft and not enacted.", f"Context:\n{ctx}\n\nQuestion: {question}")
    except RuntimeError as exc:
        raise HTTPException(502, {"error": "llm_unavailable", "message": str(exc)}) from exc
    return {"mode": "docs", "answer": ans.strip(), "sql": None, "columns": [], "rows": [], "row_count": 0, "truncated": False, "retrieval": how,
            "sources": [{"n": i + 1, "source": c["source"], "ref": c["ref"], "snippet": c["content"][:220], "score": round(float(c.get("score") or 0), 3)}
                        for i, c in enumerate(chunks)]}

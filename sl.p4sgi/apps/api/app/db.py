"""Postgres helpers. Schema ensured on startup (also via docker-entrypoint-initdb.d)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "sql" / "init.sql"


def database_url() -> str:
    host = os.getenv("POSTGRES_HOST", "db")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "slp4sgi")
    user = os.getenv("POSTGRES_USER", "slp4sgi")
    password = os.getenv("POSTGRES_PASSWORD", "change-me-in-production")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _split_sql(sql: str) -> list[str]:
    """Split SQL script into statements; strip -- comments (init.sql has no $$ bodies)."""
    cleaned_lines: list[str] = []
    for line in sql.splitlines():
        # Remove full-line and trailing -- comments (not inside quotes — init.sql is simple)
        if "--" in line:
            in_single = False
            in_double = False
            out = []
            i = 0
            while i < len(line):
                ch = line[i]
                if ch == "'" and not in_double:
                    in_single = not in_single
                    out.append(ch)
                elif ch == '"' and not in_single:
                    in_double = not in_double
                    out.append(ch)
                elif ch == "-" and not in_single and not in_double and i + 1 < len(line) and line[i + 1] == "-":
                    break
                else:
                    out.append(ch)
                i += 1
            line = "".join(out)
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)
    parts = [p.strip() for p in text.split(";")]
    return [p for p in parts if p]


def ensure_schema() -> None:
    """Apply init.sql idempotently (safe if docker-entrypoint already ran it)."""
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    stmts = _split_sql(sql)
    with get_conn() as conn:
        for stmt in stmts:
            conn.execute(stmt)


def fetchall(query: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute(query, params)
        return list(cur.fetchall())


def fetchone(query: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    with get_conn() as conn:
        cur = conn.execute(query, params)
        return cur.fetchone()


def execute(query: str, params: tuple | dict | None = None) -> None:
    with get_conn() as conn:
        conn.execute(query, params)


def jsonify(value: Any) -> Jsonb:
    return Jsonb(value)

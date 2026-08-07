"""Neon / Postgres session store for serverless persistence.

When DATABASE_URL is set (Vercel Neon integration or manual), sessions are
dual-written to Postgres as the durable source of truth. Local JSON under
SESSIONS_DIR remains for local/dev and as a best-effort cache; on Vercel
/tmp is ephemeral so Postgres survives cold starts and instance switches.

Connection uses standard libpq via psycopg3. Prefer the Neon *pooler*
endpoint in DATABASE_URL for serverless (many short-lived connections).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
_ENABLED = bool(_DATABASE_URL)
_schema_ready = False


def is_enabled() -> bool:
    """True when DATABASE_URL is present (does not guarantee connectivity)."""
    return _ENABLED


def health_check() -> dict[str, Any]:
    """Lightweight connectivity + schema probe. Never logs the connection string."""
    result: dict[str, Any] = {
        "enabled": _ENABLED,
        "connected": False,
        "schema_ready": False,
        "error": None,
    }
    if not _ENABLED:
        result["error"] = "DATABASE_URL not set"
        return result
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            result["connected"] = True
        result["schema_ready"] = ensure_schema()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("Neon health_check failed: %s", result["error"])
    return result


def _connect():
    import psycopg

    # Neon pooler + sslmode=require is typical; pass through as-is.
    return psycopg.connect(_DATABASE_URL, connect_timeout=15)


def ensure_schema() -> bool:
    """Idempotent CREATE TABLE / indexes. Safe to call on every cold start."""
    global _schema_ready
    if not _ENABLED:
        return False
    if _schema_ready:
        return True
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS df_sessions (
                        session_id      TEXT PRIMARY KEY,
                        payload         JSONB NOT NULL,
                        session_type    TEXT,
                        parent_id       TEXT,
                        status          TEXT,
                        runtime_scope   TEXT DEFAULT 'interactive',
                        agent_type      TEXT,
                        task_description TEXT,
                        message_count   INTEGER DEFAULT 0,
                        created_at      TIMESTAMPTZ,
                        updated_at      TIMESTAMPTZ,
                        last_message    TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_df_sessions_updated
                        ON df_sessions (updated_at DESC NULLS LAST);
                    CREATE INDEX IF NOT EXISTS idx_df_sessions_type
                        ON df_sessions (session_type);
                    CREATE INDEX IF NOT EXISTS idx_df_sessions_scope
                        ON df_sessions (runtime_scope);
                    """
                )
            conn.commit()
        _schema_ready = True
        logger.info("Neon session schema ready (table df_sessions)")
        return True
    except Exception as exc:
        logger.error("Neon schema init failed: %s", exp if False else exc)
        return False


def _message_stats(payload: dict[str, Any]) -> tuple[int, str]:
    """Derive message_count and last_message preview from record list."""
    record = payload.get("record")
    messages = record if isinstance(record, list) else []
    count = sum(
        1
        for m in messages
        if isinstance(m, dict) and m.get("type") not in {"system_prompt", "compression_divider"}
    )
    last = ""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("type") != "assistant":
            continue
        content = m.get("content")
        if content:
            last = str(content)[:200]
            break
    return count, last


def _parse_ts(value: Any):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def upsert_session(payload: dict[str, Any]) -> bool:
    """Insert or replace full session payload. Returns True on success."""
    if not _ENABLED:
        return False
    if not ensure_schema():
        return False
    session_id = payload.get("session_id")
    if not session_id:
        return False
    msg_count, last_msg = _message_stats(payload)
    try:
        from psycopg.types.json import Json

        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO df_sessions (
                        session_id, payload, session_type, parent_id, status,
                        runtime_scope, agent_type, task_description, message_count,
                        created_at, updated_at, last_message
                    ) VALUES (
                        %(session_id)s, %(payload)s, %(session_type)s, %(parent_id)s, %(status)s,
                        %(runtime_scope)s, %(agent_type)s, %(task_description)s, %(message_count)s,
                        %(created_at)s, %(updated_at)s, %(last_message)s
                    )
                    ON CONFLICT (session_id) DO UPDATE SET
                        payload = EXCLUDED.payload,
                        session_type = EXCLUDED.session_type,
                        parent_id = EXCLUDED.parent_id,
                        status = EXCLUDED.status,
                        runtime_scope = EXCLUDED.runtime_scope,
                        agent_type = EXCLUDED.agent_type,
                        task_description = EXCLUDED.task_description,
                        message_count = EXCLUDED.message_count,
                        created_at = COALESCE(df_sessions.created_at, EXCLUDED.created_at),
                        updated_at = EXCLUDED.updated_at,
                        last_message = EXCLUDED.last_message
                    """,
                    {
                        "session_id": str(session_id),
                        "payload": Json(payload),
                        "session_type": payload.get("session_type") or "sub",
                        "parent_id": payload.get("parent_id"),
                        "status": payload.get("status") or "completed",
                        "runtime_scope": payload.get("runtime_scope") or "interactive",
                        "agent_type": payload.get("agent_type") or "default",
                        "task_description": (payload.get("task_description") or "")[:500],
                        "message_count": msg_count,
                        "created_at": _parse_ts(payload.get("created_at")),
                        "updated_at": _parse_ts(payload.get("updated_at"))
                        or datetime.now(timezone.utc),
                        "last_message": last_msg,
                    },
                )
            conn.commit()
        return True
    except Exception as exc:
        logger.error("Neon upsert_session %s failed: %s", session_id, exc)
        return False


def load_session(session_id: str) -> dict[str, Any] | None:
    """Return full payload dict or None."""
    if not _ENABLED or not session_id:
        return None
    if not ensure_schema():
        return None
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM df_sessions WHERE session_id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        payload = row[0]
        if isinstance(payload, str):
            return json.loads(payload)
        if isinstance(payload, dict):
            return payload
        return dict(payload)
    except Exception as exc:
        logger.error("Neon load_session %s failed: %s", session_id, exc)
        return None


def list_session_rows() -> list[dict[str, Any]]:
    """Lightweight rows for SessionCatalog (no full payload)."""
    if not _ENABLED:
        return []
    if not ensure_schema():
        return []
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT session_id, session_type, parent_id, status,
                           runtime_scope, agent_type, task_description,
                           message_count, created_at, updated_at, last_message,
                           payload
                    FROM df_sessions
                    ORDER BY updated_at DESC NULLS LAST
                    """
                )
                cols = [d.name for d in cur.description]
                rows = []
                for raw in cur.fetchall():
                    row = dict(zip(cols, raw))
                    payload = row.pop("payload", None) or {}
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except json.JSONDecodeError:
                            payload = {}
                    if not isinstance(payload, dict):
                        payload = {}
                    data = {
                        "session_id": row.get("session_id") or payload.get("session_id"),
                        "session_type": row.get("session_type") or payload.get("session_type", "sub"),
                        "parent_id": row.get("parent_id") if row.get("parent_id") is not None else payload.get("parent_id"),
                        "status": row.get("status") or payload.get("status", "completed"),
                        "task_description": row.get("task_description")
                        or payload.get("task_description", ""),
                        "created_at": _ts_str(row.get("created_at")) or payload.get("created_at", ""),
                        "updated_at": _ts_str(row.get("updated_at")) or payload.get("updated_at", ""),
                        "agent_type": row.get("agent_type") or payload.get("agent_type", "default"),
                        "runtime_scope": row.get("runtime_scope")
                        or payload.get("runtime_scope", "interactive"),
                        "workspace_path": payload.get("workspace_path"),
                        "workflow_id": payload.get("workflow_id"),
                        "task_id": payload.get("task_id"),
                        "node_id": payload.get("node_id"),
                        "record": payload.get("record") if row.get("message_count") is None else [],
                    }
                    if row.get("message_count") is not None:
                        data["_db_message_count"] = int(row["message_count"])
                    if row.get("last_message"):
                        data["_db_last_message"] = str(row["last_message"])
                    rows.append(data)
                return rows
    except Exception as exc:
        logger.error("Neon list_session_rows failed: %s", exp if False else exc)
        return []


def _ts_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def delete_session(session_id: str) -> bool:
    if not _ENABLED or not session_id:
        return False
    if not ensure_schema():
        return False
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM df_sessions WHERE session_id = %s",
                    (session_id,),
                )
            conn.commit()
        return True
    except Exception as exc:
        logger.error("Neon delete_session %s failed: %s", session_id, exc)
        return False

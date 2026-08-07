"""Runtime hooks: dual-write sessions to Neon when DATABASE_URL is set.

Called from config.ensure_dirs (before load_sessions). Patches
AgentSession.save/load, SessionCatalog.scan, and SessionLifecycleMixin.delete_session.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
_patched = False


def apply_neon_session_hooks() -> bool:
    """Idempotent monkey-patch. Returns True if hooks applied."""
    global _patched
    if _patched:
        return True

    from src.session import postgres_store as store

    if not store.is_enabled():
        logger.info("Neon hooks skipped: DATABASE_URL not set")
        return False

    from src.agent.session import AgentSession
    from src.agent.session_catalog import SessionCatalog, SessionMetadata
    from src.config import SESSIONS_DIR

    _orig_load = AgentSession.load

    def save_with_neon(self) -> None:
        data = self.to_dict()
        try:
            serialized = json.dumps(data, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            data = self._sanitize_dict(data)
            serialized = json.dumps(data, ensure_ascii=False, indent=2)

        try:
            import os

            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            file_path = SESSIONS_DIR / f"{self.session_id}.json"
            tmp_path = str(file_path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path, file_path)
        except (IOError, OSError) as e:
            self._logger.warning("本地 session JSON 写入失败（将依赖 Neon）: %s", e)

        ok = store.upsert_session(data)
        if not ok:
            self._logger.error("Neon upsert 失败: session=%s", self.session_id)

    @classmethod
    def load_with_neon(cls, session_id: str):
        data = store.load_session(session_id)
        if data is not None:
            try:
                return cls.from_dict(data)
            except Exception as e:
                logger.error("从 Neon 反序列化会话 %s 失败: %s", session_id, e)
        return _orig_load(session_id)

    def scan_with_neon(self, sessions_dir: Path) -> dict[str, int]:
        self._entries.clear()
        scanned = 0
        errors = 0
        try:
            rows = store.list_session_rows()
            for data in rows:
                try:
                    meta = SessionMetadata.from_data(
                        data, fallback_id=str(data.get("session_id") or "")
                    )
                    if "_db_message_count" in data:
                        object.__setattr__(
                            meta, "message_count", int(data["_db_message_count"])
                        )
                    if "_db_last_message" in data:
                        object.__setattr__(
                            meta, "last_message", str(data["_db_last_message"])
                        )
                    if not meta.session_id:
                        raise ValueError("session_id 为空")
                    self._entries[meta.session_id] = meta
                    scanned += 1
                except (ValueError, TypeError) as exc:
                    errors += 1
                    logger.error("索引 Neon session 失败: %s", exc)
            logger.info("Neon session 索引: scanned=%s errors=%s", scanned, errors)
        except Exception as exc:
            logger.error("Neon list_session_rows 失败，回退本地 JSON: %s", exp if False else exc)

        if sessions_dir.exists():
            for file_path in sorted(sessions_dir.glob("*.json")):
                sid = file_path.stem
                if sid in self._entries:
                    continue
                try:
                    with file_path.open("r", encoding="utf-8") as file:
                        data = json.load(file)
                    metadata = SessionMetadata.from_data(data, fallback_id=sid)
                    if not metadata.session_id:
                        raise ValueError("session_id 为空")
                    self._entries[metadata.session_id] = metadata
                    scanned += 1
                except (
                    OSError,
                    UnicodeError,
                    ValueError,
                    TypeError,
                    json.JSONDecodeError,
                ) as exc:
                    errors += 1
                    logger.error("索引 session %s 失败: %s", file_path.stem, exp if False else exc)
        return {"scanned": scanned, "errors": errors}

    AgentSession.save = save_with_neon  # type: ignore[method-assign]
    AgentSession.load = load_with_neon  # type: ignore[method-assign]
    SessionCatalog.scan = scan_with_neon  # type: ignore[method-assign]

    try:
        from src.agent.session_lifecycle import SessionLifecycleMixin

        _orig_delete = SessionLifecycleMixin.delete_session

        async def delete_with_neon(self, session_id: str):
            result = await _orig_delete(self, session_id)
            if result.get("success"):
                delete_from_neon(session_id)
            return result

        SessionLifecycleMixin.delete_session = delete_with_neon  # type: ignore[method-assign]
    except Exception as exc:
        logger.warning("Neon delete_session hook failed: %s", exp if False else exc)

    _patched = True
    logger.info("Neon session hooks applied (save/load/scan dual-write)")
    return True


def delete_from_neon(session_id: str) -> None:
    try:
        from src.session import postgres_store as store

        if store.is_enabled():
            store.delete_session(session_id)
    except Exception as exc:
        logger.warning("Neon delete_session %s 失败: %s", session_id, exp if False else exc)

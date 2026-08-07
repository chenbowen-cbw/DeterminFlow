"""
Workspace 管理器 - 统一管理 Chat 和 Workflow 的工作空间生命周期

Serverless-safe: never mkdir under /var/task; fall back to /tmp.
"""
import os
import re
import shutil
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import src.config as config

logger = logging.getLogger(__name__)

_TMP_FALLBACK = Path("/tmp/determinflow-data/workspaces")


def _resolve_workspace_root(base_dir: str | Path | None = None) -> Path:
    if base_dir is not None:
        root = Path(base_dir).expanduser()
    else:
        cwb = Path(config.CODING_WORKSPACE_BASE).expanduser()
        if cwb.is_absolute():
            root = cwb
        else:
            root = config.BASE_DIR / cwb
    root = root.resolve()
    if str(root).startswith("/var/task") or getattr(config, "_ON_SERVERLESS", False):
        if not str(root).startswith("/tmp"):
            preferred = Path(
                getattr(config, "WORKFLOW_WORKSPACES_DIR", None)
                or (config.DATA_DIR / "workspaces")
            )
            root = preferred.resolve()
    return root


def resolve_workflow_workspace_path(
    workflow_id: str,
    override: str | None = None,
    *,
    base_dir: str | Path | None = None,
) -> Path:
    safe_workflow_id = WorkspaceManager._sanitize_id(workflow_id)
    workspace_root = _resolve_workspace_root(base_dir)
    default_path = workspace_root / safe_workflow_id
    override_path = override.strip() if override else ""
    if not override_path:
        return default_path
    if Path(override_path).is_absolute():
        resolved = Path(override_path).expanduser().resolve()
        allowed_roots = (
            config.BASE_DIR.resolve(),
            config.DATA_DIR.resolve(),
            workspace_root,
            _TMP_FALLBACK.resolve(),
        )
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            logger.error(
                "absolute override escaped allowed roots: %s", resolved
            )
            return default_path
        return resolved
    resolved = (config.BASE_DIR / override_path).resolve()
    if not resolved.is_relative_to(config.BASE_DIR.resolve()):
        logger.error("relative override escaped BASE_DIR: %s", resolved)
        return default_path
    return resolved


@dataclass
class WorkspaceInfo:
    session_id: str
    path: str
    size_bytes: int


class WorkspaceManager:
    def __init__(self, base_dir: str | None = None):
        self.base_dir = _resolve_workspace_root(base_dir)
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            logger.warning(
                "WorkspaceManager mkdir failed (%s): %s — fallback %s",
                self.base_dir, err, _TMP_FALLBACK,
            )
            self.base_dir = _TMP_FALLBACK
            try:
                self.base_dir.mkdir(parents=True, exist_ok=True)
            except OSError as err2:
                logger.error("/tmp fallback mkdir failed: %s", err2)
        self._workspaces: dict[str, Path] = {}
        logger.info("WorkspaceManager ready, base_dir=%s", self.base_dir)

    @staticmethod
    def _sanitize_id(raw_id: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", raw_id)
        if not safe:
            raise ValueError(f"invalid id after sanitize: {raw_id!r}")
        return safe

    def _safe_mkdir(self, path: Path) -> Path:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError as err:
            logger.warning("mkdir failed %s: %s", path, err)
            if str(path).startswith("/var/task"):
                alt = _TMP_FALLBACK / path.name
                try:
                    alt.mkdir(parents=True, exist_ok=True)
                    return alt
                except OSError:
                    pass
            raise

    def create_workspace(self, session_id: str, source_path: str | None = None) -> Path:
        session_id = self._sanitize_id(session_id)
        workspace_dir = self._safe_mkdir(self.base_dir / session_id)
        if source_path:
            source = Path(source_path)
            if source.exists() and source.is_dir():
                self._copy_workspace(source, workspace_dir)
            else:
                logger.warning("source missing or not dir: %s", source)
        self._workspaces[session_id] = workspace_dir
        logger.info("Chat workspace created: %s -> %s", session_id, workspace_dir)
        return workspace_dir

    def get_workspace(self, session_id: str) -> Path | None:
        session_id = self._sanitize_id(session_id)
        if session_id in self._workspaces:
            return self._workspaces[session_id]
        workspace_dir = self.base_dir / session_id
        if workspace_dir.exists():
            self._workspaces[session_id] = workspace_dir
            return workspace_dir
        return None

    def cleanup_workspace(self, session_id: str, force: bool = False) -> bool:
        session_id = self._sanitize_id(session_id)
        workspace_dir = self.base_dir / session_id
        if not workspace_dir.exists():
            self._workspaces.pop(session_id, None)
            return True
        try:
            shutil.rmtree(workspace_dir, ignore_errors=force)
            self._workspaces.pop(session_id, None)
            logger.info("Workspace cleaned: %s", session_id)
            return True
        except Exception as e:
            logger.error("cleanup workspace %s failed: %s", session_id, e)
            return False

    def list_workspaces(self) -> list[WorkspaceInfo]:
        workspaces = []
        if not self.base_dir.exists():
            return workspaces
        for item in self.base_dir.iterdir():
            if item.is_dir():
                workspaces.append(WorkspaceInfo(
                    session_id=item.name,
                    path=str(item),
                    size_bytes=self._get_dir_size(item),
                ))
        return workspaces

    def get_workspace_size(self, session_id: str) -> int:
        session_id = self._sanitize_id(session_id)
        workspace_dir = self.base_dir / session_id
        if workspace_dir.exists():
            return self._get_dir_size(workspace_dir)
        return 0

    def create_workflow_workspace(self, workflow_id: str) -> Path:
        workflow_id = self._sanitize_id(workflow_id)
        workflow_root = self._safe_mkdir(self.base_dir / workflow_id)
        logger.info("Workflow workspace created: %s", workflow_root)
        return workflow_root

    def create_main_task_workspace(
        self,
        session_id: str,
        task_id: str,
        *,
        mode: str = "task_isolated",
        workspace_ref: str | None = None,
    ) -> Path:
        safe_session_id = self._sanitize_id(session_id)
        safe_task_id = self._sanitize_id(task_id)
        main_root = self.base_dir / "_main" / safe_session_id
        if mode == "task_isolated":
            workspace = main_root / "tasks" / safe_task_id
        elif mode == "named_shared":
            if not workspace_ref:
                raise ValueError("named_shared requires workspace_ref")
            safe_ref = self._sanitize_id(workspace_ref)
            workspace = main_root / "shared" / safe_ref
        else:
            raise ValueError(f"unsupported main task workspace mode: {mode}")
        workspace = self._safe_mkdir(workspace)
        logger.info(
            "Main task workspace created: session=%s task=%s mode=%s path=%s",
            safe_session_id, safe_task_id, mode, workspace,
        )
        return workspace

    def get_workflow_shared_workspace(self, workflow_id: str) -> Path | None:
        workflow_id = self._sanitize_id(workflow_id)
        shared = self.base_dir / workflow_id
        if shared.exists():
            return shared
        return None

    def get_workflow_root(self, workflow_id: str) -> Path:
        workflow_id = self._sanitize_id(workflow_id)
        return self.base_dir / workflow_id

    def resolve_workflow_workspace(
        self, workflow_id: str, override: str | None = None
    ) -> Path:
        ws_path = resolve_workflow_workspace_path(
            workflow_id, override=override, base_dir=self.base_dir,
        )
        ws_path = self._safe_mkdir(ws_path)
        if override and override.strip():
            logger.info("Workflow workspace (override): %s", ws_path)
        return ws_path

    def cleanup_workflow_workspace(self, workflow_id: str) -> bool:
        workflow_id = self._sanitize_id(workflow_id)
        workflow_root = self.base_dir / workflow_id
        if not workflow_root.exists():
            return True
        try:
            shutil.rmtree(workflow_root, ignore_errors=True)
            logger.info("Workflow workspace cleaned: %s", workflow_id)
            return True
        except Exception:
            logger.exception("cleanup workflow workspace %s failed", workflow_id)
            return False

    def workflow_workspace_exists(self, workflow_id: str) -> bool:
        workflow_id = self._sanitize_id(workflow_id)
        return (self.base_dir / workflow_id).exists()

    def _copy_workspace(self, source: Path, dest: Path) -> None:
        excludes = set(
            e.strip()
            for e in config.CODING_WORKSPACE_COPY_EXCLUDES.split(",")
            if e.strip()
        )
        max_size = config.CODING_WORKSPACE_MAX_SIZE
        copied_size = 0
        for item in source.iterdir():
            if item.name in excludes:
                continue
            dest_item = dest / item.name
            try:
                if item.is_dir():
                    dir_size = self._get_dir_size(item)
                    if copied_size + dir_size > max_size:
                        logger.warning("skip dir %s: size limit", item.name)
                        continue
                    shutil.copytree(
                        item, dest_item, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*excludes),
                    )
                    copied_size += dir_size
                elif item.is_file():
                    file_size = item.stat().st_size
                    if copied_size + file_size > max_size:
                        logger.warning("skip file %s: size limit", item.name)
                        continue
                    shutil.copy2(item, dest_item)
                    copied_size += file_size
            except Exception as e:
                logger.error("copy %s failed: %s", item.name, e)
        logger.info("Workspace copy done: %s -> %s (%s bytes)", source, dest, copied_size)

    @staticmethod
    def _get_dir_size(path: Path) -> int:
        total = 0
        try:
            for entry in path.rglob("*"):
                if entry.is_file():
                    total += entry.stat().st_size
        except (OSError, PermissionError):
            pass
        return total

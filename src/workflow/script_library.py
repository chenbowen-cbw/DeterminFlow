"""Layered script-library lookup for core and extension resources."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.config import SCRIPT_LIBRARY_DIR
from src.extension_api.registrar import OwnedPath

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
SCRIPT_LIBRARY_ATTESTATION_SCHEMA = "script_library_attestation.v1"
_IGNORED_DIRECTORY_NAMES = {"__pycache__", ".git"}
_IGNORED_FILE_NAMES = {".DS_Store"}
_IGNORED_FILE_SUFFIXES = {".pyc", ".pyo"}


class ScriptLibraryError(RuntimeError):
    """Base error for fail-closed Script Library contracts."""


class ScriptLibraryConflictError(ScriptLibraryError):
    """Raised when more than one active owner claims one script identity."""


class ScriptLibraryAttestationError(ScriptLibraryError):
    """Raised when a script identity cannot be frozen or verified."""


@dataclass(frozen=True)
class ScriptLocation:
    owner: str
    directory: Path
    root: Path | None = None
    revision: str = ""


class ScriptLibraryCatalog:
    def __init__(
        self,
        user_root: Path,
        extension_roots: list[OwnedPath] | None = None,
        owner_enabled: Callable[[str], bool] | None = None,
        owner_environment: Callable[[str], dict[str, str]] | None = None,
        owner_revision: Callable[[str], str | None] | None = None,
    ):
        self.user_root = user_root.resolve()
        self.extension_roots = list(extension_roots or [])
        self.owner_enabled = owner_enabled or (lambda owner: True)
        self.owner_environment = owner_environment or (lambda owner: {})
        self.owner_revision = owner_revision or (lambda _owner: None)
        try:
            self.user_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Serverless (e.g. Vercel) has a read-only package filesystem.
            # Defer directory creation; later writable paths should use /tmp.
            logger.warning(
                "Script Library user_root 无法创建（只读文件系统？）: %s — %s",
                self.user_root,
                exc,
            )
        self._scan()

    @staticmethod
    def _validate(group: str, script_name: str = "") -> None:
        if not _NAME_RE.fullmatch(group) or (script_name and not _NAME_RE.fullmatch(script_name)):
            raise ValueError(f"非法脚本库路径: {group}/{script_name}")

    def _roots(self, *, include_inactive: bool = False) -> list[OwnedPath]:
        active_extension_roots = [
            root
            for root in self.extension_roots
            if include_inactive or self.owner_enabled(root.owner)
        ]
        return [OwnedPath("user", self.user_root), *active_extension_roots]

    @staticmethod
    def _root_revision(root: OwnedPath) -> str:
        revision = getattr(root, "revision", "")
        return str(revision).strip() if revision is not None else ""

    def _scan(
        self,
        *,
        include_inactive: bool = False,
    ) -> dict[tuple[str, str], tuple[ScriptLocation, str]]:
        discovered: dict[
            tuple[str, str],
            tuple[ScriptLocation, str],
        ] = {}
        for root in self._roots(include_inactive=include_inactive):
            root_path = root.path.resolve()
            if not root_path.exists():
                # On read-only / missing roots (serverless cold start), skip instead of crashing import.
                if root.owner == "user":
                    continue
                raise FileNotFoundError(
                    f"Script Library 根目录不存在: {root.owner}: {root_path}"
                )
            if not root_path.is_dir():
                raise ScriptLibraryError(
                    f"Script Library 根路径必须是目录: {root.owner}: {root_path}"
                )
            for group_dir in sorted(root_path.iterdir()):
                if not group_dir.is_dir() or group_dir.name.startswith("."):
                    continue
                for script_dir in sorted(group_dir.iterdir()):
                    if not script_dir.is_dir():
                        continue
                    script_type = self._detect_type(script_dir)
                    if not script_type:
                        continue
                    key = (group_dir.name, script_dir.name)
                    previous = discovered.get(key)
                    if previous is not None:
                        previous_location, _previous_type = previous
                        raise ScriptLibraryConflictError(
                            "Script Library resource 冲突: "
                            f"{group_dir.name}/{script_dir.name} "
                            f"({previous_location.owner}: "
                            f"{previous_location.directory} vs "
                            f"{root.owner}: {script_dir.resolve()})"
                        )
                    discovered[key] = (
                        ScriptLocation(
                            owner=root.owner,
                            directory=script_dir.resolve(),
                            root=root_path,
                            revision=self._root_revision(root),
                        ),
                        script_type,
                    )
        return discovered

    def validate_sources(self) -> None:
        """Validate all roots, including disabled Plugin owners."""
        self._scan(include_inactive=True)

    def list_groups(self) -> list[dict]:
        scripts = self.list_scripts()
        counts: dict[str, int] = {}
        for script in scripts:
            counts[script["group"]] = counts.get(script["group"], 0) + 1
        return [{"name": group, "script_count": counts[group]} for group in sorted(counts)]

    def list_scripts(self, group: str = "") -> list[dict]:
        discovered = self._scan()
        return [
            {
                "group": script_group,
                "name": script_name,
                "script_type": script_type,
                "owner": location.owner,
            }
            for (script_group, script_name), (location, script_type)
            in discovered.items()
            if not group or script_group == group
        ]

    @staticmethod
    def _detect_type(script_dir: Path) -> str:
        if (script_dir / f"{script_dir.name}.py").exists():
            return "python"
        if (script_dir / f"{script_dir.name}.sh").exists():
            return "shell"
        return ""

    def resolve(self, group: str, script_name: str) -> ScriptLocation | None:
        self._validate(group, script_name)
        discovered = self._scan()
        item = discovered.get((group, script_name))
        return item[0] if item is not None else None

    def resolve_file(self, group: str, script_name: str, filename: str) -> ScriptLocation | None:
        location = self.resolve(group, script_name)
        relative = Path(filename)
        if location is None or relative.is_absolute():
            return None
        candidate = (location.directory / relative).resolve()
        if (
            not candidate.is_relative_to(location.directory)
            or not candidate.is_file()
        ):
            return None
        return location

    def environment(self, owner: str) -> dict[str, str]:
        return dict(self.owner_environment(owner)) if owner != "user" else {}

    @staticmethod
    def _hash_file(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _is_ignored_file(path: Path) -> bool:
        return (
            path.name in _IGNORED_FILE_NAMES
            or path.suffix in _IGNORED_FILE_SUFFIXES
            or any(
                part in _IGNORED_DIRECTORY_NAMES
                for part in path.parts
            )
        )

    def _attested_files(
        self,
        location: ScriptLocation,
        group: str,
        script_name: str,
    ) -> list[dict[str, str]]:
        if location.root is None:
            raise ScriptLibraryAttestationError(
                f"Script Library 缺少 owner root: {group}/{script_name}"
            )
        group_dir = (location.root / group).resolve()
        if (
            not group_dir.is_relative_to(location.root)
            or not location.directory.is_relative_to(group_dir)
        ):
            raise ScriptLibraryAttestationError(
                f"Script Library 路径逃逸: {group}/{script_name}"
            )

        candidates = [
            path
            for path in group_dir.iterdir()
            if path.is_file()
        ]
        candidates.extend(
            path
            for path in location.directory.rglob("*")
            if path.is_file()
        )

        files: list[dict[str, str]] = []
        seen: set[str] = set()
        for path in sorted(candidates):
            relative = path.relative_to(group_dir)
            if self._is_ignored_file(relative):
                continue
            if path.is_symlink():
                raise ScriptLibraryAttestationError(
                    "Script Library attestation 不接受符号链接: "
                    f"{group}/{relative.as_posix()}"
                )
            resolved = path.resolve()
            if not resolved.is_relative_to(group_dir):
                raise ScriptLibraryAttestationError(
                    "Script Library 文件路径逃逸: "
                    f"{group}/{relative.as_posix()}"
                )
            normalized = relative.as_posix()
            if normalized in seen:
                continue
            files.append({
                "path": normalized,
                "content_sha256": self._hash_file(resolved),
            })
            seen.add(normalized)
        return files

    def attest(
        self,
        group: str,
        script_name: str,
        script_type: str,
    ) -> dict[str, Any]:
        """Snapshot the exact owner, revision and files used by a Task."""
        self._validate(group, script_name)
        discovered = self._scan()
        item = discovered.get((group, script_name))
        if item is None:
            raise ScriptLibraryAttestationError(
                f"Script Library 脚本不存在: {group}/{script_name}"
            )
        location, detected_type = item
        if script_type != detected_type:
            raise ScriptLibraryAttestationError(
                f"Script Library 脚本类型不匹配: {group}/{script_name}, "
                f"expected={script_type}, actual={detected_type}"
            )

        revision = location.revision
        if location.owner == "user":
            revision = revision or "local"
        else:
            revision = revision or str(
                self.owner_revision(location.owner) or ""
            ).strip()
        if not revision:
            raise ScriptLibraryAttestationError(
                f"Script Library Plugin 缺少 revision: {location.owner}"
            )

        extension = "py" if script_type == "python" else "sh"
        entrypoint = location.directory / f"{script_name}.{extension}"
        if not entrypoint.is_file():
            raise ScriptLibraryAttestationError(
                f"Script Library entrypoint 不存在: {entrypoint}"
            )
        files = self._attested_files(location, group, script_name)
        files_sha256 = hashlib.sha256(
            json.dumps(
                files,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": SCRIPT_LIBRARY_ATTESTATION_SCHEMA,
            "owner": location.owner,
            "revision": revision,
            "group": group,
            "script_name": script_name,
            "script_type": script_type,
            "entrypoint_sha256": self._hash_file(entrypoint),
            "files_sha256": files_sha256,
            "files": files,
        }

    def verify_attestation(
        self,
        expected: Any,
    ) -> dict[str, Any]:
        """Rebuild and compare an attestation immediately before execution."""
        if (
            not isinstance(expected, dict)
            or expected.get("schema_version")
            != SCRIPT_LIBRARY_ATTESTATION_SCHEMA
        ):
            raise ScriptLibraryAttestationError(
                "Script Library attestation 无效"
            )
        try:
            actual = self.attest(
                str(expected["group"]),
                str(expected["script_name"]),
                str(expected["script_type"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ScriptLibraryAttestationError(
                "Script Library attestation 字段无效"
            ) from exc
        if actual != expected:
            raise ScriptLibraryAttestationError(
                "Script Library 已漂移，拒绝执行: "
                f"{expected.get('group')}/{expected.get('script_name')}"
            )
        return actual

    def writable_directory(self, group: str, script_name: str) -> Path:
        self._validate(group, script_name)
        return self.user_root / group / script_name

    def writable_group(self, group: str) -> Path:
        self._validate(group)
        return self.user_root / group


_catalog = ScriptLibraryCatalog(SCRIPT_LIBRARY_DIR)


def configure_script_library(
    extension_roots: list[OwnedPath],
    owner_enabled: Callable[[str], bool] | None = None,
    owner_environment: Callable[[str], dict[str, str]] | None = None,
    owner_revision: Callable[[str], str | None] | None = None,
) -> ScriptLibraryCatalog:
    global _catalog
    _catalog = ScriptLibraryCatalog(
        SCRIPT_LIBRARY_DIR,
        extension_roots,
        owner_enabled,
        owner_environment,
        owner_revision,
    )
    return _catalog


def get_script_library() -> ScriptLibraryCatalog:
    return _catalog

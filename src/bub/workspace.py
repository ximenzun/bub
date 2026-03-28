from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

WORKSPACE_DIR_NAME = ".bub"
WORKSPACE_METADATA_FILE = "workspace.json"
WORKSPACES_DIR_NAME = "workspaces"
WORKSPACE_REGISTRY_FILE = "workspace-registry.json"
CONTROL_DIR_NAME = "control"
TAPE_DIR_NAME = "tapes"
SECRET_DIR_NAME = "secrets"  # noqa: S105
HISTORY_DIR_NAME = "history"
BUNDLE_VERSION = 1

type TapeExportMode = Literal["none", "metadata", "messages", "full"]
type SecretExportMode = Literal["none", "refs-only", "plaintext", "encrypted"]


class WorkspaceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    workspace_path: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    bundle_version: int = BUNDLE_VERSION


class WorkspaceBundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_version: int = BUNDLE_VERSION
    workspace_id: str
    exported_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_bub_version: str | None = None
    tape_mode: TapeExportMode = "none"
    secret_mode: SecretExportMode = "none"  # noqa: S105
    portability_notes: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    home: Path
    workspace: Path
    workspace_id: str

    @property
    def registry_path(self) -> Path:
        return self.home / WORKSPACE_REGISTRY_FILE

    @property
    def workspace_dir(self) -> Path:
        return self.home / WORKSPACES_DIR_NAME / self.workspace_id

    @property
    def metadata_path(self) -> Path:
        return self.workspace_dir / WORKSPACE_METADATA_FILE

    @property
    def control_dir(self) -> Path:
        return self.workspace_dir / CONTROL_DIR_NAME

    @property
    def bundle_dir(self) -> Path:
        return self.workspace_dir / "bundles"

    @property
    def tapes_dir(self) -> Path:
        return self.home / TAPE_DIR_NAME

    @property
    def secrets_dir(self) -> Path:
        return self.home / SECRET_DIR_NAME / self.workspace_id

    @property
    def history_dir(self) -> Path:
        return self.home / HISTORY_DIR_NAME


def default_home_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "bub"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "bub"
        return home / "AppData" / "Roaming" / "bub"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "bub"
    return home / ".local" / "state" / "bub"


def workspace_paths(workspace: Path, home: Path) -> WorkspacePaths:
    resolved_workspace = workspace.resolve()
    resolved_home = home.expanduser().resolve()
    metadata = ensure_workspace_metadata(resolved_workspace, resolved_home)
    return WorkspacePaths(home=resolved_home, workspace=resolved_workspace, workspace_id=metadata.workspace_id)


def ensure_workspace_metadata(workspace: Path, home: Path) -> WorkspaceMetadata:
    resolved_workspace = workspace.resolve()
    resolved_home = home.expanduser().resolve()
    resolved_home.mkdir(parents=True, exist_ok=True)
    registry = _load_registry(resolved_home)
    workspace_key = str(resolved_workspace)
    workspace_id = registry.get(workspace_key)
    if isinstance(workspace_id, str):
        metadata_path = resolved_home / WORKSPACES_DIR_NAME / workspace_id / WORKSPACE_METADATA_FILE
        if metadata_path.exists():
            metadata = WorkspaceMetadata.model_validate(json.loads(metadata_path.read_text(encoding="utf-8")))
            if metadata.workspace_path != workspace_key:
                metadata = metadata.model_copy(update={"workspace_path": workspace_key})
                write_workspace_metadata(resolved_workspace, resolved_home, metadata)
            return metadata

    legacy_metadata = _legacy_metadata(resolved_workspace)
    if legacy_metadata is not None:
        write_workspace_metadata(resolved_workspace, resolved_home, legacy_metadata)
        _migrate_legacy_workspace_state(resolved_workspace, resolved_home, legacy_metadata.workspace_id)
        return legacy_metadata

    metadata = WorkspaceMetadata(
        workspace_id=f"ws_{os.urandom(10).hex()}",
        workspace_path=workspace_key,
    )
    write_workspace_metadata(resolved_workspace, resolved_home, metadata)
    return metadata


def write_workspace_metadata(workspace: Path, home: Path, metadata: WorkspaceMetadata) -> None:
    resolved_workspace = workspace.resolve()
    resolved_home = home.expanduser().resolve()
    workspace_key = str(resolved_workspace)
    updated = metadata.model_copy(
        update={
            "workspace_path": workspace_key,
            "updated_at": datetime.now(UTC),
        }
    )
    paths = WorkspacePaths(home=resolved_home, workspace=resolved_workspace, workspace_id=updated.workspace_id)
    paths.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    paths.metadata_path.write_text(
        json.dumps(updated.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    registry = _load_registry(resolved_home)
    registry[workspace_key] = updated.workspace_id
    _write_registry(resolved_home, registry)


def workspace_id_for_path(workspace: Path, home: Path) -> str:
    return ensure_workspace_metadata(workspace, home).workspace_id


def copytree_contents(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def _load_registry(home: Path) -> dict[str, str]:
    path = home / WORKSPACE_REGISTRY_FILE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if isinstance(key, str) and isinstance(value, str)}


def _write_registry(home: Path, registry: dict[str, str]) -> None:
    path = home / WORKSPACE_REGISTRY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _legacy_metadata(workspace: Path) -> WorkspaceMetadata | None:
    path = workspace / WORKSPACE_DIR_NAME / WORKSPACE_METADATA_FILE
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    data: dict[str, Any] = dict(payload)
    data.setdefault("workspace_path", str(workspace))
    return WorkspaceMetadata.model_validate(data)


def _migrate_legacy_workspace_state(workspace: Path, home: Path, workspace_id: str) -> None:
    legacy_root = workspace / WORKSPACE_DIR_NAME
    if not legacy_root.exists():
        return
    paths = WorkspacePaths(home=home, workspace=workspace, workspace_id=workspace_id)
    legacy_control = legacy_root / CONTROL_DIR_NAME
    if legacy_control.exists():
        copytree_contents(legacy_control, paths.control_dir)

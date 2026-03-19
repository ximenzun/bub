"""Gateway runtime registry and single-instance guard."""

from __future__ import annotations

import hashlib
import json
import os
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GatewayRecord:
    pid: int
    workspace: Path
    started_at: str
    path: Path

    @property
    def alive(self) -> bool:
        return process_is_alive(self.pid)


class GatewayAlreadyRunningError(RuntimeError):
    """Raised when another Bub gateway is already registered for a workspace."""


def gateway_registry_dir(runtime_root: Path | None = None) -> Path:
    root = runtime_root or (Path.home() / ".bub" / "runtime" / "gateways")
    root.mkdir(parents=True, exist_ok=True)
    return root


def gateway_record_path(workspace: Path, *, runtime_root: Path | None = None) -> Path:
    resolved = workspace.expanduser().resolve()
    digest = hashlib.md5(str(resolved).encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return gateway_registry_dir(runtime_root) / f"{digest}.json"


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_gateway_record(path: Path) -> GatewayRecord | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    workspace = payload.get("workspace")
    started_at = payload.get("started_at")
    if not isinstance(pid, int) or not isinstance(workspace, str) or not isinstance(started_at, str):
        return None
    return GatewayRecord(pid=pid, workspace=Path(workspace), started_at=started_at, path=path)


def write_gateway_record(workspace: Path, *, pid: int | None = None, runtime_root: Path | None = None) -> GatewayRecord:
    resolved = workspace.expanduser().resolve()
    path = gateway_record_path(resolved, runtime_root=runtime_root)
    record = GatewayRecord(
        pid=os.getpid() if pid is None else pid,
        workspace=resolved,
        started_at=datetime.now(UTC).isoformat(),
        path=path,
    )
    path.write_text(
        json.dumps(
            {
                "pid": record.pid,
                "workspace": str(record.workspace),
                "started_at": record.started_at,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return record


def ensure_gateway_slot(workspace: Path, *, pid: int | None = None, runtime_root: Path | None = None) -> GatewayRecord:
    record_path = gateway_record_path(workspace, runtime_root=runtime_root)
    existing = read_gateway_record(record_path)
    expected_pid = os.getpid() if pid is None else pid
    if existing is not None and existing.pid != expected_pid and existing.alive:
        raise GatewayAlreadyRunningError(
            f"Another bub gateway is already running for workspace {existing.workspace} (pid {existing.pid}). "
            "Use `bub gateway-status` or `bub gateway-kill` first."
        )
    if existing is not None and not existing.alive:
        record_path.unlink(missing_ok=True)
    return write_gateway_record(workspace, pid=expected_pid, runtime_root=runtime_root)


def release_gateway_slot(workspace: Path, *, pid: int | None = None, runtime_root: Path | None = None) -> None:
    path = gateway_record_path(workspace, runtime_root=runtime_root)
    existing = read_gateway_record(path)
    if existing is None:
        return
    expected_pid = os.getpid() if pid is None else pid
    if existing.pid == expected_pid or not existing.alive:
        path.unlink(missing_ok=True)


def list_gateway_records(*, runtime_root: Path | None = None) -> list[GatewayRecord]:
    records: list[GatewayRecord] = []
    for path in sorted(gateway_registry_dir(runtime_root).glob("*.json")):
        record = read_gateway_record(path)
        if record is not None:
            records.append(record)
    return records


def kill_gateway_records(
    *,
    workspace: Path | None = None,
    kill_all: bool = False,
    force: bool = False,
    runtime_root: Path | None = None,
) -> list[str]:
    if not kill_all and workspace is None:
        raise ValueError("workspace is required unless kill_all=True")
    target_workspace = workspace.expanduser().resolve() if workspace is not None else None
    lines: list[str] = []
    for record in list_gateway_records(runtime_root=runtime_root):
        if target_workspace is not None and record.workspace != target_workspace:
            continue
        if not record.alive:
            record.path.unlink(missing_ok=True)
            lines.append(f"stale: pid={record.pid} workspace={record.workspace}")
            continue
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.kill(record.pid, sig)
        lines.append(
            f"signaled: pid={record.pid} signal={'SIGKILL' if force else 'SIGTERM'} workspace={record.workspace}"
        )
        record.path.unlink(missing_ok=True)
    return lines

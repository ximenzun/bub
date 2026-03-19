from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import bub.builtin.cli as cli
import bub.builtin.gateway_registry as registry
from bub.framework import BubFramework


def _create_app() -> object:
    framework = BubFramework()
    framework.load_hooks()
    return framework.create_cli_app()


def test_ensure_gateway_slot_rejects_other_alive_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    registry.write_gateway_record(workspace, pid=12345, runtime_root=runtime_root)
    monkeypatch.setattr(registry, "process_is_alive", lambda pid: pid == 12345)

    with pytest.raises(registry.GatewayAlreadyRunningError):
        registry.ensure_gateway_slot(workspace, pid=54321, runtime_root=runtime_root)


def test_ensure_gateway_slot_reclaims_stale_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    old = registry.write_gateway_record(workspace, pid=11111, runtime_root=runtime_root)
    monkeypatch.setattr(registry, "process_is_alive", lambda pid: False)

    record = registry.ensure_gateway_slot(workspace, pid=22222, runtime_root=runtime_root)

    assert old.path.is_file()
    stored = registry.read_gateway_record(old.path)
    assert stored is not None
    assert stored.pid == 22222
    assert record.pid == 22222


def test_kill_gateway_records_cleans_stale_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    record = registry.write_gateway_record(workspace, pid=11111, runtime_root=runtime_root)
    monkeypatch.setattr(registry, "process_is_alive", lambda pid: False)

    lines = registry.kill_gateway_records(workspace=workspace, runtime_root=runtime_root)

    assert lines == [f"stale: pid=11111 workspace={workspace.resolve()}"]
    assert not record.path.exists()


def test_gateway_status_command_renders_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = registry.GatewayRecord(
        pid=12345,
        workspace=workspace.resolve(),
        started_at="2026-03-19T00:00:00+00:00",
        path=tmp_path / "runtime" / "gateways" / "abc.json",
    )
    monkeypatch.setattr(cli, "list_gateway_records", lambda: [record])
    monkeypatch.setattr(registry, "process_is_alive", lambda pid: True)

    result = CliRunner().invoke(_create_app(), ["--workspace", str(workspace), "gateway-status"])

    assert result.exit_code == 0
    assert "pid: 12345" in result.stdout
    assert f"workspace: {workspace.resolve()}" in result.stdout


def test_gateway_kill_command_uses_current_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, object] = {}

    def fake_kill_gateway_records(*, workspace: Path | None, kill_all: bool = False, force: bool = False):
        captured["workspace"] = workspace
        captured["kill_all"] = kill_all
        captured["force"] = force
        return [f"signaled: pid=12345 signal=SIGTERM workspace={workspace}"]

    monkeypatch.setattr(cli, "kill_gateway_records", fake_kill_gateway_records)

    result = CliRunner().invoke(_create_app(), ["--workspace", str(workspace), "gateway-kill"])

    assert result.exit_code == 0
    assert captured["workspace"] == workspace.resolve()
    assert captured["kill_all"] is False
    assert captured["force"] is False
    assert "signaled: pid=12345" in result.stdout

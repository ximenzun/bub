from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from bub.framework import BubFramework
from bub.onboarding.renderer import ReviewSelection


def _create_app() -> object:
    framework = BubFramework()
    framework.load_hooks()
    return framework.create_cli_app()


class _InteractiveRenderer:
    def render_info(self, **kwargs) -> None:
        return None

    def render_external_link(self, **kwargs) -> None:
        return None

    def render_qr_challenge(self, **kwargs) -> None:
        return None

    def render_error(self, **kwargs) -> None:
        return None

    def confirm(self, **kwargs) -> bool:
        return True

    def choose_one(self, **kwargs) -> str:
        return "123:abc"

    def choose_many(self, **kwargs) -> list[str]:
        return []

    def reorder(self, **kwargs) -> list[str]:
        return []

    def prompt_field(self, *, field, default=None):
        if field.key == "allow_users":
            return ["alice"]
        if field.key == "allow_chats":
            return ["-1001"]
        if field.key == "proxy":
            return ""
        return default

    def prompt_secret(self, *, field) -> str:
        return "123:abc"

    def review_summary(self, **kwargs) -> ReviewSelection:
        return ReviewSelection(action="install")


def test_marketplace_show_install_status_and_test_plan(tmp_path: Path) -> None:
    runner = CliRunner()

    show_result = runner.invoke(
        _create_app(),
        ["--workspace", str(tmp_path), "--home", str(tmp_path / "home"), "marketplace", "show", "telegram"],
    )
    install_result = runner.invoke(
        _create_app(),
        [
            "--workspace",
            str(tmp_path),
            "--home",
            str(tmp_path / "home"),
            "marketplace",
            "install",
            "telegram",
            "--set",
            'allow_users=["alice"]',
            "--set",
            'allow_chats=["-1001"]',
            "--secret",
            "bot_token=123:abc",
        ],
    )
    status_result = runner.invoke(
        _create_app(),
        ["--workspace", str(tmp_path), "--home", str(tmp_path / "home"), "marketplace", "status", "telegram"],
    )
    test_plan_result = runner.invoke(
        _create_app(),
        ["--workspace", str(tmp_path), "--home", str(tmp_path / "home"), "marketplace", "test-plan", "telegram"],
    )

    assert show_result.exit_code == 0
    assert "Telegram" in show_result.stdout
    assert "steps:" in show_result.stdout

    assert install_result.exit_code == 0
    assert "installed: telegram" in install_result.stdout
    assert "validation: ready" in install_result.stdout

    assert status_result.exit_code == 0
    assert "plugin: telegram" in status_result.stdout
    assert "installed: True" in status_result.stdout
    assert "validation: ok" in status_result.stdout
    assert "current_state:" in status_result.stdout
    assert "Access control:" in status_result.stdout

    assert test_plan_result.exit_code == 0
    assert "Telegram test plan" in test_plan_result.stdout
    assert "Install Telegram via marketplace CLI" in test_plan_result.stdout


def test_channels_login_uses_manifest_backed_onboarding(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("bub.onboarding.renderer_for_surface", lambda *args, **kwargs: _InteractiveRenderer())

    runner = CliRunner()
    login_result = runner.invoke(
        _create_app(),
        ["--workspace", str(tmp_path), "--home", str(tmp_path / "home"), "channels", "login", "telegram", "--force"],
    )
    status_result = runner.invoke(
        _create_app(),
        ["--workspace", str(tmp_path), "--home", str(tmp_path / "home"), "channels", "status", "telegram"],
    )

    assert login_result.exit_code == 0
    assert "login: ok" in login_result.stdout
    assert "plugin_id: telegram" in login_result.stdout
    assert "validation: ready" in login_result.stdout

    assert status_result.exit_code == 0
    assert "telegram/default" in status_result.stdout
    assert "configured: True" in status_result.stdout
    assert "state: active" in status_result.stdout


def test_marketplace_install_reset_passes_blank_start_to_interactive_flow(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _fake_install_interactive(self, plugin_id: str, *, surface: str = "cli", renderer=None, reset: bool = False):
        del renderer
        captured["plugin_id"] = plugin_id
        captured["surface"] = surface
        captured["reset"] = reset
        return self.install(
            plugin_id,
            config_updates={},
            secret_values={"bot_token": "123:abc"},
            enable=True,
            surface=surface,
            reset=reset,
        )

    monkeypatch.setattr("bub.onboarding.service.MarketplaceService.install_interactive", _fake_install_interactive)
    monkeypatch.setattr("bub.onboarding.renderer_for_surface", lambda *args, **kwargs: _InteractiveRenderer())

    runner = CliRunner()
    result = runner.invoke(
        _create_app(),
        [
            "--workspace",
            str(tmp_path),
            "--home",
            str(tmp_path / "home"),
            "marketplace",
            "install",
            "telegram",
            "--reset",
        ],
    )

    assert result.exit_code == 0
    assert captured == {"plugin_id": "telegram", "surface": "cli", "reset": True}


def test_workspace_status_doctor_export_and_import(tmp_path: Path) -> None:
    runner = CliRunner()
    source_workspace = tmp_path / "source"
    target_workspace = tmp_path / "target"
    bundle_path = tmp_path / "workspace.bundle.zip"

    install_result = runner.invoke(
        _create_app(),
        [
            "--workspace",
            str(source_workspace),
            "--home",
            str(tmp_path / "home"),
            "marketplace",
            "install",
            "telegram",
            "--secret",
            "bot_token=123:abc",
        ],
    )
    status_result = runner.invoke(
        _create_app(),
        ["--workspace", str(source_workspace), "--home", str(tmp_path / "home"), "workspace", "status"],
    )
    doctor_result = runner.invoke(
        _create_app(),
        ["--workspace", str(source_workspace), "--home", str(tmp_path / "home"), "workspace", "doctor"],
    )
    export_result = runner.invoke(
        _create_app(),
        [
            "--workspace",
            str(source_workspace),
            "--home",
            str(tmp_path / "home"),
            "workspace",
            "export",
            str(bundle_path),
            "--secrets",
            "encrypted",
            "--passphrase",
            "passphrase",
        ],
    )
    import_result = runner.invoke(
        _create_app(),
        [
            "--workspace",
            str(target_workspace),
            "--home",
            str(tmp_path / "home"),
            "workspace",
            "import",
            str(bundle_path),
            "--passphrase",
            "passphrase",
            "--force",
        ],
    )

    assert install_result.exit_code == 0
    assert status_result.exit_code == 0
    assert "workspace_id:" in status_result.stdout
    assert doctor_result.exit_code == 0
    assert "installed_plugins:" in doctor_result.stdout
    assert export_result.exit_code == 0
    assert "bundle:" in export_result.stdout
    assert import_result.exit_code == 0
    assert "workspace_id:" in import_result.stdout


def test_workspace_state_defaults_to_home_not_repo(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"

    result = runner.invoke(
        _create_app(),
        [
            "--workspace",
            str(workspace),
            "--home",
            str(home),
            "workspace",
            "status",
        ],
    )

    assert result.exit_code == 0
    assert (home / "workspace-registry.json").exists()
    assert not (workspace / ".bub").exists()

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import typer
from typer.testing import CliRunner

from bub.channels.base import Channel
from bub.channels.control import ChannelControl
from bub.commands import SlashCommandSpec
from bub.framework import BubFramework
from bub.hookspecs import hookimpl
from bub.onboarding import OnboardingManifest
from bub.social import basic_channel_capabilities


class NamedChannel(Channel):
    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.label = label

    async def start(self, stop_event) -> None:
        return None

    async def stop(self) -> None:
        return None


def test_create_cli_app_sets_workspace_and_context(tmp_path: Path) -> None:
    framework = BubFramework()

    class CliPlugin:
        @hookimpl
        def register_cli_commands(self, app: typer.Typer) -> None:
            @app.command("workspace")
            def workspace_command(ctx: typer.Context) -> None:
                current = ctx.ensure_object(BubFramework)
                typer.echo(str(current.workspace))

    framework._plugin_manager.register(CliPlugin(), name="cli-plugin")
    app = framework.create_cli_app()

    result = CliRunner().invoke(app, ["--workspace", str(tmp_path), "workspace"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(tmp_path.resolve())
    assert framework.workspace == tmp_path.resolve()


def test_get_channels_prefers_high_priority_plugin_for_duplicate_names() -> None:
    framework = BubFramework()

    class LowPriorityPlugin:
        @hookimpl
        def provide_channels(self, message_handler):
            return [NamedChannel("shared", "low"), NamedChannel("low-only", "low")]

    class HighPriorityPlugin:
        @hookimpl
        def provide_channels(self, message_handler):
            return [NamedChannel("shared", "high"), NamedChannel("high-only", "high")]

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")

    channels = framework.get_channels(lambda message: None)

    assert set(channels) == {"shared", "low-only", "high-only"}
    assert channels["shared"].label == "high"
    assert channels["low-only"].label == "low"
    assert channels["high-only"].label == "high"


def test_get_channel_controls_prefers_high_priority_plugin_for_duplicate_names() -> None:
    framework = BubFramework()

    class LowPriorityPlugin:
        @hookimpl
        def provide_channel_controls(self):
            return [
                ChannelControl(channel="shared", summary="low", capabilities=basic_channel_capabilities("shared")),
                ChannelControl(channel="low-only", summary="low", capabilities=basic_channel_capabilities("low-only")),
            ]

    class HighPriorityPlugin:
        @hookimpl
        def provide_channel_controls(self):
            return [
                ChannelControl(channel="shared", summary="high", capabilities=basic_channel_capabilities("shared")),
                ChannelControl(
                    channel="high-only", summary="high", capabilities=basic_channel_capabilities("high-only")
                ),
            ]

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")

    controls = framework.get_channel_controls()

    assert set(controls) == {"shared", "low-only", "high-only"}
    assert controls["shared"].summary == "high"
    assert controls["low-only"].summary == "low"
    assert controls["high-only"].summary == "high"


def test_get_system_prompt_uses_priority_order_and_skips_empty_results() -> None:
    framework = BubFramework()

    class LowPriorityPlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str:
            return "low"

    class HighPriorityPlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str | None:
            return "high"

    class EmptyPlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str | None:
            return None

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")
    framework._plugin_manager.register(EmptyPlugin(), name="empty")

    prompt = framework.get_system_prompt(prompt="hello", state={})

    assert prompt == "low\n\nhigh"


def test_builtin_cli_exposes_login_and_keeps_message_hidden_alias() -> None:
    framework = BubFramework()
    framework.load_hooks()
    app = framework.create_cli_app()
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    alias_result = runner.invoke(app, ["message", "--help"])

    assert help_result.exit_code == 0
    assert "login" in help_result.stdout
    assert "marketplace" in help_result.stdout
    assert "channels" in help_result.stdout
    assert "gateway" in help_result.stdout
    assert "│ message" not in help_result.stdout
    assert alias_result.exit_code == 0
    assert "bub message" in alias_result.stdout
    assert "Start message listeners" in alias_result.stdout


def test_get_slash_commands_prefers_high_priority_plugin_for_duplicate_names() -> None:
    framework = BubFramework()
    low = SlashCommandSpec(name="/repo", summary="low")
    high = SlashCommandSpec(name="/repo", summary="high")
    git = SlashCommandSpec(name="/git", summary="git")

    class LowPriorityPlugin:
        @hookimpl
        def provide_slash_commands(self):
            return [low]

    class HighPriorityPlugin:
        @hookimpl
        def provide_slash_commands(self):
            return [high, git]

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")

    commands = framework.get_slash_commands()

    assert commands == [git, high]


def test_get_onboarding_manifests_prefers_high_priority_plugin_for_duplicate_ids() -> None:
    framework = BubFramework()
    framework._load_manifest_entry_points = staticmethod(lambda: [])  # type: ignore[method-assign]
    low = OnboardingManifest(plugin_id="telegram", title="low", summary="low")
    high = OnboardingManifest(plugin_id="telegram", title="high", summary="high")
    extra = OnboardingManifest(plugin_id="websearch", title="websearch", summary="search")

    class LowPriorityPlugin:
        @hookimpl
        def provide_onboarding_manifests(self):
            return [low]

    class HighPriorityPlugin:
        @hookimpl
        def provide_onboarding_manifests(self):
            return [high, extra]

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")

    manifests = framework.get_onboarding_manifests()

    assert set(manifests) == {"telegram", "websearch"}
    assert manifests["telegram"].title == "high"
    assert manifests["websearch"].summary == "search"


def test_get_onboarding_manifests_includes_entry_point_manifests(monkeypatch) -> None:
    framework = BubFramework()
    external = OnboardingManifest(plugin_id="external", title="external", summary="external")

    def fake_entry_points(*, group: str):
        if group == "bub.manifests":
            return [SimpleNamespace(load=lambda: external)]
        return []

    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)

    manifests = framework.get_onboarding_manifests()

    assert "external" in manifests
    assert manifests["external"].title == "external"


def test_load_hooks_skips_runtime_when_manifest_exists_but_plugin_not_installed(monkeypatch) -> None:
    framework = BubFramework()
    external_manifest = OnboardingManifest(plugin_id="external", title="external", summary="external")

    class FakeRuntimePlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str:
            return "runtime"

    def fake_entry_points(*, group: str):
        if group == "bub.manifests":
            return [SimpleNamespace(name="external", load=lambda: external_manifest)]
        if group == "bub":
            return [SimpleNamespace(name="external", load=lambda: FakeRuntimePlugin())]
        return []

    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)

    framework.load_hooks()

    status = framework.plugin_status()["external"]
    assert status.is_success is False
    assert "not installed/enabled" in (status.detail or "")


def test_sync_runtime_reloads_hooks_after_marketplace_install(monkeypatch, tmp_path: Path) -> None:
    framework = BubFramework()
    framework.workspace = tmp_path / "workspace"
    framework.home = tmp_path / "home"
    external_manifest = OnboardingManifest(plugin_id="external", title="external", summary="external")

    class FakeRuntimePlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str:
            return "runtime"

    def fake_entry_points(*, group: str):
        if group == "bub.manifests":
            return [SimpleNamespace(name="external", load=lambda: external_manifest)]
        if group == "bub":
            return [SimpleNamespace(name="external", load=lambda: FakeRuntimePlugin())]
        return []

    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)

    framework.load_hooks()
    assert "external" not in framework.hook_report().get("system_prompt", [])

    framework.get_marketplace_service().install("external")

    assert framework.sync_runtime() is True
    assert "external" in framework.hook_report()["system_prompt"]

"""Hook-first Bub framework runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pluggy
import typer
from loguru import logger
from republic import AsyncTapeStore
from republic.tape import TapeStore

from bub.builtin.settings import DEFAULT_HOME
from bub.envelope import content_of, field_of, unpack_batch
from bub.hook_runtime import HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs
from bub.types import Envelope, MessageHandler, OutboundChannelRouter, TurnResult

if TYPE_CHECKING:
    from bub.channels.base import Channel
    from bub.channels.control import ChannelControl
    from bub.commands import SlashCommandSpec
    from bub.onboarding import MarketplaceService, OnboardingManifest
    from bub.onboarding.bundle import WorkspaceBundleService
    from bub.onboarding.registry import PluginRegistryEntry


@dataclass(frozen=True)
class PluginStatus:
    is_success: bool
    detail: str | None = None


class BubFramework:
    """Minimal framework core. Everything grows from hook skills."""

    def __init__(self) -> None:
        self.workspace = Path.cwd().resolve()
        self.home = DEFAULT_HOME
        self._runtime_revision: str | None = None
        self._outbound_router: OutboundChannelRouter | None = None
        self._reset_hooks()

    def _reset_hooks(self) -> None:
        self._plugin_manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
        self._plugin_manager.add_hookspecs(BubHookSpecs)
        self._hook_runtime = HookRuntime(self._plugin_manager)
        self._plugin_status: dict[str, PluginStatus] = {}

    def _load_builtin_hooks(self) -> None:
        from bub.builtin.hook_impl import BuiltinImpl

        impl = BuiltinImpl(self)

        try:
            self._plugin_manager.register(impl, name="builtin")
        except Exception as exc:
            self._plugin_status["builtin"] = PluginStatus(is_success=False, detail=str(exc))
        else:
            self._plugin_status["builtin"] = PluginStatus(is_success=True)

    def _load_external_hooks(self) -> None:
        import importlib.metadata

        manifests = self.get_onboarding_manifests()
        enabled_plugins = {
            plugin_id
            for plugin_id, state in self.get_marketplace_service().states().items()
            if state.enabled
        }
        runtime_requirements: dict[str, set[str]] = {}
        for manifest in manifests.values():
            runtime_name = manifest.entry_point_name or manifest.plugin_id
            runtime_requirements.setdefault(runtime_name, set()).add(manifest.plugin_id)
        for entry_point in importlib.metadata.entry_points(group="bub"):
            required_plugin_ids = runtime_requirements.get(entry_point.name, set())
            if required_plugin_ids and not (required_plugin_ids & enabled_plugins):
                self._plugin_status[entry_point.name] = PluginStatus(
                    is_success=False,
                    detail="runtime skipped because the plugin is not installed/enabled in the workspace",
                )
                continue
            try:
                plugin = entry_point.load()
                if callable(plugin):  # Support entry points that are classes
                    plugin = plugin(self)
                self._plugin_manager.register(plugin, name=entry_point.name)
            except Exception as exc:
                logger.warning(f"Failed to load plugin '{entry_point.name}': {exc}")
                self._plugin_status[entry_point.name] = PluginStatus(is_success=False, detail=str(exc))
            else:
                self._plugin_status[entry_point.name] = PluginStatus(is_success=True)

    def load_hooks(self) -> None:
        self._reset_hooks()
        self._load_builtin_hooks()
        self._load_external_hooks()
        self._runtime_revision = self.runtime_revision()

    def runtime_revision(self) -> str:
        from bub.workspace import workspace_paths

        state_path = workspace_paths(self.workspace, self.home).control_dir / "marketplace.json"
        if not state_path.exists():
            return f"{state_path}:missing"
        stat = state_path.stat()
        return f"{state_path}:{stat.st_mtime_ns}:{stat.st_size}"

    def sync_runtime(self) -> bool:
        revision = self.runtime_revision()
        if self._runtime_revision == revision:
            return False
        self.load_hooks()
        return True

    def create_cli_app(self) -> typer.Typer:
        """Create CLI app by collecting commands from hooks. Can be used for custom CLI entry point."""
        app = typer.Typer(name="bub", help="Batteries-included, hook-first AI framework", add_completion=False)

        @app.callback(invoke_without_command=True)
        def _main(
            ctx: typer.Context,
            workspace: str | None = typer.Option(None, "--workspace", "-w", help="Path to the workspace"),
            home: str | None = typer.Option(None, "--home", help="Path to Bub home/state directory"),
        ) -> None:
            if workspace:
                self.workspace = Path(workspace).resolve()
            if home:
                self.home = Path(home).expanduser().resolve()
            ctx.obj = self

        self._hook_runtime.call_many_sync("register_cli_commands", app=app)
        return app

    async def process_inbound(self, inbound: Envelope) -> TurnResult:
        """Run one inbound message through hooks and return turn result."""

        try:
            session_id = await self._hook_runtime.call_first(
                "resolve_session", message=inbound
            ) or self._default_session_id(inbound)
            if isinstance(inbound, dict):
                inbound.setdefault("session_id", session_id)
            state = {"_runtime_workspace": str(self.workspace)}
            for hook_state in reversed(
                await self._hook_runtime.call_many("load_state", message=inbound, session_id=session_id)
            ):
                if isinstance(hook_state, dict):
                    state.update(hook_state)
            prompt = await self._hook_runtime.call_first(
                "build_prompt", message=inbound, session_id=session_id, state=state
            )
            if not prompt:
                prompt = content_of(inbound)
            model_output = ""
            try:
                model_output = await self._hook_runtime.call_first(
                    "run_model", prompt=prompt, session_id=session_id, state=state
                )
                if model_output is None:
                    await self._hook_runtime.notify_error(
                        stage="run_model:fallback",
                        error=RuntimeError("no model skill returned output"),
                        message=inbound,
                    )
                    model_output = prompt if isinstance(prompt, str) else content_of(inbound)
                else:
                    model_output = str(model_output)
            finally:
                await self._hook_runtime.call_many(
                    "save_state",
                    session_id=session_id,
                    state=state,
                    message=inbound,
                    model_output=model_output,
                )

            outbounds = await self._collect_outbounds(inbound, session_id, state, model_output)
            for outbound in outbounds:
                await self._hook_runtime.call_many("dispatch_outbound", message=outbound)
            return TurnResult(session_id=session_id, prompt=prompt, model_output=model_output, outbounds=outbounds)
        except Exception as exc:
            logger.exception("Error processing inbound message")
            await self._hook_runtime.notify_error(stage="turn", error=exc, message=inbound)
            raise

    def hook_report(self) -> dict[str, list[str]]:
        """Return hook implementation summary for diagnostics."""

        return self._hook_runtime.hook_report()

    def cleanup_runtime(self, *, force: bool = False) -> list[str]:
        """Run plugin cleanup hooks and collect user-facing result lines."""

        lines: list[str] = []
        for result in self._hook_runtime.call_many_sync(
            "cleanup_runtime",
            workspace=self.workspace,
            force=force,
        ):
            if isinstance(result, list):
                lines.extend(str(item) for item in result if item is not None)
        return lines

    def bind_outbound_router(self, router: OutboundChannelRouter | None) -> None:
        self._outbound_router = router

    async def dispatch_via_router(self, message: Envelope) -> bool:
        if self._outbound_router is None:
            return False
        return await self._outbound_router.dispatch(message)

    @staticmethod
    def _default_session_id(message: Envelope) -> str:
        session_id = field_of(message, "session_id")
        if session_id is not None:
            return str(session_id)
        channel = str(field_of(message, "channel", "default"))
        chat_id = str(field_of(message, "chat_id", "default"))
        return f"{channel}:{chat_id}"

    async def _collect_outbounds(
        self,
        message: Envelope,
        session_id: str,
        state: dict[str, Any],
        model_output: str,
    ) -> list[Envelope]:
        batches = await self._hook_runtime.call_many(
            "render_outbound",
            message=message,
            session_id=session_id,
            state=state,
            model_output=model_output,
        )
        outbounds: list[Envelope] = []
        for batch in batches:
            outbounds.extend(unpack_batch(batch))
        if outbounds:
            return outbounds
        if state.get("_suppress_default_outbound") or state.get("_suppress_fallback_outbound"):
            return []

        fallback: dict[str, Any] = {
            "content": model_output,
            "session_id": session_id,
        }
        channel = field_of(message, "channel")
        chat_id = field_of(message, "chat_id")
        if channel is not None:
            fallback["channel"] = channel
        if chat_id is not None:
            fallback["chat_id"] = chat_id
        return [fallback]

    def get_channels(self, message_handler: MessageHandler) -> dict[str, Channel]:
        channels: dict[str, Channel] = {}
        for result in self._hook_runtime.call_many_sync("provide_channels", message_handler=message_handler):
            for channel in result:
                if channel.name not in channels:
                    channels[channel.name] = channel
        return channels

    def get_channel_controls(self) -> dict[str, ChannelControl]:
        controls: dict[str, ChannelControl] = {}
        for result in self._hook_runtime.call_many_sync("provide_channel_controls"):
            for control in result:
                if control.channel not in controls:
                    controls[control.channel] = control
        return controls

    def get_onboarding_manifests(self) -> dict[str, OnboardingManifest]:
        manifests: dict[str, OnboardingManifest] = {}
        for result in self._hook_runtime.call_many_sync("provide_onboarding_manifests"):
            for manifest in result:
                if manifest.plugin_id not in manifests:
                    manifests[manifest.plugin_id] = manifest
        for manifest in self._load_manifest_entry_points():
            if manifest.plugin_id not in manifests:
                manifests[manifest.plugin_id] = manifest
        return manifests

    def get_marketplace_service(self) -> MarketplaceService:
        from bub.onboarding import MarketplaceService

        return MarketplaceService(
            workspace=self.workspace,
            home=self.home,
            manifests=self.get_onboarding_manifests().values(),
        )

    def get_workspace_bundle_service(self) -> WorkspaceBundleService:
        from bub.onboarding.bundle import WorkspaceBundleService

        return WorkspaceBundleService(
            workspace=self.workspace,
            home=self.home,
            marketplace=self.get_marketplace_service(),
        )

    def plugin_status(self) -> dict[str, PluginStatus]:
        return dict(self._plugin_status)

    def get_registry_entries(self) -> dict[str, PluginRegistryEntry]:
        from bub.onboarding.registry import builtin_registry_entries

        return {entry.plugin_id: entry for entry in builtin_registry_entries()}

    @staticmethod
    def _load_manifest_entry_points() -> list[OnboardingManifest]:
        import importlib.metadata

        manifests: list[OnboardingManifest] = []
        for entry_point in importlib.metadata.entry_points(group="bub.manifests"):
            try:
                loaded = entry_point.load()
                value = loaded() if callable(loaded) else loaded
            except Exception:
                logger.warning("Failed to load manifest entry point '{}'", entry_point.name)
                continue
            if isinstance(value, list):
                manifests.extend(item for item in value if getattr(item, "plugin_id", None))
            elif getattr(value, "plugin_id", None):
                manifests.append(value)
        return manifests

    def get_tape_store(self) -> TapeStore | AsyncTapeStore | None:
        return self._hook_runtime.call_first_sync("provide_tape_store")

    def get_slash_commands(self) -> list[SlashCommandSpec]:
        commands: dict[str, SlashCommandSpec] = {}
        for result in self._hook_runtime.call_many_sync("provide_slash_commands"):
            for command in result:
                key = command.name.casefold()
                if key not in commands:
                    commands[key] = command
        return sorted(commands.values(), key=lambda item: item.name.casefold())

    def get_system_prompt(self, prompt: str | list[dict], state: dict[str, Any]) -> str:
        return "\n\n".join(
            result
            for result in reversed(self._hook_runtime.call_many_sync("system_prompt", prompt=prompt, state=state))
            if result
        )

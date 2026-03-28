"""Builtin CLI command adapter."""

# ruff: noqa: B008
from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol

import typer
from republic.auth.openai_codex import CodexOAuthLoginError, OpenAICodexOAuthTokens, login_openai_codex_oauth

from bub.builtin.gateway_registry import (
    GatewayAlreadyRunningError,
    ensure_gateway_slot,
    kill_gateway_records,
    list_gateway_records,
    release_gateway_slot,
)
from bub.channels.control import ChannelLoginRequest
from bub.channels.message import ChannelMessage
from bub.envelope import field_of
from bub.framework import BubFramework
from bub.onboarding import OnboardingCancelledError


class _TapeExportOption(StrEnum):
    none = "none"
    metadata = "metadata"
    messages = "messages"
    full = "full"


class _SecretExportOption(StrEnum):
    none = "none"
    refs_only = "refs-only"
    plaintext = "plaintext"
    encrypted = "encrypted"


DEFAULT_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"


class _ChannelManagerRunner(Protocol):
    async def listen_and_run(self) -> None: ...


def _shutdown_signals() -> tuple[int, ...]:
    signals: list[int] = []
    for name in ("SIGTERM", "SIGHUP"):
        value = getattr(signal, name, None)
        if value is not None:
            signals.append(int(value))
    return tuple(signals)


def _install_shutdown_signal_handlers(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> tuple[int, ...]:
    installed: list[int] = []
    for signum in _shutdown_signals():
        try:
            loop.add_signal_handler(signum, callback)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(signum)
    return tuple(installed)


def _remove_shutdown_signal_handlers(loop: asyncio.AbstractEventLoop, signals_to_remove: tuple[int, ...]) -> None:
    for signum in signals_to_remove:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(signum)


async def _run_channel_manager(manager: _ChannelManagerRunner) -> None:
    loop = asyncio.get_running_loop()
    manager_task = asyncio.create_task(manager.listen_and_run())
    shutdown_requested = False

    def _request_shutdown() -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        if not manager_task.done():
            manager_task.cancel()

    installed = _install_shutdown_signal_handlers(loop, _request_shutdown)
    try:
        await manager_task
    except asyncio.CancelledError:
        if not shutdown_requested:
            raise
    finally:
        _remove_shutdown_signal_handlers(loop, installed)


def run(
    ctx: typer.Context,
    message: str = typer.Argument(..., help="Inbound message content"),
    channel: str = typer.Option("cli", "--channel", help="Message channel"),
    chat_id: str = typer.Option("local", "--chat-id", help="Chat id"),
    sender_id: str = typer.Option("human", "--sender-id", help="Sender id"),
    session_id: str | None = typer.Option(None, "--session-id", help="Optional session id"),
) -> None:
    """Run one inbound message through the framework pipeline."""

    framework = ctx.ensure_object(BubFramework)
    inbound = ChannelMessage(
        session_id=f"{channel}:{chat_id}" if session_id is None else session_id,
        content=message,
        channel=channel,
        chat_id=chat_id,
        context={"sender_id": sender_id},
    )

    result = asyncio.run(framework.process_inbound(inbound))
    for outbound in result.outbounds:
        rendered = str(field_of(outbound, "content", ""))
        target_channel = str(field_of(outbound, "channel", "stdout"))
        target_chat = str(field_of(outbound, "chat_id", "local"))
        typer.echo(f"[{target_channel}:{target_chat}]\n{rendered}")


def list_hooks(ctx: typer.Context) -> None:
    """Show hook implementation mapping."""
    framework = ctx.ensure_object(BubFramework)
    report = framework.hook_report()
    if not report:
        typer.echo("(no hook implementations)")
        return
    for hook_name, adapter_names in report.items():
        typer.echo(f"{hook_name}: {', '.join(adapter_names)}")


def gateway(
    ctx: typer.Context,
    enable_channels: list[str] = typer.Option([], "--enable-channel", help="Channels to enable for CLI (default: all)"),
) -> None:
    """Start message listeners(like telegram)."""
    from bub.channels.manager import ChannelManager

    framework = ctx.ensure_object(BubFramework)

    manager = ChannelManager(framework, enabled_channels=enable_channels or None)
    try:
        ensure_gateway_slot(framework.workspace)
    except GatewayAlreadyRunningError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    try:
        asyncio.run(_run_channel_manager(manager))
    finally:
        try:
            framework.cleanup_runtime(force=False)
        finally:
            release_gateway_slot(framework.workspace)


def gateway_status(
    ctx: typer.Context,
    all_workspaces: bool = typer.Option(False, "--all", help="Show registered gateway records for all workspaces."),
) -> None:
    """Show registered Bub gateway processes."""

    framework = ctx.ensure_object(BubFramework)
    records = list_gateway_records()
    if not records:
        typer.echo("(no registered gateways)")
        return
    current_workspace = framework.workspace.resolve()
    for record in records:
        if not all_workspaces and record.workspace != current_workspace:
            continue
        typer.echo(
            f"pid: {record.pid}\n"
            f"alive: {record.alive}\n"
            f"workspace: {record.workspace}\n"
            f"started_at: {record.started_at}\n"
            f"record: {record.path}\n"
        )


def gateway_kill(
    ctx: typer.Context,
    all_workspaces: bool = typer.Option(False, "--all", help="Kill registered gateways for all workspaces."),
    force: bool = typer.Option(False, "--force", help="Use SIGKILL instead of SIGTERM."),
) -> None:
    """Stop registered Bub gateway processes."""

    framework = ctx.ensure_object(BubFramework)
    lines = kill_gateway_records(
        workspace=None if all_workspaces else framework.workspace,
        kill_all=all_workspaces,
        force=force,
    )
    if not lines:
        typer.echo("(no matching gateways)")
        return
    typer.echo("\n".join(lines))


def cleanup(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Ask plugins to use stronger cleanup behavior when supported."),
) -> None:
    """Run plugin-owned runtime cleanup hooks."""

    framework = ctx.ensure_object(BubFramework)
    lines = framework.cleanup_runtime(force=force)
    if not lines:
        typer.echo("(no plugin cleanup actions reported)")
        return
    typer.echo("\n".join(lines))


def channels_list(ctx: typer.Context) -> None:
    """List installed channels with control-plane support."""

    framework = ctx.ensure_object(BubFramework)
    controls = framework.get_channel_controls()
    if not controls:
        typer.echo("(no channel controls reported)")
        return
    for control in controls.values():
        capability_summary = control.capabilities.provisioning_mode
        typer.echo(f"{control.channel}\nsummary: {control.summary or '-'}\nprovisioning: {capability_summary}\n")


def channels_status(
    ctx: typer.Context,
    channel: str | None = typer.Argument(None, help="Optional channel id to inspect."),
) -> None:
    """Show channel account status."""

    framework = ctx.ensure_object(BubFramework)
    controls = framework.get_channel_controls()
    selected = controls.get(channel) if channel else None
    if channel and selected is None:
        typer.echo(f"Unknown channel control: {channel}", err=True)
        raise typer.Exit(1)
    targets = [selected] if selected is not None else list(controls.values())
    if not targets:
        typer.echo("(no channel controls reported)")
        return
    rendered = False
    for control in targets:
        statuses = control.status()
        if not statuses:
            typer.echo(f"{control.channel}\n(no reported accounts)\n")
            rendered = True
            continue
        for item in statuses:
            typer.echo(
                f"{item.channel}/{item.account_id}\n"
                f"configured: {item.configured}\n"
                f"running: {item.running}\n"
                f"state: {item.state}\n"
                f"detail: {item.detail or '-'}\n"
                f"last_error: {item.last_error or '-'}\n"
                f"last_event_at: {item.last_event_at or '-'}\n"
                f"last_inbound_at: {item.last_inbound_at or '-'}\n"
                f"last_outbound_at: {item.last_outbound_at or '-'}\n"
            )
            rendered = True
    if not rendered:
        typer.echo("(no reported accounts)")


def channels_login(
    ctx: typer.Context,
    channel: str = typer.Argument(..., help="Channel id to log into."),
    account: Annotated[str | None, typer.Option("--account", help="Optional account id to re-login.")] = None,
    force: bool = typer.Option(False, "--force", help="Force a fresh login flow even when credentials already exist."),
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Optional login timeout in seconds for channels that support it."),
    ] = None,
) -> None:
    """Run a channel login / provisioning flow."""

    framework = ctx.ensure_object(BubFramework)
    controls = framework.get_channel_controls()
    control = controls.get(channel)
    if control is None:
        typer.echo(f"Unknown channel control: {channel}", err=True)
        raise typer.Exit(1)
    try:
        result = control.login(ChannelLoginRequest(account_id=account, force=force, timeout_seconds=timeout))
    except NotImplementedError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if result.lines:
        typer.echo("\n".join(result.lines))


def channels_logout(
    ctx: typer.Context,
    channel: str = typer.Argument(..., help="Channel id to log out from."),
    account: Annotated[str | None, typer.Option("--account", help="Optional account id to remove.")] = None,
    force: bool = typer.Option(False, "--force", help="Allow stronger logout behavior when supported."),
) -> None:
    """Remove channel credentials or disable an account."""

    framework = ctx.ensure_object(BubFramework)
    controls = framework.get_channel_controls()
    control = controls.get(channel)
    if control is None:
        typer.echo(f"Unknown channel control: {channel}", err=True)
        raise typer.Exit(1)
    try:
        lines = control.logout(account, force)
    except NotImplementedError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if not lines:
        typer.echo("(no logout actions reported)")
        return
    typer.echo("\n".join(lines))


def marketplace_list(ctx: typer.Context) -> None:
    """List onboarding marketplace entries."""

    framework = ctx.ensure_object(BubFramework)
    service = framework.get_marketplace_service()
    registry = framework.get_registry_entries()
    blocks: list[str] = []
    for manifest in service.manifests():
        state = service.state(manifest.plugin_id)
        blocks.append(
            f"{manifest.plugin_id}\n"
            f"title: {manifest.title}\n"
            f"category: {manifest.category}\n"
            f"installed: {state is not None}\n"
            f"enabled: {state.enabled if state is not None else False}\n"
            f"runtime_available: {manifest.runtime_is_available()}\n"
            f"surfaces: {', '.join(manifest.surfaces)}\n"
            f"summary: {manifest.summary}\n"
        )
    for plugin_id, entry in sorted(registry.items()):
        if plugin_id in {manifest.plugin_id for manifest in service.manifests()}:
            continue
        blocks.append(
            f"{plugin_id}\n"
            f"title: {entry.title}\n"
            f"category: registry\n"
            f"installed: False\n"
            f"enabled: False\n"
            f"runtime_available: False\n"
            f"surfaces: provided by plugin package\n"
            f"summary: {entry.summary}\n"
            f"install_hint: {entry.install_hint}\n"
        )
    typer.echo("\n".join(blocks) if blocks else "(no marketplace entries)")


def marketplace_show(
    ctx: typer.Context,
    plugin: str = typer.Argument(..., help="Plugin id to inspect."),
) -> None:
    """Show one marketplace manifest."""

    framework = ctx.ensure_object(BubFramework)
    service = framework.get_marketplace_service()
    try:
        typer.echo(service.render_manifest(plugin))
    except KeyError:
        registry = framework.get_registry_entries().get(plugin)
        if registry is None:
            raise
        typer.echo(
            f"{registry.title}\n"
            f"id: {registry.plugin_id}\n"
            f"category: registry\n"
            f"summary: {registry.summary}\n"
            f"package: {registry.package_name}\n"
            f"install_hint: {registry.install_hint}"
        )


def marketplace_status(
    ctx: typer.Context,
    plugin: str | None = typer.Argument(None, help="Optional plugin id to inspect."),
) -> None:
    """Show installation and validation status."""

    framework = ctx.ensure_object(BubFramework)
    service = framework.get_marketplace_service()
    target_ids = [plugin] if plugin else [manifest.plugin_id for manifest in service.manifests()]
    typer.echo("\n\n".join("\n".join(service.status_lines(plugin_id)) for plugin_id in target_ids))


def marketplace_validate(
    ctx: typer.Context,
    plugin: str = typer.Argument(..., help="Plugin id to validate."),
) -> None:
    """Validate one marketplace entry."""

    framework = ctx.ensure_object(BubFramework)
    service = framework.get_marketplace_service()
    report = service.validate(plugin)
    typer.echo(f"ok: {report.ok}\nsummary: {report.summary}")
    for issue in report.issues:
        typer.echo(f"{issue.level}: {issue.message}")
    if not report.ok:
        raise typer.Exit(1)


def marketplace_install(
    ctx: typer.Context,
    plugin: str = typer.Argument(..., help="Plugin id to install."),
    set_values: list[str] = typer.Option([], "--set", help="Config override as key=value (value may be JSON)."),
    secret_values: list[str] = typer.Option([], "--secret", help="Secret override as key=value."),
    force: bool = typer.Option(False, "--force", help="Re-run onboarding even if already installed."),
    reset: bool = typer.Option(
        False, "--reset", help="Start from a blank config instead of reusing the current install."
    ),
) -> None:
    """Install or update a marketplace entry."""

    framework = ctx.ensure_object(BubFramework)
    service = framework.get_marketplace_service()
    current = service.state(plugin)
    if plugin not in {manifest.plugin_id for manifest in service.manifests()}:
        registry = framework.get_registry_entries().get(plugin)
        if registry is not None:
            typer.echo(registry.install_hint, err=True)
            raise typer.Exit(1)
    if current is not None and not force and not reset and not set_values and not secret_values:
        typer.echo(f"{plugin}: already installed; use --force to re-run onboarding")
        return

    if set_values or secret_values:
        state = service.install(
            plugin,
            config_updates={key: _parse_cli_value(value) for key, value in _parse_key_values(set_values).items()},
            secret_values=_parse_key_values(secret_values),
            enable=True,
            surface="cli",
            reset=reset,
        )
    else:
        try:
            from bub.onboarding import renderer_for_surface

            state = service.install_interactive(
                plugin,
                surface="cli",
                renderer=renderer_for_surface("cli"),
                reset=reset,
            )
        except OnboardingCancelledError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc

    report = service.validate(plugin)
    typer.echo(
        f"installed: {state.plugin_id}\n"
        f"enabled: {state.enabled}\n"
        f"validation: {report.summary}\n"
        f"config_keys: {', '.join(sorted(state.config)) if state.config else '-'}\n"
        f"secret_keys: {', '.join(sorted(state.secret_refs)) if state.secret_refs else '-'}"
    )
    if not report.ok:
        raise typer.Exit(1)


def marketplace_uninstall(
    ctx: typer.Context,
    plugin: str = typer.Argument(..., help="Plugin id to uninstall."),
) -> None:
    """Remove one marketplace entry and stored secrets."""

    framework = ctx.ensure_object(BubFramework)
    service = framework.get_marketplace_service()
    service.uninstall(plugin)
    typer.echo(f"uninstalled: {plugin}")


def marketplace_test_plan(
    ctx: typer.Context,
    plugin: str | None = typer.Argument(None, help="Optional plugin id to inspect."),
) -> None:
    """Render manual/automated test guidance for marketplace entries."""

    framework = ctx.ensure_object(BubFramework)
    service = framework.get_marketplace_service()
    if plugin is not None:
        typer.echo(service.render_test_plan(plugin))
        return
    typer.echo("\n\n".join(service.render_test_plan(manifest.plugin_id) for manifest in service.manifests()))


def workspace_status(ctx: typer.Context) -> None:
    """Show stable workspace identity and bundle-related paths."""

    framework = ctx.ensure_object(BubFramework)
    service = framework.get_workspace_bundle_service()
    typer.echo(
        f"workspace: {framework.workspace}\n"
        f"workspace_id: {service.metadata.workspace_id}\n"
        f"home: {service.home}\n"
        f"tape_prefix: {service.metadata.workspace_id}__"
    )


def workspace_doctor(ctx: typer.Context) -> None:
    """Inspect bundle portability and rebind requirements."""

    framework = ctx.ensure_object(BubFramework)
    report = framework.get_workspace_bundle_service().doctor()
    typer.echo(report.render())


def workspace_export(
    ctx: typer.Context,
    destination: Path = typer.Argument(..., help="Destination bundle zip file."),
    tapes: _TapeExportOption = typer.Option(_TapeExportOption.messages, "--tapes", help="Tape export mode."),
    secrets: _SecretExportOption = typer.Option(_SecretExportOption.none, "--secrets", help="Secret export mode."),
    passphrase: str | None = typer.Option(None, "--passphrase", help="Passphrase for encrypted secret bundles."),
) -> None:
    """Export this workspace into a portable bundle."""

    framework = ctx.ensure_object(BubFramework)
    output = framework.get_workspace_bundle_service().export_bundle(
        destination,
        tape_mode=tapes.value,
        secret_mode=secrets.value,
        passphrase=passphrase,
    )
    typer.echo(f"bundle: {output}")


def workspace_import(
    ctx: typer.Context,
    bundle: Path = typer.Argument(..., help="Path to a Bub workspace bundle."),
    passphrase: str | None = typer.Option(None, "--passphrase", help="Passphrase for encrypted secret bundles."),
    force: bool = typer.Option(False, "--force", help="Replace a different existing workspace id."),
) -> None:
    """Import a workspace bundle into the current --workspace target."""

    framework = ctx.ensure_object(BubFramework)
    manifest = framework.get_workspace_bundle_service().import_bundle(bundle, passphrase=passphrase, force=force)
    typer.echo(f"workspace_id: {manifest.workspace_id}\ntapes: {manifest.tape_mode}\nsecrets: {manifest.secret_mode}")


def chat(
    ctx: typer.Context,
    chat_id: str = typer.Option("local", "--chat-id", help="Chat id"),
    session_id: str | None = typer.Option(None, "--session-id", help="Optional session id"),
) -> None:
    """Start a REPL chat session."""
    from bub.channels.manager import ChannelManager

    framework = ctx.ensure_object(BubFramework)

    manager = ChannelManager(framework, enabled_channels=["cli"])
    channel = manager.get_channel("cli")
    if channel is None:
        typer.echo("CLI channel not found. Please check your hook implementations.")
        raise typer.Exit(1)
    channel.set_metadata(chat_id=chat_id, session_id=session_id)  # type: ignore[attr-defined]
    try:
        asyncio.run(_run_channel_manager(manager))
    finally:
        framework.cleanup_runtime(force=False)


def _prompt_for_codex_redirect(authorize_url: str) -> str:
    typer.echo("Open this URL in your browser and complete the Codex sign-in flow:\n")
    typer.echo(authorize_url)
    typer.echo("\nPaste the full callback URL or the authorization code.")
    return str(typer.prompt("callback")).strip()


def _resolve_codex_home(codex_home: Path | None) -> Path:
    if codex_home is not None:
        return codex_home.expanduser()
    return Path("~/.codex").expanduser()


def _render_codex_login_result(tokens: OpenAICodexOAuthTokens, auth_path: Path) -> None:
    typer.echo("login: ok")
    typer.echo(f"account_id: {tokens.account_id or '-'}")
    typer.echo(f"auth_file: {auth_path}")
    typer.echo("usage: set BUB_MODEL=openai:gpt-5-codex and omit BUB_API_KEY")


def _parse_key_values(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            raise typer.BadParameter(f"Expected key=value, got: {raw}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise typer.BadParameter(f"Expected non-empty key in: {raw}")
        result[key] = value
    return result


def _parse_cli_value(value: str):
    lowered = value.strip().casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def login(
    provider: str = typer.Argument(..., help="Authentication provider"),
    codex_home: Path | None = typer.Option(None, "--codex-home", help="Directory to store Codex OAuth credentials"),
    open_browser: bool = typer.Option(True, "--browser/--no-browser", help="Open the OAuth URL in a browser"),
    manual: bool = typer.Option(
        False,
        "--manual",
        help="Paste the callback URL or code instead of waiting for a local callback server",
    ),
    timeout_seconds: float = typer.Option(300.0, "--timeout", help="OAuth wait timeout in seconds"),
) -> None:
    """Authenticate with a provider and persist the resulting credentials."""

    if provider != "openai":
        typer.echo(f"Unsupported auth provider: {provider}", err=True)
        raise typer.Exit(1)

    resolved_codex_home = _resolve_codex_home(codex_home)
    prompt_for_redirect = _prompt_for_codex_redirect if manual or not open_browser else None

    try:
        tokens = login_openai_codex_oauth(
            codex_home=resolved_codex_home,
            prompt_for_redirect=prompt_for_redirect,
            open_browser=open_browser,
            redirect_uri=DEFAULT_CODEX_REDIRECT_URI,
            timeout_seconds=timeout_seconds,
        )
    except CodexOAuthLoginError as exc:
        typer.echo(f"Codex login failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    _render_codex_login_result(tokens, resolved_codex_home / "auth.json")

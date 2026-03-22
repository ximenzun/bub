"""Builtin CLI command adapter."""

# ruff: noqa: B008
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from typing import Annotated, Callable, Protocol

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
    return Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()


def _render_codex_login_result(tokens: OpenAICodexOAuthTokens, auth_path: Path) -> None:
    typer.echo("login: ok")
    typer.echo(f"account_id: {tokens.account_id or '-'}")
    typer.echo(f"auth_file: {auth_path}")
    typer.echo("usage: set BUB_MODEL=openai:gpt-5-codex and omit BUB_API_KEY")


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

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner

import bub.builtin.cli as cli
from bub.channels.control import ChannelAccountStatus, ChannelControl, ChannelLoginResult
from bub.framework import BubFramework
from bub.social import basic_channel_capabilities


def _create_app() -> object:
    framework = BubFramework()
    framework.load_hooks()
    return framework.create_cli_app()


def test_login_openai_runs_oauth_flow_and_prints_usage_hint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_login_openai_codex_oauth(**kwargs: object) -> cli.OpenAICodexOAuthTokens:
        captured.update(kwargs)
        prompt_for_redirect = kwargs["prompt_for_redirect"]
        assert callable(prompt_for_redirect)
        callback = prompt_for_redirect("https://auth.openai.com/authorize")
        assert callback == "http://localhost:1455/auth/callback?code=test"
        return cli.OpenAICodexOAuthTokens(
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            expires_at=123,
            account_id="acct_123",
        )

    monkeypatch.setattr(cli, "login_openai_codex_oauth", fake_login_openai_codex_oauth)
    monkeypatch.setattr(cli.typer, "prompt", lambda message: "http://localhost:1455/auth/callback?code=test")

    result = CliRunner().invoke(
        _create_app(),
        ["login", "openai", "--manual", "--no-browser", "--codex-home", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert captured["codex_home"] == tmp_path
    assert captured["open_browser"] is False
    assert captured["redirect_uri"] == cli.DEFAULT_CODEX_REDIRECT_URI
    assert captured["timeout_seconds"] == 300.0
    assert "login: ok" in result.stdout
    assert "account_id: acct_123" in result.stdout
    assert f"auth_file: {tmp_path / 'auth.json'}" in result.stdout
    assert "BUB_MODEL=openai:gpt-5-codex" in result.stdout


def test_login_openai_surfaces_oauth_errors(monkeypatch) -> None:
    def fake_login_openai_codex_oauth(**kwargs: object) -> cli.OpenAICodexOAuthTokens:
        raise cli.CodexOAuthLoginError("bad redirect")

    monkeypatch.setattr(cli, "login_openai_codex_oauth", fake_login_openai_codex_oauth)

    result = CliRunner().invoke(_create_app(), ["login", "openai", "--manual"])

    assert result.exit_code == 1
    assert "Codex login failed: bad redirect" in result.stderr


def test_login_rejects_unsupported_provider() -> None:
    result = CliRunner().invoke(_create_app(), ["login", "anthropic"])

    assert result.exit_code == 1
    assert "Unsupported auth provider: anthropic" in result.stderr


def test_cleanup_command_renders_framework_cleanup_lines(monkeypatch, tmp_path: Path) -> None:
    def fake_cleanup_runtime(self, *, force: bool = False) -> list[str]:
        assert force is True
        return ["cleaned: browser runtime", "cleaned: test plugin"]

    monkeypatch.setattr(BubFramework, "cleanup_runtime", fake_cleanup_runtime)

    result = CliRunner().invoke(_create_app(), ["--workspace", str(tmp_path), "cleanup", "--force"])

    assert result.exit_code == 0
    assert "cleaned: browser runtime" in result.stdout
    assert "cleaned: test plugin" in result.stdout


def test_channels_commands_render_control_plane_data(monkeypatch) -> None:
    control = ChannelControl(
        channel="wechat_clawbot",
        summary="WeChat native bridge",
        capabilities=basic_channel_capabilities("wechat_clawbot"),
        status_handler=lambda: [
            ChannelAccountStatus(
                channel="wechat_clawbot",
                account_id="acct-1",
                configured=True,
                running=True,
                state="active",
                detail="long-poll connected",
            )
        ],
        login_handler=lambda request: ChannelLoginResult(
            channel="wechat_clawbot",
            account_id=request.account_id or "acct-1",
            lines=("login: ok", f"account_id: {request.account_id or 'acct-1'}"),
        ),
        logout_handler=lambda account_id, force: [f"logout: {account_id or 'all'} force={force}"],
    )

    monkeypatch.setattr(BubFramework, "get_channel_controls", lambda self: {"wechat_clawbot": control})

    runner = CliRunner()
    list_result = runner.invoke(_create_app(), ["channels", "list"])
    status_result = runner.invoke(_create_app(), ["channels", "status", "wechat_clawbot"])
    login_result = runner.invoke(_create_app(), ["channels", "login", "wechat_clawbot", "--account", "acct-9"])
    logout_result = runner.invoke(_create_app(), ["channels", "logout", "wechat_clawbot", "--account", "acct-9"])

    assert list_result.exit_code == 0
    assert "wechat_clawbot" in list_result.stdout
    assert "WeChat native bridge" in list_result.stdout

    assert status_result.exit_code == 0
    assert "wechat_clawbot/acct-1" in status_result.stdout
    assert "state: active" in status_result.stdout

    assert login_result.exit_code == 0
    assert "login: ok" in login_result.stdout
    assert "account_id: acct-9" in login_result.stdout

    assert logout_result.exit_code == 0
    assert "logout: acct-9 force=False" in logout_result.stdout


def test_gateway_cleans_up_runtime_on_keyboard_interrupt(monkeypatch) -> None:
    framework = BubFramework()
    calls: list[tuple[str, object]] = []

    class DummyCtx:
        def ensure_object(self, cls):
            assert cls is BubFramework
            return framework

    class DummyManager:
        def __init__(self, framework_obj, enabled_channels=None) -> None:
            assert framework_obj is framework
            self.enabled_channels = enabled_channels

        async def listen_and_run(self) -> None:
            raise AssertionError("asyncio.run should be interrupted before awaiting manager coroutine")

    def fake_cleanup_runtime(*, force: bool = False) -> list[str]:
        calls.append(("cleanup", force))
        return ["cleaned"]

    def fake_ensure_gateway_slot(workspace: Path) -> None:
        calls.append(("ensure", workspace))

    def fake_release_gateway_slot(workspace: Path) -> None:
        calls.append(("release", workspace))

    def fake_asyncio_run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(framework, "cleanup_runtime", fake_cleanup_runtime)
    monkeypatch.setattr(cli, "ensure_gateway_slot", fake_ensure_gateway_slot)
    monkeypatch.setattr(cli, "release_gateway_slot", fake_release_gateway_slot)
    monkeypatch.setattr(asyncio, "run", fake_asyncio_run)

    import bub.channels.manager as channel_manager_module

    monkeypatch.setattr(channel_manager_module, "ChannelManager", DummyManager)

    with pytest.raises(KeyboardInterrupt):
        cli.gateway(DummyCtx(), enable_channels=[])

    assert calls[0][0] == "ensure"
    assert calls[1] == ("cleanup", False)
    assert calls[2][0] == "release"


def test_chat_cleans_up_runtime_on_keyboard_interrupt(monkeypatch) -> None:
    framework = BubFramework()
    calls: list[tuple[str, object]] = []

    class DummyChannel:
        def set_metadata(self, *, chat_id: str, session_id: str | None) -> None:
            calls.append(("metadata", (chat_id, session_id)))

    class DummyCtx:
        def ensure_object(self, cls):
            assert cls is BubFramework
            return framework

    class DummyManager:
        def __init__(self, framework_obj, enabled_channels=None) -> None:
            assert framework_obj is framework
            self.channel = DummyChannel()

        def get_channel(self, name: str):
            assert name == "cli"
            return self.channel

        async def listen_and_run(self) -> None:
            raise AssertionError("asyncio.run should be interrupted before awaiting manager coroutine")

    def fake_cleanup_runtime(*, force: bool = False) -> list[str]:
        calls.append(("cleanup", force))
        return ["cleaned"]

    def fake_asyncio_run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(framework, "cleanup_runtime", fake_cleanup_runtime)
    monkeypatch.setattr(asyncio, "run", fake_asyncio_run)

    import bub.channels.manager as channel_manager_module

    monkeypatch.setattr(channel_manager_module, "ChannelManager", DummyManager)

    with pytest.raises(KeyboardInterrupt):
        cli.chat(DummyCtx(), chat_id="room", session_id="sess")

    assert calls[0] == ("metadata", ("room", "sess"))
    assert calls[1] == ("cleanup", False)


def test_install_shutdown_signal_handlers_skips_unsupported(monkeypatch) -> None:
    class FakeLoop:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def add_signal_handler(self, signum: int, callback) -> None:
            del callback
            self.calls.append(signum)
            if signum == 999:
                raise NotImplementedError

    loop = FakeLoop()
    monkeypatch.setattr(cli, "_shutdown_signals", lambda: (int(signal.SIGTERM), 999))

    installed = cli._install_shutdown_signal_handlers(loop, lambda: None)

    assert installed == (int(signal.SIGTERM),)
    assert loop.calls == [int(signal.SIGTERM), 999]


@pytest.mark.asyncio
async def test_run_channel_manager_cancels_manager_on_shutdown_signal(monkeypatch) -> None:
    callbacks: dict[str, object] = {}

    class DummyManager:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def listen_and_run(self) -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True

    manager = DummyManager()

    def fake_install(loop: asyncio.AbstractEventLoop, callback) -> tuple[int, ...]:
        del loop
        callbacks["callback"] = callback
        return (int(signal.SIGTERM),)

    def fake_remove(loop: asyncio.AbstractEventLoop, signals_to_remove: tuple[int, ...]) -> None:
        del loop
        callbacks["removed"] = signals_to_remove

    monkeypatch.setattr(cli, "_install_shutdown_signal_handlers", fake_install)
    monkeypatch.setattr(cli, "_remove_shutdown_signal_handlers", fake_remove)

    task = asyncio.create_task(cli._run_channel_manager(manager))
    await manager.started.wait()

    shutdown_callback = callbacks["callback"]
    assert callable(shutdown_callback)
    shutdown_callback()
    await task

    assert manager.cancelled is True
    assert callbacks["removed"] == (int(signal.SIGTERM),)

from __future__ import annotations

import pytest

from bub.channels.bridge import BridgeChannel


class DummyBridgeChannel(BridgeChannel):
    name = "dummy_bridge"

    @property
    def command(self):
        return ["dummy"]


@pytest.mark.asyncio
async def test_bridge_stop_uses_terminate_helper_and_clears_runtime_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, float]] = []

    async def fake_on_receive(_message) -> None:
        return

    async def fake_terminate_process(process, *, timeout_seconds: float, kill_process_group: bool = False) -> bool:
        calls.append((process, timeout_seconds))
        return True

    class FakeProcess:
        returncode = None

    channel = DummyBridgeChannel(on_receive=fake_on_receive)
    channel._process = FakeProcess()  # type: ignore[assignment]
    channel._ready.set()

    monkeypatch.setattr("bub.channels.bridge.terminate_process", fake_terminate_process)

    await channel.stop()

    assert len(calls) == 1
    assert calls[0][1] == 5.0
    assert channel._process is None
    assert channel.is_ready is False

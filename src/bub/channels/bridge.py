from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
from collections.abc import Sequence
from typing import Any

from loguru import logger

from bub.channels.base import Channel
from bub.channels.bridge_protocol import BRIDGE_PROTOCOL_VERSION, build_action_frame
from bub.channels.message import ChannelMessage
from bub.social import OutboundAction
from bub.types import MessageHandler


class BridgeChannel(Channel):
    """Base class for subprocess-backed social adapters."""

    _process: asyncio.subprocess.Process | None = None

    def __init__(self, on_receive: MessageHandler) -> None:
        self._on_receive = on_receive
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._bridge_info: dict[str, Any] = {}

    async def start(self, stop_event: asyncio.Event) -> None:
        command = self.command
        if not command:
            logger.info("bridge.start channel={} configured=false", self.name)
            return
        self._ready.clear()
        self._bridge_info = {}
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stdout_task = asyncio.create_task(self._stdout_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        logger.info("bridge.start channel={} command={}", self.name, command)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self.ready_timeout_seconds):
                await self._ready.wait()
        if not self._ready.is_set():
            logger.warning("bridge.start channel={} ready_timeout_seconds={}", self.name, self.ready_timeout_seconds)

    async def stop(self) -> None:
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._stdout_task = None
        self._stderr_task = None
        if self._process is not None:
            if self._process.returncode is None:
                self._process.terminate()
                with contextlib.suppress(ProcessLookupError):
                    await self._process.wait()
            self._process = None
        self._ready.clear()
        logger.info("bridge.stopped channel={}", self.name)

    async def send(self, action: OutboundAction) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError(f"{self.name} bridge is not running.")
        if not self._ready.is_set():
            async with asyncio.timeout(self.ready_timeout_seconds):
                await self._ready.wait()
        payload = json.dumps(build_action_frame(action), ensure_ascii=False) + "\n"
        self._process.stdin.write(payload.encode("utf-8"))
        await self._process.stdin.drain()

    @property
    def command(self) -> Sequence[str]:
        return ()

    @property
    def ready_timeout_seconds(self) -> float:
        return 5.0

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def bridge_info(self) -> dict[str, Any]:
        return dict(self._bridge_info)

    async def _stdout_loop(self) -> None:
        if self._process is None or self._process.stdout is None:
            return
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                logger.info("bridge.stdout channel={} raw={}", self.name, raw)
                continue
            await self._handle_record(record)

    async def _stderr_loop(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            raw = line.decode("utf-8", errors="replace").strip()
            if raw:
                logger.warning("bridge.stderr channel={} raw={}", self.name, raw)

    async def _handle_record(self, record: dict[str, Any]) -> None:
        record_type = str(record.get("type", ""))
        version = str(record.get("version", BRIDGE_PROTOCOL_VERSION))
        if version != BRIDGE_PROTOCOL_VERSION:
            logger.warning("bridge.record.version_mismatch channel={} version={}", self.name, version)
        if record_type == "ready":
            self._bridge_info = {key: value for key, value in record.items() if key not in {"type"}}
            self._ready.set()
            logger.info("bridge.ready channel={} info={}", self.name, self._bridge_info)
            return
        if record_type in {"inbound", "message", "inbound_message"}:
            payload = record.get("message", record)
            if not isinstance(payload, dict):
                logger.warning("bridge.record.invalid channel={} payload={}", self.name, record)
                return
            await self._on_receive(ChannelMessage(**payload))
            return
        if record_type == "log":
            logger.info("bridge.log channel={} message={}", self.name, record.get("message", ""))
            return
        logger.debug("bridge.record.ignored channel={} type={}", self.name, record_type)


def split_command(value: str | None) -> list[str]:
    if value is None:
        return []
    return shlex.split(value)

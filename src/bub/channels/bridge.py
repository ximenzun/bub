from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from loguru import logger

from bub.channels.base import Channel
from bub.channels.bridge_protocol import BRIDGE_PROTOCOL_VERSION, build_action_frame
from bub.channels.message import ChannelMessage
from bub.social import OutboundAction, ProvisioningInfo
from bub.types import MessageHandler
from bub.utils import terminate_process


class BridgeChannel(Channel):
    """Base class for subprocess-backed social adapters."""

    _process: asyncio.subprocess.Process | None = None

    def __init__(self, on_receive: MessageHandler) -> None:
        self._on_receive = on_receive
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._bridge_info: dict[str, Any] = {}
        self._bridge_state: str | None = None
        self._bridge_provisioning: ProvisioningInfo | None = None

    async def start(self, stop_event: asyncio.Event) -> None:
        command = self.command
        if not command:
            logger.info("bridge.start channel={} configured=false", self.name)
            return
        await self.prepare()
        self._ready.clear()
        self._bridge_info = {}
        self._bridge_state = None
        self._bridge_provisioning = None
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stdout_task = asyncio.create_task(self._stdout_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        for frame in self.startup_frames:
            await self._send_frame(frame)
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
                forced_kill = await terminate_process(
                    self._process,
                    timeout_seconds=max(self.ready_timeout_seconds, 1.0),
                )
                if forced_kill:
                    logger.warning("bridge.stop force_killed channel={}", self.name)
            self._process = None
        self._ready.clear()
        self._bridge_state = None
        self._bridge_provisioning = None
        logger.info("bridge.stopped channel={}", self.name)

    async def send(self, action: OutboundAction) -> None:
        if not self._ready.is_set():
            async with asyncio.timeout(self.ready_timeout_seconds):
                await self._ready.wait()
        await self._send_frame(build_action_frame(action))

    @property
    def command(self) -> Sequence[str]:
        return ()

    @property
    def startup_frames(self) -> list[dict[str, Any]]:
        return []

    async def prepare(self) -> None:
        return

    @property
    def ready_timeout_seconds(self) -> float:
        return 5.0

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def bridge_info(self) -> dict[str, Any]:
        return dict(self._bridge_info)

    @property
    def bridge_state(self) -> str | None:
        return self._bridge_state

    @property
    def bridge_provisioning(self) -> ProvisioningInfo | None:
        return self._bridge_provisioning

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
        if record_type == "state":
            self._bridge_state = str(record.get("state", "unknown"))
            logger.info("bridge.state channel={} state={}", self.name, self._bridge_state)
            return
        if record_type == "provisioning":
            provisioning = record.get("provisioning", {})
            if not isinstance(provisioning, dict):
                logger.warning("bridge.provisioning.invalid channel={} payload={}", self.name, record)
                return
            self._bridge_provisioning = ProvisioningInfo.from_mapping(provisioning)
            logger.info("bridge.provisioning channel={} state={}", self.name, self._bridge_provisioning.state)
            return
        if record_type in {"inbound", "message", "inbound_message"}:
            payload = record.get("message", record)
            if not isinstance(payload, dict):
                logger.warning("bridge.record.invalid channel={} payload={}", self.name, record)
                return
            await self._on_receive(ChannelMessage(**payload))
            return
        if record_type == "log":
            level = str(record.get("level", "info")).lower()
            log_method = getattr(logger, level, logger.info)
            extras = {key: value for key, value in record.items() if key not in {"type", "version", "level", "message"}}
            if extras:
                log_method("bridge.log channel={} message={} extras={}", self.name, record.get("message", ""), extras)
            else:
                log_method("bridge.log channel={} message={}", self.name, record.get("message", ""))
            return
        logger.debug("bridge.record.ignored channel={} type={}", self.name, record_type)

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError(f"{self.name} bridge is not running.")
        payload = json.dumps(frame, ensure_ascii=False) + "\n"
        self._process.stdin.write(payload.encode("utf-8"))
        await self._process.stdin.drain()


def split_command(value: str | None) -> list[str]:
    if value is None:
        return []
    return shlex.split(value)


async def run_command(command: Sequence[str], *, cwd: Path | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"command failed exit={process.returncode}: {(stderr or stdout).decode('utf-8', errors='replace').strip()}"
        )

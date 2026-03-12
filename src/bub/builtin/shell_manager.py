from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import uuid
from dataclasses import dataclass, field

from bub.utils import terminate_process


@dataclass(slots=True)
class ManagedShell:
    shell_id: str
    cmd: str
    cwd: str | None
    process: asyncio.subprocess.Process
    stdout_chunks: list[str] = field(default_factory=list)
    stderr_chunks: list[str] = field(default_factory=list)
    output_chunks: list[str] = field(default_factory=list)
    read_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def stdout(self) -> str:
        return "".join(self.stdout_chunks)

    @property
    def stderr(self) -> str:
        return "".join(self.stderr_chunks)

    @property
    def output(self) -> str:
        return "".join(self.output_chunks)

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    @property
    def status(self) -> str:
        return "running" if self.returncode is None else "exited"


class ShellManager:
    def __init__(self) -> None:
        self._shells: dict[str, ManagedShell] = {}

    async def start(self, *, cmd: str, cwd: str | None, env: dict[str, str] | None = None) -> ManagedShell:
        if os.name != "nt":
            process = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        else:
            process = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        shell = ManagedShell(shell_id=f"bash-{uuid.uuid4().hex[:8]}", cmd=cmd, cwd=cwd, process=process)
        shell.read_tasks.extend(
            [
                asyncio.create_task(self._drain_stream(shell, process.stdout, shell.stdout_chunks)),
                asyncio.create_task(self._drain_stream(shell, process.stderr, shell.stderr_chunks)),
            ]
        )
        self._shells[shell.shell_id] = shell
        return shell

    def get(self, shell_id: str) -> ManagedShell:
        try:
            return self._shells[shell_id]
        except KeyError as exc:
            raise KeyError(f"unknown shell id: {shell_id}") from exc

    async def terminate(self, shell_id: str, *, timeout_seconds: float = 3.0) -> ManagedShell:
        shell = self.get(shell_id)
        if shell.returncode is None:
            await terminate_process(
                shell.process,
                timeout_seconds=timeout_seconds,
                kill_process_group=os.name != "nt" and sys.platform != "win32",
            )
        await self._finalize_shell(shell)
        return shell

    async def wait_closed(self, shell_id: str) -> ManagedShell:
        shell = self.get(shell_id)
        if shell.returncode is None:
            await shell.process.wait()
        await self._finalize_shell(shell)
        return shell

    async def _finalize_shell(self, shell: ManagedShell) -> None:
        for task in shell.read_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _drain_stream(
        self,
        shell: ManagedShell,
        stream: asyncio.StreamReader | None,
        chunks: list[str],
    ) -> None:
        if stream is None:
            return
        while chunk := await stream.read(4096):
            text = chunk.decode("utf-8", errors="replace")
            chunks.append(text)
            shell.output_chunks.append(text)


shell_manager = ShellManager()

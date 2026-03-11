import asyncio
import contextlib
import os
import signal
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from republic import TapeEntry

from bub.types import State


def exclude_none(d: dict[str, Any]) -> dict[str, Any]:
    """Exclude None values from a dictionary."""
    return {k: v for k, v in d.items() if v is not None}


async def wait_until_stopped[T](coro: Coroutine[None, None, T], stop_event: asyncio.Event) -> T:
    """Run a coroutine until a stop event is set."""
    task = asyncio.create_task(coro)
    waiter = asyncio.create_task(stop_event.wait())
    _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if stop_event.is_set():
        task.cancel()
        await task
        raise asyncio.CancelledError("Operation cancelled due to stop event")
    else:
        waiter.cancel()
        return task.result()


def workspace_from_state(state: State) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def get_entry_text(entry: TapeEntry) -> str:
    import yaml

    return yaml.safe_dump({"kind": entry.kind, "data": entry.payload}, sort_keys=False, allow_unicode=True)


async def terminate_process(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float = 5.0,
    kill_process_group: bool = False,
) -> bool:
    """Terminate a subprocess and force-kill it if it does not exit in time.

    Returns True when the helper had to escalate from terminate to kill.
    """

    if process.returncode is not None:
        return False

    forced_kill = False

    def _send(sig: signal.Signals) -> None:
        if kill_process_group and os.name != "nt" and process.pid is not None:
            os.killpg(process.pid, sig)
            return
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()

    with contextlib.suppress(ProcessLookupError):
        _send(signal.SIGTERM)
    try:
        async with asyncio.timeout(timeout_seconds):
            await process.wait()
    except TimeoutError:
        forced_kill = True
        with contextlib.suppress(ProcessLookupError):
            _send(signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
    return forced_kill

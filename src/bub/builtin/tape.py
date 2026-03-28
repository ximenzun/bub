import contextlib
import hashlib
import json
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic.dataclasses import dataclass
from republic import LLM, AsyncTapeStore, Tape, TapeEntry, TapeQuery

from bub.builtin.context import (
    DEFAULT_TAPE_VIEW,
    TapeView,
    build_tape_context,
    default_tape_context,
    restore_tape_state,
    select_messages_and_resources,
)
from bub.builtin.store import ForkTapeStore
from bub.workspace import workspace_id_for_path


@dataclass(frozen=True)
class TapeInfo:
    """Runtime tape info summary."""

    name: str
    entries: int
    anchors: int
    last_anchor: str | None
    entries_since_last_anchor: int
    last_token_usage: int | None


@dataclass(frozen=True)
class AnchorSummary:
    """Rendered anchor summary."""

    name: str
    state: dict[str, object]
    date: str


@dataclass(frozen=True)
class TapeContextSnapshot:
    """Resolved tape view plus hydrated state."""

    name: str
    view: str
    anchor: str | None
    state: dict[str, object]
    messages: list[dict[str, object]]
    resources: list[dict[str, object]]


class TapeService:
    def __init__(self, llm: LLM, archive_path: Path, store: ForkTapeStore) -> None:
        self._llm = llm
        self._archive_path = archive_path
        self._store = store

    async def info(self, tape_name: str) -> TapeInfo:
        tape = self._llm.tape(tape_name)
        entries = list(await tape.query_async.all())
        anchors = [entry for entry in entries if entry.kind == "anchor"]
        last_anchor = anchors[-1].payload.get("name") if anchors else None
        if last_anchor is not None:
            entries_since_last_anchor = [entry for entry in entries if entry.id > anchors[-1].id]
        else:
            entries_since_last_anchor = entries
        last_token_usage: int | None = None
        for entry in reversed(entries_since_last_anchor):
            if entry.kind == "event" and entry.payload.get("name") == "run":
                with contextlib.suppress(AttributeError):
                    token_usage = entry.payload.get("data", {}).get("usage", {}).get("total_tokens")
                    if token_usage and isinstance(token_usage, int):
                        last_token_usage = token_usage
                        break
        return TapeInfo(
            name=tape.name,
            entries=len(entries),
            anchors=len(anchors),
            last_anchor=str(last_anchor) if last_anchor else None,
            entries_since_last_anchor=len(entries_since_last_anchor),
            last_token_usage=last_token_usage,
        )

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        tape = self._llm.tape(tape_name)
        anchors = list(await tape.query_async.kinds("anchor").all())
        if not anchors:
            await tape.handoff_async("session/start", state={"owner": "human"})

    async def anchors(self, tape_name: str, limit: int = 20) -> list[AnchorSummary]:
        tape = self._llm.tape(tape_name)
        entries = list(await tape.query_async.kinds("anchor").all())
        results: list[AnchorSummary] = []
        for entry in entries[-limit:]:
            name = str(entry.payload.get("name", "-"))
            state = entry.payload.get("state")
            state_dict: dict[str, object] = dict(state) if isinstance(state, dict) else {}
            results.append(AnchorSummary(name=name, state=state_dict, date=entry.date))
        return results

    async def latest_anchor(self, tape_name: str) -> AnchorSummary | None:
        anchors = await self.anchors(tape_name, limit=1)
        if not anchors:
            return None
        return anchors[-1]

    async def _archive(self, tape_name: str) -> Path:
        tape = self._llm.tape(tape_name)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self._archive_path.mkdir(parents=True, exist_ok=True)
        archive_path = self._archive_path / f"{tape.name}.jsonl.{stamp}.bak"
        with archive_path.open("w", encoding="utf-8") as f:
            for entry in await tape.query_async.all():
                f.write(json.dumps(asdict(entry)) + "\n")
        return archive_path

    async def reset(self, tape_name: str, *, archive: bool = False) -> str:
        tape = self._llm.tape(tape_name)
        archive_path: Path | None = None
        if archive:
            archive_path = await self._archive(tape_name)
        state = {"owner": "human"}
        if archive_path is not None:
            state["archived"] = str(archive_path)
        await tape.handoff_async("session/start", state=state)
        return f"Archived: {archive_path}" if archive_path else "ok"

    async def handoff(self, tape_name: str, *, name: str, state: dict[str, Any] | None = None) -> list[TapeEntry]:
        tape = self._llm.tape(tape_name)
        entries = await tape.handoff_async(name, state=state)
        return cast(list[TapeEntry], entries)

    async def search(self, query: TapeQuery[AsyncTapeStore]) -> list[TapeEntry]:
        return list(await self._store.fetch_all(query))

    async def append_event(self, tape_name: str, name: str, payload: dict[str, Any], **meta: Any) -> None:
        tape = self._llm.tape(tape_name)
        await tape.append_async(TapeEntry.event(name=name, data=payload, **meta))

    def session_tape(self, session_id: str, workspace: Path) -> Tape:
        workspace_id = workspace_id_for_path(workspace, self._archive_path.parent)
        tape_name = (
            workspace_id + "__" + hashlib.md5(session_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        )
        return self._llm.tape(tape_name, context=default_tape_context())

    async def hydrate_context(self, tape: Tape, runtime_state: dict[str, Any] | None = None) -> AnchorSummary | None:
        anchor = await self.latest_anchor(tape.name)
        merged_state = restore_tape_state(
            runtime_state,
            anchor_name=anchor.name if anchor else None,
            anchor_state=cast(dict[str, Any], anchor.state) if anchor else None,
        )
        tape.context = build_tape_context(state=merged_state)
        return anchor

    async def context_snapshot(
        self,
        tape_name: str,
        *,
        view: TapeView = DEFAULT_TAPE_VIEW,
        runtime_state: dict[str, Any] | None = None,
    ) -> TapeContextSnapshot:
        anchor = await self.latest_anchor(tape_name)
        merged_state = restore_tape_state(
            runtime_state,
            anchor_name=anchor.name if anchor else None,
            anchor_state=cast(dict[str, Any], anchor.state) if anchor else None,
            view=view,
        )
        read_anchor = None if view == "timeline" else build_tape_context().anchor
        tape = self._llm.tape(tape_name, context=build_tape_context(state=merged_state, view=view, anchor=read_anchor))
        entries = _entries_for_view(list(await tape.query_async.all()), view=view)
        messages, resources = select_messages_and_resources(entries, tape.context)
        visible_state = cast(dict[str, object], dict(anchor.state) if anchor else {})
        return TapeContextSnapshot(
            name=tape_name,
            view=view,
            anchor=anchor.name if anchor else None,
            state=visible_state,
            messages=messages,
            resources=resources,
        )

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        async with self._store.fork(tape_name, merge_back=merge_back):
            yield


def _entries_for_view(entries: list[TapeEntry], *, view: TapeView) -> list[TapeEntry]:
    if view == "timeline":
        return entries
    last_anchor_index = None
    for index, entry in enumerate(entries):
        if entry.kind == "anchor":
            last_anchor_index = index
    if last_anchor_index is None:
        return entries
    return entries[last_anchor_index + 1 :]

import contextlib
import hashlib
import json
import re
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic.dataclasses import dataclass
from rapidfuzz import fuzz, process
from republic import LLM, Tape, TapeEntry

from bub.builtin.store import ForkTapeStore
from bub.utils import get_entry_text

WORD_PATTERN = re.compile(r"[a-z0-9_/-]+")
MIN_FUZZY_QUERY_LENGTH = 3
MIN_FUZZY_SCORE = 80
MAX_FUZZY_CANDIDATES = 128


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
            results.append(AnchorSummary(name=name, state=state_dict))
        return results

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
        await tape.reset_async()
        state = {"owner": "human"}
        if archive_path is not None:
            state["archived"] = str(archive_path)
        await tape.handoff_async("session/start", state=state)
        return f"Archived: {archive_path}" if archive_path else "ok"

    async def handoff(self, tape_name: str, *, name: str, state: dict[str, Any] | None = None) -> list[TapeEntry]:
        tape = self._llm.tape(tape_name)
        entries = await tape.handoff_async(name, state=state)
        return cast(list[TapeEntry], entries)

    async def search(
        self,
        tape_name: str,
        query: str,
        *,
        limit: int = 20,
        start: str | None = None,
        end: str | None = None,
        kinds: tuple[str, ...] = ("message", "tool_result"),
    ) -> list[TapeEntry]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []
        results: list[TapeEntry] = []
        tapes = [self._llm.tape(tape_name)]
        seen: set[str] = set()
        start_dt = _parse_datetime_bound(start, is_end=False)
        end_dt = _parse_datetime_bound(end, is_end=True)

        for tape in tapes:
            count = 0
            for entry in reversed(list(await tape.query_async.all())):
                if kinds and entry.kind not in kinds:
                    continue
                if not _entry_matches_date_range(entry, start_dt=start_dt, end_dt=end_dt):
                    continue
                payload_text = get_entry_text(entry).lower()
                if payload_text in seen:
                    continue
                seen.add(payload_text)

                if normalized_query in payload_text or self._is_fuzzy_match(normalized_query, payload_text):
                    results.append(entry)
                    count += 1
                    if count >= limit:
                        break
        return results

    @staticmethod
    def _is_fuzzy_match(normalized_query: str, payload_text: str) -> bool:
        if len(normalized_query) < MIN_FUZZY_QUERY_LENGTH:
            return False

        query_tokens = WORD_PATTERN.findall(normalized_query)
        if not query_tokens:
            return False
        query_phrase = " ".join(query_tokens)
        window_size = len(query_tokens)

        source_tokens = WORD_PATTERN.findall(payload_text)
        if not source_tokens:
            return False

        candidates: list[str] = []
        for token in source_tokens:
            candidates.append(token)
            if len(candidates) >= MAX_FUZZY_CANDIDATES:
                break

        if window_size > 1:
            max_window_start = len(source_tokens) - window_size + 1
            for idx in range(max(0, max_window_start)):
                candidates.append(" ".join(source_tokens[idx : idx + window_size]))
                if len(candidates) >= MAX_FUZZY_CANDIDATES:
                    break

        best_match = process.extractOne(
            query_phrase,
            candidates,
            scorer=fuzz.WRatio,
            score_cutoff=MIN_FUZZY_SCORE,
        )
        return best_match is not None

    async def append_event(self, tape_name: str, name: str, payload: dict[str, Any], **meta: Any) -> None:
        tape = self._llm.tape(tape_name)
        await tape.append_async(TapeEntry.event(name=name, data=payload, **meta))

    def session_tape(self, session_id: str, workspace: Path) -> Tape:
        workspace_hash = hashlib.md5(str(workspace.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        tape_name = (
            workspace_hash + "__" + hashlib.md5(session_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        )
        return self._llm.tape(tape_name)

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        async with self._store.fork(tape_name, merge_back=merge_back):
            yield


def _entry_matches_date_range(
    entry: TapeEntry,
    *,
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> bool:
    if start_dt is None and end_dt is None:
        return True
    entry_dt = datetime.fromisoformat(entry.date)
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=UTC)
    if start_dt is not None and entry_dt < start_dt:
        return False
    return not (end_dt is not None and entry_dt > end_dt)


def _parse_datetime_bound(value: str | None, *, is_end: bool) -> datetime | None:
    if value is None or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if len(value.strip()) == 10 and is_end:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed

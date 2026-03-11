from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from republic import TapeEntry

from bub.builtin.tape import TapeService


def _entry(entry_id: int, kind: str, payload: dict, date: str) -> TapeEntry:
    return TapeEntry(id=entry_id, kind=kind, payload=payload, meta={}, date=date)


def _service(entries: list[TapeEntry]) -> TapeService:
    tape = MagicMock()
    tape.query_async = SimpleNamespace(all=AsyncMock(return_value=entries))
    tape.append_async = AsyncMock()
    llm = MagicMock()
    llm.tape.return_value = tape
    return TapeService(llm=llm, archive_path=MagicMock(), store=MagicMock())


@pytest.mark.asyncio
async def test_tape_search_includes_tool_results_by_default() -> None:
    service = _service(
        [
            _entry(1, "message", {"role": "user", "content": "hello"}, "2026-03-10T00:00:00+00:00"),
            _entry(2, "tool_result", {"results": ["rg found needle"]}, "2026-03-11T00:00:00+00:00"),
        ]
    )

    results = await service.search("test", "needle")

    assert [entry.kind for entry in results] == ["tool_result"]


@pytest.mark.asyncio
async def test_tape_search_filters_by_date_range_and_kind() -> None:
    service = _service(
        [
            _entry(1, "message", {"role": "user", "content": "hello old"}, "2026-03-09T08:00:00+00:00"),
            _entry(2, "tool_result", {"results": ["hello tool"]}, "2026-03-11T08:00:00+00:00"),
            _entry(3, "message", {"role": "user", "content": "hello new"}, "2026-03-11T09:00:00+00:00"),
        ]
    )

    results = await service.search("test", "hello", start="2026-03-11", end="2026-03-11", kinds=("message",))

    assert [entry.id for entry in results] == [3]


@pytest.mark.asyncio
async def test_append_event_stores_payload_under_data_field() -> None:
    service = _service([])
    tape = service._llm.tape.return_value

    await service.append_event("test", "loop.step.start", {"prompt": "hello"})

    appended = tape.append_async.await_args.args[0]
    assert appended.payload["name"] == "loop.step.start"
    assert appended.payload["data"] == {"prompt": "hello"}

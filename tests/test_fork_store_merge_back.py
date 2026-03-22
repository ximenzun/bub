from __future__ import annotations

from datetime import UTC, datetime

import pytest
from republic import TapeEntry, TapeQuery
from republic.tape import InMemoryTapeStore

from bub.builtin.store import FileTapeStore, ForkTapeStore


@pytest.mark.asyncio
async def test_fork_merge_back_true_merges_entries() -> None:
    """With merge_back=True (default), forked entries are merged into the parent."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)

    async with store.fork("test-tape", merge_back=True):
        await store.append("test-tape", TapeEntry.event(name="step", data={"x": 1}))
        await store.append("test-tape", TapeEntry.event(name="step", data={"x": 2}))

    entries = parent.read("test-tape")
    assert entries is not None
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_fork_merge_back_false_discards_entries() -> None:
    """With merge_back=False, forked entries are NOT merged into the parent."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)

    async with store.fork("test-tape", merge_back=False):
        await store.append("test-tape", TapeEntry.event(name="step", data={"x": 1}))

    entries = parent.read("test-tape")
    # No entries should have been merged
    assert entries is None or len(entries) == 0


@pytest.mark.asyncio
async def test_fork_default_merge_back_is_true() -> None:
    """The default value of merge_back should be True."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)

    async with store.fork("test-tape"):
        await store.append("test-tape", TapeEntry.event(name="step", data={"v": 1}))

    entries = parent.read("test-tape")
    assert entries is not None
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_fork_fetch_all_replays_full_query_semantics_for_live_entries() -> None:
    parent = InMemoryTapeStore()
    parent.append(
        "test-tape",
        TapeEntry(
            0,
            "event",
            {"name": "step", "data": {"x": 1}},
            date=datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        ),
    )
    store = ForkTapeStore(parent)

    async with store.fork("test-tape", merge_back=False):
        await store.append(
            "test-tape",
            TapeEntry(
                0,
                "event",
                {"name": "step", "data": {"x": 2}},
                date=datetime(2026, 2, 1, tzinfo=UTC).isoformat(),
            ),
        )
        query = (
            TapeQuery(tape="test-tape", store=store).between_dates("2026-02-01", "2026-02-28").kinds("event").limit(1)
        )

        entries = list(await store.fetch_all(query))

    assert len(entries) == 1
    assert entries[0].payload["data"]["x"] == 2


@pytest.mark.asyncio
async def test_fork_preserves_multimodal_content_on_merge_back() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)

    async with store.fork("test-tape", merge_back=True):
        await store.append(
            "test-tape",
            TapeEntry.message({
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
                ],
            }),
        )

    entries = parent.read("test-tape")
    assert entries is not None
    assert entries[0].payload["content"][1]["type"] == "image_url"


def test_file_tape_store_preserves_entry_date(tmp_path) -> None:
    store = FileTapeStore(tmp_path)
    stamp = datetime(2026, 3, 1, 12, 30, tzinfo=UTC).isoformat()

    store.append("test-tape", TapeEntry(0, "event", {"name": "step"}, date=stamp))
    entries = store.read("test-tape")

    assert entries is not None
    assert entries[0].date == stamp

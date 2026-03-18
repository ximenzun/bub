from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from republic import LLM, TapeEntry, ToolContext
from republic.tape import InMemoryTapeStore

from bub.builtin.context import default_tape_context
from bub.builtin.resource_refs import RESOURCE_REFS_KEY
from bub.builtin.store import ForkTapeStore
from bub.builtin.tape import TapeService
from bub.builtin.tools import tape_anchors, tape_context, tape_handoff, tape_resources, tape_search, tape_view


def _make_tape_service(tmp_path: Path) -> tuple[TapeService, InMemoryTapeStore]:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)
    llm = LLM("openai:gpt-4o", tape_store=store, context=default_tape_context())
    return TapeService(llm, tmp_path, store), parent


async def _seed_tape(service: TapeService, tmp_path: Path) -> str:
    tape = service.session_tape("user/session", tmp_path)
    await service.ensure_bootstrap_anchor(tape.name)
    await service.handoff(tape.name, name="phase-1", state={"summary": "carry this forward", "owner": "agent"})
    await tape.append_async(TapeEntry.system("system note"))
    await tape.append_async(TapeEntry.message({"role": "user", "content": "next question"}))
    await tape.append_async(
        TapeEntry.tool_call(
            [{"id": "call-1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]
        )
    )
    await tape.append_async(TapeEntry.tool_result([{"status": "ok"}]))
    return tape.name


def _tool_context(service: TapeService, tape_name: str) -> ToolContext:
    agent = SimpleNamespace(tapes=service)
    return ToolContext(tape=tape_name, run_id="test-run", state={"_runtime_agent": agent})


@pytest.mark.asyncio
async def test_context_snapshot_active_includes_handoff_summary(tmp_path: Path) -> None:
    service, _ = _make_tape_service(tmp_path)
    tape_name = await _seed_tape(service, tmp_path)

    snapshot = await service.context_snapshot(tape_name, runtime_state={"session_id": "user/session"})

    assert snapshot.anchor == "phase-1"
    assert snapshot.state == {"summary": "carry this forward", "owner": "agent"}
    assert snapshot.messages[0]["role"] == "system"
    assert "carry this forward" in str(snapshot.messages[0]["content"])
    assert snapshot.messages[1] == {"role": "user", "content": "next question"}


@pytest.mark.asyncio
async def test_context_snapshot_timeline_renders_system_anchor_and_events(tmp_path: Path) -> None:
    service, _ = _make_tape_service(tmp_path)
    tape_name = await _seed_tape(service, tmp_path)

    snapshot = await service.context_snapshot(tape_name, view="timeline")
    rendered = "\n".join(str(message.get("content", "")) for message in snapshot.messages)

    assert "[anchor] phase-1" in rendered
    assert "[event:handoff]" in rendered
    assert "system note" in rendered


@pytest.mark.asyncio
async def test_context_snapshot_sanitizes_multimodal_messages_and_tool_results(tmp_path: Path) -> None:
    service, _ = _make_tape_service(tmp_path)
    tape = service.session_tape("user/session", tmp_path)
    await service.ensure_bootstrap_anchor(tape.name)
    await tape.append_async(
        TapeEntry.message(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}},
                ],
            }
        )
    )
    await tape.append_async(
        TapeEntry.tool_result(
            [
                json.dumps(
                    {
                        "ok": True,
                        "base64": "cG5n",
                        "preview": "data:image/png;base64,cG5n",
                    },
                    ensure_ascii=False,
                )
            ]
        )
    )

    snapshot = await service.context_snapshot(tape.name, runtime_state={"session_id": "user/session"})

    assert snapshot.messages[0]["role"] == "user"
    assert snapshot.messages[0]["content"] == "describe this\n\n[1 image omitted from tape history]"
    assert snapshot.messages[1]["role"] == "tool"
    assert "data:image/png;base64" not in snapshot.messages[1]["content"]
    assert '"base64": "[base64 omitted: 4 chars]"' in snapshot.messages[1]["content"]
    assert "[data URL omitted: image/png; 4 chars]" in snapshot.messages[1]["content"]


@pytest.mark.asyncio
async def test_context_snapshot_collects_structured_resources_without_dumping_locators(tmp_path: Path) -> None:
    service, _ = _make_tape_service(tmp_path)
    tape = service.session_tape("user/session", tmp_path)
    await service.ensure_bootstrap_anchor(tape.name)
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"png")
    await tape.append_async(
        TapeEntry.message(
            {
                "role": "user",
                "content": "[Lark image]",
                RESOURCE_REFS_KEY: [
                    {
                        "kind": "image",
                        "scope": "message",
                        "content_type": "image/*",
                        "locator": {
                            "kind": "channel_file",
                            "channel": "lark",
                            "message_id": "om_1",
                            "file_key": "img_1",
                            "resource_type": "image",
                        },
                    }
                ],
            }
        )
    )
    await tape.append_async(
        TapeEntry.tool_call(
            [{"id": "call-1", "type": "function", "function": {"name": "browser_snapshot", "arguments": "{}"}}]
        )
    )
    await tape.append_async(
        TapeEntry.tool_result(
            [
                json.dumps(
                    {
                        "ok": True,
                        "artifacts": [
                            {
                                "kind": "image",
                                "name": "screen.png",
                                "path": str(image_path),
                                "content_type": "image/png",
                                "transport": "local_path",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            ]
        )
    )

    snapshot = await service.context_snapshot(tape.name, runtime_state={"session_id": "user/session"})

    assert len(snapshot.resources) == 2
    assert snapshot.resources[0]["locator"]["kind"] == "channel_file"
    assert snapshot.resources[1]["origin_name"] == "browser_snapshot"
    assert snapshot.resources[1]["locator"]["path"] == str(image_path)
    assert str(image_path) not in snapshot.messages[-1]["content"]
    assert '"locator_kind": "path"' in snapshot.messages[-1]["content"]


@pytest.mark.asyncio
async def test_tape_search_defaults_cover_anchor_and_event_entries(tmp_path: Path) -> None:
    service, _ = _make_tape_service(tmp_path)
    tape_name = await _seed_tape(service, tmp_path)
    context = _tool_context(service, tape_name)

    result = await tape_search.run(query="carry this forward", context=context)

    assert "[tape.search]:" in result
    assert "carry this forward" in result
    assert '"name": "phase-1"' in result


@pytest.mark.asyncio
async def test_tape_anchors_tool_renders_anchor_state(tmp_path: Path) -> None:
    service, _ = _make_tape_service(tmp_path)
    tape_name = await _seed_tape(service, tmp_path)
    context = _tool_context(service, tape_name)

    result = await tape_anchors.run(context=context)

    assert "- phase-1 @" in result
    assert '"summary": "carry this forward"' in result


@pytest.mark.asyncio
async def test_tape_view_and_context_tools_show_active_state(tmp_path: Path) -> None:
    service, _ = _make_tape_service(tmp_path)
    tape_name = await _seed_tape(service, tmp_path)
    context = _tool_context(service, tape_name)

    rendered_view = await tape_view.run(view="active", limit=10, context=context)
    rendered_context = await tape_context.run(view="active", limit=10, context=context)
    rendered_resources = await tape_resources.run(view="active", limit=10, context=context)

    assert "view: active" in rendered_view
    assert '"role": "system"' in rendered_view
    assert "carry this forward" in rendered_view
    assert "resources:" in rendered_view
    assert "anchor_state:" in rendered_context
    assert "resources:" in rendered_context
    assert '"owner": "agent"' in rendered_context
    assert "resources: 0" in rendered_resources


@pytest.mark.asyncio
async def test_tape_handoff_tool_accepts_structured_state_and_updates_context(tmp_path: Path) -> None:
    service, _ = _make_tape_service(tmp_path)
    tape = service.session_tape("user/session", tmp_path)
    await service.ensure_bootstrap_anchor(tape.name)
    context = _tool_context(service, tape.name)

    result = await tape_handoff.run(
        name="phase-2",
        summary="done",
        state_json='{"owner":"agent","step":2}',
        context=context,
    )
    snapshot = await service.context_snapshot(tape.name)

    assert result == "anchor added: phase-2"
    assert snapshot.anchor == "phase-2"
    assert snapshot.state == {"owner": "agent", "step": 2, "summary": "done"}
    assert context.state["summary"] == "done"


@pytest.mark.asyncio
async def test_tape_reset_keeps_history_and_starts_new_segment(tmp_path: Path) -> None:
    service, parent = _make_tape_service(tmp_path)
    tape_name = await _seed_tape(service, tmp_path)
    before = parent.read(tape_name)

    result = await service.reset(tape_name)
    after = parent.read(tape_name)
    snapshot = await service.context_snapshot(tape_name)

    assert result == "ok"
    assert before is not None
    assert after is not None
    assert len(after) == len(before) + 2
    assert snapshot.anchor == "session/start"
    assert snapshot.messages == []

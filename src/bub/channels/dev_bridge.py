from __future__ import annotations

import argparse
import json
import sys

from bub.channels.bridge_protocol import build_inbound_message_frame, build_log_frame, build_ready_frame
from bub.channels.message import ChannelMessage


def main() -> int:
    parser = argparse.ArgumentParser(description="Development JSONL bridge for Bub bridge channels.")
    parser.add_argument("--channel", default="bridge_demo")
    parser.add_argument("--chat-id", default="dev-chat")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--boot-message", default="")
    parser.add_argument("--echo-actions", action="store_true")
    args = parser.parse_args()

    session_id = args.session_id or f"{args.channel}:{args.chat_id}"
    _emit(build_ready_frame(args.channel, name="dev_bridge"))

    if args.boot_message:
        _emit(
            build_inbound_message_frame(
                ChannelMessage(
                    session_id=session_id,
                    channel=args.channel,
                    chat_id=args.chat_id,
                    content=args.boot_message,
                    is_active=True,
                )
            )
        )

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            _emit(build_log_frame("invalid json from host", level="warning", raw=raw))
            continue

        action = frame.get("action", {})
        action_kind = action.get("kind", "unknown")
        _emit(build_log_frame(f"received action {action_kind}", action=action_kind))
        if args.echo_actions:
            text = action.get("text", "")
            if text:
                _emit(
                    build_inbound_message_frame(
                        ChannelMessage(
                            session_id=session_id,
                            channel=args.channel,
                            chat_id=args.chat_id,
                            content=f"echo: {text}",
                        )
                    )
                )

    return 0


def _emit(frame: dict[str, object]) -> None:
    print(json.dumps(frame, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

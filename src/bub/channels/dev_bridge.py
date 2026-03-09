from __future__ import annotations

import argparse
import json
import sys

from bub.channels.bridge_protocol import (
    build_inbound_message_frame,
    build_log_frame,
    build_provisioning_frame,
    build_ready_frame,
    build_state_frame,
)
from bub.channels.message import ChannelMessage
from bub.social import ProvisioningInfo


def main() -> int:
    parser = argparse.ArgumentParser(description="Development JSONL bridge for Bub bridge channels.")
    parser.add_argument("--channel", default="bridge_demo")
    parser.add_argument("--chat-id", default="dev-chat")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--boot-message", default="")
    parser.add_argument("--echo-actions", action="store_true")
    parser.add_argument("--require-config", action="store_true")
    args = parser.parse_args()

    session_id = args.session_id or f"{args.channel}:{args.chat_id}"
    configured = not args.require_config
    if configured:
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

        if frame.get("type") == "configure":
            config = frame.get("config", {})
            bot_id = config.get("bot_id") if isinstance(config, dict) else None
            configured = True
            _emit(
                build_provisioning_frame(
                    ProvisioningInfo(
                        mode="interactive_pairing",
                        state="active" if bot_id else "pending",
                        pairing_code=config.get("pairing_code") if isinstance(config, dict) else None,
                        config_key=config.get("config_key") if isinstance(config, dict) else None,
                    )
                )
            )
            _emit(build_state_frame("configured", configured=bool(bot_id)))
            _emit(build_ready_frame(args.channel, name="dev_bridge", configured=bool(bot_id)))
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

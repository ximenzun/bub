---
name: wecom
description: |
  Enterprise WeCom outbound communication skill for Bub. Use when Bub needs to
  send proactive WeCom messages, push webhook notifications, send WeCom template cards,
  or trigger native WeCom long-connection actions such as proactive sends and card updates.
metadata:
  channel: wecom
---

# WeCom Skill

Agent-facing execution guide for Enterprise WeCom outbound communication.

Assumption: the relevant `BUB_WECOM_*` environment variables are already configured.

## Choose The Path

1. Use `wecom_longconn_send.py` when the target is the long-connection smart bot and you need:
   - proactive bot messages to a WeCom user or group chat
   - native WeCom template cards
   - native `update_card` with a callback `reply_grant.token`
2. Use `wecom_webhook_send.py` when the target is the outbound-only webhook channel and you need:
   - notifications or alerts
   - webhook text/markdown/image/file/voice/template_card sends
3. If Bub is already processing an inbound `wecom_longconn_bot` message and a normal reply is enough, prefer Bub's native channel reply path instead of calling these scripts.

## Required Inputs

Collect these before execution:

- target mode: `longconn` or `webhook`
- message content or card payload
- `chat_id` for long-connection proactive sends
- `reply_token` for long-connection `reply_message` or `update_card`
- `card_json` for template card send/update
- attachment path or URL when sending webhook image/file/voice or passive long-connection image replies

## Execution Policy

1. Default to `wecom_longconn_send.py` for real conversations and `wecom_webhook_send.py` for notifications.
2. Do not use `wecom_webhook_send.py` for `edit_message` or `update_card`; webhook does not support them.
3. Do not use `edit_message` for WeCom long-connection; use `update_card` with `--card-json` and `--reply-token`.
4. For proactive long-connection sends, prefer `text`, `rich_text`, or `card`.
5. For webhook template cards and long-connection template cards, pass full JSON with `--card-json`.
6. For multiline content, prefer heredoc command substitution instead of embedding raw line breaks in quoted strings.

## Command Templates

Paths are relative to this skill directory.

```bash
# Long-connection proactive message
uv run ./scripts/wecom_longconn_send.py \
  --chat-id <CHAT_ID> \
  --message "<TEXT>"

# Long-connection proactive markdown-style message
uv run ./scripts/wecom_longconn_send.py \
  --chat-id <CHAT_ID> \
  --content-type rich_text \
  --message "$(cat <<'EOF'
Build finished.
- 93 tests passed
EOF
)"

# Long-connection proactive template card
uv run ./scripts/wecom_longconn_send.py \
  --chat-id <CHAT_ID> \
  --content-type card \
  --card-json '<TEMPLATE_CARD_JSON>'

# Long-connection native card update
uv run ./scripts/wecom_longconn_send.py \
  --kind update_card \
  --chat-id <CHAT_ID> \
  --reply-token <REQ_ID> \
  --event-type template_card_event \
  --card-json '<TEMPLATE_CARD_JSON>' \
  --target-id <USER_ID>

# Webhook text notification
uv run ./scripts/wecom_webhook_send.py \
  --message "<TEXT>"

# Webhook markdown notification
uv run ./scripts/wecom_webhook_send.py \
  --content-type rich_text \
  --message "<TEXT>"

# Webhook template card
uv run ./scripts/wecom_webhook_send.py \
  --content-type card \
  --card-json '<TEMPLATE_CARD_JSON>'

# Webhook image/file/voice send
uv run ./scripts/wecom_webhook_send.py \
  --content-type image \
  --attachment ./chart.png
```

## Script Interface Reference

### `wecom_longconn_send.py`

- `--chat-id`: required
- `--kind`: `send_message`, `reply_message`, `update_card`
- `--content-type`: `text`, `rich_text`, `card`, `image`, `file`, `audio`
- `--message`: optional text content
- `--card-json`: optional JSON object for template cards
- `--attachment`: optional local path, `file://`, `http(s)://`, or data URL
- `--reply-token`: optional callback `req_id`, required for `reply_message` and `update_card`
- `--reply-to-message-id`: optional message id
- `--event-type`: optional callback event type, mainly for `enter_chat` or `template_card_event`
- `--target-id`: optional repeatable user id list for `update_card`
- `--mention-user`, `--mention-mobile`, `--mention-all`: optional mention controls

### `wecom_webhook_send.py`

- `--message`: optional text content
- `--content-type`: `text`, `rich_text`, `card`, `image`, `file`, `audio`
- `--card-json`: optional JSON object for template cards
- `--attachment`: optional local path, `file://`, `http(s)://`, or data URL
- `--mention-user`, `--mention-mobile`, `--mention-all`: optional mention controls

## Active Response Policy

When this skill is in scope:

1. Send a quick acknowledgment if the user expects an action in WeCom.
2. Use a template card when the result is status-heavy or needs follow-up buttons.
3. If a long-connection card must change after user interaction, use `update_card`, not a fresh message.
4. If execution fails, report the exact failing path: webhook send, long-connection bridge startup, or card update.

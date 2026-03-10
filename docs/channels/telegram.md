# Telegram

Telegram is the builtin remote channel adapter in current core Bub.

## Configuration

Environment variables are read by `TelegramSettings` (`src/bub/channels/telegram.py`).

Required:

```bash
BUB_TELEGRAM_TOKEN=123456:token
```

Optional allowlists (comma-separated):

```bash
BUB_TELEGRAM_ALLOW_USERS=123456789,your_username
BUB_TELEGRAM_ALLOW_CHATS=123456789,-1001234567890
```

Optional proxy:

```bash
BUB_TELEGRAM_PROXY=http://127.0.0.1:7890
```

## Message Behavior

- Session id is `telegram:<chat_id>`.
- `/start` is handled by builtin channel logic.
- `/bub ...` is accepted and normalized to plain prompt content.
- Non-command messages are ingested; active/follow-up behavior is decided by channel filter metadata plus debounce handling.

## Outbound Behavior

- Outbound is sent back to Telegram chat via bot API.
- Empty outbound text is ignored.
- If outbound content is JSON, the `"message"` field is used when present.
- Telegram draft progress is available through the native `set_draft` action and maps to `sendMessageDraft`.
- Draft progress currently depends on Telegram's draft support for bots with forum topic mode enabled; unsupported chats fall back to `typing`.

## Access Control

- If `BUB_TELEGRAM_ALLOW_CHATS` is set, non-listed chats are ignored.
- If `BUB_TELEGRAM_ALLOW_USERS` is set, non-listed users are denied.
- In group chats, keep allowlists strict for production bots.

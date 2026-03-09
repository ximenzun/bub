# Channels

Bub uses channel adapters to run the same agent pipeline across different I/O endpoints.

## Builtin Channels

- `cli`: local interactive terminal channel (`uv run bub chat`)
- `telegram`: Telegram bot channel (`uv run bub gateway`)
- `wecom_webhook`: Enterprise WeCom webhook push channel (outbound-only)

See [Telegram](telegram.md) for channel-specific configuration and runtime behavior.

## Run Modes

Local interactive mode:

```bash
uv run bub chat
```

Channel listener mode (all non-`cli` channels by default):

```bash
uv run bub gateway
```

Enable only Telegram:

```bash
uv run bub gateway --enable-channel telegram
```

Enable only the WeCom webhook adapter:

```bash
uv run bub gateway --enable-channel wecom_webhook
```

## Session Semantics

- `run` command default session id: `<channel>:<chat_id>`
- Telegram channel session id: `telegram:<chat_id>`
- `chat` command default session id: `cli_session` (override with `--session-id`)

## Debounce Behavior

- `cli` does not debounce; each input is processed immediately.
- Other channels can debounce and batch inbound messages per session.
- Comma commands (`,` prefix) always bypass debounce and execute immediately.

## About WeCom

- `wecom_webhook` is currently outbound-only.
- It targets the Enterprise WeCom webhook push surface (`/cgi-bin/webhook/send`).
- Long-connection WeCom smart bots are not implemented yet.

## About Discord

Core Bub does not currently include a builtin Discord adapter.
If you need Discord, implement it in an external plugin via `provide_channels`.

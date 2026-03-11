# Channels

Bub uses channel adapters to run the same agent pipeline across different I/O endpoints.

## Builtin Channels

- `cli`: local interactive terminal channel (`uv run bub chat`)
- `telegram`: Telegram bot channel (`uv run bub gateway`)

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

## Session Semantics

- `run` command default session id: `<channel>:<chat_id>`
- Telegram channel session id: `telegram:<chat_id>`
- `chat` command default session id: `cli_session` (override with `--session-id`)

## Debounce Behavior

- `cli` does not debounce; each input is processed immediately.
- Other channels can debounce and batch inbound messages per session.
- Comma commands (`,` prefix) always bypass debounce and execute immediately.

## External WeCom Plugin

Install `bub-wecom` to add the `wecom_webhook` and `wecom_longconn_bot` channels.

```bash
uv pip install bub-wecom
uv run bub gateway --enable-channel wecom_longconn_bot
```

`bub-wecom` reuses Bub's generic bridge infrastructure. For development, you can still point `BUB_WECOM_LONGCONN_COMMAND` at the bundled core dev bridge, for example:

```bash
BUB_WECOM_LONGCONN_COMMAND="python -m bub.channels.dev_bridge --channel wecom_longconn_bot --boot-message 'hello'"
```

## About Discord

Core Bub does not currently include a builtin Discord adapter.
If you need Discord, implement it in an external plugin via `provide_channels`.

# Key Features

## Framework Core

- Hook-first architecture powered by `pluggy`.
- Deterministic turn pipeline in `BubFramework.process_inbound()`.
- Safe fallback to prompt text when `run_model_stream` returns no value (with `on_error` notification).
- Automatic fallback send/reply action when `render_actions` produces nothing and final text is non-empty.

## Runtime And Commands

- Builtin CLI commands: `run`, `hooks`, `message`, `chat`.
- Builtin `RuntimeEngine`:
  - normal input goes through model + tool loop (Republic)
  - comma-prefixed input enters internal command mode (`,help`, `,tools`, `,fs.read`, etc.)
  - unknown internal commands fall back to shell execution via the `bash` tool
- Runtime events are persisted to tapes (default under `~/.bub/tapes`).
- Model transport can be selected with `BUB_API_MODE=auto|chat|responses|native`.

## Channel Capability

- Builtin channels: `cli` and `telegram`.
- `message` mode runs the same framework pipeline for channel-driven traffic.
- Outbound delivery is routed by `ChannelManager`, keeping business hooks channel-agnostic.
- Telegram supports native draft-style progress updates through the `text_draft` progress surface.

## Plugin Extensibility

- External plugins are loaded via Python entry points (`group="bub"`).
- Later-registered plugins run first and can override builtin behavior.
- Supports both first-result hooks (override style) and broadcast hooks (observer style).

## Current Boundaries

- No strict envelope schema: `Envelope` is intentionally flexible.
- No centralized key contract for shared plugin `state`.
- Core repository does not currently ship a builtin Discord channel adapter.

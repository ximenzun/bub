# Marketplace Testing

This page lists the current manual test matrix for Bub V2 onboarding marketplace entries.
The same information is available from `bub marketplace test-plan`.

## Telegram

1. Install with:
   `uv run bub marketplace install telegram`
2. Confirm state with:
   `uv run bub marketplace status telegram`
3. Smoke test runtime with:
   `uv run bub gateway --enable-channel telegram`

Expected:

- validation becomes `ready`
- Telegram starts without configuration errors
- a Telegram message receives a reply

## Bub Codex

The remaining integrations such as Lark, WeCom, WeChat, Codex, Social Coding, WebSearch, and Stitch must now provide their own manifests from their own plugin packages.
If one of those packages is installed and exposes `bub.manifests`, use its own `bub marketplace test-plan <plugin>` output as the source of truth.

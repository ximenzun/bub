# Bub V2 Onboarding Surfaces

This document defines Bub's breaking-change V2 onboarding/control-plane model.
It replaces environment-variable-first plugin setup with a marketplace-driven control plane that can render onboarding flows across multiple operator surfaces.

## Goals

- Make plugin setup discoverable without reading separate docs.
- Separate stable config, credentials, and session overlays.
- Support multiple onboarding surfaces instead of assuming all channels can self-bootstrap.
- Keep secrets out of model-visible tapes.
- Preserve a plugin test matrix as first-class metadata.

## Core Terms

- `Marketplace manifest`: the machine-readable plugin entry used for install, status, validation, and test plans.
- `Manifest entry point`: an external plugin-provided `bub.manifests` entry point used to register onboarding manifests without loading the runtime plugin.
- `Onboarding surface`: the UI surface used to drive setup, for example `cli`, `chat_card`, or `terminal_qr`.
- `Control plane`: the persistent store for plugin install state, onboarding sessions, and audit events.
- `Secret store`: the file-backed credential store keyed by `SecretRef`.
- `Conversation tape`: model-visible task history; never stores raw secrets.
- `Control tape`: audit/event history for onboarding and validation actions.

## Design Rules

1. Plugins do not define user-facing setup as "fill these env vars".
2. Plugins declare onboarding manifests and typed config models in their own package.
3. Secrets are stored out-of-band and referenced via `SecretRef`.
4. Marketplace install status is workspace-scoped.
5. Channel onboarding must support a bootstrap surface outside the channel itself.
6. Conversation tapes may store redacted summaries and session overlays, but not raw credentials.

## Data Model

The V2 implementation introduced:

- `OnboardingManifest`
- `OnboardingStep`
- `OnboardingField`
- `SecretRequirement`
- `PluginInstallState`
- `OnboardingSessionRecord`
- `ValidationReport`
- `PluginTestCase`

These live under `src/bub/onboarding/`.

## Persistent Stores

Workspace-scoped control-plane state:

- `.bub/control/marketplace.json`
- `.bub/control/events.jsonl`

Home-scoped secrets:

- `${BUB_HOME:-~/.bub}/secrets/<workspace-hash>/...`

The control-plane state is intentionally shareable and inspectable.
The secret store is intentionally separate and never merged into conversation tape state.

## Runtime Flow

1. `BubFramework` collects manifests from `provide_onboarding_manifests`.
2. External plugin packages can also register manifests via `bub.manifests` entry points, independent from runtime loading.
3. `MarketplaceService` merges manifests, config, secret refs, and validation state.
4. CLI/control surfaces call the marketplace service to install, validate, and uninstall entries.
5. Runtime-facing integrations can resolve typed runtime config via `manifest.runtime_factory`.
6. Channel startup prefers marketplace-enabled channels over legacy env-only enable lists.
7. External runtime plugins in `bub` are skipped when a matching manifest exists but the plugin is not installed/enabled in the workspace.

## Surfaces

The current schema supports:

- `cli`
- `chat_card`
- `chat_text`
- `chat_image`
- `web_modal`
- `browser_open`
- `terminal_qr`

This is intentionally broader than "cards".
For example:

- `Lark`: best fit for `chat_card`
- `WeCom`: good fit for `chat_card` plus external secure inputs
- `wechat_clawbot`: bootstrap via `terminal_qr` / `web_modal`, then manage through chat or other channels
- `wechat_qclaw`: connector-first, not chat-card-first

## Security Model

Allowed in conversation tape:

- plugin ids
- config version ids
- validation summaries
- enabled/disabled state
- session-level non-secret overlays

Forbidden in conversation tape:

- access tokens
- API keys
- bot secrets
- refresh tokens
- raw `.env` payloads

If a plugin needs secrets, it receives `SecretRef` in stored state and resolves the secret only at runtime or validation time.

## Channel Fit

### Lark / Feishu

- Strong card surface
- Good post-bootstrap control plane
- Initial tenant-app bootstrap may still require external admin setup

### WeCom Long Connection Bot

- Good mixed card + external secure-input flow
- Pairing/status/policy can be driven from cards
- Secret entry should still prefer secure non-chat surfaces

### WeChat Clawbot

- QR/bootstrap-first
- Not self-bootstrappable from WeChat itself
- Strong candidate for cross-surface onboarding driven from CLI, web, or another connected channel

### WeChat QClaw

- Connector-first, not channel-card-first
- Better represented as a bridge setup flow than a chat-native install card

## Breaking Changes

V2 is intentionally breaking:

- plugin onboarding is no longer expressed as "edit `.env`"
- new plugins should provide onboarding manifests
- runtime channel enablement prefers marketplace state
- channel login/status is backed by marketplace state where available

Legacy env variables remain documented as compatibility references, not as the primary operator path.

## Current In-Repo Coverage

Builtin manifests currently cover:

- `agent`
- `channel_manager`
- `telegram`

External plugin packages are expected to provide their own manifests via `bub.manifests`.
Bub core may still expose registry hints for known external plugins, but it does not own their onboarding contracts.

Only `telegram` currently has in-repo runtime materialization because it is the only matching builtin channel implementation inside this repository.

## CLI

V2 adds:

- `bub marketplace list`
- `bub marketplace show <plugin>`
- `bub marketplace install <plugin>`
- `bub marketplace status [plugin]`
- `bub marketplace validate <plugin>`
- `bub marketplace uninstall <plugin>`
- `bub marketplace test-plan [plugin]`
- `bub workspace status`
- `bub workspace doctor`
- `bub workspace export <bundle.zip>`
- `bub workspace import <bundle.zip>`

`bub channels login/status/logout` now reuse manifest-backed control data when available.

## Migration Bundles

Workspace bundles are zip files that can carry:

- stable `workspace_id`
- control-plane state
- workspace events
- selected tape history
- optional secret payloads

Tape naming and secret namespaces are keyed by `workspace_id`, not by absolute filesystem path, so bundles can be restored on another machine or under a different directory without losing identity continuity.

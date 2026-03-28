from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PluginRegistryEntry:
    plugin_id: str
    title: str
    summary: str
    package_name: str
    install_hint: str
    repo_url: str | None = None


def builtin_registry_entries() -> list[PluginRegistryEntry]:
    return [
        PluginRegistryEntry(
            plugin_id="lark",
            title="Lark / Feishu",
            summary="External Lark channel plugin. The plugin package owns its onboarding manifest.",
            package_name="bub-lark",
            install_hint="Install the external Lark plugin package, then rerun `bub marketplace list`.",
        ),
        PluginRegistryEntry(
            plugin_id="wecom",
            title="WeCom",
            summary="External WeCom plugin. The plugin package owns its onboarding manifest.",
            package_name="bub-wecom",
            install_hint="Install the external WeCom plugin package, then rerun `bub marketplace list`.",
        ),
        PluginRegistryEntry(
            plugin_id="wechat_clawbot",
            title="WeChat Clawbot",
            summary="External WeChat clawbot plugin. The plugin package owns its onboarding manifest.",
            package_name="wechat-clawbot",
            install_hint="Install the external WeChat clawbot package, then rerun `bub marketplace list`.",
        ),
        PluginRegistryEntry(
            plugin_id="wechat_qclaw",
            title="WeChat QClaw Connector",
            summary="External local connector package. The connector package owns its onboarding manifest.",
            package_name="wechat-qclaw",
            install_hint="Install the connector package, then rerun `bub marketplace list`.",
        ),
        PluginRegistryEntry(
            plugin_id="codex",
            title="Bub Codex",
            summary="External coding plugin. The plugin package owns its onboarding manifest.",
            package_name="bub-codex",
            install_hint="Install the Bub Codex package, then rerun `bub marketplace list`.",
        ),
        PluginRegistryEntry(
            plugin_id="social_coding",
            title="Social Coding",
            summary="External social coding plugin. The plugin package owns its onboarding manifest.",
            package_name="bub-social-coding",
            install_hint="Install the social coding package, then rerun `bub marketplace list`.",
        ),
        PluginRegistryEntry(
            plugin_id="websearch",
            title="WebSearch",
            summary="External websearch plugin. The plugin package owns its onboarding manifest.",
            package_name="bub-websearch",
            install_hint="Install the websearch package, then rerun `bub marketplace list`.",
        ),
        PluginRegistryEntry(
            plugin_id="stitch",
            title="Stitch",
            summary="External Stitch plugin. The plugin package owns its onboarding manifest.",
            package_name="bub-stitch",
            install_hint="Install the Stitch package, then rerun `bub marketplace list`.",
        ),
    ]

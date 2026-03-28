from __future__ import annotations

import json as jsonlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import typer
from pydantic import ValidationError

from bub.onboarding.models import (
    InstallContext,
    OnboardingCondition,
    OnboardingField,
    OnboardingManifest,
    OnboardingSessionRecord,
    OnboardingStep,
    PluginInstallState,
    ValidationIssue,
    ValidationReport,
)
from bub.onboarding.renderer import OnboardingCancelledError, OnboardingRenderer, ReviewSelection, renderer_for_surface
from bub.onboarding.store import MarketplaceStore, SecretStore

type StepResult = PluginInstallState | ReviewSelection | None


class MarketplaceService:
    def __init__(
        self,
        *,
        workspace: Path,
        home: Path,
        manifests: Iterable[OnboardingManifest],
    ) -> None:
        self.workspace = workspace.resolve()
        self.home = home.expanduser().resolve()
        self.store = MarketplaceStore(self.workspace, self.home)
        self.secrets = SecretStore(self.home, self.workspace)
        self._manifests = {manifest.plugin_id: manifest for manifest in manifests}

    def manifests(self) -> list[OnboardingManifest]:
        return sorted(self._manifests.values(), key=lambda item: item.plugin_id.casefold())

    def manifest(self, plugin_id: str) -> OnboardingManifest:
        try:
            return self._manifests[plugin_id]
        except KeyError as exc:
            raise KeyError(f"Unknown marketplace plugin: {plugin_id}") from exc

    def states(self) -> dict[str, PluginInstallState]:
        return self.store.load_states()

    def sessions(self) -> dict[str, OnboardingSessionRecord]:
        return self.store.load_sessions()

    def state(self, plugin_id: str) -> PluginInstallState | None:
        return self.states().get(plugin_id)

    def create_session(self, plugin_id: str, *, surface: str = "cli") -> OnboardingSessionRecord:
        manifest = self.manifest(plugin_id)
        session = manifest.create_session(surface=surface)  # type: ignore[arg-type]
        self.store.save_session(session)
        self.store.append_event(
            "plugin.session.started",
            {"plugin_id": plugin_id, "session_id": session.session_id, "surface": surface},
        )
        return session

    def status_lines(self, plugin_id: str) -> list[str]:
        manifest = self.manifest(plugin_id)
        state = self.state(plugin_id)
        report = self.validate(plugin_id)
        lines = [
            f"plugin: {manifest.plugin_id}",
            f"title: {manifest.title}",
            f"category: {manifest.category}",
            f"runtime_available: {manifest.runtime_is_available()}",
            f"installed: {state is not None}",
            f"enabled: {state.enabled if state else False}",
            f"validation: {'ok' if report.ok else 'error'}",
            f"summary: {report.summary}",
        ]
        if manifest.channel_name:
            lines.append(f"channel: {manifest.channel_name}")
        if state is not None:
            lines.append(f"installed_via: {state.installed_via}")
            lines.append(f"updated_at: {state.updated_at.isoformat()}")
            if state.secret_refs:
                lines.append(f"secret_refs: {len(state.secret_refs)}")
            if state.config:
                lines.append(f"config_keys: {', '.join(sorted(state.config))}")
            lines.extend(self._status_sections(manifest=manifest, state=state))
        else:
            lines.append("next_action: marketplace install")
        if report.issues:
            lines.append("issues:")
            for issue in report.issues:
                lines.append(f"- {issue.level}: {issue.message}")
        if manifest.legacy_env_vars:
            lines.append(f"legacy_env: {', '.join(manifest.legacy_env_vars)}")
        return lines

    def validate(self, plugin_id: str) -> ValidationReport:
        manifest = self.manifest(plugin_id)
        state = self.state(plugin_id)
        if state is None:
            return ValidationReport(
                ok=False,
                summary="not installed",
                issues=[ValidationIssue(level="error", message="Plugin has not been installed via Bub V2.")],
            )

        issues = self._validate_model(manifest, state)
        issues.extend(self._validate_secrets(manifest, state))
        if not manifest.runtime_is_available():
            issues.append(ValidationIssue(level="warning", message="runtime package/entry point not available"))
        issues.extend(self._validate_custom(manifest, state))
        errors = [issue for issue in issues if issue.level == "error"]
        summary = "ready" if not errors else f"{len(errors)} validation error(s)"
        report = ValidationReport(ok=not errors, summary=summary, issues=issues)
        self._save_validation(plugin_id, state, report)
        return report

    def install(
        self,
        plugin_id: str,
        *,
        config_updates: dict[str, Any] | None = None,
        secret_values: dict[str, str] | None = None,
        enable: bool = True,
        surface: str = "cli",
        reset: bool = False,
    ) -> PluginInstallState:
        manifest = self.manifest(plugin_id)
        current = None if reset else self.state(plugin_id)
        config = dict(current.config if current else {})
        secret_refs = dict(current.secret_refs if current else {})
        if config_updates:
            config.update(config_updates)
        if secret_values:
            for key, value in secret_values.items():
                secret_refs[key] = self.secrets.put(plugin_id=plugin_id, key=key, value=value)
        now = datetime.now(UTC)
        state = PluginInstallState(
            plugin_id=plugin_id,
            title=manifest.title,
            enabled=enable,
            config=config,
            secret_refs=secret_refs,
            installed_at=current.installed_at if current else now,
            updated_at=now,
            installed_via=surface,  # type: ignore[arg-type]
            channel_name=manifest.channel_name,
            notes=list(current.notes if current else []),
        )
        self.store.save_state(state)
        self.store.append_event(
            "plugin.install",
            {
                "plugin_id": plugin_id,
                "surface": surface,
                "config_keys": sorted(config),
                "secret_keys": sorted(secret_refs),
                "enabled": enable,
            },
        )
        self.validate(plugin_id)
        return self.state(plugin_id) or state

    def uninstall(self, plugin_id: str) -> None:
        current = self.state(plugin_id)
        if current is not None:
            for ref in current.secret_refs.values():
                self.secrets.delete(ref)
        self.store.delete_state(plugin_id)
        self.store.append_event("plugin.uninstall", {"plugin_id": plugin_id})

    def resolve_secret(self, plugin_id: str, key: str) -> str | None:
        state = self.state(plugin_id)
        if state is None:
            return None
        ref = state.secret_refs.get(key)
        if ref is None:
            return None
        return self.secrets.get(ref)

    def load_runtime(self, plugin_id: str) -> Any:
        manifest = self.manifest(plugin_id)
        state = self.state(plugin_id)
        if manifest.runtime_factory is None or state is None:
            return None
        return manifest.runtime_factory(
            InstallContext(workspace=self.workspace, service=self, manifest=manifest, state=state)
        )

    def legacy_env(self, plugin_id: str) -> dict[str, str]:
        manifest = self.manifest(plugin_id)
        state = self.state(plugin_id)
        if manifest.legacy_env_factory is None or state is None:
            return {}
        payload = manifest.legacy_env_factory(
            InstallContext(workspace=self.workspace, service=self, manifest=manifest, state=state)
        )
        return {key: value for key, value in payload.items() if value}

    def materialize_runtime_env(self) -> dict[str, str]:
        materialized: dict[str, str] = {}
        for manifest in self.manifests():
            state = self.state(manifest.plugin_id)
            if state is None or not state.enabled:
                continue
            materialized.update(self.legacy_env(manifest.plugin_id))
        return materialized

    def enabled_channels(self) -> list[str]:
        result: list[str] = []
        for manifest in self.manifests():
            state = self.state(manifest.plugin_id)
            if manifest.channel_name and state is not None and state.enabled:
                result.append(manifest.channel_name)
        return sorted(result)

    def render_manifest(self, plugin_id: str) -> str:
        manifest = self.manifest(plugin_id)
        lines = [
            f"{manifest.title}",
            f"id: {manifest.plugin_id}",
            f"category: {manifest.category}",
            f"runtime_available: {manifest.runtime_is_available()}",
            f"surfaces: {', '.join(manifest.surfaces)}",
            f"summary: {manifest.summary}",
        ]
        if manifest.description:
            lines.append(f"description: {manifest.description}")
        if manifest.secret_requirements:
            lines.append("secrets:")
            lines.extend(
                f"- {item.key}: {item.title}" + (f" ({item.description})" if item.description else "")
                for item in manifest.secret_requirements
            )
        if manifest.steps:
            lines.append("steps:")
            for step in manifest.steps:
                lines.extend(self._render_manifest_step(step))
        schema = manifest.config_schema()
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if isinstance(properties, dict) and properties:
            lines.append("config:")
            for key, value in properties.items():
                if not isinstance(value, dict):
                    continue
                lines.append(f"- {key}: {value.get('title') or value.get('type') or 'value'}")
        return "\n".join(lines)

    def _render_manifest_step(self, step: OnboardingStep) -> list[str]:
        lines = [f"- {step.id}: {step.kind} :: {step.title}"]
        lines.extend(self._render_manifest_step_header(step))
        lines.extend(self._render_manifest_step_options(step))
        lines.extend(self._render_manifest_step_fields(step))
        return lines

    def _status_sections(self, *, manifest: OnboardingManifest, state: PluginInstallState) -> list[str]:
        session = manifest.create_session(surface="cli")
        session.answers.update(state.config)
        visible_steps = [
            step
            for step in manifest.steps
            if step.kind not in {"validate", "complete"}
            and self._step_is_visible(step, config=state.config, session=session)
        ]
        summary_sections = self._summary_sections(
            visible_steps=visible_steps,
            session=session,
            current=state,
            config=state.config,
            secrets_payload={},
        )
        if not summary_sections:
            return []
        return ["current_state:", *summary_sections]

    def _render_manifest_step_header(self, step: OnboardingStep) -> list[str]:
        lines: list[str] = []
        if step.description:
            lines.append(f"  description: {step.description}")
        if step.scenario_hint:
            lines.append(f"  scenario_hint: {step.scenario_hint}")
        if step.result_key:
            lines.append(f"  result_key: {step.result_key}")
        if step.source_key:
            lines.append(f"  source_key: {step.source_key}")
        if step.summary_label:
            lines.append(f"  summary_label: {step.summary_label}")
        if step.summary_template:
            lines.append(f"  summary_template: {step.summary_template}")
        if step.skippable:
            lines.append("  skippable: true")
        if step.when:
            lines.append(f"  when: {self._render_conditions(step.when)}")
        return lines

    @staticmethod
    def _render_manifest_step_options(step: OnboardingStep) -> list[str]:
        if not step.options:
            return []
        lines = ["  options:"]
        for option in step.options:
            line = f"  - {option.value}: {option.label}"
            if option.description:
                line += f" ({option.description})"
            if option.recommended:
                line += " [recommended]"
            lines.append(line)
            if option.recommendation_reason:
                lines.append(f"    recommendation_reason: {option.recommendation_reason}")
        return lines

    def _render_manifest_step_fields(self, step: OnboardingStep) -> list[str]:
        if not step.fields:
            return []
        lines = ["  fields:"]
        for field in step.fields:
            details: list[str] = [field.kind]
            details.append("required" if field.required else "optional")
            if field.default not in (None, "", [], (), {}):
                details.append(f"default={_render_summary_value(field.default)}")
            if field.recommended_value not in (None, "", [], (), {}):
                details.append(f"recommended={_render_summary_value(field.recommended_value)}")
            line = f"  - {field.key}: {field.title} [{' | '.join(details)}]"
            lines.append(line)
            if field.recommendation_reason:
                lines.append(f"    recommendation_reason: {field.recommendation_reason}")
            if field.scenario_hint:
                lines.append(f"    scenario_hint: {field.scenario_hint}")
            if field.summary_label:
                lines.append(f"    summary_label: {field.summary_label}")
            if field.summary_template:
                lines.append(f"    summary_template: {field.summary_template}")
            if field.when:
                lines.append(f"    when: {self._render_conditions(field.when)}")
        return lines

    def render_test_plan(self, plugin_id: str) -> str:
        manifest = self.manifest(plugin_id)
        if not manifest.test_plan:
            return "(no test plan)"
        lines = [f"{manifest.title} test plan"]
        for case in manifest.test_plan:
            lines.append(f"- [{case.mode}] {case.title}")
            if case.description:
                lines.append(f"  {case.description}")
            for command in case.commands:
                lines.append(f"  command: {command}")
            for assertion in case.assertions:
                lines.append(f"  assert: {assertion}")
        return "\n".join(lines)

    def install_interactive(
        self,
        plugin_id: str,
        *,
        surface: str = "cli",
        renderer: OnboardingRenderer | None = None,
        reset: bool = False,
    ) -> PluginInstallState:
        manifest = self.manifest(plugin_id)
        session = self.create_session(plugin_id, surface=surface)
        current = None if reset else self.state(plugin_id)
        config = dict(current.config if current else {})
        secrets_payload: dict[str, str] = {}
        resolved_renderer = renderer or renderer_for_surface(surface)
        run_install = getattr(resolved_renderer, "run_install", None)
        if callable(run_install):
            result = run_install(
                service=self,
                plugin_id=plugin_id,
                manifest=manifest,
                session=session,
                current=current,
                config=config,
                secrets_payload=secrets_payload,
                surface=surface,
            )
            return cast(PluginInstallState, result)
        return self._run_interactive_install_loop(
            manifest=manifest,
            session=session,
            current=current,
            config=config,
            secrets_payload=secrets_payload,
            renderer=resolved_renderer,
            plugin_id=plugin_id,
            surface=surface,
            reset=reset,
        )

    def _run_interactive_install_loop(
        self,
        *,
        manifest: OnboardingManifest,
        session: OnboardingSessionRecord,
        current: PluginInstallState | None,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
        renderer: OnboardingRenderer,
        plugin_id: str,
        surface: str,
        reset: bool,
    ) -> PluginInstallState:
        resume_from_step_id: str | None = None
        while True:
            result = self._run_interactive_pass(
                manifest=manifest,
                session=session,
                current=current,
                config=config,
                secrets_payload=secrets_payload,
                renderer=renderer,
                plugin_id=plugin_id,
                surface=surface,
                resume_from_step_id=resume_from_step_id,
                reset=reset,
            )
            if isinstance(result, PluginInstallState):
                return result
            resume_from_step_id = result

    def _run_interactive_pass(
        self,
        *,
        manifest: OnboardingManifest,
        session: OnboardingSessionRecord,
        current: PluginInstallState | None,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
        renderer: OnboardingRenderer,
        plugin_id: str,
        surface: str,
        resume_from_step_id: str | None,
        reset: bool,
    ) -> PluginInstallState | str | None:
        validate_seen = False
        active_resume = resume_from_step_id
        for step in manifest.steps:
            if active_resume is not None and step.id != active_resume:
                continue
            active_resume = None
            if not self._step_is_visible(step, config=config, session=session):
                continue
            if step.kind == "validate":
                validate_seen = True
            preview = self._run_interactive_step(
                manifest=manifest,
                session=session,
                step=step,
                plugin_id=plugin_id,
                surface=surface,
                config=config,
                secrets_payload=secrets_payload,
                renderer=renderer,
                current=current,
                reset=reset,
            )
            if isinstance(preview, ReviewSelection) and preview.action == "edit":
                self._rewind_session(session, manifest, preview.step_id)
                return preview.step_id
            if isinstance(preview, PluginInstallState):
                return preview
        if validate_seen:
            raise RuntimeError("validate step did not produce an install result")
        decision = self._review_install_summary(
            manifest=manifest,
            renderer=renderer,
            session=session,
            current=current,
            config=config,
            secrets_payload=secrets_payload,
        )
        if decision.action == "edit":
            self._rewind_session(session, manifest, decision.step_id)
            return decision.step_id
        installed = self.install(
            plugin_id,
            config_updates=config,
            secret_values=secrets_payload,
            enable=True,
            surface=surface,
            reset=reset,
        )
        session.done = True
        session.updated_at = datetime.now(UTC)
        self.store.save_session(session)
        return installed

    def _validate_model(self, manifest: OnboardingManifest, state: PluginInstallState) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if manifest.config_model is None:
            return issues
        try:
            manifest.config_model.model_validate(dict(state.config))
        except ValidationError as exc:
            for error in exc.errors():
                issues.append(
                    ValidationIssue(level="error", message=_humanize_validation_error(error, manifest=manifest))
                )
        return issues

    def _validate_secrets(self, manifest: OnboardingManifest, state: PluginInstallState) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for requirement in manifest.secret_requirements:
            ref = state.secret_refs.get(requirement.key)
            if requirement.required and ref is None:
                issues.append(ValidationIssue(level="error", message=f"{requirement.title} is required."))
                continue
            if ref is None:
                continue
            try:
                self.secrets.get(ref)
            except Exception as exc:  # pragma: no cover - defensive I/O path
                issues.append(
                    ValidationIssue(level="error", message=f"Could not read secret {requirement.title}: {exc}")
                )
        return issues

    def _validate_custom(self, manifest: OnboardingManifest, state: PluginInstallState) -> list[ValidationIssue]:
        if manifest.validator is None:
            return []
        report = manifest.validator(
            InstallContext(workspace=self.workspace, service=self, manifest=manifest, state=state)
        )
        return list(report.issues)

    def _save_validation(self, plugin_id: str, state: PluginInstallState, report: ValidationReport) -> None:
        updated = state.model_copy(
            update={
                "last_validation_ok": report.ok,
                "last_validation_summary": report.summary,
                "last_validation_at": datetime.now(UTC),
            }
        )
        self.store.save_state(updated)
        self.store.append_event(
            "plugin.validation",
            {
                "plugin_id": plugin_id,
                "ok": report.ok,
                "summary": report.summary,
                "issues": [issue.model_dump(mode="json") for issue in report.issues],
            },
        )

    def _run_interactive_step(
        self,
        *,
        manifest: OnboardingManifest,
        session: OnboardingSessionRecord,
        step,
        plugin_id: str,
        surface: str,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
        renderer: OnboardingRenderer,
        current: PluginInstallState | None,
        reset: bool,
    ) -> StepResult:
        session.current_step_id = step.id
        session.updated_at = datetime.now(UTC)
        self.store.save_session(session)
        if step.skippable and not self._should_run_skippable_step(
            step, config=config, session=session, renderer=renderer
        ):
            self._mark_step_completed(session, step.id)
            return None
        if step.kind == "validate":
            decision = self._review_install_summary(
                manifest=manifest,
                renderer=renderer,
                session=session,
                current=current,
                config=config,
                secrets_payload=secrets_payload,
            )
            if decision.action == "edit":
                return decision
            preview = self.install(
                plugin_id,
                config_updates=config,
                secret_values=secrets_payload,
                enable=True,
                surface=surface,
                reset=reset,
            )
            self._mark_step_completed(session, step.id)
            session.answers.update(config)
            session.done = True
            session.updated_at = datetime.now(UTC)
            self.store.save_session(session)
            return preview
        self._dispatch_interactive_step(
            manifest=manifest,
            session=session,
            step=step,
            config=config,
            secrets_payload=secrets_payload,
            renderer=renderer,
        )
        self._mark_step_completed(session, step.id)
        return None

    def _review_install_summary(
        self,
        *,
        manifest: OnboardingManifest,
        renderer: OnboardingRenderer,
        session: OnboardingSessionRecord,
        current: PluginInstallState | None,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
    ) -> ReviewSelection:
        editable_steps = [
            step
            for step in manifest.steps
            if step.kind not in {"validate", "complete"} and self._step_is_visible(step, config=config, session=session)
        ]
        summary = self._summary_text(
            manifest=manifest,
            session=session,
            visible_steps=editable_steps,
            current=current,
            config=config,
            secrets_payload=secrets_payload,
        )
        decision = renderer.review_summary(
            title=f"{manifest.title} Summary",
            text=summary,
            editable_steps=editable_steps,
        )
        if decision.action == "cancel":
            raise OnboardingCancelledError("onboarding cancelled")
        return decision

    def _summary_text(
        self,
        *,
        manifest: OnboardingManifest,
        session: OnboardingSessionRecord,
        visible_steps: list[OnboardingStep],
        current: PluginInstallState | None,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
    ) -> str:
        lines = [
            f"plugin: {manifest.plugin_id}",
            f"title: {manifest.title}",
            f"category: {manifest.category}",
        ]
        section_blocks = self._summary_sections(
            visible_steps=visible_steps,
            session=session,
            current=current,
            config=config,
            secrets_payload=secrets_payload,
        )
        if section_blocks:
            lines.append("")
            lines.extend(section_blocks)
        else:
            lines.append("")
            lines.append("(no configurable values captured)")
        return "\n".join(lines)

    def _summary_sections(
        self,
        *,
        visible_steps: list[OnboardingStep],
        session: OnboardingSessionRecord,
        current: PluginInstallState | None,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
    ) -> list[str]:
        existing_secret_keys = set(current.secret_refs) if current is not None else set()
        blocks: list[str] = []
        covered_secret_keys: set[str] = set()
        for step in visible_steps:
            entries = self._summary_entries_for_step(
                step=step,
                session=session,
                config=config,
                secrets_payload=secrets_payload,
                existing_secret_keys=existing_secret_keys,
                covered_secret_keys=covered_secret_keys,
            )
            if entries:
                blocks.append(f"{step.title}:")
                blocks.extend(entries)
                blocks.append("")
        extra_secret_lines: list[str] = []
        secret_keys = set(existing_secret_keys) | set(secrets_payload)
        for secret_key in sorted(secret_keys):
            if secret_key in covered_secret_keys:
                continue
            extra_secret_lines.append(
                f"- {secret_key}: {_secret_status(secret_key, secrets_payload=secrets_payload, existing_secret_keys=existing_secret_keys)}"
            )
        if extra_secret_lines:
            blocks.append("Additional Secrets:")
            blocks.extend(extra_secret_lines)
            blocks.append("")
        if blocks and blocks[-1] == "":
            blocks.pop()
        return blocks

    def _summary_entries_for_step(
        self,
        *,
        step: OnboardingStep,
        session: OnboardingSessionRecord,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
        existing_secret_keys: set[str],
        covered_secret_keys: set[str],
    ) -> list[str]:
        if step.kind in {"choice", "multi_choice", "reorder"}:
            return self._summary_option_entries(step=step, session=session, config=config)
        if step.kind == "form":
            return self._summary_form_entries(step=step, session=session, config=config)
        if step.kind == "secret_input":
            return self._summary_secret_entries(
                step=step,
                session=session,
                config=config,
                secrets_payload=secrets_payload,
                existing_secret_keys=existing_secret_keys,
                covered_secret_keys=covered_secret_keys,
            )
        return []

    def _summary_option_entries(
        self,
        *,
        step: OnboardingStep,
        session: OnboardingSessionRecord,
        config: dict[str, Any],
    ) -> list[str]:
        target_key = step.result_key or step.id
        value = config.get(target_key, session.answers.get(target_key))
        rendered = _render_option_summary(step, value)
        if rendered is None:
            return []
        label = step.summary_label or "Selection"
        return [_render_summary_line(label=label, value=rendered, template=step.summary_template)]

    def _summary_form_entries(
        self,
        *,
        step: OnboardingStep,
        session: OnboardingSessionRecord,
        config: dict[str, Any],
    ) -> list[str]:
        entries: list[str] = []
        for field in step.fields:
            if not self._field_is_visible(field, config=config, session=session):
                continue
            if field.key not in config:
                continue
            entries.append(
                _render_summary_line(
                    label=field.summary_label or field.title,
                    value=_render_summary_value(config[field.key]),
                    template=field.summary_template,
                )
            )
        return entries

    def _summary_secret_entries(
        self,
        *,
        step: OnboardingStep,
        session: OnboardingSessionRecord,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
        existing_secret_keys: set[str],
        covered_secret_keys: set[str],
    ) -> list[str]:
        entries: list[str] = []
        for field in step.fields:
            if not self._field_is_visible(field, config=config, session=session):
                continue
            covered_secret_keys.add(field.key)
            entries.append(
                _render_summary_line(
                    label=field.summary_label or field.title,
                    value=_secret_status(
                        field.key, secrets_payload=secrets_payload, existing_secret_keys=existing_secret_keys
                    ),
                    template=field.summary_template,
                )
            )
        return entries

    def _dispatch_interactive_step(
        self,
        *,
        manifest: OnboardingManifest,
        session: OnboardingSessionRecord,
        step: OnboardingStep,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
        renderer: OnboardingRenderer,
    ) -> None:
        if step.kind == "info":
            renderer.render_info(manifest=manifest, step=step)
            return
        if step.kind == "external_link":
            renderer.render_external_link(manifest=manifest, step=step)
            return
        if step.kind == "choice":
            self._collect_choice_step(
                manifest,
                step,
                config=config,
                session=session,
                secrets_payload=secrets_payload,
                renderer=renderer,
            )
            return
        if step.kind == "multi_choice":
            self._collect_multi_choice_step(
                manifest,
                step,
                config=config,
                session=session,
                secrets_payload=secrets_payload,
                renderer=renderer,
            )
            return
        if step.kind == "reorder":
            self._collect_reorder_step(
                manifest,
                step,
                config=config,
                session=session,
                secrets_payload=secrets_payload,
                renderer=renderer,
            )
            return
        if step.kind == "form":
            self._collect_step_fields(
                manifest,
                step,
                step.fields,
                config=config,
                session=session,
                secrets_payload=secrets_payload,
                renderer=renderer,
            )
            return
        if step.kind == "secret_input":
            self._collect_secret_fields(
                manifest,
                step,
                step.fields,
                secrets_payload,
                config=config,
                session=session,
                renderer=renderer,
            )
            return
        if step.kind == "qr_challenge":
            renderer.render_qr_challenge(manifest=manifest, step=step)

    def _should_run_skippable_step(
        self,
        step: OnboardingStep,
        *,
        config: dict[str, Any],
        session: OnboardingSessionRecord,
        renderer: OnboardingRenderer,
    ) -> bool:
        if self._step_has_existing_values(step, config=config, session=session):
            return True
        text = step.description or "This step is optional. Do you want to configure it now?"
        return renderer.confirm(title=step.title, text=text, default=False)

    @staticmethod
    def _mark_step_completed(session: OnboardingSessionRecord, step_id: str) -> None:
        if step_id not in session.completed_step_ids:
            session.completed_step_ids.append(step_id)

    @staticmethod
    def _step_has_existing_values(
        step: OnboardingStep, *, config: dict[str, Any], session: OnboardingSessionRecord
    ) -> bool:
        target_keys = [field.key for field in step.fields]
        if step.result_key:
            target_keys.append(step.result_key)
        for key in target_keys:
            value = config.get(key, session.answers.get(key))
            if value not in (None, "", [], (), {}):
                return True
        return False

    def _rewind_session(
        self, session: OnboardingSessionRecord, manifest: OnboardingManifest, step_id: str | None
    ) -> None:
        if not step_id:
            return
        seen = False
        retained: list[str] = []
        for step in manifest.steps:
            if step.id == step_id:
                seen = True
            if not seen and step.id in session.completed_step_ids:
                retained.append(step.id)
        session.completed_step_ids = retained
        session.current_step_id = step_id
        session.done = False
        session.updated_at = datetime.now(UTC)
        self.store.save_session(session)

    def _collect_step_fields(
        self,
        manifest: OnboardingManifest,
        step: OnboardingStep,
        fields: tuple[OnboardingField, ...],
        *,
        config: dict[str, Any],
        session: OnboardingSessionRecord,
        secrets_payload: dict[str, str],
        renderer: OnboardingRenderer,
    ) -> None:
        while True:
            draft = dict(config)
            for field in fields:
                if not self._field_is_visible(field, config=draft, session=session):
                    continue
                while True:
                    try:
                        value = renderer.prompt_field(field=field, default=draft.get(field.key))
                    except (ValueError, typer.BadParameter) as exc:
                        renderer.render_error(title=field.title, text=str(exc))
                        continue
                    draft[field.key] = value
                    break
            errors = self._interactive_step_errors(
                manifest=manifest,
                step=step,
                config=draft,
                secrets_payload=secrets_payload,
                session=session,
            )
            if not errors:
                config.update(draft)
                for field in fields:
                    if field.key in draft:
                        session.answers[field.key] = draft[field.key]
                return
            renderer.render_error(title=f"{step.title} Error", text="\n".join(errors))

    def _collect_secret_fields(
        self,
        manifest: OnboardingManifest,
        step: OnboardingStep,
        fields: tuple[OnboardingField, ...],
        secrets_payload: dict[str, str],
        *,
        config: dict[str, Any],
        session: OnboardingSessionRecord,
        renderer: OnboardingRenderer,
    ) -> None:
        while True:
            draft = dict(secrets_payload)
            for field in fields:
                if not self._field_is_visible(field, config=config, session=session):
                    continue
                value = renderer.prompt_secret(field=field)
                if value or field.required:
                    draft[field.key] = value
            errors = self._interactive_step_errors(
                manifest=manifest,
                step=step,
                config=config,
                secrets_payload=draft,
                session=session,
            )
            if not errors:
                secrets_payload.clear()
                secrets_payload.update(draft)
                for field in fields:
                    if field.key in draft:
                        session.answers[field.key] = draft[field.key]
                return
            renderer.render_error(title=f"{step.title} Error", text="\n".join(errors))

    def _collect_choice_step(
        self,
        manifest: OnboardingManifest,
        step: OnboardingStep,
        *,
        config: dict[str, Any],
        session: OnboardingSessionRecord,
        secrets_payload: dict[str, str],
        renderer: OnboardingRenderer,
    ) -> None:
        if not step.options:
            return
        target_key = step.result_key or (step.fields[0].key if step.fields else step.id)
        options = list(step.options)
        while True:
            current = config.get(target_key, session.answers.get(target_key))
            raw = renderer.choose_one(step=step, current=current, options=options).strip()
            selected = self._resolve_choice_value(step, raw)
            draft = dict(config)
            draft[target_key] = selected
            errors = self._interactive_step_errors(
                manifest=manifest,
                step=step,
                config=draft,
                secrets_payload=secrets_payload,
                session=session,
            )
            if not errors:
                config[target_key] = selected
                session.answers[target_key] = selected
                return
            renderer.render_error(title=f"{step.title} Error", text="\n".join(errors))

    def _collect_multi_choice_step(
        self,
        manifest: OnboardingManifest,
        step: OnboardingStep,
        *,
        config: dict[str, Any],
        session: OnboardingSessionRecord,
        secrets_payload: dict[str, str],
        renderer: OnboardingRenderer,
    ) -> None:
        if not step.options:
            return
        target_key = step.result_key or (step.fields[0].key if step.fields else step.id)
        options = list(step.options)
        while True:
            current = config.get(target_key, session.answers.get(target_key))
            raw_values = renderer.choose_many(step=step, current=current, options=options)
            selected = self._resolve_multi_choice_values(step, raw_values)
            draft = dict(config)
            draft[target_key] = selected
            errors = self._interactive_step_errors(
                manifest=manifest,
                step=step,
                config=draft,
                secrets_payload=secrets_payload,
                session=session,
            )
            if not errors:
                config[target_key] = selected
                session.answers[target_key] = selected
                return
            renderer.render_error(title=f"{step.title} Error", text="\n".join(errors))

    def _collect_reorder_step(
        self,
        manifest: OnboardingManifest,
        step: OnboardingStep,
        *,
        config: dict[str, Any],
        session: OnboardingSessionRecord,
        secrets_payload: dict[str, str],
        renderer: OnboardingRenderer,
    ) -> None:
        options = self._step_options(step, config=config, session=session)
        target_key = step.result_key or step.id
        if not options:
            config[target_key] = []
            session.answers[target_key] = []
            return
        while True:
            current = config.get(target_key, session.answers.get(target_key))
            option_values = {option.value for option in options}
            current_values = (
                [str(item) for item in current if str(item) in option_values]
                if isinstance(current, list)
                else [option.value for option in options]
            )
            if len(options) == 1:
                selected = [options[0].value]
            else:
                raw_values = renderer.reorder(step=step, current=current_values, options=options)
                selected = self._resolve_reorder_values(options, raw_values)
            draft = dict(config)
            draft[target_key] = selected
            errors = self._interactive_step_errors(
                manifest=manifest,
                step=step,
                config=draft,
                secrets_payload=secrets_payload,
                session=session,
            )
            if not errors:
                config[target_key] = selected
                session.answers[target_key] = selected
                return
            renderer.render_error(title=f"{step.title} Error", text="\n".join(errors))

    @staticmethod
    def _resolve_choice_value(step: OnboardingStep, raw: str) -> str:
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(step.options):
                return step.options[index - 1].value
        for option in step.options:
            if raw == option.value or raw == option.label:
                return option.value
        raise typer.BadParameter(f"Unknown choice: {raw}")

    @staticmethod
    def _resolve_multi_choice_values(step: OnboardingStep, raw_values: list[str]) -> list[str]:
        if not raw_values:
            return []
        selected: list[str] = []
        for raw in raw_values:
            value = MarketplaceService._resolve_choice_value(step, raw)
            if value not in selected:
                selected.append(value)
        return selected

    @staticmethod
    def _resolve_reorder_values(options: list, raw_values: list[str]) -> list[str]:
        if not raw_values:
            return [option.value for option in options]
        step = OnboardingStep(id="reorder", kind="reorder", title="reorder", options=tuple(options))
        selected: list[str] = []
        for raw in raw_values:
            value = MarketplaceService._resolve_choice_value(step, raw)
            if value not in selected:
                selected.append(value)
        for option in options:
            if option.value not in selected:
                selected.append(option.value)
        return selected

    def _step_options(
        self,
        step: OnboardingStep,
        *,
        config: dict[str, Any],
        session: OnboardingSessionRecord,
    ) -> list:
        options = list(step.options)
        if step.source_key is None:
            return options
        source_values = config.get(step.source_key, session.answers.get(step.source_key))
        if not isinstance(source_values, list):
            return options
        selected = [str(item) for item in source_values if str(item).strip()]
        if not selected:
            return []
        option_by_value = {option.value: option for option in options}
        return [option_by_value[value] for value in selected if value in option_by_value]

    def _step_is_visible(
        self, step: OnboardingStep, *, config: dict[str, Any], session: OnboardingSessionRecord
    ) -> bool:
        return self._conditions_match(step.when, config=config, session=session)

    def _field_is_visible(
        self, field: OnboardingField, *, config: dict[str, Any], session: OnboardingSessionRecord
    ) -> bool:
        return self._conditions_match(field.when, config=config, session=session)

    def _interactive_step_errors(
        self,
        *,
        manifest: OnboardingManifest,
        step: OnboardingStep,
        config: dict[str, Any],
        secrets_payload: dict[str, str],
        session: OnboardingSessionRecord,
    ) -> list[str]:
        if manifest.config_model is None:
            return []
        payload = manifest.config_defaults()
        payload.update(config)
        payload.update({key: value for key, value in secrets_payload.items() if value})
        try:
            manifest.config_model.model_validate(payload)
        except ValidationError as exc:
            relevant_keys = {field.key for field in step.fields}
            if step.result_key:
                relevant_keys.add(step.result_key)
            else:
                relevant_keys.add(step.id)
            messages: list[str] = []
            for error in exc.errors():
                loc = tuple(str(item) for item in error.get("loc", ()))
                if (loc and loc[0] in relevant_keys) or not loc:
                    messages.append(_humanize_validation_error(error, manifest=manifest))
            return messages
        return []

    @staticmethod
    def _render_conditions(conditions: tuple[OnboardingCondition, ...]) -> str:
        rendered: list[str] = []
        for condition in conditions:
            if condition.equals is not None:
                rendered.append(f"{condition.key}={condition.equals}")
            elif condition.one_of:
                rendered.append(f"{condition.key} in {list(condition.one_of)}")
            elif condition.contains is not None:
                rendered.append(f"{condition.key} contains {condition.contains}")
            elif condition.present is not None:
                rendered.append(f"{condition.key} present={condition.present}")
            else:
                rendered.append(condition.key)
        return ", ".join(rendered)

    def _conditions_match(
        self,
        conditions: tuple[OnboardingCondition, ...],
        *,
        config: dict[str, Any],
        session: OnboardingSessionRecord,
    ) -> bool:
        if not conditions:
            return True
        values = dict(session.answers)
        values.update(config)
        return all(condition.matches(values) for condition in conditions)


def _render_summary_value(value: Any) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return ", ".join(item for item in value if item) or "(empty)"
        return str(value)
    if isinstance(value, str):
        return value or "(empty)"
    try:
        rendered = jsonlib.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)
    return str(rendered)


def _field_index(manifest: OnboardingManifest) -> dict[str, OnboardingField]:
    result: dict[str, OnboardingField] = {}
    for step in manifest.steps:
        for field in step.fields:
            result.setdefault(field.key, field)
    return result


def _humanize_validation_error(error: Mapping[str, Any], *, manifest: OnboardingManifest) -> str:
    loc = tuple(str(item) for item in error.get("loc", ()))
    error_type = str(error.get("type", ""))
    raw_message = str(error.get("msg", "invalid"))
    message = _strip_value_error_prefix(raw_message)
    field = _field_index(manifest).get(loc[0]) if loc else None
    label = field.title if field is not None else (loc[0] if loc else "")

    if not loc:
        return _humanize_global_validation_message(message)
    if label:
        humanized = _humanize_field_validation_message(error_type=error_type, label=label, error=error)
        if humanized is not None:
            return humanized
    if label:
        return f"{label}: {message}"
    return message


def _strip_value_error_prefix(message: str) -> str:
    prefix = "Value error, "
    return message[len(prefix) :] if message.startswith(prefix) else message


def _humanize_global_validation_message(message: str) -> str:
    normalized = message.casefold()
    if "default_order contains provider(s) that are not enabled" in normalized:
        return "Provider priority can only include search providers that are enabled. Go back and enable them first."
    if "enabled_providers must include at least one provider" in normalized:
        return "Select at least one search provider."
    if "incompatible with responses" in normalized or ("chat-completions" in normalized and "responses" in normalized):
        return (
            "This API base cannot be used with API format 'responses'. "
            "Switch API format to 'messages' or use a Responses-compatible base URL."
        )
    return message


def _humanize_field_validation_message(
    *,
    error_type: str,
    label: str,
    error: Mapping[str, Any],
) -> str | None:
    if error_type == "missing":
        return f"{label} is required."
    if error_type == "greater_than":
        return f"{label} must be greater than {error.get('ctx', {}).get('gt')}."
    if error_type == "greater_than_equal":
        return f"{label} must be at least {error.get('ctx', {}).get('ge')}."
    if error_type == "less_than":
        return f"{label} must be less than {error.get('ctx', {}).get('lt')}."
    if error_type == "less_than_equal":
        return f"{label} must be at most {error.get('ctx', {}).get('le')}."
    if error_type in {"int_parsing", "int_type"}:
        return f"{label} must be a whole number."
    if error_type == "string_type":
        return f"{label} must be text."
    if error_type == "list_type":
        return f"{label} must be a list."
    return None


def _render_summary_line(*, label: str, value: str, template: str | None) -> str:
    if template:
        return f"- {template.format(label=label, value=value)}"
    return f"- {label}: {value}"


def _render_option_summary(step: OnboardingStep, value: Any) -> str | None:
    if value in (None, "", [], (), {}):
        return None
    label_by_value = {option.value: option.label for option in step.options}
    if isinstance(value, list):
        labels = [label_by_value.get(str(item), str(item)) for item in value]
        return ", ".join(labels) if labels else None
    return label_by_value.get(str(value), str(value))


def _secret_status(
    key: str,
    *,
    secrets_payload: dict[str, str],
    existing_secret_keys: set[str],
) -> str:
    if key in secrets_payload:
        return "updated in this install"
    if key in existing_secret_keys:
        return "already stored"
    return "not set"

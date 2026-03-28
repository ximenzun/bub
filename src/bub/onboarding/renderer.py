from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import typer
from prompt_toolkit import prompt as ptk_prompt
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import CheckboxList, Label, RadioList

from bub.onboarding.models import OnboardingField, OnboardingOption, OnboardingStep

type ReviewAction = Literal["install", "edit", "cancel"]


@dataclass(frozen=True, slots=True)
class ReviewSelection:
    action: ReviewAction
    step_id: str | None = None


class OnboardingCancelledError(RuntimeError):
    """Raised when the user cancels an interactive onboarding flow."""


class OnboardingRenderer(Protocol):
    def render_info(self, *, manifest, step: OnboardingStep) -> None: ...

    def render_external_link(self, *, manifest, step: OnboardingStep) -> None: ...

    def render_qr_challenge(self, *, manifest, step: OnboardingStep) -> None: ...

    def render_error(self, *, title: str, text: str) -> None: ...

    def confirm(self, *, title: str, text: str, default: bool = False) -> bool: ...

    def choose_one(
        self,
        *,
        step: OnboardingStep,
        current: Any = None,
        options: list[OnboardingOption] | None = None,
    ) -> str: ...

    def choose_many(
        self,
        *,
        step: OnboardingStep,
        current: Any = None,
        options: list[OnboardingOption] | None = None,
    ) -> list[str]: ...

    def reorder(
        self,
        *,
        step: OnboardingStep,
        current: list[str],
        options: list[OnboardingOption],
    ) -> list[str]: ...

    def review_summary(
        self,
        *,
        title: str,
        text: str,
        editable_steps: list[OnboardingStep],
    ) -> ReviewSelection: ...

    def prompt_field(self, *, field: OnboardingField, default: Any = None) -> Any: ...

    def prompt_secret(self, *, field: OnboardingField) -> str: ...


class CliOnboardingRenderer:
    _STYLE = Style.from_dict(
        {
            "": "",
            "label": "",
            "toolbar": "italic",
            "checkbox": "",
            "checkbox-selected": "#86efac bold",
            "radio": "",
            "radio-selected": "#7dd3fc bold",
        }
    )

    def render_info(self, *, manifest, step: OnboardingStep) -> None:
        self._print_section(f"{manifest.title} · {step.title}", _step_guidance_text(step) or manifest.summary)
        ptk_prompt("> ", default="", bottom_toolbar="Enter to continue", mouse_support=False)

    def render_external_link(self, *, manifest, step: OnboardingStep) -> None:
        parts = [_step_guidance_text(step)] if _step_guidance_text(step) else []
        if step.external_url:
            parts.append(f"Open: {step.external_url}")
        self._print_section(f"{manifest.title} · {step.title}", "\n\n".join(part for part in parts if part))
        ptk_prompt("> ", default="", bottom_toolbar="Enter to continue", mouse_support=False)

    def render_qr_challenge(self, *, manifest, step: OnboardingStep) -> None:
        text = _step_guidance_text(step) or "Complete the QR challenge from the target surface, then continue here."
        self._print_section(f"{manifest.title} · {step.title}", text)
        ptk_prompt("> ", default="", bottom_toolbar="Enter to continue", mouse_support=False)

    def render_error(self, *, title: str, text: str) -> None:
        self._print_section(f"Error: {title}", text)
        ptk_prompt("> ", default="", bottom_toolbar="Enter to continue", mouse_support=False)

    def confirm(self, *, title: str, text: str, default: bool = False) -> bool:
        self._print_section(title, text)
        result = self._run_inline_select(
            values=[("yes", "Continue"), ("no", "Cancel")],
            default="yes" if default else "no",
            multiple=False,
            toolbar="↑/↓ move · enter submit · esc cancel",
        )
        return bool(default if result is None else result == "yes")

    def choose_one(
        self,
        *,
        step: OnboardingStep,
        current: Any = None,
        options: list[OnboardingOption] | None = None,
    ) -> str:
        resolved_options = _prioritize_options(list(options or step.options))
        if not resolved_options:
            raise ValueError(f"Choice step '{step.id}' has no options")
        default_value = _default_option_value(resolved_options, current)
        self._print_section(step.title, _step_guidance_text(step))
        result = self._run_inline_select(
            values=[(option.value, _dialog_option_text(option)) for option in resolved_options],
            default=default_value,
            multiple=False,
            toolbar="↑/↓ move · enter submit · esc cancel",
        )
        if result is None:
            raise OnboardingCancelledError("onboarding cancelled")
        return str(result)

    def choose_many(
        self,
        *,
        step: OnboardingStep,
        current: Any = None,
        options: list[OnboardingOption] | None = None,
    ) -> list[str]:
        resolved_options = _prioritize_options(list(options or step.options))
        if not resolved_options:
            raise ValueError(f"Multi-choice step '{step.id}' has no options")
        default_values = [str(item) for item in current] if isinstance(current, list) else []
        self._print_section(step.title, _step_guidance_text(step))
        result = self._run_inline_select(
            values=[(option.value, _dialog_option_text(option)) for option in resolved_options],
            default=default_values,
            multiple=True,
            toolbar="↑/↓ move · space toggle · enter submit · esc cancel",
        )
        if result is None:
            raise OnboardingCancelledError("onboarding cancelled")
        return [str(item) for item in result]

    def reorder(
        self,
        *,
        step: OnboardingStep,
        current: list[str],
        options: list[OnboardingOption],
    ) -> list[str]:
        resolved_options = _prioritize_options(list(options))
        remaining = [option.value for option in resolved_options]
        ordered: list[str] = []
        title_by_value = {option.value: option.label for option in resolved_options}

        for index in range(1, len(resolved_options)):
            ordered_lines = [f"{i}. {title_by_value[value]}" for i, value in enumerate(ordered, start=1)]
            guidance_parts = [_step_guidance_text(step)]
            if current:
                current_lines = [f"{i}. {title_by_value.get(value, value)}" for i, value in enumerate(current, start=1)]
                guidance_parts.append("Current order:\n" + "\n".join(current_lines))
            if ordered_lines:
                guidance_parts.append("New order so far:\n" + "\n".join(ordered_lines))
            guidance_parts.append(f"Choose provider #{index}.")
            result = self._run_inline_select(
                values=[(value, title_by_value[value]) for value in remaining],
                default=remaining[0] if remaining else None,
                multiple=False,
                toolbar="↑/↓ move · enter select · esc cancel",
                title=f"{step.title} ({index}/{len(resolved_options)})",
                text="\n\n".join(part for part in guidance_parts if part),
            )
            if result is None:
                raise OnboardingCancelledError("onboarding cancelled")
            selected_value = str(result)
            ordered.append(selected_value)
            remaining = [value for value in remaining if value != selected_value]

        if remaining:
            ordered.extend(remaining)
        return ordered

    def review_summary(
        self,
        *,
        title: str,
        text: str,
        editable_steps: list[OnboardingStep],
    ) -> ReviewSelection:
        while True:
            self._print_section(title, text)
            action = self._run_inline_select(
                values=[
                    ("install", "Install"),
                    *([("edit", "Edit a step")] if editable_steps else []),
                    ("cancel", "Cancel"),
                ],
                default="install",
                multiple=False,
                toolbar="↑/↓ move · enter submit · esc cancel",
            )
            if action in {None, "cancel"}:
                return ReviewSelection(action="cancel")
            if action == "install":
                return ReviewSelection(action="install")
            if action == "edit":
                self._print_section("Edit Step", "Select a step to revisit.")
                result = self._run_inline_select(
                    values=[(step.id, step.title) for step in editable_steps],
                    default=editable_steps[0].id if editable_steps else None,
                    multiple=False,
                    toolbar="↑/↓ move · enter submit · esc cancel",
                )
                if result is None:
                    continue
                return ReviewSelection(action="edit", step_id=str(result))

    def prompt_field(self, *, field: OnboardingField, default: Any = None) -> Any:
        prompt_default = default if default is not None else field.default
        if field.kind == "bool":
            return self._prompt_bool_field(field, prompt_default)
        self._print_section(field.title, _field_guidance_text(field))
        raw = ptk_prompt(
            "> ",
            default=_render_default_value(prompt_default) if prompt_default not in (None, []) else "",
            bottom_toolbar="Enter to submit · Ctrl+C to cancel",
            mouse_support=False,
        )
        return self._convert_prompt_value(field, str(raw))

    def _prompt_bool_field(self, field: OnboardingField, prompt_default: Any) -> bool | None:
        values = [("yes", "Yes"), ("no", "No")]
        if not field.required:
            values.append(("unset", "Leave unset"))
        default_value = "unset" if prompt_default is None else ("yes" if bool(prompt_default) else "no")
        self._print_section(field.title, _field_guidance_text(field))
        result = self._run_inline_select(
            values=values,
            default=default_value,
            multiple=False,
            toolbar="↑/↓ move · enter submit · esc cancel",
        )
        if result is None:
            raise OnboardingCancelledError("onboarding cancelled")
        raw = str(result).casefold()
        if raw == "unset":
            return None
        if raw in {"yes", "y", "true", "1"}:
            return True
        if raw in {"no", "n", "false", "0"}:
            return False
        raise ValueError(f"{field.title} must be yes or no.")

    @staticmethod
    def _convert_prompt_value(field: OnboardingField, raw: str) -> Any:
        if field.kind == "int":
            if raw == "" and not field.required:
                return None
            return int(raw)
        if field.kind == "json":
            if raw == "" and not field.required:
                return None
            return json.loads(raw)
        if field.kind == "string_list":
            return [item.strip() for item in raw.split(",") if item.strip()]
        return raw

    def prompt_secret(self, *, field: OnboardingField) -> str:
        self._print_section(field.title, _field_guidance_text(field))
        return str(ptk_prompt("> ", is_password=True, bottom_toolbar="Enter to submit · Ctrl+C to cancel", mouse_support=False))

    @staticmethod
    def _print_section(title: str, text: str) -> None:
        typer.echo()
        typer.secho(f"◆ {title}", bold=True)
        if text:
            typer.echo(_prefix_block(text, prefix="│ "))

    def _run_inline_select(
        self,
        *,
        values: list[tuple[str, str]],
        default: str | list[str] | None,
        multiple: bool,
        toolbar: str,
        title: str | None = None,
        text: str | None = None,
    ) -> Any:
        body: Any
        if multiple:
            default_values = [str(item) for item in default] if isinstance(default, list) else []
            body = CheckboxList(values=values, default_values=default_values)
            body.show_scrollbar = False
            result_getter = lambda: list(body.current_values)
        else:
            default_value = str(default) if isinstance(default, str) else None
            body = RadioList(values=values, default=default_value, show_scrollbar=False)
            result_getter = lambda: body.values[body._selected_index][0]

        kb = KeyBindings()

        @kb.add("enter", eager=True)
        def _accept(event) -> None:
            event.app.exit(result=result_getter())

        @kb.add("escape")
        @kb.add("c-c")
        def _cancel(event) -> None:
            event.app.exit(result=None)

        app: Application[Any] = Application(
            layout=Layout(
                HSplit(
                    [
                        *([Label(title, style="class:label")] if title else []),
                        *([Label(text)] if text else []),
                        body,
                        Label(toolbar, style="class:toolbar"),
                    ]
                ),
                focused_element=body,
            ),
            key_bindings=kb,
            style=self._STYLE,
            full_screen=False,
            mouse_support=False,
        )
        return app.run()

def renderer_for_surface(surface: str) -> OnboardingRenderer:
    if surface != "cli":
        raise RuntimeError("Interactive onboarding renderer is only implemented for the CLI surface.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("CLI onboarding requires an interactive terminal.")
    return CliOnboardingRenderer()


def _prioritize_options(options: list[OnboardingOption] | tuple[OnboardingOption, ...]) -> list[OnboardingOption]:
    recommended = [option for option in options if option.recommended]
    remaining = [option for option in options if not option.recommended]
    return [*recommended, *remaining]


def _default_option_value(options: list[OnboardingOption], current: Any) -> str:
    if current is not None:
        current_value = str(current)
        if any(option.value == current_value for option in options):
            return current_value
    return options[0].value


def _resolve_typer_option(raw: str, options: list[OnboardingOption]) -> str:
    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(options):
            return options[index - 1].value
    for option in options:
        if raw == option.value or raw == option.label:
            return option.value
    raise ValueError(f"Unknown choice: {raw}")


def _resolve_typer_options(raw: str, options: list[OnboardingOption]) -> list[str]:
    selected: list[str] = []
    for token in [item.strip() for item in raw.split(",") if item.strip()]:
        value = _resolve_typer_option(token, options)
        if value not in selected:
            selected.append(value)
    return selected


def _render_typer_option_lines(options: list[OnboardingOption] | tuple[OnboardingOption, ...]) -> list[str]:
    lines: list[str] = []
    for index, option in enumerate(options, start=1):
        line = f"{index}. {option.label}"
        if option.recommended:
            line += " [recommended]"
        if option.description:
            line += f" - {option.description}"
        lines.append(line)
        if option.recommendation_reason:
            lines.append(f"   why: {option.recommendation_reason}")
    return lines


def _dialog_option_text(option: OnboardingOption) -> str:
    lines = [option.label]
    secondary: list[str] = []
    if option.recommended:
        secondary.append("recommended")
    if option.description:
        secondary.append(option.description)
    if secondary:
        lines.append(" · ".join(secondary))
    if option.recommendation_reason:
        lines.append(f"why: {option.recommendation_reason}")
    return "\n".join(lines)


def _prefix_block(text: str, *, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in text.splitlines())


def _step_guidance_text(step: OnboardingStep) -> str:
    parts: list[str] = []
    if step.description:
        parts.append(step.description)
    if step.scenario_hint:
        parts.append(f"Scenario: {step.scenario_hint}")
    return "\n\n".join(parts)


def _render_default_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _field_guidance_text(field: OnboardingField) -> str:
    parts: list[str] = []
    if field.description:
        parts.append(field.description)
    if field.recommended_value not in (None, "", [], (), {}):
        parts.append(f"Recommended: {_render_default_value(field.recommended_value)}")
    if field.recommendation_reason:
        parts.append(f"Why: {field.recommendation_reason}")
    if field.scenario_hint:
        parts.append(f"Scenario: {field.scenario_hint}")
    if field.example:
        parts.append(f"Example: {field.example}")
    if field.kind == "string_list":
        parts.append("Enter a comma-separated list.")
    if field.kind == "json":
        parts.append("Enter valid JSON.")
    return "\n\n".join(parts)


def _move_reorder_value(order: list[str], selected_value: str, direction: int) -> tuple[list[str], int]:
    if selected_value not in order or direction == 0:
        return list(order), max(0, order.index(selected_value)) if selected_value in order else 0
    index = order.index(selected_value)
    target = max(0, min(len(order) - 1, index + direction))
    if target == index:
        return list(order), index
    updated = list(order)
    updated[index], updated[target] = updated[target], updated[index]
    return updated, target


def _resolve_reorder_tokens(raw: str, options: list[OnboardingOption]) -> list[str]:
    tokens = [item.strip() for item in raw.split(",") if item.strip()]
    if not tokens:
        return [option.value for option in options]
    selected: list[str] = []
    for token in tokens:
        value = _resolve_typer_option(token, options)
        if value not in selected:
            selected.append(value)
    for option in options:
        if option.value not in selected:
            selected.append(option.value)
    return selected

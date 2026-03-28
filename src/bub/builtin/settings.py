from __future__ import annotations

import pathlib
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator

DEFAULT_MODEL = "openrouter:qwen/qwen3-coder-next"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_HOME = pathlib.Path.home() / ".bub"


class AgentSettings(BaseModel):
    """Strict runtime settings for Bub's builtin agent."""

    home: pathlib.Path = Field(default=DEFAULT_HOME)
    model: str = DEFAULT_MODEL
    fallback_model: str | None = None
    api_key: str | dict[str, str] | None = None
    api_base: str | dict[str, str] | None = None
    api_format: Literal["completion", "responses", "messages"] = "completion"
    max_steps: int = 50
    max_tokens: int = DEFAULT_MAX_TOKENS
    model_timeout_seconds: int | None = None

    @model_validator(mode="after")
    def _validate_api_shape(self) -> AgentSettings:
        if self.api_format != "responses" or not isinstance(self.api_base, str):
            return self
        path = urlparse(self.api_base).path.casefold()
        if "chat-completions" in path:
            raise ValueError(
                "api_format=responses is incompatible with a chat-completions endpoint base URL. "
                "Use api_format=messages for chat-completions-style providers, "
                "or point api_base at a Responses-compatible root base URL instead."
            )
        return self

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LLMError(RuntimeError):
    """Base error for optional LLM enrichment."""


class LLMUnavailable(LLMError):
    """The configured LLM cannot currently be reached."""


class LLMInvalidResponse(LLMError):
    """The LLM returned a response that cannot be trusted or parsed."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    model: str = ""


class LLMProvider(Protocol):
    def generate(self, *, system: str, user: str, options: dict[str, Any] | None = None) -> LLMResponse:
        ...

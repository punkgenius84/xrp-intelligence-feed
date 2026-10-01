from __future__ import annotations

import json
import os
from typing import Any

import requests

from .base import LLMInvalidResponse, LLMResponse, LLMUnavailable


DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "qwen2.5:7b"


class OllamaProvider:
    """Small local Ollama adapter. Failure is explicit; callers decide whether to degrade."""

    def __init__(self, endpoint: str | None = None, model: str | None = None, timeout: float = 45.0):
        self.endpoint = (endpoint or os.getenv("OLLAMA_ENDPOINT") or DEFAULT_ENDPOINT).strip()
        self.model = (model or os.getenv("OLLAMA_MODEL") or DEFAULT_MODEL).strip()
        self.timeout = timeout

    def generate(self, *, system: str, user: str, options: dict[str, Any] | None = None) -> LLMResponse:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if options:
            payload["options"] = options
        try:
            response = requests.post(self.endpoint, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LLMUnavailable(f"Ollama unavailable: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise LLMUnavailable(f"Ollama returned HTTP {response.status_code}")
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMInvalidResponse("Ollama returned invalid JSON") from exc
        message = body.get("message") if isinstance(body, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LLMInvalidResponse("Ollama response did not contain message.content")
        return LLMResponse(content=content.strip(), model=str(body.get("model") or self.model))

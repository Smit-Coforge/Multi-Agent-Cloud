"""
This module talks to a language model and parses its JSON reply.

Responsibilities:
- Call a local Ollama model for a live run.
- Return canned JSON replies from FakeLLM in tests.
- Parse a reply into one JSON object.
"""

from __future__ import annotations

import json
import os
from typing import Any

import ollama


def parse_json_object(content: str) -> dict[str, Any]:
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("LLM response must be a JSON object")  # noqa: TRY004
    return data


class FakeLLM:
    """Canned JSON replies for tests. Same complete() shape as OllamaLLM."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}
        self.last_content: str | None = None

    def complete(self, messages: list[dict[str, str]], *, agent: str) -> str:
        # wrap() clears last_usage before the call, so set it here.
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}
        payload = self.responses[agent]
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.last_content = text
        return text


class OllamaLLM:
    def __init__(
        self,
        host: str | None = None,
        model: str = "qwen3:8b",
    ) -> None:
        self._client = ollama.Client(
            host=host or os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
        )
        self.model = model
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.last_content: str | None = None

    def complete(self, messages: list[dict[str, str]], *, agent: str) -> str:
        resp = self._client.chat(
            model=self.model,
            messages=messages,
            format="json",
            think=False,
            options={"num_predict": 512},
        )
        self.last_usage = {
            "prompt_tokens": int(resp.prompt_eval_count or 0),
            "completion_tokens": int(resp.eval_count or 0),
        }
        self.last_content = resp.message.content or ""
        return self.last_content

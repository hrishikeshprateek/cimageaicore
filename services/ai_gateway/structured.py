"""Structured text-only call through the configured provider (used by the Video Composer's cut refinement).

New file, not a change to the existing gateway: it reuses GeminiProvider's request/retry helpers
for a prompt that carries no video. Providers may implement `generate_structured()` themselves.
"""
from __future__ import annotations

from typing import Any

from services.ai_gateway.base import ProviderError, RawModelOutput


def generate_structured(provider, *, system_instruction: str, prompt: str, json_schema: dict[str, Any]) -> RawModelOutput:
    custom = getattr(provider, "generate_structured", None)
    if callable(custom):
        return custom(system_instruction=system_instruction, prompt=prompt, json_schema=json_schema)
    if getattr(provider, "name", None) == "gemini":
        interaction = provider._create_with_retry(input=[{"type": "text", "text": prompt}], system_instruction=system_instruction, json_schema=json_schema)
        return provider._to_output(interaction)
    raise ProviderError(f"provider '{getattr(provider, 'name', type(provider).__name__)}' does not support structured text generation")

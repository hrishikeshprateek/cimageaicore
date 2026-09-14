"""AI Gateway - provider-agnostic interface.

The application calls `analyze_video()` / `repair_json()`; it never talks to a
vendor SDK directly. Gemini is the first provider; a local model provider can
be added later without touching the block engine or API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

StageCallback = Callable[[str, dict[str, Any]], None]  # (state, detail)


@dataclass
class VideoInput:
    """What a provider receives. Exactly one of `path` / `url` is set."""

    name: str
    path: Path | None = None
    url: str | None = None
    mime_type: str | None = None


@dataclass
class ExtraPass:
    """A further structured call over the same video (e.g. a focused people pass)."""

    name: str
    system_instruction: str
    prompt: str
    json_schema: dict[str, Any]


@dataclass
class VideoAnalysisRequest:
    video: VideoInput
    system_instruction: str
    prompt: str
    json_schema: dict[str, Any]
    extra_passes: list[ExtraPass] = field(default_factory=list)


@dataclass
class RawModelOutput:
    text: str
    model: str
    usage: dict[str, int | None] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, "RawModelOutput"] = field(default_factory=dict)  # keyed by ExtraPass.name


def sum_usage(*outputs: "RawModelOutput") -> dict[str, int | None]:
    keys = {k for o in outputs for k in o.usage}
    return {k: sum((o.usage.get(k) or 0) for o in outputs) for k in keys}


class ProviderError(RuntimeError):
    """Raised for any provider-side failure (upload, API, timeout)."""


class AIProvider(Protocol):
    name: str
    model: str

    def analyze_video(self, request: VideoAnalysisRequest, on_stage: StageCallback) -> RawModelOutput: ...

    def repair_json(self, invalid_text: str, error: str, json_schema: dict[str, Any]) -> RawModelOutput: ...

    def generate_structured(
        self, system_instruction: str, prompt: str, json_schema: dict[str, Any], *, model: str | None = None, thinking_level: str | None = None
    ) -> RawModelOutput: ...

"""AI Gateway: build the configured provider."""
from __future__ import annotations

from services.ai_gateway.base import AIProvider, ProviderError, RawModelOutput, VideoAnalysisRequest, VideoInput


def build_provider(settings) -> AIProvider:
    kind = settings.resolved_provider
    if kind == "gemini":
        from services.ai_gateway.gemini import GeminiProvider

        return GeminiProvider(
            settings.gemini_api_key,
            settings.gemini_model,
            thinking_level=settings.gemini_thinking_level,
            max_output_tokens=settings.gemini_max_output_tokens,
            video_resolution=settings.gemini_video_resolution,
            video_fps=settings.gemini_video_fps,
            delete_uploads=settings.gemini_delete_uploads,
            store=settings.gemini_store,
            timeout_seconds=settings.gemini_timeout_seconds,
            upload_timeout_seconds=settings.gemini_upload_timeout_seconds,
            poll_timeout_seconds=settings.gemini_poll_timeout_seconds,
        )
    if kind == "mock":
        from services.ai_gateway.mock import MockProvider

        return MockProvider()
    raise ValueError(f"unknown AI provider: {kind}")


__all__ = ["AIProvider", "ProviderError", "RawModelOutput", "VideoAnalysisRequest", "VideoInput", "build_provider"]

"""Gemini provider (google-genai >= 2.3, Interactions API).

Flow for a local file:  Files API upload -> poll until ACTIVE -> interactions.create
Flow for a YouTube URL: interactions.create with the URL as a video part
Structured output is enforced with `response_format` + our JSON schema.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from google import genai
from google.genai import types as genai_types

from services.ai_gateway.base import ProviderError, RawModelOutput, StageCallback, VideoAnalysisRequest

log = logging.getLogger(__name__)

_TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}
_RETRY_HINT = re.compile(r"retry in ([0-9.]+)\s*s", re.IGNORECASE)
_MAX_BACKOFF = 120.0


def _status_of(exc: BaseException) -> int | None:
    """Files API errors carry `.code`; Interactions errors carry `.status_code`."""
    for attr in ("status_code", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    return None


def _retry_after(exc: BaseException, attempt: int) -> float:
    m = _RETRY_HINT.search(str(exc))
    if m:
        return min(float(m.group(1)) + 1.0, _MAX_BACKOFF)
    return min(2.0 ** attempt, _MAX_BACKOFF)


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        *,
        thinking_level: str = "medium",  # gemini-3.8-flash accepts low | medium | high
        max_output_tokens: int | None = None,
        video_resolution: str = "medium",
        video_fps: float | None = None,
        delete_uploads: bool = True,
        store: bool = False,
        timeout_seconds: float = 1800,
        upload_timeout_seconds: float = 600,
        poll_timeout_seconds: float = 30,
        poll_interval: float = 5.0,
        max_attempts: int = 3,
    ):
        if not api_key:
            raise ProviderError("GEMINI_API_KEY is not set")
        self.model = model
        self.thinking_level = thinking_level
        self.max_output_tokens = max_output_tokens
        self.video_resolution = video_resolution
        self.video_fps = video_fps
        self.delete_uploads = delete_uploads
        self.store = store
        self.timeout = timeout_seconds                    # analysis call: may legitimately take minutes
        self.upload_timeout = upload_timeout_seconds      # per upload chunk
        self.poll_timeout = poll_timeout_seconds          # tiny status polls: fail fast, retry
        self.poll_interval = poll_interval
        self.max_attempts = max_attempts
        # Client default is short; long timeouts are passed per call where they are warranted.
        self.client = genai.Client(api_key=api_key, http_options=genai_types.HttpOptions(timeout=int(poll_timeout_seconds * 1000)))

    # ------------------------------------------------------------------ public
    def analyze_video(self, request: VideoAnalysisRequest, on_stage: StageCallback) -> RawModelOutput:
        v = request.video
        uploaded: genai_types.File | None = None
        try:
            if v.url:
                video_part: dict[str, Any] = {"type": "video", "uri": v.url}
                on_stage("UPLOADED", {"note": "online source passed by URL", "url": v.url})
            else:
                uploaded = self._upload(v.path, v.name, v.mime_type, on_stage)
                video_part = {"type": "video", "uri": uploaded.uri, "mime_type": uploaded.mime_type}
            video_part["resolution"] = self.video_resolution
            if self.video_fps:
                video_part["processing"] = {"type": "static", "fps": self.video_fps}

            on_stage("ANALYZING", {"model": self.model, "thinking_level": self.thinking_level, "resolution": self.video_resolution, "fps": self.video_fps})
            interaction = self._create_with_retry(
                input=[video_part, {"type": "text", "text": request.prompt}],
                system_instruction=request.system_instruction,
                json_schema=request.json_schema,
            )
            out = self._to_output(interaction)
            for extra in request.extra_passes:
                on_stage("ANALYZING", {"pass": extra.name, "model": self.model})
                it = self._create_with_retry(
                    input=[video_part, {"type": "text", "text": extra.prompt}],
                    system_instruction=extra.system_instruction,
                    json_schema=extra.json_schema,
                )
                out.extras[extra.name] = self._to_output(it)
            return out
        finally:
            if uploaded is not None and self.delete_uploads:
                try:
                    self.client.files.delete(name=uploaded.name, config={"http_options": {"timeout": int(self.poll_timeout * 1000)}})
                except Exception as exc:  # noqa: BLE001 - cleanup must never mask the real result
                    log.warning("could not delete uploaded file %s: %s", uploaded.name, exc)

    def repair_json(self, invalid_text: str, error: str, json_schema: dict[str, Any]) -> RawModelOutput:
        prompt = (
            "The JSON below was meant to conform to the given JSON schema but failed validation.\n"
            "Return a corrected JSON document that conforms to the schema. Keep all information; "
            "only fix structure, types, missing fields and formats. Return ONLY the JSON.\n\n"
            f"VALIDATION ERROR:\n{error}\n\nSCHEMA:\n{json.dumps(json_schema)}\n\nINVALID JSON:\n{invalid_text}"
        )
        interaction = self._create_with_retry(
            input=[{"type": "text", "text": prompt}],
            system_instruction="You repair JSON documents so they conform exactly to a JSON schema.",
            json_schema=json_schema,
        )
        return self._to_output(interaction)

    # ----------------------------------------------------------------- helpers
    def _upload(self, path, name: str, mime_type: str | None, on_stage: StageCallback) -> genai_types.File:
        cfg: dict[str, Any] = {"display_name": name, "http_options": {"timeout": int(self.upload_timeout * 1000)}}
        if mime_type:
            cfg["mime_type"] = mime_type
        started = time.monotonic()
        try:
            f = self.client.files.upload(file=path, config=cfg)
        except Exception as exc:  # noqa: BLE001 - both SDK error families end up here
            raise ProviderError(f"Gemini upload failed ({_status_of(exc)}): {exc}") from exc
        upload_seconds = round(time.monotonic() - started, 1)

        deadline = time.monotonic() + self.timeout
        poll_cfg = {"http_options": {"timeout": int(self.poll_timeout * 1000)}}
        failures = 0
        while f.state is None or f.state.name != "ACTIVE":
            if f.state is not None and f.state.name == "FAILED":
                raise ProviderError(f"Gemini could not process the uploaded file {f.name}")
            if time.monotonic() > deadline:
                raise ProviderError(f"Timed out waiting for Gemini to process {f.name}")
            time.sleep(self.poll_interval)
            try:
                f = self.client.files.get(name=f.name, config=poll_cfg)
                failures = 0
            except Exception as exc:  # noqa: BLE001 - a dropped connection must not hang the job
                failures += 1
                log.warning("files.get(%s) failed (%d/%d): %s", f.name, failures, self.max_attempts, exc)
                if failures >= self.max_attempts:
                    raise ProviderError(f"Gemini file status poll failed repeatedly: {exc}") from exc
        on_stage("UPLOADED", {"file": f.name, "uri": f.uri, "mime_type": f.mime_type, "size_bytes": f.size_bytes, "upload_seconds": upload_seconds})
        return f

    def generate_structured(self, system_instruction: str, prompt: str, json_schema: dict[str, Any], *, model: str | None = None, thinking_level: str | None = None) -> RawModelOutput:
        """Text-only structured call (used by content agents)."""
        it = self._create_with_retry(
            input=[{"type": "text", "text": prompt}], system_instruction=system_instruction, json_schema=json_schema,
            model=model, thinking_level=thinking_level,
        )
        return self._to_output(it)

    def describe_images(self, system_instruction: str, prompt: str, images: list[tuple[bytes, str]], json_schema: dict[str, Any], *,
                        model: str | None = None, thinking_level: str | None = None) -> RawModelOutput:
        """Inline images (base64) + text -> structured JSON. Keeps each image under a few hundred KB; callers pre-size them."""
        import base64

        parts: list[dict[str, Any]] = [{"type": "image", "data": base64.b64encode(data).decode("ascii"), "mime_type": mime} for data, mime in images]
        parts.append({"type": "text", "text": prompt})
        it = self._create_with_retry(input=parts, system_instruction=system_instruction, json_schema=json_schema, model=model, thinking_level=thinking_level)
        return self._to_output(it)

    def _create_with_retry(self, *, input: list[dict[str, Any]], system_instruction: str, json_schema: dict[str, Any], model: str | None = None, thinking_level: str | None = None):
        gen_cfg: dict[str, Any] = {"thinking_level": thinking_level or self.thinking_level}
        if self.max_output_tokens:
            gen_cfg["max_output_tokens"] = self.max_output_tokens

        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.client.interactions.create(
                    model=model or self.model,
                    system_instruction=system_instruction,
                    input=input,
                    response_format={"type": "text", "mime_type": "application/json", "schema": json_schema},
                    generation_config=gen_cfg,
                    store=self.store,
                    timeout=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001 - see _status_of: two SDK error families
                last_exc = exc
                code = _status_of(exc)
                if code not in _TRANSIENT_CODES or attempt == self.max_attempts:
                    raise ProviderError(f"Gemini API error ({code or type(exc).__name__}): {exc}") from exc
                wait = _retry_after(exc, attempt)
                log.warning("transient Gemini error %s (attempt %d/%d), retrying in %.0fs", code, attempt, self.max_attempts, wait)
                time.sleep(wait)
        raise ProviderError(f"Gemini API error: {last_exc}")

    def _to_output(self, interaction) -> RawModelOutput:
        status = str(getattr(interaction, "status", "") or "")
        if status and status != "completed":
            errs = getattr(interaction, "errors", None)
            raise ProviderError(f"Gemini interaction ended with status '{status}': {errs or 'no details'}")
        text = interaction.output_text or ""
        if not text.strip():
            raise ProviderError("Gemini returned an empty response")
        u = getattr(interaction, "usage", None)
        usage = {
            "input_tokens": getattr(u, "total_input_tokens", None),
            "output_tokens": getattr(u, "total_output_tokens", None),
            "thought_tokens": getattr(u, "total_thought_tokens", None),
            "total_tokens": getattr(u, "total_tokens", None),
        }
        return RawModelOutput(
            text=text,
            model=getattr(interaction, "model", None) or self.model,
            usage=usage,
            meta={"interaction_id": getattr(interaction, "id", None), "status": status},
        )

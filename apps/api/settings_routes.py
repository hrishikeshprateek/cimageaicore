"""Settings page: the transcription and reel-cut knobs, editable in the admin UI and applied live.

Saving writes data/runtime_settings.json, pushes the values onto the live settings objects and rebuilds
the transcriber, so a change takes effect on the next video without a restart. A field cleared in the UI
goes back to inheriting the .env value.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from apps.api.runtime_settings import CUT_FIELDS, LANGUAGES, TRANSCRIBE_FIELDS, RuntimeSettings, apply, check_against, load, save

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

# label + help for each field, so the page stays in step with the model instead of repeating it in JS
FIELDS: dict[str, dict[str, Any]] = {
    "whisper_language": {
        "label": "Spoken language", "type": "choice", "choices": [{"value": c, "label": lbl} for c, lbl in LANGUAGES],
        "help": "Whisper decodes one language per 30-second window and has no code-switching mode. For Hindi/English "
                "speech choose Hindi: English words stay in Latin script and both scripts get punctuated.",
    },
    "whisper_carry_context": {
        "label": "Carry context between windows", "type": "bool",
        "help": "What makes Whisper emit full stops and the Devanagari danda. With it off every window is decoded "
                "blind, the punctuation goes missing and the cutter has no sentence ends to finish a reel on.",
    },
    "whisper_sentence_gap": {
        "label": "Silence that ends a sentence", "type": "seconds", "min": 0.1, "max": 3.0, "step": 0.05,
        "help": "Hindi speakers breathe mid-sentence, so below about 0.5 s sentences get chopped into fragments.",
    },
    "whisper_initial_prompt": {
        "label": "Punctuation sample", "type": "text", "rows": 3,
        "help": "Whisper imitates this sample, so write it the way the transcript should come out: both scripts, "
                "every sentence closed, institution names spelled correctly.",
    },
    "min_cut_seconds": {"label": "Shortest reel", "type": "seconds", "min": 1, "max": 600, "step": 1,
                        "help": "A cut is stretched to at least this, then on to the next sentence end."},
    "max_cut_seconds": {"label": "Longest reel", "type": "seconds", "min": 2, "max": 600, "step": 1,
                        "help": "The furthest a cut may run to reach a sentence end before it is pulled back to an earlier one."},
    "target_cut_seconds": {"label": "Preferred length", "type": "seconds", "min": 2, "max": 600, "step": 1,
                           "help": "The cutter picks the sentence end closest to this length."},
    "max_cuts": {"label": "Cuts proposed per video", "type": "int", "min": 1, "max": 20, "step": 1,
                 "help": "How many reel windows each analysed video offers in the studio."},
}
GROUPS = [
    {"key": "transcription", "title": "Transcription", "fields": list(TRANSCRIBE_FIELDS),
     "note": "How speech becomes the sentences a reel is allowed to end on. Changing these affects the next "
             "transcription; videos already transcribed keep their word timings until you re-run them."},
    {"key": "cuts", "title": "Reel cuts", "fields": list(CUT_FIELDS),
     "note": "Every cut ends on a sentence end inside these limits."},
]


def _paths(request: Request):
    st = request.app.state
    return st.settings, _composer_settings(), st.settings.runtime_settings_file


def _composer_settings():
    from services.video_composer.settings import get_composer_settings

    return get_composer_settings()


def _describe(request: Request) -> dict[str, Any]:
    settings, composer, path = _paths(request)
    stored = load(path).set_values()
    values: dict[str, Any] = {}
    for f in TRANSCRIBE_FIELDS:
        values[f] = getattr(settings, f)
    for f in CUT_FIELDS:
        values[f] = getattr(composer, f)
    return {
        "groups": [{**g, "fields": [{"key": f, "value": values[f], "source": "ui" if f in stored else "env", **FIELDS[f]} for f in g["fields"]]}
                   for g in GROUPS],
        "transcribe_provider": getattr(request.app.state.transcriber, "name", "off"),
        "stored": stored,
    }


@router.get("")
def get_settings_page(request: Request) -> dict[str, Any]:
    return _describe(request)


class SettingsBody(BaseModel):
    values: dict[str, Any]
    reset: list[str] = []          # keys to hand back to .env


@router.put("")
def put_settings(request: Request, body: SettingsBody) -> dict[str, Any]:
    settings, composer, path = _paths(request)
    unknown = (set(body.values) | set(body.reset)) - set(FIELDS)
    if unknown:
        raise HTTPException(400, f"unknown setting(s): {', '.join(sorted(unknown))}")

    merged = load(path).set_values()
    for k in body.reset:
        merged.pop(k, None)
    for k, v in body.values.items():
        if v is None or (isinstance(v, str) and not v.strip() and k != "whisper_language"):
            merged.pop(k, None)    # cleared in the UI -> inherit .env again
        else:
            merged[k] = v
    try:
        cfg = RuntimeSettings.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(400, "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {e['msg']}" for e in exc.errors())) from exc
    try:
        check_against(cfg, composer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    save(path, cfg)
    apply(cfg, settings, composer)
    # the transcriber holds its decode options, so it has to be rebuilt for them to take effect
    from services.transcribe import build_transcriber

    request.app.state.transcriber = build_transcriber(settings)
    ctx = getattr(request.app.state, "composer", None)
    if ctx is not None:
        ctx.settings = composer
    request.app.state.store.audit("admin", "settings.saved", "settings", None, cfg.set_values())
    log.info("settings saved from the admin UI: %s", cfg.set_values())
    return _describe(request)

"""Measured transcripts: the word timings a precise cut is made on.

Runs automatically after every analysis (TRANSCRIBE_ON_ANALYSIS); these endpoints let the studio read the sentences,
re-run the measurement, and see how far along it is.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from services.transcribe import TranscribeError, Transcript

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["transcripts"])


def whisper_language_of(result) -> str | None:
    """The language the analysis found ('hi', 'en', 'hi-en' ...) as a Whisper code. A mix is transcribed as the main
    language - Whisper keeps the English words inside a Hindi line, which is what Hinglish speech actually looks like."""
    if result is None:
        return None
    analysis = getattr(result, "analysis", None)
    raw = (getattr(getattr(analysis, "video", None), "language", None) or "").strip().lower()
    if not raw:
        langs = [(getattr(s, "language", "") or "").lower() for s in (getattr(analysis, "transcript", None) or [])]
        raw = max(set(langs), key=langs.count) if any(langs) else ""
    code = raw.split("-")[0].split("/")[0].strip()[:2]
    return code or None


def _store(request: Request):
    st = getattr(request.app.state, "transcripts", None)
    if st is None:
        raise HTTPException(501, "transcripts are not available")
    return st


def transcribe_job(state, job_id: str, *, force: bool = False) -> None:
    """Measure the word timings of one job's media. Safe to call from a worker thread: it never raises."""
    store, transcriber = state.store, getattr(state, "transcriber", None)
    tstore = getattr(state, "transcripts", None)
    if transcriber is None or tstore is None:
        return
    try:
        job = store.get(job_id)
    except Exception:  # noqa: BLE001 - the app may be shutting down while a measurement is queued
        return
    if job is None:
        return
    path = Path(job.source.path) if job.source.path else None
    if path is None or not path.exists():
        log.info("job %s has no local media - skipping word timings (analysed from a link?)", job_id)
        return
    if not force and tstore.get(job_id) is not None:
        return
    try:
        tstore.mark(job_id, "running", media_id=job.media_id)
    except Exception:  # noqa: BLE001
        return
    try:
        result = store.load_result(job)
        segments = result.analysis.transcript if result else None
        # Whisper left to guess will call Hindi-with-English-words "English" and translate it; the analysis already
        # knows what is being spoken, so tell it.
        lang = state.settings.whisper_language or whisper_language_of(result)
        t = transcriber.transcribe(path, job_id=job_id, segments=segments, language=lang)
        if not t.sentences:
            tstore.mark(job_id, "failed", "no speech found in this video", media_id=job.media_id)
            return
        tstore.put(t, media_id=job.media_id)
        try:
            store.audit("transcriber", "transcript.ready", "job", job_id,
                        {"model": t.model, "sentences": len(t.sentences), "seconds": t.seconds, "language": t.language})
        except Exception:  # noqa: BLE001 - the JSON store has no audit log
            pass
        log.info("job %s transcript ready: %d sentences, %.1fs, %s", job_id, len(t.sentences), t.seconds, t.model)
    except TranscribeError as exc:
        tstore.mark(job_id, "failed", str(exc), media_id=job.media_id)
    except Exception as exc:  # noqa: BLE001 - a transcript is an enhancement, never a reason to lose the job
        log.warning("job %s transcription failed: %s: %s", job_id, type(exc).__name__, exc)
        try:
            tstore.mark(job_id, "failed", f"{type(exc).__name__}: {exc}", media_id=job.media_id)
        except Exception:  # noqa: BLE001 - nothing left to record it in (shutdown)
            pass


@router.get("/jobs/{job_id}/transcript")
def get_transcript(request: Request, job_id: str, words: bool = False) -> dict[str, Any]:
    """The measured sentences of a job: what the timeline editor draws and the cutter snaps to."""
    st = _store(request)
    t: Transcript | None = st.get(job_id)
    info = st.status(job_id) or {}
    if t is None:
        return {"job_id": job_id, "ready": False, "status": info.get("status", "none"), "error": info.get("error"),
                "provider": getattr(getattr(request.app.state, "transcriber", None), "name", None), "sentences": []}
    return {
        "job_id": job_id, "ready": True, "status": "ready", "language": t.language, "model": t.model, "seconds": t.seconds,
        "sentences": [{"i": s.i, "start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker,
                       "ends_open": s.ends_open(), "starts_with_filler": s.starts_with_filler(),
                       **({"words": [w.model_dump() for w in s.words]} if words else {})} for s in t.sentences],
    }


@router.post("/jobs/{job_id}/transcript", status_code=status.HTTP_202_ACCEPTED)
def run_transcript(request: Request, job_id: str, force: bool = True) -> dict[str, Any]:
    """Measure (or re-measure) this video's word timings."""
    state = request.app.state
    if state.store.get(job_id) is None:
        raise HTTPException(404, "job not found")
    if getattr(state, "transcriber", None) is None:
        raise HTTPException(501, "word timings are off - set TRANSCRIBE_PROVIDER=whisper")
    state.runner.run_async(lambda: transcribe_job(state, job_id, force=force))
    return {"job_id": job_id, "status": "running", "provider": state.transcriber.name}


@router.delete("/jobs/{job_id}/transcript")
def delete_transcript(request: Request, job_id: str) -> dict[str, Any]:
    return {"deleted": _store(request).delete(job_id)}

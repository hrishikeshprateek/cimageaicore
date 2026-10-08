"""Where measured transcripts live: one row per job in Postgres, a JSON sidecar when there is no database."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.transcribe.model import Transcript

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JsonTranscriptStore:
    kind = "json"

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def _p(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.json"

    def put(self, t: Transcript, *, media_id: str | None = None) -> Transcript:
        self._p(t.job_id).write_text(t.model_dump_json(indent=1), encoding="utf-8")
        return t

    def get(self, job_id: str) -> Transcript | None:
        p = self._p(job_id)
        if not p.exists():
            return None
        try:
            return Transcript.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt sidecar just means "not transcribed"
            return None

    def status(self, job_id: str) -> dict[str, Any] | None:
        t = self.get(job_id)
        return None if t is None else {"job_id": job_id, "status": "ready", "model": t.model, "language": t.language,
                                       "seconds": t.seconds, "sentence_count": len(t.sentences),
                                       "word_count": sum(len(s.words) for s in t.sentences), "error": None}

    def mark(self, job_id: str, status: str, error: str | None = None, media_id: str | None = None) -> None:
        if status == "failed":
            log.warning("transcription failed for %s: %s", job_id, error)

    def delete(self, job_id: str) -> bool:
        p = self._p(job_id)
        if p.exists():
            p.unlink()
            return True
        return False


class PostgresTranscriptStore:
    kind = "postgres"

    def __init__(self, pool):
        self.pool = pool

    def put(self, t: Transcript, *, media_id: str | None = None) -> Transcript:
        from psycopg.types.json import Jsonb

        words = sum(len(s.words) for s in t.sentences)
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                """INSERT INTO transcripts (job_id, media_id, language, model, seconds, sentence_count, word_count, status, error, data, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'ready', NULL, %s, now())
                   ON CONFLICT (job_id) DO UPDATE SET media_id = EXCLUDED.media_id, language = EXCLUDED.language, model = EXCLUDED.model,
                        seconds = EXCLUDED.seconds, sentence_count = EXCLUDED.sentence_count, word_count = EXCLUDED.word_count,
                        status = 'ready', error = NULL, data = EXCLUDED.data, updated_at = now()""",
                (t.job_id, media_id, t.language, t.model, t.seconds, len(t.sentences), words, Jsonb(json.loads(t.model_dump_json()))),
            )
        return t

    def get(self, job_id: str) -> Transcript | None:
        with self.pool.connection() as conn:
            r = conn.execute("SELECT data, status FROM transcripts WHERE job_id = %s", (job_id,)).fetchone()
        if not r or r["status"] != "ready" or not r["data"]:
            return None
        try:
            return Transcript.model_validate(r["data"])
        except Exception:  # noqa: BLE001
            log.exception("stored transcript for %s cannot be read", job_id)
            return None

    def status(self, job_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            r = conn.execute("""SELECT job_id, status, model, language, seconds, sentence_count, word_count, error, updated_at
                                FROM transcripts WHERE job_id = %s""", (job_id,)).fetchone()
        if not r:
            return None
        return {**r, "updated_at": r["updated_at"].isoformat()}

    def mark(self, job_id: str, status: str, error: str | None = None, media_id: str | None = None) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                """INSERT INTO transcripts (job_id, media_id, model, status, error, updated_at) VALUES (%s, %s, '', %s, %s, now())
                   ON CONFLICT (job_id) DO UPDATE SET status = EXCLUDED.status, error = EXCLUDED.error, updated_at = now()""",
                (job_id, media_id, status, (error or "")[:1000] or None),
            )
        if status == "failed":
            log.warning("transcription failed for %s: %s", job_id, error)

    def delete(self, job_id: str) -> bool:
        with self.pool.connection() as conn, conn.transaction():
            return conn.execute("DELETE FROM transcripts WHERE job_id = %s", (job_id,)).rowcount > 0


def build_transcript_store(store, transcripts_dir: Path):
    """Postgres when the app has it, otherwise JSON files next to the other job data."""
    if getattr(store, "supports_vectors", False):
        return PostgresTranscriptStore(store.pool)
    return JsonTranscriptStore(transcripts_dir)

"""Who produces the word timings: local Whisper (faster-whisper / CTranslate2), or a mock for tests."""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from services.transcribe.model import Sentence, Transcript, Word, attach_speakers, sentences_from_words

log = logging.getLogger(__name__)
_MODELS: dict[tuple, object] = {}      # loaded once per (size, device, compute type) - loading costs seconds


class TranscribeError(RuntimeError):
    pass


def read_audio(media: Path, *, sample_rate: int = 16000):
    """Decode the audio to the mono float32 Whisper expects, with ffmpeg rather than the decoder bundled in the model
    library (that one keeps breaking against new PyAV, and ffmpeg reads every camera format anyway)."""
    import numpy as np

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise TranscribeError("ffmpeg not found on PATH")
    proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(media), "-vn", "-ac", "1",
                           "-ar", str(sample_rate), "-f", "s16le", "-"], capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise TranscribeError(f"could not read the audio of {Path(media).name}: {proc.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype("float32") / 32768.0


def find_silences(audio, *, sample_rate: int = 16000, hop: float = 0.01, win: float = 0.03,
                  min_seconds: float = 0.1, floor_db: float = -42.0) -> list[tuple[float, float]]:
    """Where nobody is speaking, measured on the waveform. A cut placed inside one of these never clips a syllable -
    Whisper's own word gaps are not enough, because it reports fast speech as back-to-back words."""
    import numpy as np

    if audio is None or len(audio) < int(sample_rate * win):
        return []
    h, w = max(1, int(sample_rate * hop)), max(1, int(sample_rate * win))
    frames = 1 + (len(audio) - w) // h
    if frames < 3:
        return []
    strided = np.lib.stride_tricks.as_strided(audio, shape=(frames, w), strides=(audio.strides[0] * h, audio.strides[0]))
    rms = np.sqrt((strided.astype("float32") ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    speech = db[db > np.percentile(db, 40)]
    threshold = min(floor_db, float(np.median(speech)) - 18.0) if speech.size else floor_db   # adapt to a noisy room / music bed
    quiet = db < threshold
    out: list[tuple[float, float]] = []
    start = None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            a, b = start * hop, i * hop
            if b - a >= min_seconds:
                out.append((round(a, 3), round(b, 3)))
            start = None
    if start is not None:
        a, b = start * hop, len(quiet) * hop
        if b - a >= min_seconds:
            out.append((round(a, 3), round(b, 3)))
    return out


class WhisperTranscriber:
    """faster-whisper with word timestamps and voice-activity detection. Runs on the CPU of the box; no network after
    the model is downloaded once into `download_root`."""

    name = "whisper"

    def __init__(self, model_size: str = "large-v3-turbo", *, device: str = "auto", compute_type: str = "int8",
                 language: str | None = None, download_root: Path | None = None, beam_size: int = 5, threads: int = 0):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language or None
        self.download_root = str(download_root) if download_root else None
        self.beam_size = beam_size
        self.threads = threads

    @property
    def model(self):
        key = (self.model_size, self.device, self.compute_type, self.download_root, self.threads)
        if key not in _MODELS:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:   # the package is optional: everything else works without it
                raise TranscribeError("faster-whisper is not installed - pip install faster-whisper (or set TRANSCRIBE_PROVIDER=none)") from exc
            started = time.monotonic()
            _MODELS[key] = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type,
                                        download_root=self.download_root, cpu_threads=self.threads)
            log.info("whisper model %s (%s/%s) loaded in %.1fs", self.model_size, self.device, self.compute_type, time.monotonic() - started)
        return _MODELS[key]

    def transcribe(self, media: Path, *, job_id: str = "", language: str | None = None, segments=None) -> Transcript:
        started = time.monotonic()
        audio = read_audio(Path(media))
        silences = find_silences(audio)
        it, info = self.model.transcribe(
            audio, language=language or self.language, beam_size=self.beam_size, word_timestamps=True,
            vad_filter=False,                      # VAD squeezes the silences out of the timeline; the cutter needs them
            condition_on_previous_text=False,      # stops one bad line from derailing the rest
        )
        words: list[Word] = []
        for seg in it:
            for w in (seg.words or []):
                text = (w.word or "").strip()
                if text:
                    words.append(Word(w=text, start=round(float(w.start), 3), end=round(float(w.end), 3), prob=round(float(w.probability or 1.0), 3)))
        sentences = sentences_from_words(words)
        if segments:
            attach_speakers(sentences, segments)
        t = Transcript(job_id=job_id, language=getattr(info, "language", None), model=f"whisper:{self.model_size}",
                       seconds=round(words[-1].end, 3) if words else 0.0, sentences=sentences, silences=silences)
        log.info("transcribed %s: %d words / %d sentences in %.1fs (%.1fx realtime)", Path(media).name, len(words), len(sentences),
                 time.monotonic() - started, (t.seconds / max(0.1, time.monotonic() - started)))
        return t


class MockTranscriber:
    """Words derived from the analysis transcript by splitting each segment evenly - no model, no network.
    Good enough to exercise every path (sentences, snapping, multi-part cuts); never used for a real cut."""

    name = "mock"
    model_size = "mock"

    def transcribe(self, media: Path, *, job_id: str = "", language: str | None = None, segments=None) -> Transcript:
        from services.block_engine.media import ts_to_seconds

        words: list[Word] = []
        for seg in segments or []:
            a, b = ts_to_seconds(getattr(seg, "start_time", None)), ts_to_seconds(getattr(seg, "end_time", None))
            if a is None or b is None:
                continue
            a, b = float(a), float(max(b, a + 0.5))
            tokens = (getattr(seg, "text", "") or "").split()
            if not tokens:
                continue
            step = (b - a) / len(tokens)
            for i, tok in enumerate(tokens):
                words.append(Word(w=tok, start=round(a + i * step, 3), end=round(a + (i + 1) * step - 0.01, 3), prob=0.5))
        sentences = sentences_from_words(words, max_gap=0.8)
        if segments:
            attach_speakers(sentences, segments)
        return Transcript(job_id=job_id, language="mock", model="mock", seconds=round(words[-1].end, 3) if words else 0.0, sentences=sentences)


def build_transcriber(settings):
    """The configured word-timing provider, or None when precise cutting is switched off."""
    provider = (getattr(settings, "transcribe_provider", "") or "none").lower()
    if provider in ("", "none", "off"):
        return None
    if provider == "mock":
        return MockTranscriber()
    if provider == "whisper":
        return WhisperTranscriber(settings.whisper_model, device=settings.whisper_device, compute_type=settings.whisper_compute_type,
                                  language=settings.whisper_language or None, download_root=settings.models_dir,
                                  threads=settings.whisper_threads)
    log.warning("unknown TRANSCRIBE_PROVIDER=%s - precise cutting stays off", provider)
    return None

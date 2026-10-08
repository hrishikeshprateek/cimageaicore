"""Word-level transcripts: what was said, and exactly when.

Gemini's analysis gives meaning (speakers, quotes, topics) with timestamps that are good to a second or two; that is why
a reel cut from it clips syllables. This module measures the timing instead - local Whisper with word timestamps - and
exposes the sentence boundaries and the silences between them, so a cut can land where nobody is speaking.
"""
from __future__ import annotations

from services.transcribe.model import Sentence, Transcript, Word, attach_speakers, sentences_from_words
from services.transcribe.providers import MockTranscriber, TranscribeError, WhisperTranscriber, build_transcriber

__all__ = ["Sentence", "Transcript", "Word", "attach_speakers", "sentences_from_words",
           "MockTranscriber", "TranscribeError", "WhisperTranscriber", "build_transcriber"]

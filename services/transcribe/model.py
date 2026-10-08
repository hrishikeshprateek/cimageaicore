"""The transcript as the cutter needs it: words with real times, sentences, and the silence between them."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field

# A sentence ends here. Devanagari danda, and the usual Latin stops; a comma never ends a sentence.
_ENDERS = "।॥.?!…"
_SENTENCE_END = re.compile(rf"[{re.escape(_ENDERS)}]+[\"'”’)\]]*$")
# Words that must not be left dangling at the end of a cut, or lead it - the clip would sound unfinished.
TRAILING_STOPWORDS = {"और", "लेकिन", "क्योंकि", "तो", "कि", "जो", "पर", "फिर", "अगर", "या",
                      "and", "but", "because", "so", "that", "which", "if", "or", "then", "when", "while", "with", "the", "a", "an"}
LEADING_FILLERS = {"तो", "अच्छा", "हाँ", "हां", "मतलब", "यानी", "umm", "um", "uh", "ah", "so", "ok", "okay", "yeah", "right", "like"}


class Word(BaseModel):
    w: str
    start: float
    end: float
    prob: float = 1.0


class Sentence(BaseModel):
    i: int
    text: str
    start: float
    end: float
    speaker: str | None = None
    words: list[Word] = Field(default_factory=list)

    @property
    def seconds(self) -> float:
        return round(self.end - self.start, 3)

    def ends_open(self) -> bool:
        """True when the sentence trails off on a conjunction - cutting here sounds interrupted."""
        last = (self.words[-1].w if self.words else self.text.split()[-1] if self.text.split() else "").strip(_ENDERS + ",;:\"'")
        return last.lower() in TRAILING_STOPWORDS

    def starts_with_filler(self) -> bool:
        first = (self.words[0].w if self.words else self.text.split()[0] if self.text.split() else "").strip(_ENDERS + ",;:\"'")
        return first.lower() in LEADING_FILLERS


class Transcript(BaseModel):
    job_id: str
    language: str | None = None
    model: str = ""
    seconds: float = 0.0
    sentences: list[Sentence] = Field(default_factory=list)
    silences: list[tuple[float, float]] = Field(default_factory=list, description="measured quiet spans - where a cut can land cleanly")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ lookups
    def words(self) -> list[Word]:
        return [w for s in self.sentences for w in s.words]

    def text(self) -> str:
        return " ".join(s.text for s in self.sentences)

    def between(self, a: float, b: float) -> list[Sentence]:
        """Sentences that overlap [a, b] at all."""
        return [s for s in self.sentences if s.end > a and s.start < b]

    def inside(self, a: float, b: float) -> list[Sentence]:
        """Sentences fully inside [a, b] - what a cut actually contains."""
        return [s for s in self.sentences if s.start >= a - 0.05 and s.end <= b + 0.05]

    def at(self, t: float) -> Sentence | None:
        for s in self.sentences:
            if s.start - 0.25 <= t <= s.end + 0.25:
                return s
        return min(self.sentences, key=lambda s: min(abs(s.start - t), abs(s.end - t)), default=None)

    # ------------------------------------------------------------------ edges
    def gap_before(self, s: Sentence) -> tuple[float, float]:
        """The silence in front of a sentence: (start of the gap, the sentence's first word)."""
        prev = next((p for p in reversed(self.sentences) if p.end <= s.start + 0.01 and p is not s), None)
        return (prev.end if prev else max(0.0, s.start - 1.0)), s.start

    def gap_after(self, s: Sentence) -> tuple[float, float]:
        nxt = next((n for n in self.sentences if n.start >= s.end - 0.01 and n is not s), None)
        return s.end, (nxt.start if nxt else min(self.seconds or s.end + 1.0, s.end + 1.0))

    def silence_at(self, t: float, *, reach: float = 0.4) -> tuple[float, float] | None:
        """The measured quiet span around `t`, if there is one within `reach`."""
        best = None
        for a, b in self.silences:
            if a - reach <= t <= b + reach:
                d = 0.0 if a <= t <= b else min(abs(a - t), abs(b - t))
                if best is None or d < best[0]:
                    best = (d, (a, b))
        return best[1] if best else None

    def snap_in(self, t: float, *, pad: float = 0.1, max_shift: float = 6.0) -> float:
        """Move a proposed start onto the start of the sentence it lands in, then into the quiet just before the voice."""
        s = self.at(t)
        at = s.start if (s is not None and abs(s.start - t) <= max_shift) else t
        quiet = self.silence_at(at)
        if quiet:
            a, b = quiet
            return round(max(0.0, max(a + 0.03, min(b - 0.02, at - pad))), 3)   # just inside the quiet, never into the previous word
        return round(max(0.0, at - 0.06), 3)

    def snap_out(self, t: float, *, pad: float = 0.16, max_shift: float = 8.0) -> float:
        """Move a proposed end onto the end of the sentence it lands in, then into the quiet after the last word."""
        s = self.at(t)
        at = s.end if (s is not None and abs(s.end - t) <= max_shift) else t
        quiet = self.silence_at(at)
        if quiet:
            a, b = quiet
            return round(min(self.seconds or b, min(b - 0.02, max(a + 0.03, at + pad))), 3)
        return round(at + 0.08, 3)

    def window_for(self, first: int, last: int, *, pad_in: float = 0.1, pad_out: float = 0.16) -> tuple[float, float]:
        """The exact in/out for a run of sentences, placed in the silences on either side."""
        a = next(s for s in self.sentences if s.i == first)
        b = next(s for s in self.sentences if s.i == last)
        return self.snap_in(a.start, pad=pad_in), self.snap_out(b.end, pad=pad_out)


def sentences_from_words(words: list[Word], *, max_gap: float = 0.45, max_seconds: float = 9.0, min_words: int = 2) -> list[Sentence]:
    """Group words into sentences: punctuation first, then a long pause, then a hard length cap."""
    out: list[Sentence] = []
    buf: list[Word] = []

    def flush() -> None:
        if not buf:
            return
        out.append(Sentence(i=len(out), text=" ".join(w.w.strip() for w in buf).strip(), start=round(buf[0].start, 3), end=round(buf[-1].end, 3), words=list(buf)))
        buf.clear()

    def flush_long() -> None:
        """A run with no punctuation and no real pause still has to be split: use its widest gap, not an arbitrary word."""
        if len(buf) < 2 * min_words:
            flush()
            return
        gaps = [(buf[i + 1].start - buf[i].end, i) for i in range(min_words - 1, len(buf) - min_words)]
        if not gaps:
            flush()
            return
        _, at = max(gaps)
        head, tail = buf[: at + 1], buf[at + 1:]
        buf[:] = head
        flush()
        buf.extend(tail)

    for i, w in enumerate(words):
        buf.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends = bool(_SENTENCE_END.search(w.w.strip()))
        pause = (nxt.start - w.end) if nxt else 99.0
        if (ends and len(buf) >= min_words) or pause >= max_gap:
            flush()
        elif buf and (w.end - buf[0].start) >= max_seconds:
            flush_long()
        if nxt is None:
            flush()
    return out


def attach_speakers(sentences: list[Sentence], segments) -> list[Sentence]:
    """Carry the speaker names from the analysis transcript onto the measured sentences, by time overlap."""
    from services.block_engine.media import ts_to_seconds

    spans = []
    for seg in segments or []:
        a, b = ts_to_seconds(getattr(seg, "start_time", None)), ts_to_seconds(getattr(seg, "end_time", None))
        if a is None or b is None:
            continue
        spans.append((float(a), float(max(b, a + 0.5)), getattr(seg, "speaker", None)))
    for s in sentences:
        best, score = None, 0.0
        for a, b, who in spans:
            overlap = max(0.0, min(s.end, b) - max(s.start, a))
            if who and overlap > score:
                best, score = who, overlap
        s.speaker = best
    return sentences

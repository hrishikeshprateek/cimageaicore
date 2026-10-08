"""Word-level transcripts: sentence boundaries, measured silences and the snapping a precise cut depends on."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from services.transcribe import MockTranscriber, Transcript, Word, sentences_from_words
from services.transcribe.model import Sentence, attach_speakers
from services.transcribe.providers import find_silences
from tests.test_content import pg_app  # noqa: F401 - fixture
from tests.test_pg_store import test_db_url  # noqa: F401 - fixture


def _words(spec: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(w=w, start=a, end=b) for w, a, b in spec]


def test_sentences_split_on_punctuation_then_pauses_then_length():
    words = _words([("हम", 0.0, 0.3), ("यहाँ", 0.35, 0.8), ("हैं।", 0.85, 1.3),          # danda ends it
                    ("फिर", 2.4, 2.7), ("चलते", 2.75, 3.1), ("हैं", 3.15, 3.5),           # a 1.1s pause opened a new one
                    ("और", 3.6, 3.8), ("फिर", 3.85, 4.1)])
    ss = sentences_from_words(words)
    assert [s.text for s in ss][0] == "हम यहाँ हैं।"
    assert len(ss) == 2 and ss[1].start == 2.4
    assert ss[1].ends_open()                      # trails off on "फिर" - cutting here sounds interrupted


def test_a_long_run_without_punctuation_breaks_at_its_widest_pause():
    words = _words([(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(30)])
    words[14] = Word(w="w14", start=7.0, end=7.4)
    words[15] = Word(w="w15", start=7.75, end=8.1)       # the widest gap in the run
    ss = sentences_from_words(words, max_gap=0.5, max_seconds=8.0)
    assert len(ss) >= 2 and any(abs(s.start - 7.75) < 0.01 for s in ss)
    assert all(s.seconds <= 9.5 for s in ss)


def test_silences_are_found_in_the_waveform():
    import numpy as np

    sr = 16000
    speech = (np.random.default_rng(1).random(sr * 2).astype("float32") - 0.5) * 0.4   # 2s of "speech"
    quiet = np.zeros(sr, dtype="float32")                                              # 1s of silence
    audio = np.concatenate([speech, quiet, speech])
    spans = find_silences(audio, sample_rate=sr)
    assert spans and any(a <= 2.3 and b >= 2.7 for a, b in spans)                      # the quiet second is found
    assert not any(a < 1.0 for a, b in spans)                                          # no silence inside the speech


def test_snapping_puts_the_cut_inside_the_quiet_not_on_the_word():
    t = Transcript(job_id="j", seconds=20.0, silences=[(4.80, 5.20), (9.60, 10.10)], sentences=[
        Sentence(i=0, text="one", start=0.5, end=4.85, words=_words([("one", 0.5, 4.85)])),
        Sentence(i=1, text="two", start=5.15, end=9.70, words=_words([("two", 5.15, 9.70)])),
        Sentence(i=2, text="three", start=10.05, end=14.0, words=_words([("three", 10.05, 14.0)])),
    ])
    a, b = t.window_for(1, 1)
    assert 4.80 < a < 5.15 and 9.70 < b < 10.10                 # both edges land in the measured quiet
    assert t.snap_in(5.4) == a and t.snap_out(9.4) == b          # a request a little off still snaps to the sentence
    assert t.inside(a, b) == [t.sentences[1]] and len(t.between(a, b)) == 1
    assert t.snap_in(0.0) >= 0.0 and t.snap_out(19.9) <= t.seconds


def test_speakers_come_from_the_analysis_transcript():
    class Seg:
        def __init__(self, a, b, who):
            self.start_time, self.end_time, self.speaker = a, b, who

    ss = [Sentence(i=0, text="a", start=1.0, end=4.0), Sentence(i=1, text="b", start=12.0, end=14.0)]
    attach_speakers(ss, [Seg("00:00:00", "00:00:06", "Dr. Neeraj Agrawal"), Seg("00:00:10", "00:00:20", "Student")])
    assert [s.speaker for s in ss] == ["Dr. Neeraj Agrawal", "Student"]


def test_mock_transcriber_derives_words_from_the_analysis(tiny_video: Path):
    class Seg:
        def __init__(self, a, b, text):
            self.start_time, self.end_time, self.text, self.speaker, self.language = a, b, text, "Speaker 1", "hi"

    t = MockTranscriber().transcribe(tiny_video, job_id="j", segments=[Seg("00:00:00", "00:00:04", "एक दो तीन चार।"), Seg("00:00:05", "00:00:09", "पाँच छह सात आठ।")])
    assert t.sentences and t.seconds > 0 and all(s.words for s in t.sentences)
    assert t.sentences[0].speaker == "Speaker 1"
    assert all(w.end > w.start for s in t.sentences for w in s.words)


def test_transcript_through_the_api(pg_app, tiny_video):  # noqa: F811
    client = pg_app
    with tiny_video.open("rb") as f:
        job_id = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")}).json()["job_id"]
    for _ in range(150):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("CONTENT_CANDIDATE", "INDEXED", "BLOCKS_COMPLETE", "FAILED"):
            break
        time.sleep(0.1)
    assert job["state"] != "FAILED"

    # the mock transcriber runs on its own right after the analysis
    for _ in range(100):
        d = client.get(f"/api/v1/jobs/{job_id}/transcript").json()
        if d["ready"]:
            break
        time.sleep(0.1)
    assert d["ready"] and d["sentences"], d
    first = d["sentences"][0]
    assert {"i", "start", "end", "text", "speaker", "ends_open"} <= set(first) and "words" not in first
    assert client.get(f"/api/v1/jobs/{job_id}/transcript?words=true").json()["sentences"][0]["words"]
    assert client.post(f"/api/v1/jobs/{job_id}/transcript").status_code == 202      # re-measure on demand
    assert client.post("/api/v1/jobs/nope/transcript").status_code == 404
    assert client.get("/api/v1/jobs/nope/transcript").json()["ready"] is False
    assert client.delete(f"/api/v1/jobs/{job_id}/transcript").json()["deleted"] in (True, False)

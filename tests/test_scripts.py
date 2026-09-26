"""Script writer: an idea (typed or dictated) -> evidence from our own videos -> a timed, shootable script."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from agents.script_agent.agent import ScriptAgent, scene_plan, word_budget
from agents.script_agent.schemas import Scene, ScriptV1
from services.ai_gateway.mock import MockProvider
from services.retrieval.retriever import EvidenceBlock, EvidencePack
from tests.test_content import pg_app  # noqa: F401 - fixture
from tests.test_pg_store import test_db_url  # noqa: F401 - fixture


class _Retriever:
    def __init__(self, blocks):
        self.blocks = blocks
        self.asked = None

    def evidence_for(self, queries, job_id=None, **kw):
        self.asked = (queries, job_id, kw)
        return EvidencePack(queries=queries, job_id=job_id, blocks=self.blocks, sources=[{"media_id": "m1", "source_name": "Open day", "blocks": len(self.blocks)}])


class _Pic:
    def __init__(self, pid, desc):
        self.id, self.description = pid, desc

    def offer_line(self):
        return f"- [img={self.id}] {self.description}"


def _blocks(n=4):
    return [EvidenceBlock(block_id=f"job1:quote:{i}", block_type="quote", timestamp=f"00:0{i}:00", text=f"Quote {i} about placements.",
                          source_name="Open day 2026", media_id="m1", score=0.9 - i / 10) for i in range(n)]


def test_length_maths_matches_what_a_presenter_can_say():
    assert scene_plan(30) == (5, 6.0) and scene_plan(15)[0] >= 2
    assert scene_plan(600)[0] <= 40 and sum([scene_plan(600)[1]] * scene_plan(600)[0]) == pytest.approx(600, abs=15)
    assert word_budget(60, "hi") == 126 and word_budget(60, "en") == 150   # Hindi carries more per word, so fewer of them fit


def test_script_is_grounded_timed_and_in_the_asked_language(tmp_path: Path):
    r = _Retriever(_blocks())
    pics = [_Pic("img1", "students in the lab"), _Pic("ghost", "not offered")]
    agent = ScriptAgent(MockProvider(delay_seconds=0), r, institution_context="CIMAGE Patna")
    res = agent.write("placement ke bare me ek reel banao", language="hi", seconds=30, style="viral_reel",
                      images_for=lambda ev: pics[:1])

    s = res.script
    assert s.language == "hi" and s.scenes and [sc.n for sc in s.scenes] == list(range(1, len(s.scenes) + 1))
    assert sum(sc.seconds for sc in s.scenes) == pytest.approx(30, abs=6)          # fits the length that was asked for
    assert all(i in {b.block_id for b in r.blocks} for sc in s.scenes for i in sc.evidence_ids)   # only real evidence ids survive
    assert any(sc.b_roll_block_id for sc in s.scenes) and any(sc.b_roll_image_id == "img1" for sc in s.scenes)
    assert res.prompt_version == "script_v1" and res.target_seconds == 30 and res.words > 0
    assert r.asked[0][0].startswith("placement")                                    # the spoken idea is what we search for


def test_invented_ids_are_dropped_and_reported():
    r = _Retriever(_blocks(2))
    agent = ScriptAgent(MockProvider(delay_seconds=0), r)
    bad = ScriptV1(title="A test script", hook="short hook", scenes=[
        Scene(n=1, seconds=10, visual="wide shot", voiceover="line [id=job1:quote:0]", evidence_ids=["job1:quote:0", "made:up:1"],
              b_roll_block_id="made:up:2", b_roll_image_id="nope"),
        Scene(n=9, seconds=10, visual="close up", voiceover="another line"),
    ])
    warnings = agent._ground(bad, EvidencePack(queries=["q"], blocks=r.blocks), [], 20, word_budget(20, "hi"))
    assert bad.scenes[0].evidence_ids == ["job1:quote:0"] and bad.scenes[0].b_roll_block_id is None and bad.scenes[0].b_roll_image_id is None
    assert "[id=" not in bad.scenes[0].voiceover                                    # markers never reach the presenter
    assert [sc.n for sc in bad.scenes] == [1, 2]
    assert any("not in the library" in w for w in warnings) and any("shoot it instead" in w for w in warnings)


def test_no_evidence_is_an_error_not_an_invented_script():
    agent = ScriptAgent(MockProvider(delay_seconds=0), _Retriever([]))
    with pytest.raises(Exception, match="no evidence"):
        agent.write("something we never filmed", seconds=30)


def test_script_through_the_api_end_to_end(pg_app, tiny_video):  # noqa: F811
    client = pg_app
    with tiny_video.open("rb") as f:
        job_id = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")}).json()["job_id"]
    for _ in range(120):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("CONTENT_CANDIDATE", "INDEXED", "FAILED"):
            break
        time.sleep(0.1)
    assert job["state"] != "FAILED"

    opts = client.get("/api/v1/script-options").json()
    assert opts["available"] and {o["key"] for o in opts["languages"]} == {"hi", "en", "hinglish"} and opts["dictation"]["provider"] == "browser"
    assert any(l["seconds"] == 30 for l in opts["lengths"]) and any(s["key"] == "viral_reel" for s in opts["styles"])

    r = client.post("/api/v1/scripts", json={"idea": "campus ka ek chhota reel banao", "language": "hi", "seconds": 30, "spoken": True, "job_id": job_id})
    assert r.status_code == 202 and r.json()["status"] == "generating" and r.json()["spoken"] is True
    sid = r.json()["id"]
    for _ in range(150):
        s = client.get(f"/api/v1/scripts/{sid}").json()
        if s["status"] != "generating":
            break
        time.sleep(0.1)
    assert s["status"] == "new", s.get("error")
    assert s["scenes"] and s["title"] and s["hook"] and s["planned_seconds"] == pytest.approx(30, abs=6) and s["words"] > 0
    assert s["evidence"]["blocks"] and all(sc["n"] == i + 1 for i, sc in enumerate(s["scenes"]))

    # the editor rewrites a line: previous scenes are kept, the version bumps, the script goes back to review
    scenes = s["scenes"]
    scenes[0]["voiceover"] = "नया hook line"
    e = client.put(f"/api/v1/scripts/{sid}", json={"title": "Campus reel", "scenes": scenes}).json()
    assert e["version"] == s["version"] + 1 and e["status"] == "in_review" and e["scenes"][0]["voiceover"] == "नया hook line"
    assert [v["edited_by"] for v in client.get(f"/api/v1/scripts/{sid}/versions").json()][-1] == "editor"

    # teleprompter / shooting script downloads
    md = client.get(f"/api/v1/scripts/{sid}/export").text
    assert md.startswith("# Campus reel") and "## Scene 1" in md
    assert "नया hook line" in client.get(f"/api/v1/scripts/{sid}/export?format=txt").text

    assert client.post(f"/api/v1/scripts/{sid}/status", json={"status": "approved"}).json()["status"] == "approved"
    assert [x["id"] for x in client.get("/api/v1/scripts").json()] == [sid]
    assert client.get("/api/v1/scripts?status=rejected").json() == []
    assert client.post("/api/v1/scripts", json={"idea": "a reel about the library", "job_id": "nope"}).status_code == 404
    assert client.post("/api/v1/scripts", json={"idea": "no"}).status_code == 422       # an idea has to say something
    assert client.delete(f"/api/v1/scripts/{sid}").json()["deleted"] == sid
    assert client.get(f"/api/v1/scripts/{sid}").status_code == 404

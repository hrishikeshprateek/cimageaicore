import json
from pathlib import Path

from services.ai_gateway.base import RawModelOutput
from services.ai_gateway.mock import MockProvider
from services.block_engine.engine import BlockEngine, BlockValidationError
from services.block_engine.schemas import flatten_blocks
from services.block_engine.sources import from_upload


def test_mock_provider_end_to_end(tiny_video: Path):
    engine = BlockEngine(MockProvider(delay_seconds=0), institution_context="Test College")
    stages = []
    result = engine.analyze("job1", from_upload(tiny_video), lambda s, d: stages.append(s))
    assert stages == ["UPLOADED", "ANALYZING", "ANALYZING", "BLOCKS_PARTIAL"]  # main pass + people pass
    assert result.provider == "mock" and result.block_counts["transcript"] == 2
    assert result.source.sha256 and result.source.size_bytes > 0
    blocks = flatten_blocks(result)
    assert {b.block_type for b in blocks} >= {"video", "summary", "event", "person", "transcript", "quote"}
    assert all(b.text for b in blocks)


class _Flaky(MockProvider):
    """Returns broken JSON first, then valid JSON on repair."""

    def analyze_video(self, request, on_stage):
        good = super().analyze_video(request, on_stage)
        self._good = good.text
        return RawModelOutput(text='{"video": {"title": "broken"}', model=self.model)

    def repair_json(self, invalid_text, error, json_schema):
        assert "video" in error or "JSON" in error or "Expecting" in error
        return RawModelOutput(text=self._good, model=self.model, usage={"input_tokens": 10, "output_tokens": 5})


def test_engine_repairs_invalid_output(tiny_video: Path):
    engine = BlockEngine(_Flaky(delay_seconds=0))
    result = engine.analyze("job2", from_upload(tiny_video), lambda s, d: None)
    assert result.repaired is True
    assert result.usage.input_tokens == 10


class _Broken(MockProvider):
    def analyze_video(self, request, on_stage):
        return RawModelOutput(text="not json at all", model=self.model)


def test_engine_raises_after_failed_repair(tiny_video: Path):
    import pytest

    engine = BlockEngine(_Broken(delay_seconds=0))
    with pytest.raises(BlockValidationError):
        engine.analyze("job3", from_upload(tiny_video), lambda s, d: None)


def test_coarse_transcript_warning():
    from services.block_engine.engine import BlockEngine
    from services.block_engine.schemas import VideoAnalysisV1
    from services.block_engine.sources import VideoSource
    from services.block_engine.schemas import SourceInfo

    mock = MockProvider(delay_seconds=0)
    raw = mock.analyze_video(
        type("R", (), {"video": type("V", (), {"name": "x.mp4", "path": None})(), "extra_passes": []})(), lambda s, d: None
    )
    analysis = VideoAnalysisV1.model_validate_json(raw.text)  # 2 segments
    long_src = VideoSource(info=SourceInfo(kind="upload", name="long.mp4", duration_seconds=3600))  # 2 segments for an hour
    warnings = BlockEngine._warnings(analysis, long_src)
    assert any("coarse" in w for w in warnings)
    short_src = VideoSource(info=SourceInfo(kind="upload", name="short.mp4", duration_seconds=40))
    assert not any("coarse" in w for w in BlockEngine._warnings(analysis, short_src))


def test_known_people_roster_enters_prompt(tmp_path):
    from services.block_engine.engine import BlockEngine, load_known_people

    roster = tmp_path / "people.txt"
    roster.write_text("# comment\nA Person — Director\n\nB Person — Dean\n")
    assert load_known_people(roster) == "- A Person — Director\n- B Person — Dean"
    assert load_known_people(tmp_path / "missing.txt") == "- none listed"
    engine = BlockEngine(MockProvider(delay_seconds=0), prompt_version="v2", known_people_file=roster)
    system = engine.system_template.format(institution_context="X", known_people=engine.known_people)
    assert "A Person — Director" in system and '`identified_by` to "recognised"' in system
    v1 = BlockEngine(MockProvider(delay_seconds=0), prompt_version="v1", known_people_file=roster)
    assert "{known_people}" not in v1.system_template and "roster" not in v1.system_template


def test_engine_retries_once_when_transcript_is_coarse(tiny_video):
    import json as _json
    from services.block_engine.schemas import SourceInfo
    from services.block_engine.sources import VideoSource

    class _Coarse(MockProvider):
        calls = 0

        def analyze_video(self, request, on_stage):
            self.calls += 1
            out = super().analyze_video(request, on_stage)
            if self.calls == 1:  # first answer: one giant segment
                d = _json.loads(out.text)
                d["transcript"] = [d["transcript"][0] | {"end_time": "00:10:00"}]
                out.text = _json.dumps(d)
            return out

    prov = _Coarse(delay_seconds=0)
    engine = BlockEngine(prov)
    src = VideoSource(info=SourceInfo(kind="upload", name="ten_min.mp4", duration_seconds=600), path=tiny_video)
    stages = []
    result = engine.analyze("j", src, lambda s, d: stages.append((s, d)))
    assert prov.calls == 2 and result.block_counts["transcript"] == 2
    assert any(d.get("retry") == "transcript too coarse" for s, d in stages)
    assert any("coarse" in w for w in result.warnings)  # 2 segments for 10 min is still coarse -> flagged for review


def test_people_pass_merges_into_analysis(tiny_video):
    engine = BlockEngine(MockProvider(delay_seconds=0))  # people_v1 on by default
    stages = []
    result = engine.analyze("j", from_upload(tiny_video), lambda s, d: stages.append((s, d)))
    names = [(p.name, p.identified_by) for p in result.analysis.people]
    assert ("Voiceover", "unnamed") in names and ("[MOCK] Known Person", "recognised") in names
    assert ("Speaker 1", None) in names  # main-pass person kept because the people pass did not list it
    assert any(d.get("pass") == "people" for s, d in stages)
    assert result.block_counts["people"] == 3


def test_people_pass_can_be_disabled(tiny_video):
    engine = BlockEngine(MockProvider(delay_seconds=0), people_pass_version=None)
    result = engine.analyze("j", from_upload(tiny_video), lambda s, d: None)
    assert [p.name for p in result.analysis.people] == ["Speaker 1"]

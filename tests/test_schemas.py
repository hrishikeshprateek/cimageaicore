import json

from services.block_engine.schemas import VideoAnalysisV1, provider_json_schema


def test_provider_schema_is_gemini_compatible():
    s = provider_json_schema(VideoAnalysisV1)
    txt = json.dumps(s)
    assert "$ref" not in txt and "$defs" not in txt and "anyOf" not in txt
    assert "default" not in s["properties"]["video"]
    # property named `title` must survive (it's a field, not the schema keyword)
    assert "title" in s["properties"]["video"]["properties"]
    assert s["properties"]["events"]["items"]["properties"]["date"]["type"] == ["string", "null"]
    assert "enum" in s["properties"]["key_moments"]["items"]["properties"]["importance"]
    assert set(s["required"]) == set(VideoAnalysisV1.model_fields)


def test_confidence_bounds_are_enforced():
    from pydantic import ValidationError
    import pytest
    from services.block_engine.schemas import EventBlock

    with pytest.raises(ValidationError):
        EventBlock(name="x", date=None, venue=None, organizer=None, description="d", confidence=1.7)


def test_schema_1_1_is_backward_compatible_and_gemini_safe():
    import json
    from pathlib import Path
    from services.block_engine.schemas import AnalysisResult, PersonBlock

    # a 1.0 result (no identified_by) still validates
    old = Path("data/analyses/68ad4ad8f78a.json")
    if old.exists():
        r = AnalysisResult.model_validate_json(old.read_text())
        assert r.analysis.people[0].identified_by is None
    s = provider_json_schema(VideoAnalysisV1)
    prop = s["properties"]["people"]["items"]["properties"]["identified_by"]
    assert prop["type"] == ["string", "null"] and "recognised" in prop["enum"] and "default" not in prop
    assert "identified_by" not in s["properties"]["people"]["items"]["required"]
    assert PersonBlock(name="x", role=None, context="c", timestamps=[], identified_by="recognised", confidence=0.7).identified_by == "recognised"


def test_timestamps_are_normalised_to_hhmmss():
    from services.block_engine.schemas import normalise_timestamps

    a = VideoAnalysisV1.model_validate({
        "video": {"title": "t", "video_type": "other", "language": "en", "description": "d", "observed_duration": "4:35"},
        "events": [], "people": [{"name": "x", "role": None, "context": "c", "timestamps": ["1:02:03", "35"], "identified_by": None, "confidence": 0.5}],
        "transcript": [{"speaker": "s", "start_time": "0:00", "end_time": "12:34", "text": "t", "language": "en"}],
        "topics": [], "key_moments": [{"timestamp": "02:58", "description": "d", "importance": "low"}],
        "quotes": [], "media": [], "summary": {"short_summary": "s", "detailed_summary": "d", "key_points": []}, "content_opportunities": [],
    })
    bad = normalise_timestamps(a)
    assert a.video.observed_duration == "00:04:35" and a.transcript[0].end_time == "00:12:34" and a.key_moments[0].timestamp == "00:02:58"
    assert a.people[0].timestamps[0] == "01:02:03" and bad == ["35"]

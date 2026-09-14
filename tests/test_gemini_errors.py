"""Retry/backoff behaviour of the Gemini provider, without network."""
from unittest.mock import MagicMock, patch

import pytest

from services.ai_gateway.base import ProviderError
from services.ai_gateway.gemini import GeminiProvider, _retry_after, _status_of


class _RateLimit(Exception):
    status_code = 429


class _Bad(Exception):
    status_code = 400


class _FilesErr(Exception):
    code = 503


def test_status_extraction_covers_both_sdk_error_families():
    assert _status_of(_RateLimit("x")) == 429
    assert _status_of(_FilesErr("x")) == 503
    assert _status_of(ValueError("x")) is None


def test_retry_after_honours_google_hint():
    assert _retry_after(_RateLimit("... Please retry in 23.8s."), 1) == pytest.approx(24.8)
    assert _retry_after(_RateLimit("no hint"), 2) == 4.0


@patch("services.ai_gateway.gemini.time.sleep")
@patch("services.ai_gateway.gemini.genai.Client")
def test_retries_on_429_then_succeeds(client_cls, sleep):
    client = client_cls.return_value
    ok = MagicMock(status="completed", output_text='{"a":1}', usage=None, model="m", id="i")
    client.interactions.create.side_effect = [_RateLimit("retry in 2s"), ok]
    p = GeminiProvider("key", "m")
    out = p.repair_json("{}", "err", {"type": "object"})
    assert out.text == '{"a":1}' and client.interactions.create.call_count == 2
    sleep.assert_called_once_with(pytest.approx(3.0))


@patch("services.ai_gateway.gemini.genai.Client")
def test_non_transient_error_is_not_retried(client_cls):
    client_cls.return_value.interactions.create.side_effect = _Bad("'minimal' is not supported")
    with pytest.raises(ProviderError, match="400"):
        GeminiProvider("key", "m").repair_json("{}", "err", {})
    assert client_cls.return_value.interactions.create.call_count == 1


@patch("services.ai_gateway.gemini.time.sleep")
@patch("services.ai_gateway.gemini.genai.Client")
def test_upload_poll_survives_dropped_connection(client_cls, sleep):
    client = client_cls.return_value
    processing = MagicMock(name="f", uri="u", mime_type="video/mp4", size_bytes=1)
    processing.name = "files/x"
    processing.state.name = "PROCESSING"
    active = MagicMock(uri="u", mime_type="video/mp4", size_bytes=1)
    active.name = "files/x"
    active.state.name = "ACTIVE"
    client.files.upload.return_value = processing
    client.files.get.side_effect = [ConnectionError("read timed out"), active]
    stages = []
    f = GeminiProvider("key", "m")._upload("/tmp/x.mp4", "x.mp4", "video/mp4", lambda s, d: stages.append((s, d)))
    assert f is active and stages[0][0] == "UPLOADED" and "upload_seconds" in stages[0][1]
    # tiny polls carry a short timeout, uploads a long one
    assert client.files.get.call_args.kwargs["config"]["http_options"]["timeout"] == 30_000
    assert client.files.upload.call_args.kwargs["config"]["http_options"]["timeout"] == 600_000


@patch("services.ai_gateway.gemini.time.sleep")
@patch("services.ai_gateway.gemini.genai.Client")
def test_upload_poll_gives_up_after_repeated_failures(client_cls, sleep):
    client = client_cls.return_value
    processing = MagicMock(uri="u", mime_type="video/mp4", size_bytes=1)
    processing.name = "files/x"
    processing.state.name = "PROCESSING"
    client.files.upload.return_value = processing
    client.files.get.side_effect = ConnectionError("dead")
    with pytest.raises(ProviderError, match="poll failed repeatedly"):
        GeminiProvider("key", "m")._upload("/tmp/x.mp4", "x.mp4", "video/mp4", lambda s, d: None)

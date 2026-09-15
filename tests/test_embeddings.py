import json
import math

from services.ai_gateway.embeddings import MockEmbedder


def test_mock_embedder_is_deterministic_normalised_and_similarity_aware():
    e = MockEmbedder(dimensions=256)
    a, b = e.embed_documents([("t", "students discussed placements and careers"), ("t", "robotics lab with humanoid robots")])
    assert len(a) == 256 and abs(math.sqrt(sum(x * x for x in a)) - 1) < 1e-6
    assert a == e.embed_documents([("t", "students discussed placements and careers")])[0]
    q = e.embed_query("placements careers students")
    dot = lambda u, v: sum(x * y for x, y in zip(u, v))
    assert dot(q, a) > dot(q, b)


def test_ollama_embedder_prompts_batches_and_errors(monkeypatch):
    """Ollama's /api/embed, exercised through a mock transport: prompt formats, batching, dimension guard, 404 hint, retry."""
    import httpx
    import pytest

    from services.ai_gateway.embeddings import EmbeddingError, OllamaEmbedder

    calls: list[dict] = []
    state = {"fail_first": 0, "dims": 4, "missing": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if state["missing"]:
            return httpx.Response(404, json={"error": "model 'x' not found, try pulling it first"})
        if state["fail_first"] > 0:
            state["fail_first"] -= 1
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"embeddings": [[1.0, 2.0, 2.0, 0.0][: state["dims"]] for _ in body["input"]]})

    e = OllamaEmbedder("http://ollama.test", "embeddinggemma", dimensions=4, batch_size=2, transport=httpx.MockTransport(handler))
    vecs = e.embed_documents([("Campus tour", "placements at Wipro"), ("Campus tour", "library"), ("Talk", "rumours")])
    assert len(vecs) == 3 and len(calls) == 2 and calls[0]["input"] == ["title: Campus tour | text: placements at Wipro", "title: Campus tour | text: library"]
    assert calls[0]["model"] == "embeddinggemma" and calls[0]["truncate"] is True
    assert abs(math.sqrt(sum(x * x for x in vecs[0])) - 1.0) < 1e-9          # normalised
    q = e.embed_query("who spoke about placements?")
    assert calls[-1]["input"] == ["task: search result | query: who spoke about placements?"] and len(q) == 4

    state["dims"] = 3
    with pytest.raises(EmbeddingError, match="EMBEDDING_DIMENSIONS=4"):
        e.embed_query("x")
    state["dims"], state["missing"] = 4, True
    with pytest.raises(EmbeddingError, match="ollama pull embeddinggemma"):
        e.embed_query("x")
    state["missing"], state["fail_first"] = False, 1
    import services.ai_gateway.embeddings as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)                    # no real backoff in tests
    assert len(e.embed_query("retry")) == 4                                    # one transport failure, then success


def test_build_embedder_picks_provider_and_default_model():
    from types import SimpleNamespace

    from services.ai_gateway.embeddings import GeminiEmbedder, OllamaEmbedder, build_embedder

    base = dict(gemini_api_key="", embedding_model="auto", embedding_dimensions=768, embedding_batch_size=8, ollama_url="http://o:11434", ollama_timeout_seconds=5)
    assert build_embedder(SimpleNamespace(embedding_provider="auto", **base)).name == "mock"
    o = build_embedder(SimpleNamespace(embedding_provider="ollama", **base))
    assert isinstance(o, OllamaEmbedder) and o.model == "embeddinggemma" and o.url == "http://o:11434" and o.dimensions == 768
    o2 = build_embedder(SimpleNamespace(embedding_provider="ollama", **{**base, "embedding_model": "nomic-embed-text"}))
    assert o2.model == "nomic-embed-text"
    g = build_embedder(SimpleNamespace(embedding_provider="gemini", **{**base, "gemini_api_key": "k"}))
    assert isinstance(g, GeminiEmbedder) and g.model == "gemini-embedding-2"

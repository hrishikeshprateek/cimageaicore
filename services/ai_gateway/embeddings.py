"""Embedding providers behind one interface: Gemini Embedding 2 (API), EmbeddingGemma via Ollama (local), or a deterministic mock.

Documents are embedded as  "title: <title> | text: <content>"  and queries as
"task: search result | query: <q>"  - the formats Google documents for gemini-embedding-2 *and* EmbeddingGemma,
so the two are drop-in for each other (both 768-d). Vectors from different models are never comparable: the store
tracks `embedding_model` per block, re-embeds blocks from another model, and vector search only matches the current one.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from typing import Protocol

log = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str
    model: str
    dimensions: int

    def embed_documents(self, items: list[tuple[str, str]]) -> list[list[float]]: ...  # (title, text)

    def embed_query(self, text: str) -> list[float]: ...


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class MockEmbedder:
    """Bag-of-words hashed into `dimensions` buckets: similar texts get similar vectors,
    so retrieval can be tested offline. Not semantic."""

    name = "mock"

    def __init__(self, dimensions: int = 768, model: str = "mock-embed-v1"):
        self.dimensions = dimensions
        self.model = model

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dimensions
        for word in re.findall(r"\w+", text.lower()):
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            v[h % self.dimensions] += 1.0
        return _normalise(v)

    def embed_documents(self, items: list[tuple[str, str]]) -> list[list[float]]:
        return [self._vec(f"{title} {text}") for title, text in items]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class GeminiEmbedder:
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-embedding-2", dimensions: int = 768, *, batch_size: int = 32, timeout_seconds: float = 60, max_attempts: int = 3):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        from google import genai
        from google.genai import types as gt

        self._gt = gt
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.client = genai.Client(api_key=api_key, http_options=gt.HttpOptions(timeout=int(timeout_seconds * 1000)))

    def embed_documents(self, items: list[tuple[str, str]]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(items), self.batch_size):
            chunk = items[i : i + self.batch_size]
            out.extend(self._embed([f"title: {t} | text: {x}" for t, x in chunk]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"task: search result | query: {text}"])[0]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        from services.ai_gateway.gemini import _retry_after, _status_of, _TRANSIENT_CODES

        # each text wrapped as its own Content -> one vector per text (a bare list is aggregated into one)
        contents = [self._gt.Content(parts=[self._gt.Part(text=t)]) for t in texts]
        cfg = self._gt.EmbedContentConfig(output_dimensionality=self.dimensions)
        for attempt in range(1, self.max_attempts + 1):
            try:
                res = self.client.models.embed_content(model=self.model, contents=contents, config=cfg)
                vecs = [list(e.values) for e in res.embeddings]
                if len(vecs) != len(texts):
                    raise RuntimeError(f"embedding count mismatch: {len(vecs)} for {len(texts)} texts")
                return vecs
            except Exception as exc:  # noqa: BLE001
                code = _status_of(exc)
                if code not in _TRANSIENT_CODES or attempt == self.max_attempts:
                    raise
                wait = _retry_after(exc, attempt)
                log.warning("transient embedding error %s (attempt %d/%d), retrying in %.0fs", code, attempt, self.max_attempts, wait)
                time.sleep(wait)
        raise RuntimeError("unreachable")


class EmbeddingError(RuntimeError):
    pass


class OllamaEmbedder:
    """A local embedding model served by Ollama (default: EmbeddingGemma-300M, 768-d, 100+ languages incl. Hindi).

    No API cost and no internet needed at query time. Once per machine:  ollama pull embeddinggemma
    Ollama's /api/embed takes a list of inputs and returns one vector per input.
    """

    name = "ollama"

    def __init__(self, url: str = "http://localhost:11434", model: str = "embeddinggemma", dimensions: int = 768, *,
                 batch_size: int = 32, timeout_seconds: float = 120, max_attempts: int = 3, transport=None):
        import httpx

        self._httpx = httpx
        self.url = url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.client = httpx.Client(base_url=self.url, timeout=timeout_seconds, transport=transport)

    def embed_documents(self, items: list[tuple[str, str]]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(items), self.batch_size):
            chunk = items[i : i + self.batch_size]
            out.extend(self._embed([f"title: {t} | text: {x}" for t, x in chunk]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"task: search result | query: {text}"])[0]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        httpx = self._httpx
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self.client.post("/api/embed", json={"model": self.model, "input": texts, "truncate": True})
                if r.status_code == 404:
                    raise EmbeddingError(f"Ollama at {self.url} has no model '{self.model}' - run:  ollama pull {self.model}")
                if r.status_code >= 500:
                    raise httpx.TransportError(f"Ollama returned {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                vecs = r.json().get("embeddings") or []
                if len(vecs) != len(texts):
                    raise EmbeddingError(f"embedding count mismatch: {len(vecs)} for {len(texts)} texts")
                if vecs and len(vecs[0]) != self.dimensions:
                    raise EmbeddingError(f"'{self.model}' returns {len(vecs[0])}-d vectors but EMBEDDING_DIMENSIONS={self.dimensions} "
                                         "(the knowledge_blocks.embedding column); pick a model with matching dimensions")
                return [_normalise(v) for v in vecs]
            except httpx.TransportError as exc:   # daemon not up yet, model loading, connection reset
                if attempt == self.max_attempts:
                    raise EmbeddingError(f"Ollama unreachable at {self.url} ({exc}); is `ollama serve` running?") from exc
                wait = 2.0 * attempt
                log.warning("Ollama embedding attempt %d/%d failed (%s), retrying in %.0fs", attempt, self.max_attempts, exc, wait)
                time.sleep(wait)
        raise RuntimeError("unreachable")


DEFAULT_MODELS = {"gemini": "gemini-embedding-2", "ollama": "embeddinggemma", "mock": "mock-embed-v1"}


def build_embedder(settings) -> Embedder:
    kind = settings.embedding_provider
    if kind == "auto":
        kind = "gemini" if settings.gemini_api_key else "mock"
    model = settings.embedding_model if settings.embedding_model not in ("", "auto") else DEFAULT_MODELS[kind]
    if kind == "gemini":
        return GeminiEmbedder(settings.gemini_api_key, model, settings.embedding_dimensions, batch_size=settings.embedding_batch_size)
    if kind == "ollama":
        return OllamaEmbedder(settings.ollama_url, model, settings.embedding_dimensions, batch_size=settings.embedding_batch_size,
                              timeout_seconds=settings.ollama_timeout_seconds)
    return MockEmbedder(settings.embedding_dimensions, model)

"""Embedding providers behind one interface (Gemini Embedding 2, or a deterministic mock).

Documents are embedded as  "title: <title> | text: <content>"  and queries as
"task: search result | query: <q>"  - the formats Google documents for gemini-embedding-2.
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


def build_embedder(settings) -> Embedder:
    kind = settings.embedding_provider
    if kind == "auto":
        kind = "gemini" if settings.gemini_api_key else "mock"
    if kind == "gemini":
        return GeminiEmbedder(settings.gemini_api_key, settings.embedding_model, settings.embedding_dimensions, batch_size=settings.embedding_batch_size)
    return MockEmbedder(settings.embedding_dimensions)

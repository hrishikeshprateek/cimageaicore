"""Evidence retrieval for content agents: turns a brief into a bounded, source-referenced evidence pack."""
from __future__ import annotations

import logging

from typing import Any

from pydantic import BaseModel, Field

# Facts first, then colour; suggestions are never evidence.
_TYPE_PRIORITY = ["video", "summary", "event", "person", "quote", "key_moment", "topic", "transcript", "media"]
_EXCLUDED_TYPES = {"content_opportunity"}


class EvidenceBlock(BaseModel):
    block_id: str
    block_type: str
    timestamp: str | None = None
    text: str
    source_name: str
    media_id: str | None = None
    score: float = 0.0


class EvidencePack(BaseModel):
    queries: list[str]
    job_id: str | None = None
    blocks: list[EvidenceBlock] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)   # {media_id, source_name, blocks}
    truncated: bool = False

    def ids(self) -> set[str]:
        return {b.block_id for b in self.blocks}

    def render(self) -> str:
        """The list handed to the model. `[id=...]` markers are what citations must point at."""
        lines = []
        for b in self.blocks:
            where = f"{b.source_name}" + (f" @ {b.timestamp}" if b.timestamp else "")
            lines.append(f"- [id={b.block_id}] ({b.block_type}, {where}) {b.text}")
        return "\n".join(lines)


log = logging.getLogger(__name__)


class Retriever:
    def __init__(self, store, embedder):
        self.store = store
        self.embedder = embedder

    def search(self, q: str, *, k: int = 10, block_type: str | None = None, media_id: str | None = None, mode: str = "hybrid"):
        qvec = None
        if mode != "keyword" and getattr(self.store, "supports_vectors", False):
            try:
                qvec = self.embedder.embed_query(q)
            except Exception as exc:  # noqa: BLE001 - the writer must still get evidence when the embedder is down
                log.warning("embedder unavailable (%s); retrieval degraded to keyword-only", exc)
                mode = "keyword"
        return self.store.search(q, block_type, k, query_vector=qvec, media_id=media_id, mode=mode, vector_model=self.embedder.model)

    def evidence_for(
        self,
        queries: list[str],
        *,
        job_id: str | None = None,
        k_per_query: int = 12,
        max_blocks: int = 40,
        max_chars: int = 14000,
        media_id: str | None = None,
    ) -> EvidencePack:
        """All blocks of the anchoring job (if any) + hybrid hits for each query across the whole library,
        de-duplicated, ordered by type priority then relevance, and bounded by count and characters."""
        pack = EvidencePack(queries=queries, job_id=job_id)
        seen: dict[str, EvidenceBlock] = {}

        if job_id:
            for b in self.store.blocks(job_id):
                if b.block_type in _EXCLUDED_TYPES:
                    continue
                seen[b.block_id] = EvidenceBlock(block_id=b.block_id, block_type=b.block_type, timestamp=b.timestamp, text=b.text,
                                                 source_name=b.source_name, media_id=getattr(b, "media_id", None), score=1.0)
        for q in queries:
            for h in self.search(q, k=k_per_query, media_id=media_id):
                if h.block_type in _EXCLUDED_TYPES or h.block_id in seen:
                    continue
                seen[h.block_id] = EvidenceBlock(block_id=h.block_id, block_type=h.block_type, timestamp=h.timestamp, text=h.text,
                                                 source_name=h.source_name, media_id=h.media_id, score=h.rank)

        ordered = sorted(seen.values(), key=lambda b: (_TYPE_PRIORITY.index(b.block_type) if b.block_type in _TYPE_PRIORITY else 99, -b.score))
        chars = 0
        for b in ordered:
            if len(pack.blocks) >= max_blocks or chars + len(b.text) > max_chars:
                pack.truncated = True
                break
            pack.blocks.append(b)
            chars += len(b.text)

        by_media: dict[str, dict[str, Any]] = {}
        for b in pack.blocks:
            key = b.media_id or b.source_name
            by_media.setdefault(key, {"media_id": b.media_id, "source_name": b.source_name, "blocks": 0})["blocks"] += 1
        pack.sources = list(by_media.values())
        return pack

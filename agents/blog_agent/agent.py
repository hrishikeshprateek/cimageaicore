"""Blog Agent: brief (+ optional anchoring video job) -> evidence pack -> grounded draft."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from agents.blog_agent.schemas import BlogDraftV1, BlogDraftV2, Citation, ImagePlacement
from services.ai_gateway.base import AIProvider, RawModelOutput
from services import prompts
from services.block_engine.engine import PROMPTS_DIR, _fence_strip, load_prompt
from services.block_engine.schemas import provider_json_schema
from services.retrieval.retriever import EvidencePack, Retriever

log = logging.getLogger(__name__)
MIN_CITATIONS = 3
MAX_INLINE_IMAGES = 6
_MARKER = re.compile(r"\s*\[id=[^\]]+\]")
_IMG_LINE = re.compile(r"^[ \t]*\[img=([^\]]+)\][ \t]*$", re.MULTILINE)
_IMG_ANY = re.compile(r"\[img=([^\]]+)\]")
_H2 = re.compile(r"^## .*$", re.MULTILINE)

DEPTHS = {
    "standard": "Target length: about {target_words} words with 4-7 '## ' sections.",
    "in_depth": (
        "This is an IN-DEPTH feature of about {target_words} words. Use 6-8 '## ' sections, each with 2-4 substantial paragraphs. "
        "Work through the evidence thoroughly: cover every speaker, segment and detail that is relevant to the brief, explain the context "
        "and why it matters for prospective students and parents, and quote generously (verbatim, attributed). "
        "Add a '## Key Takeaways' bullet list before the close and, if the evidence supports at least three answers, a "
        "'## Frequently Asked Questions' section with 3-5 question/answer pairs (question in bold on its own line, answer below). "
        "Depth comes from evidence, not adjectives - never pad with generic statements."
    ),
}
EVIDENCE_BUDGET = {"standard": {"max_blocks": 40, "max_chars": 14000}, "in_depth": {"max_blocks": 80, "max_chars": 30000}}


def strip_citation_markers(markdown: str) -> str:
    """Remove inline [id=...] markers (kept in the stored draft for review) for preview/publishing."""
    return re.sub(r"[ \t]+\n", "\n", _MARKER.sub("", markdown or ""))


def image_marker_ids(markdown: str) -> list[str]:
    ids: list[str] = []
    for m in _IMG_ANY.finditer(markdown or ""):
        v = m.group(1).strip().removeprefix("img=").strip()
        if v and v not in ids:
            ids.append(v)
    return ids


def render_image_markers(markdown: str, images: list[dict[str, Any]] | list[ImagePlacement], url_for=lambda image_id: f"/api/v1/images/{image_id}") -> str:
    """[img=<id>] lines -> Markdown figures (image + italic caption) for preview/publishing. Unknown markers are dropped."""
    meta: dict[str, dict[str, Any]] = {}
    for im in images or []:
        d = im.model_dump() if isinstance(im, ImagePlacement) else dict(im)
        meta[d["image_id"]] = d

    def sub(m: re.Match) -> str:
        iid = m.group(1).strip().removeprefix("img=").strip()
        d = meta.get(iid)
        if d is None:
            return ""
        alt = (d.get("alt_text") or d.get("caption") or "").replace("]", ")").replace("\n", " ")
        cap = (d.get("caption") or "").strip()
        return f"![{alt}]({url_for(iid)})" + (f"\n*{cap}*" if cap else "")

    out = _IMG_LINE.sub(sub, markdown or "")
    return _IMG_ANY.sub("", out)   # a marker that was not on its own line is never rendered


def inline_citation_ids(markdown: str) -> list[str]:
    ids: list[str] = []
    for m in re.finditer(r"\[id=([^\]]+)\]", markdown or ""):
        for part in m.group(1).split(","):
            part = part.strip().removeprefix("id=").strip()
            if part and part not in ids:
                ids.append(part)
    return ids


class DraftValidationError(RuntimeError):
    pass


@dataclass
class BlogAgentResult:
    draft: BlogDraftV2
    evidence: EvidencePack
    model: str
    prompt_version: str
    usage: dict[str, int | None]
    seconds: float
    warnings: list[str] = field(default_factory=list)
    repaired: bool = False
    offered_images: list[Any] = field(default_factory=list)   # ImageRecord-like objects the writer could choose from
    depth: str = "standard"


class BlogAgent:
    name = "blog"

    def __init__(
        self,
        provider: AIProvider,
        retriever: Retriever,
        *,
        prompt_version: str = "blog_v1",
        style_guide_path: Path | None = None,
        institution_context: str = "",
        model: str | None = None,
        thinking_level: str | None = None,
    ):
        self.provider = provider
        self.retriever = retriever
        self.prompt_version = prompt_version
        self.institution_context = institution_context
        self.model = model
        self.thinking_level = thinking_level
        self.style_guide_path = style_guide_path
        self.system_template, self.user_template = prompts.prompt("blog", prompt_version)
        self.style_guide = self._load_style_guide()
        self.json_schema = provider_json_schema(BlogDraftV2)

    def _load_style_guide(self) -> str:
        if self.style_guide_path is not None and self.style_guide_path.exists():
            return self.style_guide_path.read_text(encoding="utf-8")
        return prompts.text("style-guide") or "(no style guide)"

    def reload(self, registry) -> None:
        """Pick up the active blog prompt, style guide and institution context from the registry (UI change, no restart)."""
        self.prompt_version = registry.active("blog")
        self.institution_context = registry.institution_context
        self.system_template, self.user_template = registry.prompt("blog")
        self.style_guide = registry.text("style-guide") or "(no style guide)"

    def draft(
        self,
        brief: str,
        *,
        job_id: str | None = None,
        extra_queries: list[str] | None = None,
        formats: list[str] | None = None,
        target_words: int = 1000,
        depth: str = "standard",
        images_for: Callable[[EvidencePack], list[Any]] | None = None,
    ) -> BlogAgentResult:
        """`images_for(evidence)` returns the pictures the writer may use (ImageRecord-like: id, description, offer_line())."""
        started = time.monotonic()
        depth = depth if depth in DEPTHS else "standard"
        queries = [brief] + [q for q in (extra_queries or []) if q]
        evidence = self.retriever.evidence_for(queries, job_id=job_id, **EVIDENCE_BUDGET[depth])
        if not evidence.blocks:
            raise DraftValidationError("no evidence found for this brief - analyse a relevant video first")
        offered = list(images_for(evidence)) if images_for else []

        fmt = {
            "institution_context": self.institution_context,
            "style_guide": self.style_guide,
            "brief": brief,
            "target_words": target_words,
            "formats": ", ".join(formats or ["linkedin", "instagram", "facebook"]),
            "evidence": evidence.render(),
            "images": "\n".join(im.offer_line() for im in offered) or "- (no pictures available - use none)",
            "depth_instructions": DEPTHS[depth].format(target_words=target_words),
        }
        raw = self.provider.generate_structured(
            self.system_template.format(**fmt), self.user_template.format(**fmt), self.json_schema,
            model=self.model, thinking_level=self.thinking_level,
        )
        draft, repaired, used = self._validate_or_repair(raw)
        if draft.hero_block_id and any(draft.hero_block_id == im.id for im in offered):
            draft.hero_block_id = None   # v1 field filled with a picture id: the picture list is the source of truth
        warnings = self._scrub_image_citations(draft, {im.id for im in offered})
        warnings += self._ground(draft, evidence, target_words)
        warnings += self._ground_images(draft, offered)
        if evidence.truncated:
            warnings.append("evidence was truncated to fit the prompt budget")
        return BlogAgentResult(
            draft=draft, evidence=evidence, model=used.model, prompt_version=self.prompt_version,
            usage=used.usage, seconds=round(time.monotonic() - started, 2), warnings=warnings, repaired=repaired,
            offered_images=offered, depth=depth,
        )

    # ------------------------------------------------------------------
    def _validate_or_repair(self, raw: RawModelOutput) -> tuple[BlogDraftV1, bool, RawModelOutput]:
        try:
            return self._parse(raw.text), False, raw
        except (ValidationError, json.JSONDecodeError) as first:
            log.warning("blog draft failed validation, asking provider to repair: %s", first)
            fixed = self.provider.repair_json(raw.text, str(first), self.json_schema)
            try:
                fixed.usage = {k: (raw.usage.get(k) or 0) + (fixed.usage.get(k) or 0) for k in raw.usage} or fixed.usage
                return self._parse(fixed.text), True, fixed
            except (ValidationError, json.JSONDecodeError) as second:
                raise DraftValidationError(f"invalid draft after repair: {second}") from second

    @staticmethod
    def _parse(text: str) -> BlogDraftV2:
        return BlogDraftV2.model_validate(json.loads(_fence_strip(text)))

    @staticmethod
    def _scrub_image_citations(draft: BlogDraftV2, image_ids: set[str]) -> list[str]:
        """Writers sometimes 'cite' a picture ([id=<picture id>]). Those are not evidence: drop them quietly from the brackets
        and the citation list instead of counting them as bad citations."""
        if not image_ids:
            return []
        n = 0

        def fix(m: re.Match) -> str:
            nonlocal n
            parts = [x.strip().removeprefix("id=").strip() for x in m.group(1).split(",")]
            keep = [x for x in parts if x not in image_ids]
            n += len(parts) - len(keep)
            return (" [" + ", ".join(f"id={x}" for x in keep) + "]") if keep else ""

        draft.body_markdown = re.sub(r"\s*\[id=([^\]]+)\]", fix, draft.body_markdown or "")
        before = len(draft.citations)
        draft.citations = [c for c in draft.citations if c.block_id not in image_ids]
        n += before - len(draft.citations)
        return [f"{n} picture id(s) were written as citations and removed"] if n else []

    @staticmethod
    def _ground_images(draft: BlogDraftV2, offered: list[Any]) -> list[str]:
        """Images work like citations: anything not offered is dropped; markers and placements are reconciled;
        a missing hero falls back to the best offered frame."""
        w: list[str] = []
        by_id = {im.id: im for im in offered}
        body = draft.body_markdown or ""
        unknown_markers = [i for i in image_marker_ids(body) if i not in by_id]
        if unknown_markers:
            body = _IMG_LINE.sub(lambda m: "" if m.group(1).strip() not in by_id else m.group(0), body)
            body = _IMG_ANY.sub(lambda m: "" if m.group(1).strip() not in by_id else m.group(0), body)
            w.append(f"{len(unknown_markers)} image marker(s) pointed outside the offered pictures and were removed")
        # markers must be on their own line: lift inline ones out of the paragraph
        body = re.sub(r"(\S)[ \t]*(\[img=[^\]]+\])", r"\1\n\n\2", body)
        body = re.sub(r"(\[img=[^\]]+\])[ \t]*(\S)", r"\1\n\n\2", body)
        placements = [p for p in draft.images if p.image_id in by_id]
        if len(placements) != len(draft.images):
            w.append(f"{len(draft.images) - len(placements)} image placement(s) pointed outside the offered pictures and were dropped")
        heroes = [p for p in placements if p.placement == "hero"]
        if len(heroes) > 1:
            for extra in heroes[1:]:
                extra.placement = "inline"
            w.append("more than one hero image; the first one was kept")
        hero_id = heroes[0].image_id if heroes else None
        if hero_id:   # the hero never sits in the body as well
            body = _IMG_LINE.sub(lambda m: "" if m.group(1).strip() == hero_id else m.group(0), body)
        marker_ids = image_marker_ids(body)
        seen: set[str] = set()

        def dedupe(m: re.Match) -> str:
            iid = m.group(1).strip()
            if iid in seen:
                return ""
            seen.add(iid)
            return m.group(0)

        body = _IMG_LINE.sub(dedupe, body)
        marker_ids = image_marker_ids(body)
        inline = {p.image_id: p for p in placements if p.placement == "inline"}
        for iid in marker_ids:   # marker without an entry -> entry from the picture's own description
            if iid not in inline:
                im = by_id[iid]
                inline[iid] = ImagePlacement(image_id=iid, placement="inline", caption=im.description[:200], alt_text=im.description[:120])
        missing = [iid for iid in inline if iid not in marker_ids]
        if missing:   # entry without a marker -> place before successive H2 headings (2nd onwards), else at the end
            heads = [m.start() for m in _H2.finditer(body)][1:]
            for k, iid in enumerate(missing):
                marker = f"[img={iid}]\n\n"
                if k < len(heads):
                    pos = heads[k]
                    body = body[:pos] + marker + body[pos:]
                    heads = [m.start() for m in _H2.finditer(body)][1:]
                else:
                    body = body.rstrip() + f"\n\n[img={iid}]\n"
            w.append(f"{len(missing)} inline image(s) had no marker in the body and were placed automatically")
        marker_ids = image_marker_ids(body)
        if len(marker_ids) > MAX_INLINE_IMAGES:
            for iid in marker_ids[MAX_INLINE_IMAGES:]:
                body = _IMG_LINE.sub(lambda m, iid=iid: "" if m.group(1).strip() == iid else m.group(0), body)
                inline.pop(iid, None)
            w.append(f"more than {MAX_INLINE_IMAGES} inline images; extra ones were removed")
        if hero_id is None and offered:
            pick = next((im for im in offered if "blog_hero" in getattr(im, "suitable_for", [])), None) or \
                   next((im for im in offered if getattr(im, "kind", "") == "frame"), None) or offered[0]
            if pick.id not in inline:
                heroes = [ImagePlacement(image_id=pick.id, placement="hero", caption=pick.description[:200], alt_text=pick.description[:120])]
                w.append("no hero image chosen by the writer; the best offered picture was used")
        draft.images = heroes[:1] + [inline[i] for i in image_marker_ids(body) if i in inline]
        draft.body_markdown = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
        return w

    @staticmethod
    def _ground(draft: BlogDraftV1, evidence: EvidencePack, target_words: int) -> list[str]:
        """Drop citations that point outside the evidence; flag weak grounding and length drift."""
        w: list[str] = []
        ids = evidence.ids()
        listed = {c.block_id for c in draft.citations}
        for bid in inline_citation_ids(draft.body_markdown):
            if bid in ids and bid not in listed:
                draft.citations.append(Citation(block_id=bid, used_for="cited inline"))
                listed.add(bid)
        bad = [c for c in draft.citations if c.block_id not in ids]
        if bad:
            draft.citations = [c for c in draft.citations if c.block_id in ids]
            w.append(f"{len(bad)} citation(s) pointed outside the evidence and were dropped")
        if len(draft.citations) < MIN_CITATIONS:
            w.append(f"weakly grounded: only {len(draft.citations)} citation(s)")
        if draft.hero_block_id and draft.hero_block_id not in ids:
            draft.hero_block_id = None
            w.append("hero_block_id was not in the evidence and was cleared")
        words = len(re.findall(r"\w+", draft.body_markdown))
        if words < target_words * 0.5 or words > target_words * 1.8:
            w.append(f"length {words} words vs target {target_words}")
        if len(draft.meta_description) > 160:
            w.append("meta_description longer than 155 characters")
        if not re.search(r"^## ", draft.body_markdown, re.MULTILINE):
            w.append("no H2 subheadings in the body")
        if draft.evidence_gaps:
            w.append(f"{len(draft.evidence_gaps)} evidence gap(s) reported by the writer")
        return w


def load_prompt_from(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    sections = re.split(r"^## (\w+)\s*$", text, flags=re.MULTILINE)
    parts = {sections[i].strip(): sections[i + 1].strip() for i in range(1, len(sections) - 1, 2)}
    return parts["system"], parts["user"]

"""What the Blog Agent must return (strict JSON)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Citation(BaseModel):
    block_id: str = Field(description="An [id=...] value from the evidence list.")
    used_for: str = Field(description="Which fact, quote or detail in the article this evidence supports.")


class SocialPosts(BaseModel):
    linkedin: str = Field(description="Professional tone, 80-150 words, 2-3 hashtags.")
    instagram: str = Field(description="Warm, 40-80 words, emojis allowed, 5-8 hashtags.")
    facebook: str = Field(description="Friendly, 50-100 words, 1-3 hashtags.")


class BlogDraftV1(BaseModel):
    title: str
    slug: str = Field(description="URL slug: lowercase words joined by hyphens.")
    seo_title: str = Field(description="<= 60 characters, includes Patna or Bihar where natural.")
    meta_description: str = Field(description="<= 155 characters.")
    excerpt: str = Field(description="1-2 sentence teaser.")
    body_markdown: str = Field(description="The article in Markdown: opening paragraphs, 4-7 '## ' subheadings, bullet lists where the style guide allows, closing call to action.")
    tags: list[str] = Field(description="6-12 tags mixing topic and location.")
    citations: list[Citation] = Field(description="Every fact, name, date, number and quote in the article must be covered by at least one citation.")
    hero_block_id: str | None = Field(description="The [id=...] of a media block that would make the best featured image, or null.")
    social: SocialPosts
    evidence_gaps: list[str] = Field(description="Things you would have liked to say but the evidence did not support - left out of the article.")

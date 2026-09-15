"""The writer's Markdown -> WordPress block markup (Gutenberg), so the post is fully editable on the site.

Handles what the Blog Agent produces: paragraphs, ## / ### headings, bullet lists, > quotes, **bold**, *italic*, links,
`[img=<id>]` picture lines (resolved through a callback to uploaded media) and `[id=...]` citation markers (stripped).
"""
from __future__ import annotations

import html
import re
from typing import Callable

_CITE = re.compile(r"\s*\[id=[^\]]+\]")
_IMG = re.compile(r"^\s*\[img=([^\]]+)\]\s*$")
_IMG_ANY = re.compile(r"\[img=[^\]]+\]")


def inline_html(text: str) -> str:
    t = html.escape(_CITE.sub("", text), quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(^|[^*])\*(?!\*)(.+?)\*", r"\1<em>\2</em>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t.strip()


class UploadedImage:
    def __init__(self, media_id: int, url: str, alt: str = "", caption: str = ""):
        self.media_id, self.url, self.alt, self.caption = media_id, url, alt, caption


def image_block(im: UploadedImage, *, caption: bool = True) -> str:
    cap = f'<figcaption class="wp-element-caption">{html.escape(im.caption, quote=False)}</figcaption>' if (caption and im.caption) else ""
    return (f'<!-- wp:image {{"id":{im.media_id},"sizeSlug":"large","linkDestination":"none"}} -->\n'
            f'<figure class="wp-block-image size-large"><img src="{html.escape(im.url, quote=True)}" alt="{html.escape(im.alt, quote=True)}" class="wp-image-{im.media_id}"/>{cap}</figure>\n'
            f'<!-- /wp:image -->')


def markdown_to_blocks(markdown: str, resolve_image: Callable[[str], UploadedImage | None] | None = None, *, hero: UploadedImage | None = None,
                       image_position: str = "under_heading", captions: bool = False) -> str:
    """Block markup for the post content, laid out like the site's own posts (cimage.in house style by default):
    the hero right after the intro paragraph, each section's picture directly under its heading, no captions.
    image_position='as_placed' keeps the writer's positions; captions=True adds figcaptions."""
    out: list[tuple[str, str]] = []          # (kind, markup)  kind: p | h | ul | q | img
    para: list[str] = []
    items: list[str] = []

    def flush_para() -> None:
        if para:
            out.append(("p", f"<!-- wp:paragraph -->\n<p>{inline_html(' '.join(para))}</p>\n<!-- /wp:paragraph -->"))
            para.clear()

    def flush_list() -> None:
        if items:
            lis = "".join(f"<!-- wp:list-item -->\n<li>{inline_html(x)}</li>\n<!-- /wp:list-item -->\n" for x in items)
            out.append(("ul", f'<!-- wp:list -->\n<ul class="wp-block-list">{lis}</ul>\n<!-- /wp:list -->'))
            items.clear()

    for raw in (markdown or "").replace("\r", "").split("\n"):
        line = raw.rstrip()
        m = _IMG.match(line)
        if m:
            flush_para(); flush_list()
            im = resolve_image(m.group(1).strip()) if resolve_image else None
            if im:
                out.append(("img", image_block(im, caption=captions)))
            continue
        line = _IMG_ANY.sub("", line)
        if not line.strip():
            flush_para(); flush_list()
            continue
        h = re.match(r"^(#{1,4})\s+(.*)$", line)
        if h:
            flush_para(); flush_list()
            level = min(4, max(2, len(h.group(1)) + 1 if len(h.group(1)) == 1 else len(h.group(1))))
            out.append(("h", f'<!-- wp:heading {{"level":{level}}} -->\n<h{level} class="wp-block-heading">{inline_html(h.group(2))}</h{level}>\n<!-- /wp:heading -->'))
            continue
        q = re.match(r"^>\s?(.*)$", line)
        if q:
            flush_para(); flush_list()
            out.append(("q", f'<!-- wp:quote -->\n<blockquote class="wp-block-quote"><!-- wp:paragraph -->\n<p>{inline_html(q.group(1))}</p>\n<!-- /wp:paragraph --></blockquote>\n<!-- /wp:quote -->'))
            continue
        li = re.match(r"^\s*[-*]\s+(.*)$", line)
        if li:
            flush_para()
            items.append(li.group(1))
            continue
        flush_list()
        if re.match(r"^\*\*[^*]+\*\*\s*$", line.strip()):   # a bold-only line (FAQ question) stays its own paragraph
            flush_para()
            para.append(line.strip())
            flush_para()
            continue
        para.append(line.strip())
    flush_para(); flush_list()

    if image_position == "under_heading":   # first picture of every section moves directly under that section's heading
        hoisted: list[tuple[str, str]] = []
        i = 0
        while i < len(out):
            kind, markup = out[i]
            hoisted.append(out[i])
            if kind == "h":
                j = i + 1
                while j < len(out) and out[j][0] != "h":
                    if out[j][0] == "img":
                        hoisted.append(out.pop(j))
                        break
                    j += 1
            i += 1
        out = hoisted
    if hero is not None:                     # the site's template does not show the featured image: lead with it after the intro
        first_p = next((k for k, (kind, _) in enumerate(out) if kind == "p"), None)
        at = first_p + 1 if first_p is not None else 0
        out.insert(at, ("img", image_block(hero, caption=captions)))
    return "\n\n".join(m for _, m in out) + ("\n" if out else "")


def plain_excerpt(text: str, limit: int = 300) -> str:
    t = re.sub(r"\s+", " ", _CITE.sub("", text or "")).strip()
    return t if len(t) <= limit else t[: limit - 1].rsplit(" ", 1)[0] + "…"

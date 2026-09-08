"""Render builder-agnostic blocks into Gutenberg block markup.

Used for Gutenberg/classic sites (the catch-all when a site is neither Divi nor
Elementor). No post meta needed. Same small block set as the Divi 4 renderer.
"""
from __future__ import annotations

import html

from .rendered import RenderedContent


def _esc_html(text: str) -> str:
    return html.escape(str(text), quote=False)


def _esc_attr(text: str) -> str:
    return html.escape(str(text), quote=True)


def _render_block(block: dict) -> str | None:
    kind = block.get("kind")
    if kind == "heading":
        level = block.get("level", 2)
        try:
            level = int(level)
        except (ValueError, TypeError):
            level = 2
        level = min(max(level, 1), 6)
        return (f'<!-- wp:heading {{"level":{level}}} -->\n'
                f'<h{level}>{_esc_html(block.get("text", ""))}</h{level}>\n'
                f'<!-- /wp:heading -->')
    if kind == "paragraph":
        return (f'<!-- wp:paragraph -->\n'
                f'<p>{_esc_html(block.get("text", ""))}</p>\n'
                f'<!-- /wp:paragraph -->')
    if kind == "button":
        text = _esc_html(block.get("text", ""))
        url = _esc_attr(block.get("url", ""))
        return (
            '<!-- wp:buttons -->\n'
            '<div class="wp-block-buttons"><!-- wp:button -->\n'
            f'<div class="wp-block-button"><a class="wp-block-button__link wp-element-button" '
            f'href="{url}">{text}</a></div>\n'
            '<!-- /wp:button --></div>\n'
            '<!-- /wp:buttons -->'
        )
    if kind == "image":
        url = block.get("url") or ""
        if not url:
            return None  # no source -> nothing to place, skip like an unknown kind
        # core/image always carries alt, empty when there is no alt text.
        alt = _esc_attr(block.get("alt") or "")
        return (f'<!-- wp:image --><figure class="wp-block-image">'
                f'<img src="{_esc_attr(url)}" alt="{alt}"/></figure>'
                f'<!-- /wp:image -->')
    return None


def render_gutenberg(blocks: list[dict]) -> RenderedContent:
    parts = [b for b in (_render_block(x) for x in blocks) if b is not None]
    return RenderedContent(content="\n\n".join(parts), meta={})

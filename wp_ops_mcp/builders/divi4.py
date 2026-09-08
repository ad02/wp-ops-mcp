"""Render builder-agnostic blocks into Divi 4 shortcodes.

Divi 4 stores layouts as nested shortcodes in post_content plus post meta that turns
the builder on. This is the reliable bulk path (works over plain REST). Block set is
intentionally small (heading/paragraph/button/image) and grows as real templates are
needed.

Builder-agnostic block shapes:
    {"kind": "heading", "level": 2, "text": "..."}
    {"kind": "paragraph", "text": "..."}
    {"kind": "button", "text": "...", "url": "..."}
    {"kind": "image", "url": "...", "alt": "optional"}
"""
from __future__ import annotations

import html

from .rendered import RenderedContent

# Divi modules carry a builder version; use a known-good 4.x value.
_BUILDER_VERSION = "4.27.6"


def _esc_html(text: str) -> str:
    return html.escape(str(text), quote=False)


def _esc_attr(text: str) -> str:
    # quote=True escapes both " and ' so the value can't break the shortcode attr.
    return html.escape(str(text), quote=True)


def _render_module(block: dict) -> str | None:
    kind = block.get("kind")
    if kind == "heading":
        level = block.get("level", 2)
        try:
            level = int(level)
        except (ValueError, TypeError):
            level = 2
        level = min(max(level, 1), 6)
        inner = f"<h{level}>{_esc_html(block.get('text', ''))}</h{level}>"
        return f'[et_pb_text _builder_version="{_BUILDER_VERSION}"]{inner}[/et_pb_text]'
    if kind == "paragraph":
        inner = f"<p>{_esc_html(block.get('text', ''))}</p>"
        return f'[et_pb_text _builder_version="{_BUILDER_VERSION}"]{inner}[/et_pb_text]'
    if kind == "button":
        text = _esc_attr(block.get("text", ""))
        url = _esc_attr(block.get("url", ""))
        return (f'[et_pb_button button_text="{text}" button_url="{url}" '
                f'_builder_version="{_BUILDER_VERSION}" /]')
    if kind == "image":
        url = block.get("url") or ""
        if not url:
            return None  # no source -> nothing to place, skip like an unknown kind
        alt = block.get("alt") or ""
        alt_attr = f' alt="{_esc_attr(alt)}"' if alt else ""
        return (f'[et_pb_image src="{_esc_attr(url)}"{alt_attr} '
                f'_builder_version="{_BUILDER_VERSION}" /]')
    return None  # unknown kinds are skipped


def render_divi4(blocks: list[dict]) -> RenderedContent:
    modules = [m for m in (_render_module(b) for b in blocks) if m is not None]
    body = "".join(modules)
    content = (
        f'[et_pb_section fb_built="1" _builder_version="{_BUILDER_VERSION}"]'
        f'[et_pb_row _builder_version="{_BUILDER_VERSION}"]'
        f'[et_pb_column type="4_4" _builder_version="{_BUILDER_VERSION}"]'
        f'{body}'
        f'[/et_pb_column][/et_pb_row][/et_pb_section]'
    )
    meta = {
        "_et_pb_use_builder": "on",
        "_et_pb_page_layout": "et_full_width_page",
        "_et_pb_old_content": "",
    }
    return RenderedContent(content=content, meta=meta)

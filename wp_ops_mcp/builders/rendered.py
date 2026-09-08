"""Shared result type for builder renderers."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RenderedContent:
    """The output of rendering builder-agnostic blocks into a site's builder format.

    `content` goes into post_content; `meta` is post meta that must be set for the
    builder to render it (e.g. Divi 4 needs _et_pb_use_builder = "on").
    """
    content: str
    meta: dict = field(default_factory=dict)

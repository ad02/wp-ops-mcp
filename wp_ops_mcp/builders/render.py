"""Builder-aware render dispatcher.

The MCP content tools pass builder-agnostic blocks; the site profile decides the
target format. Divi 5 (block/JSON) and Elementor rendering are deliberately deferred
(see goals/wp-ops-mcp-server.md) — we raise rather than guess attribute paths and
ship broken modules onto live medical sites.
"""
from __future__ import annotations

from .rendered import RenderedContent
from .divi4 import render_divi4
from .gutenberg import render_gutenberg


class UnsupportedBuilderError(Exception):
    """Raised when rendering for a builder/version we don't support yet."""


def render_content(blocks: list[dict], builder: str, divi_major: int | None) -> RenderedContent:
    if builder == "divi":
        if divi_major == 4:
            return render_divi4(blocks)
        if divi_major == 5:
            raise UnsupportedBuilderError(
                "Divi 5 (block/JSON) rendering is deferred — needs the companion plugin + "
                "verified attribute knowledge (see goals/wp-ops-mcp-server.md). Refusing to guess."
            )
        raise UnsupportedBuilderError(
            f"Divi detected but major version is unknown ({divi_major!r}); "
            "run wp_discover_site and confirm before authoring."
        )
    if builder == "elementor":
        raise UnsupportedBuilderError("Elementor rendering is not implemented yet (deferred).")
    if builder == "gutenberg":
        return render_gutenberg(blocks)
    # Unknown/classic theme: block markup is valid HTML, safe fallback.
    return render_gutenberg(blocks)

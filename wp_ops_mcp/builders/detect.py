"""Detect the active page builder and Divi major version for a site.

Pure logic over WP-CLI output (theme template/stylesheet + theme/plugin lists), so it
is authoritative (not HTML-regex guessing) and unit-testable. Divi 4 (shortcodes) and
Divi 5 (block/JSON) are different data models; every Divi op must branch on divi_major.

Precedence: Divi (theme or builder plugin) > Elementor > Gutenberg.
"""
from __future__ import annotations

from dataclasses import dataclass

_ACTIVE = ("active", "active-network")
_DIVI_PLUGIN_SLUGS = ("divi-builder",)


@dataclass
class BuilderInfo:
    builder: str               # "divi" | "elementor" | "gutenberg" | "unknown"
    divi_major: int | None     # 4 or 5 when builder == "divi", else None
    source: str | None         # "theme" | "plugin" | None
    version: str | None        # builder version string when known


def _active(plugins: list[dict]) -> list[dict]:
    return [p for p in plugins if p.get("status") in _ACTIVE]


def _find_version(themes: list[dict], name: str) -> str | None:
    for t in themes:
        if t.get("name", "").lower() == name.lower():
            return t.get("version")
    return None


def _major(version: str | None) -> int | None:
    if not version:
        return None
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError):
        return None


def detect_builder(
    template: str,
    stylesheet: str,
    themes: list[dict],
    plugins: list[dict],
) -> BuilderInfo:
    """Classify the site's builder.

    `template` is the active theme's parent dir (catches Divi child themes);
    `stylesheet` is the active (possibly child) theme dir.
    """
    template = (template or "").strip()
    stylesheet = (stylesheet or "").strip()
    active_plugins = _active(plugins)
    active_slugs = {p.get("name", "").lower() for p in active_plugins}

    # --- Divi via theme (parent or child) ---
    if template.lower() == "divi" or stylesheet.lower() == "divi":
        version = _find_version(themes, "Divi")
        return BuilderInfo("divi", _major(version), "theme", version)

    # --- Divi via builder plugin on another theme ---
    for slug in _DIVI_PLUGIN_SLUGS:
        if slug in active_slugs:
            version = next((p.get("version") for p in active_plugins
                            if p.get("name", "").lower() == slug), None)
            return BuilderInfo("divi", _major(version), "plugin", version)

    # --- Elementor ---
    if "elementor" in active_slugs:
        version = next((p.get("version") for p in active_plugins
                        if p.get("name", "").lower() == "elementor"), None)
        return BuilderInfo("elementor", None, "plugin", version)

    # --- Default: Gutenberg / classic ---
    return BuilderInfo("gutenberg", None, None, None)

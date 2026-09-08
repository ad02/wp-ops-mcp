"""Site fingerprinting (wp_discover_site).

All facts are gathered in ONE SSH session (WPE throttles rapid reconnects) via a
single combined WP-CLI command, then parsed offline. The command deliberately uses
NO single quotes — paramiko mangles nested quotes through the WPE gateway.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .builders.detect import _major, detect_builder
from .profiles.store import ProfileStore, SiteProfile
from .registry import Site

_SECTIONS = ("WP_VERSION", "CLI_INFO", "TEMPLATE", "STYLESHEET", "THEMES", "PLUGINS", "MULTISITE")


def build_discovery_command(install: str) -> str:
    """Build the single combined WP-CLI command for an install.

    Constraints learned the hard way against the WPE SSH gateway:
    - ONE line, ';'-separated. The gateway collapses newlines to spaces, which makes a
      following `wp ...` swallow the next `echo @@X@@` as positional args.
    - NO single quotes; paramiko mangles nested quotes through the gateway.
    """
    pairs = [
        ("WP_VERSION", "wp core version"),
        ("CLI_INFO", "wp cli info"),
        ("TEMPLATE", "wp option get template"),
        ("STYLESHEET", "wp option get stylesheet"),
        ("THEMES", "wp theme list --format=json"),
        ("PLUGINS", "wp plugin list --format=json"),
        ("MULTISITE", "wp config get MULTISITE"),
    ]
    parts = [f"cd ~/sites/{install} 2>/dev/null"]
    for sentinel, cmd in pairs:
        parts.append(f"echo @@{sentinel}@@; {cmd}")
    return "; ".join(parts)


_SENTINEL_RE = re.compile(r"@@(" + "|".join(_SECTIONS) + r")@@")


def _split_sections(raw: str) -> dict[str, list[str]]:
    """Split on @@SECTION@@ markers wherever they appear.

    `wp --format=json` prints no trailing newline, so a sentinel can be concatenated
    onto the closing ']' of the previous section (e.g. '[...]@@PLUGINS@@'). A regex
    split on the markers handles that; a line-based split would miss it.
    """
    sections: dict[str, list[str]] = {s: [] for s in _SECTIONS}
    # parts = [pre, NAME1, body1, NAME2, body2, ...]
    parts = _SENTINEL_RE.split(raw)
    for i in range(1, len(parts) - 1, 2):
        name, body = parts[i], parts[i + 1]
        if name in sections:
            sections[name] = body.splitlines()
    return sections


def _first_value(lines: list[str]) -> str | None:
    for line in lines:
        s = line.strip()
        if not s or s.lower().startswith("error"):
            continue
        return s
    return None


def _parse_json_list(lines: list[str]) -> list:
    blob = "\n".join(lines).strip()
    if not blob:
        return []
    # PHP deprecation/notice lines (common on PHP 7.4 + WP 6.9) get prepended to the
    # --format=json output. Slice from the first '[' to the last ']' before parsing.
    start, end = blob.find("["), blob.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(blob[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _parse_php_version(lines: list[str]) -> str | None:
    for line in lines:
        if "php version" in line.lower():
            # "PHP version:\t8.2.28" -> "8.2.28"
            return line.split(":", 1)[1].strip() if ":" in line else None
    return None


def parse_discovery(raw: str) -> dict:
    """Parse the combined discovery output into a fingerprint dict."""
    sec = _split_sections(raw)
    multisite_val = _first_value(sec["MULTISITE"])
    return {
        "wp_version": _first_value(sec["WP_VERSION"]),
        "php_version": _parse_php_version(sec["CLI_INFO"]),
        "template": _first_value(sec["TEMPLATE"]),
        "stylesheet": _first_value(sec["STYLESHEET"]),
        "themes": _parse_json_list(sec["THEMES"]),
        "plugins": _parse_json_list(sec["PLUGINS"]),
        "multisite": multisite_val == "1",
    }


def _theme_version(themes: list[dict], name: str | None) -> str | None:
    if not name:
        return None
    for t in themes:
        if t.get("name", "").lower() == name.lower():
            return t.get("version")
    return None


async def discover_site(
    site: Site,
    transport,
    store: ProfileStore,
    now: str | None = None,
) -> SiteProfile:
    """Fingerprint a site over one SSH session, persist, and return its profile.

    `transport` is duck-typed: must expose `async run_raw(command) -> SSHResult`.
    """
    result = await transport.run_raw(build_discovery_command(site.install))
    fp = parse_discovery(result.stdout)
    builder = detect_builder(fp["template"], fp["stylesheet"], fp["themes"], fp["plugins"])

    profile = SiteProfile(
        install=site.install,
        account=site.account,
        environment=site.environment,
        domain=site.domain,
        discovered_at=now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        wp_version=fp["wp_version"],
        php_version=fp["php_version"],
        active_theme=fp["stylesheet"],
        active_theme_version=_theme_version(fp["themes"], fp["stylesheet"]),
        builder=builder.builder,
        divi_major=builder.divi_major,
        multisite=fp["multisite"],
        plugins=fp["plugins"],
        raw={
            "template": fp["template"],
            "stylesheet": fp["stylesheet"],
            "builder_source": builder.source,
            "builder_version": builder.version,
        },
    )
    store.upsert(profile)
    return profile


async def rest_discover(gateway, site: Site, now: str | None = None) -> SiteProfile:
    """Fingerprint a site from the wp-ops-connect /info endpoint over REST - NO SSH.

    This is the REST-only sibling of discover_site: it lets a site that has application-
    password credentials but no SSH reach (our stagings / team sites, and every cloud
    deployment) get a SiteProfile so the content/edit tools stop refusing it.

    `gateway` is duck-typed: it need only expose `async ensure_plugin() -> dict`
    returning the /info payload ({wp, theme, divi, seo_plugin, plugin_version}).
    Identity (account / environment / domain) comes from the already-resolved `site`:
    the real fleet Site for a registered install, or the synthetic Site
    (_site_or_synthetic) for a credential-only one.

    Unlike discover_site this does NOT persist - the caller (discover_site_payload)
    owns the store.upsert so both transports share one persistence path.

    /info carries less than the SSH fingerprint, so several fields are honestly blank:
      - php_version -> "" (not exposed over REST),
      - active_theme_version -> None (/info gives no theme version),
      - plugins -> [] (no plugin list; plugin_count therefore reads 0),
      - multisite -> False (not exposed; the common case for these sites).
    Builder: a non-null `divi` version -> "divi" + its major component; a null `divi`
    -> "gutenberg" (matching detect_builder's classic-theme default). Elementor and
    other builders are not distinguishable via /info v1.1 - acceptable, since REST-only
    sites are our Divi / Gutenberg stagings and team sites.
    """
    info = await gateway.ensure_plugin()
    divi = info.get("divi")
    if divi:
        # str() first: /info's divi may be a bare number (int/float) as well as a string.
        # _major guards non-numeric leads ("v5.0.1", a child theme's free-text Version) ->
        # None, so a Divi site we cannot version stays "divi" (divi_major None) not a crash.
        builder, divi_major = "divi", _major(str(divi))
    else:
        builder, divi_major = "gutenberg", None
    return SiteProfile(
        install=site.install,
        account=site.account,
        environment=site.environment,
        domain=site.domain,
        discovered_at=now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        wp_version=info.get("wp"),
        php_version="",                       # not in /info - honest empty over REST
        active_theme=info.get("theme"),       # /info theme = the active stylesheet
        active_theme_version=None,            # /info carries no theme version
        builder=builder,
        divi_major=divi_major,
        multisite=False,                      # not in /info - default (unknown over REST)
        plugins=[],                           # /info carries no plugin list
        raw={"source": "rest", "info": info},
    )

"""FastMCP entrypoint for the WordPress fleet operations server.

Phase 1 surface: wp_list_sites, wp_site_health. Uses the FastMCP bundled in the
official `mcp` SDK (no extra dependency). Tool bodies are thin wrappers over pure
payload helpers (list_sites_payload / site_health_payload) so the logic is testable
offline.

Run:  python -m wp_ops_mcp.server     (stdio transport)
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

from .config import SiteConfig
from .registry import Site, SiteRegistry, derive_environment, resolve_environment
from .health import check_site_health
from .discover import discover_site, rest_discover
from .profiles.store import ProfileStore, SiteProfile
from .ops.content import ContentOps
from .ops.edit import EditOps
from .ops.seo import SeoOps                       # SSH-free: safe at import time
from .ops.acf import AcfOps                       # SSH-free: safe at import time
from .ops.media import MediaOps                   # SSH-free: safe at import time
from .ops.menus import MenuOps                    # SSH-free: safe at import time
from .ops.site import SiteOps                     # SSH-free: safe at import time
from .ops.admin import AdminOps                   # SSH-free: safe at import time
from .ops.terms import TermOps                    # SSH-free: safe at import time
from .ops.plugins import PluginOps                # SSH-free: safe at import time
from .ops.credentials import load_credentials
from .ops.rest_gateway import RestContentGateway
from .transport.rest import WPRestClient
from .verify.probe import probe_url

mcp = FastMCP("wp-ops-mcp")

_PROFILE_DB = Path("data/wp_ops_mcp/profiles.db")

_registry: SiteRegistry | None = None
_store: ProfileStore | None = None


def get_registry() -> SiteRegistry:
    """Lazily load the live fleet registry from data/sites.json."""
    global _registry
    if _registry is None:
        _registry = SiteRegistry.from_config(SiteConfig())
    return _registry


def get_store() -> ProfileStore:
    """Lazily open the site-profile store."""
    global _store
    if _store is None:
        _store = ProfileStore(_PROFILE_DB)
    return _store


def _site_or_synthetic(registry: SiteRegistry, install: str) -> Site | None:
    """Resolve an install to a Site: the registry entry when the fleet inventory knows
    it, else a SYNTHETIC Site when REST credentials exist for it. None when neither does.

    Credentials-only installs (our stagings / team sites, absent from sites.json) are
    the reason this exists: with a synthetic Site the tool layer stops refusing them as
    "unknown". The synthetic is honest about what REST alone can't tell us - account is
    the sentinel "rest-only", environment is derived from the install name (same
    heuristic the registry uses), domain is the host of the credentials base_url, and
    php_version / cf_zone_id (sites.json-only fields) are left empty. Gated strictly on
    credentials existing, so no new resolution surface opens for uncredentialed names.
    """
    site = registry.get(install)
    if site is not None:
        return site
    creds = load_credentials(install)
    if creds is None:
        return None
    return Site(
        install=install,
        account="rest-only",
        domain=urlsplit(creds.base_url).netloc,
        environment=resolve_environment(install, creds.environment),
        php_version="",
        cf_zone_id="",
    )


def _site_summary(site: Site) -> dict:
    return {
        "install": site.install,
        "account": site.account,
        "environment": site.environment,
        "domain": site.domain,
    }


def list_sites_payload(
    registry: SiteRegistry,
    account: str | None = None,
    environment: str | None = None,
    query: str | None = None,
) -> dict:
    sites = registry.filter(account=account, environment=environment, query=query)
    return {"count": len(sites), "sites": [_site_summary(s) for s in sites]}


async def site_health_payload(
    registry: SiteRegistry,
    install: str,
    rest_client=None,
    cli_transport=None,
) -> dict:
    site = registry.get(install)
    if site is None:
        return {"error": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    health = await check_site_health(site, rest_client=rest_client, cli_transport=cli_transport)
    return health.to_dict()


def _profile_summary(p: SiteProfile) -> dict:
    return {
        "install": p.install,
        "account": p.account,
        "environment": p.environment,
        "builder": p.builder,
        "divi_major": p.divi_major,
        "wp_version": p.wp_version,
        "php_version": p.php_version,
        "active_theme": p.active_theme,
        "plugin_count": len(p.plugins),
        "multisite": p.multisite,
        "discovered_at": p.discovered_at,
    }


async def discover_site_payload(
    registry: SiteRegistry,
    store: ProfileStore,
    install: str,
    transport=None,
    now: str | None = None,
    gateway=None,
) -> dict:
    """Fingerprint an install and persist its profile, over REST or SSH.

    Routing mirrors every other tool: the gateway from _content_gateway decides the
    transport. A RestContentGateway -> REST discovery from the plugin /info (no SSH);
    anything else -> the SSH/WP-CLI fingerprint. Both persist through the SAME
    store.upsert so _resolve_editable / the edit layer read one profile shape. Uses
    _site_or_synthetic so a credentialed-but-unregistered install is discoverable too.
    """
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"error": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw = _content_gateway(site, gateway)
    if isinstance(gw, RestContentGateway):
        profile = await rest_discover(gw, site, now=now)
        store.upsert(profile)                     # payload owns REST persistence
        return {**_profile_summary(profile), "transport": "rest"}
    if gw is None:
        # rest-mode without creds / malformed WPOPS_TRANSPORT: fail closed rather than
        # loading the SSH transport (the cloud REST-only guarantee).
        return {"error": f"transport unavailable for {install}: "
                         f"WPOPS_TRANSPORT={os.environ.get('WPOPS_TRANSPORT', 'auto')!r} "
                         "invalid or no REST credentials (data/wp_ops_mcp/credentials.json)"}
    # SSH/WP-CLI path: discovery runs over the raw SSH transport, not the content
    # gateway (gw is only the router here). Lazy wpcli import keeps the cloud REST-only
    # build from loading SSH code at module import time.
    t = transport
    if t is None:
        from .transport.wpcli import WPCliTransport
        t = WPCliTransport(site.install)
    profile = await discover_site(site, t, store, now=now)
    return {**_profile_summary(profile), "transport": "wpcli"}


def _content_gateway(site: Site, gateway=None):
    """Select a content gateway per WPOPS_TRANSPORT (auto | rest | wpcli).

    - explicit `gateway` (the test/contract seam) always wins;
    - the env value is normalized (strip + lowercase) then validated against the
      allowlist; ANY other value (a typo, "REST ", "ssh", an empty string) FAILS
      CLOSED to None so a malformed setting can never silently load SSH code -
      this is the cloud REST-only guarantee (allowlist, not denylist);
    - `rest`: REST creds present -> RestContentGateway, else None (caller refuses);
    - `auto` (default): REST creds present -> REST, else fall through to wpcli;
    - `wpcli`: SSH/WP-CLI gateway.

    Cloud REST-only rule: the wpcli transport (SSH) is imported lazily, INSIDE the
    branch that needs it, so a REST-only deployment never loads SSH code or keys.
    """
    if gateway is not None:
        return gateway
    mode = os.environ.get("WPOPS_TRANSPORT", "auto").strip().lower()
    if mode not in ("auto", "rest", "wpcli"):
        return None  # fail closed: unknown/malformed mode never loads SSH
    if mode in ("rest", "auto"):
        creds = load_credentials(site.install)
        if creds is not None:
            client = WPRestClient(creds.base_url, app_user=creds.username,
                                  app_password=creds.app_password,
                                  user_agent=creds.user_agent)
            return RestContentGateway(client)
        if mode == "rest":
            return None
    from .transport.wpcli import WPCliTransport          # lazy: cloud runs REST-only
    from .ops.wpcli_gateway import WPCliContentGateway
    return WPCliContentGateway(WPCliTransport(site.install))


def _gateway_or_refusal(site: Site, gateway=None):
    """Resolve a content gateway, or a refusal dict when REST creds are absent.

    Returns (gateway, None) on success or (None, refusal). Every payload that
    builds a gateway routes through this so a missing-credentials rest-mode run
    yields a uniform {"action": "refused", ...} instead of an opaque crash.
    """
    gw = _content_gateway(site, gateway)
    if gw is None:
        return None, {"action": "refused",
                      "reason": (
                          f"transport unavailable for {site.install}: "
                          f"WPOPS_TRANSPORT={os.environ.get('WPOPS_TRANSPORT', 'auto')!r} "
                          "invalid or no REST credentials "
                          "(data/wp_ops_mcp/credentials.json)")}
    return gw, None


def _seo_gateway_or_refusal(site: Site, gateway=None):
    """Resolve a SEO-capable gateway, or a refusal dict.

    Layers the SEO transport check on top of _gateway_or_refusal: SEO meta mapping
    lives entirely on the REST path, so only the REST gateway exposes
    ``ensure_seo_capable`` - the SSH/wpcli gateway is refused. Returns (gw, None) on
    success or (None, refusal).
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "ensure_seo_capable"):
        return None, {"action": "refused",
                      "reason": "SEO tools require the REST transport "
                                "(wpcli gateway has no SEO support)"}
    return gw, None


def _media_gateway_or_refusal(site: Site, gateway=None):
    """Resolve a media-capable gateway, or a refusal dict.

    Same shape as _seo_gateway_or_refusal: binary upload lives only on the REST path
    (WP core's media endpoint), so only the REST gateway exposes ``upload_media`` -
    the SSH/wpcli gateway is refused rather than silently doing nothing.
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "upload_media"):
        return None, {"action": "refused",
                      "reason": "media upload requires the REST transport "
                                "(wpcli gateway has no media support)"}
    return gw, None


def _menu_gateway_or_refusal(site: Site, gateway=None):
    """Resolve a menu-capable gateway, or a refusal dict.

    Same shape as _seo_gateway_or_refusal: menus live on WP core's REST endpoints
    (/wp/v2/menus + /wp/v2/menu-items), so only the REST gateway exposes
    ``list_menus`` - the SSH/wpcli gateway is refused.
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "list_menus"):
        return None, {"action": "refused",
                      "reason": "menus require the REST transport "
                                "(wpcli gateway has no menu support)"}
    return gw, None


def _site_gateway_or_refusal(site: Site, gateway=None):
    """Resolve a slug/settings-capable gateway, or a refusal dict.

    Same shape as _menu_gateway_or_refusal: slug renames and site settings live on WP
    core's REST endpoints (/wp/v2/{pages,posts}/{id} and /wp/v2/settings), so only the
    REST gateway exposes ``update_post_slug`` - the SSH/wpcli gateway is refused.
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "update_post_slug"):
        return None, {"action": "refused",
                      "reason": "slug/settings tools require the REST transport "
                                "(wpcli gateway has no slug/settings support)"}
    return gw, None


def _acf_gateway_or_refusal(site: Site, gateway=None):
    """Resolve an ACF-capable gateway, or a refusal dict.

    Same shape as _seo_gateway_or_refusal: ACF reads/writes go through wp-ops-connect's
    /acf route (ACF's own get_fields/update_field), which only the REST gateway exposes
    as ``get_acf_fields`` - the SSH/wpcli gateway is refused. Note the deeper "ACF not
    active on this site" case is NOT a transport refusal: the gateway IS REST-capable,
    so it surfaces from AcfOps as an ``{"action": "error"}`` dict, not from here.
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "get_acf_fields"):
        return None, {"action": "refused",
                      "reason": "ACF tools require the REST transport "
                                "(wpcli gateway has no ACF support)"}
    return gw, None


def _admin_gateway_or_refusal(site: Site, gateway=None):
    """Resolve a user/comment-capable gateway, or a refusal dict.

    Same shape as _menu_gateway_or_refusal: users and comments live on WP core's REST
    endpoints (/wp/v2/users + /wp/v2/comments), so only the REST gateway exposes
    ``list_users`` - the SSH/wpcli gateway is refused. The deeper "this app password is
    not an administrator" case is NOT a transport refusal: the gateway IS REST-capable,
    so WP's 403 surfaces from AdminOps as an error dict.
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "list_users"):
        return None, {"action": "refused",
                      "reason": "admin tools require the REST transport "
                                "(wpcli gateway has no user/comment support)"}
    return gw, None


def _options_gateway_or_refusal(site: Site, gateway=None):
    """Resolve an arbitrary-option-capable gateway, or a refusal dict.

    Arbitrary options ride wp-ops-connect's /option route (core REST's /wp/v2/settings
    only reaches registered options), which only the REST gateway exposes as
    ``get_option``. The plugin VERSION floor (1.3.0) is a different failure and is not
    checked here - it surfaces from AdminOps as an error dict, exactly like ACF's
    "not active" case.
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "get_option"):
        return None, {"action": "refused",
                      "reason": "admin tools require the REST transport "
                                "(wpcli gateway has no option support)"}
    return gw, None


def _theme_file_gateway_or_refusal(site: Site, gateway=None):
    """Resolve a theme-file-capable gateway, or a refusal dict.

    Theme files ride wp-ops-connect's /theme-file route, which only the REST gateway
    exposes. Checked on ``get_theme_file`` specifically rather than borrowing the
    options check - a gateway can support options without supporting theme files, and
    the plugin VERSION floor (1.4.0) is a separate failure raised by the gateway.
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "get_theme_file"):
        return None, {"action": "refused",
                      "reason": "theme file tools require the REST transport and "
                                "wp-ops-connect 1.4.0+"}
    return gw, None


def _term_gateway_or_refusal(site: Site, gateway=None):
    """Resolve a taxonomy/term-capable gateway, or a refusal dict.

    Same shape as _menu_gateway_or_refusal: taxonomies and terms live on WP core's own
    REST endpoints (/wp/v2/taxonomies plus each taxonomy's rest_base), so only the REST
    gateway exposes ``list_taxonomies`` - the SSH/wpcli gateway is refused. "That
    taxonomy is not registered on this site" is NOT a transport refusal: the gateway IS
    REST-capable, so it surfaces from TermOps as an error dict naming the taxonomies
    that do exist.
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "list_taxonomies"):
        return None, {"action": "refused",
                      "reason": "taxonomy tools require the REST transport "
                                "(wpcli gateway has no taxonomy/term support)"}
    return gw, None


def _plugin_gateway_or_refusal(site: Site, gateway=None):
    """Resolve a plugin-capable gateway, or a refusal dict.

    Same shape as _menu_gateway_or_refusal: the plugin inventory lives on WP core's own
    /wp/v2/plugins route, so only the REST gateway exposes ``list_plugins`` - the
    SSH/wpcli gateway is refused. "This WordPress is older than 5.5, so the route does
    not exist" and "this app password lacks activate_plugins" are NOT transport
    refusals: the gateway IS REST-capable, so both surface from PluginOps as error dicts
    naming the real cause.
    """
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return None, refusal
    if not hasattr(gw, "list_plugins"):
        return None, {"action": "refused",
                      "reason": "plugin tools require the REST transport "
                                "(wpcli gateway has no plugin support)"}
    return gw, None


async def get_content_payload(
    registry: SiteRegistry,
    install: str,
    post_type: str = "page",
    query: str | None = None,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"error": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    ops = ContentOps(gw)
    return await ops.get_content(post_type, query=query)


async def _apply_create_seo(gw, result: dict, seo: dict, dry_run: bool) -> None:
    """Thread create-time SEO into a create_content result, mutating it in place.

    dry_run: echo the requested SEO as ``seo_preview`` (writes nothing). Real run:
    for each created item, apply the SEO meta and attach the per-item outcome under
    ``seo`` - or a per-item refusal when the gateway is the SSH/wpcli one (no SEO
    support), so a create still succeeds and only the SEO step is flagged.
    """
    if dry_run:
        result["seo_preview"] = seo
        return
    seo_ops = SeoOps(gw) if hasattr(gw, "ensure_seo_capable") else None
    for item in result.get("items", []):
        if item.get("action") != "created":
            continue
        if seo_ops is not None:
            item["seo"] = await seo_ops.set_seo(item["id"], seo, dry_run=False)
        else:
            item["seo"] = {"action": "refused", "reason": "SEO requires REST transport"}


async def create_content_payload(
    registry: SiteRegistry,
    store: ProfileStore,
    install: str,
    items: list[dict],
    post_type: str = "page",
    dry_run: bool = True,
    allow_prod: bool = False,
    seo: dict | None = None,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"error": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    if seo is not None and not isinstance(seo, dict):
        return {"error": "seo must be an object"}
    profile = store.get(install)
    if profile is None:
        return {"error": f"no profile for {install} — run wp_discover_site first "
                         "(needed to know the builder/Divi version)"}
    if not dry_run and site.environment == "prod" and not allow_prod:
        return {"error": f"{install} is prod; refusing to apply writes. "
                         "Re-run with allow_prod=true to override (or use dry_run)."}
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    ops = ContentOps(gw)
    result = await ops.create_content(
        profile.builder, profile.divi_major, post_type, items, dry_run=dry_run)
    if seo is not None:
        await _apply_create_seo(gw, result, seo, dry_run)
    return result


# --- Phase 4a edit layer ----------------------------------------------------
# Builder dialect mapping + the shared edit preconditions (discovered profile,
# editable builder, prod gate). These mirror wp_create_content's wiring but speak
# the edit layer's {"action": ...} result vocabulary (as EditOps does) instead of
# {"error": ...}, so a caller gets one coherent shape across the whole edit flow.

def _edit_builder_for(profile) -> str | None:
    """Map a discovered profile to an editable builder dialect, or None.

    Accepts either a SiteProfile (attribute access, the real runtime shape) or a
    plain dict with the same fields (used by contract tests). divi+4 -> "divi4",
    divi+5 -> "divi5", gutenberg -> "gutenberg"; anything else is not editable.
    """
    get = profile.get if isinstance(profile, dict) else lambda k: getattr(profile, k, None)
    b = get("builder")
    if b == "divi":
        return {4: "divi4", 5: "divi5"}.get(get("divi_major"))
    if b == "gutenberg":
        return "gutenberg"
    return None


def _tree_summary(node, address=None) -> list[dict]:
    """Recursive element-only summary of a parsed content tree (skips #text)."""
    address = address or []
    out = []
    for i, child in enumerate(node.children):
        if child.tag == "#text":
            continue
        caddr = address + [i]
        out.append({
            "address": caddr,
            "tag": child.tag,
            "admin_label": child.attrs.get("admin_label"),
            "text_preview": (child.content or "")[:80],
            "children": _tree_summary(child, caddr),
        })
    return out


def _resolve_editable(registry, store, install):
    """Shared edit precondition: resolve (site, profile, builder) or an error dict.

    Returns (site, profile, builder, None) on success, else (None, None, None, err)
    where err is a refusal/error dict ready to return to the caller.
    """
    site = _site_or_synthetic(registry, install)
    if site is None:
        return None, None, None, {
            "action": "refused",
            "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    profile = store.get(install)
    if profile is None:
        return None, None, None, {
            "action": "refused",
            "reason": f"no profile for {install} — run wp_discover_site first "
                      "(needed to know the builder/Divi version)"}
    builder = _edit_builder_for(profile)
    if builder is None:
        return None, None, None, {
            "action": "refused",
            "reason": f"builder {profile.builder!r} not editable"}
    return site, profile, builder, None


def _prod_write_blocked(site, allow_prod: bool, dry_run: bool = False):
    """Prod gate for edit writes (mirrors wp_create_content's inline check)."""
    if not dry_run and site.environment == "prod" and not allow_prod:
        return {"action": "refused",
                "reason": f"{site.install} is prod; refusing to apply writes. "
                          "Re-run with allow_prod=true to override (or use dry_run)."}
    return None


async def _title_for(gateway, post_id) -> str | None:
    """Best-effort page/post title lookup for the staged preview_hint.

    Truly best-effort: a successful stage must never be lost to a title lookup
    failure, so any exception here degrades to None (caller falls back to id).
    """
    try:
        for post_type in ("page", "post"):
            for p in await gateway.list_posts(post_type):
                if p.get("id") == post_id:
                    return p.get("title")
    except Exception:
        return None
    return None


async def find_page_payload(
    registry: SiteRegistry,
    install: str,
    query: str,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    ops = ContentOps(gw)
    truncated = False
    try:
        pages = await ops.get_content("page", query=query)
        truncated = truncated or getattr(gw, "last_list_truncated", False)
        posts = await ops.get_content("post", query=query)
        truncated = truncated or getattr(gw, "last_list_truncated", False)
    except Exception as e:  # bounded find can still fail (SSH/parse) -> dict, never raise
        return {"action": "error", "error": f"{type(e).__name__}: {e}"}
    items = pages["items"] + posts["items"]
    result = {"count": len(items), "items": items}
    # The gateway resets last_list_truncated per list_posts call, so the pages listing's
    # flag would be lost by the time posts is listed - OR it after each read. Only the
    # REST gateway sets the attr; wpcli/fake gateways lack it and getattr yields False.
    if truncated:
        result["truncated"] = True
    return result


async def extract_page_payload(
    registry: SiteRegistry,
    store: ProfileStore,
    install: str,
    post_id: int,
    gateway=None,
) -> dict:
    site, _profile, builder, err = _resolve_editable(registry, store, install)
    if err is not None:
        return err
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    editor = EditOps(gw, builder)  # single source of truth for the dialect parser
    try:
        content = await gw.get_post_content(post_id)
    except Exception as e:  # expected failure (e.g. post not found) -> dict, never raise
        return {"action": "error", "error": f"{type(e).__name__}: {e}"}
    # rt_ok gate first (mirrors EditOps.edit_page): parse() raises ParseError on
    # malformed Divi/block content, which must not cross the tool boundary. rt_ok
    # swallows that and returns False -> refuse; a True result guarantees parse()
    # succeeds, so we parse exactly once for the tree summary.
    if not editor.rt_ok(content):
        return {"action": "refused",
                "reason": "round-trip mismatch - page needs a human"}
    root = editor.parse(content)
    return {"builder": builder,
            "roundtrip_ok": True,
            "tree": _tree_summary(root)}


async def edit_page_payload(
    registry: SiteRegistry,
    store: ProfileStore,
    install: str,
    post_id: int,
    ops: list[dict],
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site, _profile, builder, err = _resolve_editable(registry, store, install)
    if err is not None:
        return err
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    result = await EditOps(gw, builder).edit_page(post_id, ops, dry_run=dry_run)
    if result.get("action") == "staged":
        title = await _title_for(gw, post_id) or f"post {post_id}"
        result["preview_hint"] = (
            f"wp-admin > Pages > drafts: '{title} [wpops draft]' "
            f"(id {result['draft_id']})")
    return result


async def publish_swap_payload(
    registry: SiteRegistry,
    store: ProfileStore,
    install: str,
    live_id: int,
    draft_id: int,
    allow_prod: bool = False,
    gateway=None,
    probe=None,
) -> dict:
    site, profile, builder, err = _resolve_editable(registry, store, install)
    if err is not None:
        return err
    gate = _prod_write_blocked(site, allow_prod)
    if gate is not None:
        return gate
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    result = await EditOps(gw, builder).publish_swap(live_id, draft_id)
    domain = getattr(profile, "domain", None)
    if domain and result.get("action") in ("published", "published_with_warnings"):
        probe_fn = probe or probe_url
        try:
            result["probe"] = await probe_fn(f"https://{domain}/?p={live_id}")
        except Exception as e:  # verification must never crash a completed publish
            result["probe"] = {"ok": False, "error": str(e)}
    return result


async def discard_draft_payload(
    registry: SiteRegistry,
    store: ProfileStore,
    install: str,
    draft_id: int,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site, _profile, builder, err = _resolve_editable(registry, store, install)
    if err is not None:
        return err
    gate = _prod_write_blocked(site, allow_prod)
    if gate is not None:
        return gate
    gw, refusal = _gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await EditOps(gw, builder).discard_draft(draft_id)


# --- Phase 5a SEO layer -----------------------------------------------------
# wp_get_seo / wp_set_seo map generic SEO fields (title/description/canonical/
# noindex) to the active plugin's post meta via SeoOps. SEO is builder-independent,
# so - unlike the edit layer - these do NOT require a discovered profile or an
# editable builder; they only require the REST transport (SEO lives on that path).

async def get_seo_payload(
    registry: SiteRegistry,
    install: str,
    post_id: int,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _seo_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await SeoOps(gw).get_seo(post_id)


async def set_seo_payload(
    registry: SiteRegistry,
    install: str,
    post_id: int,
    title: str | None = None,
    description: str | None = None,
    canonical: str | None = None,
    noindex: bool | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    # Only non-None params become fields (noindex=False is a real "index" value).
    fields = {name: value for name, value in (
        ("title", title), ("description", description),
        ("canonical", canonical), ("noindex", noindex)) if value is not None}
    if not fields:
        return {"action": "refused",
                "reason": "no SEO fields provided (title/description/canonical/noindex)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _seo_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await SeoOps(gw).set_seo(post_id, fields, dry_run=dry_run)


# --- Phase 5b media layer ---------------------------------------------------
# wp_upload_media pushes a LOCAL image file into a site's media library over REST.
# Like SEO, it is builder-independent (no profile/builder precondition) and REST-only.
# The allowlist/size/filename checks live in MediaOps; this layer owns the fleet
# guardrails: unknown install, prod gate, transport capability.

async def upload_media_payload(
    registry: SiteRegistry,
    install: str,
    file_path: str,
    alt: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _media_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await MediaOps(gw).upload(file_path, alt=alt, dry_run=dry_run)


# --- Phase 5d menu layer ----------------------------------------------------
# wp_list_menus / wp_add_menu_item / wp_update_menu_item / wp_remove_menu_item manage a
# site's navigation over WP core's menu REST endpoints. Builder-independent like
# SEO/media (no profile
# or builder precondition); this layer owns the fleet guardrails (unknown install,
# prod gate, transport capability) and MenuOps owns menu resolution + target
# validation + readback.

async def list_menus_payload(
    registry: SiteRegistry,
    install: str,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _menu_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await MenuOps(gw).list()


async def add_menu_item_payload(
    registry: SiteRegistry,
    install: str,
    menu: int | str,
    title: str,
    page_id: int | None = None,
    url: str | None = None,
    parent: int = 0,
    position: int | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _menu_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await MenuOps(gw).add(menu, title, page_id=page_id, url=url,
                                 parent=parent, position=position, dry_run=dry_run)


async def update_menu_item_payload(
    registry: SiteRegistry,
    install: str,
    item_id: int,
    title: str | None = None,
    position: int | None = None,
    parent: int | None = None,
    url: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _menu_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await MenuOps(gw).update(item_id, title=title, position=position,
                                    parent=parent, url=url, dry_run=dry_run)


async def remove_menu_item_payload(
    registry: SiteRegistry,
    install: str,
    item_id: int,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _menu_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await MenuOps(gw).remove(item_id, dry_run=dry_run)


# --- Phase 5e site layer ----------------------------------------------------
# wp_change_slug renames a page/post's URL slug (WP core 301s the old URL by itself);
# wp_get_settings / wp_set_setting read and write an ALLOWLISTED subset of the site's
# options. Builder-independent like SEO/media/menus (no profile or builder
# precondition); this layer owns the fleet guardrails (unknown install, prod gate,
# transport capability) and SiteOps owns slug sanitizing, the settings allowlist and
# the write verification.

async def change_slug_payload(
    registry: SiteRegistry,
    install: str,
    post_id: int,
    new_slug: str,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _site_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await SiteOps(gw).change_slug(post_id, new_slug, dry_run=dry_run)


async def get_settings_payload(
    registry: SiteRegistry,
    install: str,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _site_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await SiteOps(gw).get_settings()


async def set_setting_payload(
    registry: SiteRegistry,
    install: str,
    key: str,
    value,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _site_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await SiteOps(gw).set_setting(key, value, dry_run=dry_run)


# --- Phase 5i ACF layer -----------------------------------------------------
# wp_get_acf / wp_set_acf read and write a post's ACF field VALUES (not the field-group
# schema) through wp-ops-connect's /acf route, which calls ACF's own get_fields/
# update_field so field-key linkage stays correct. Builder-independent like SEO/media/
# menus/site (no profile or builder precondition); this layer owns the fleet guardrails
# (unknown install, fields shape, prod gate, transport capability) and AcfOps owns the
# dry-run preview and the readback verification. "ACF not active" is an AcfOps error
# dict, not a transport refusal (the transport IS REST) - see _acf_gateway_or_refusal.

async def get_acf_payload(
    registry: SiteRegistry,
    install: str,
    post_id: int,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _acf_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await AcfOps(gw).get(post_id)


async def set_acf_payload(
    registry: SiteRegistry,
    install: str,
    post_id: int,
    fields: dict,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    # Fields shape is checked ABOVE the prod gate, matching set_seo_payload: with nothing
    # to write there is no write to gate, so a prod install + empty fields must name the
    # caller's actual mistake instead of sending them back with allow_prod=true. AcfOps.set
    # keeps its own equivalent validation (defense in depth for direct ops callers).
    if not isinstance(fields, dict) or not fields:
        return {"action": "refused",
                "reason": "no ACF fields provided (pass an object of field->value)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _acf_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await AcfOps(gw).set(post_id, fields, dry_run=dry_run)


# --- Phase 5j admin layer ---------------------------------------------------
# Users (wp_list_users / wp_get_user / wp_create_user / wp_update_user /
# wp_delete_user), ARBITRARY options (wp_list_options / wp_get_option / wp_set_option /
# wp_delete_option) and comments (wp_list_comments / wp_moderate_comment /
# wp_delete_comment). The widest blast radius in the server and deliberately without an
# allowlist (full-parity authorization) - which is exactly why the layering is unchanged:
# this layer owns the fleet guardrails (unknown install, prod gate, transport
# capability) and AdminOps owns validation, previews, password masking and readback.
# The read-only tools carry NO prod gate: reading a prod site's users or options changes
# nothing, and gating it would only train operators to pass allow_prod reflexively.

async def list_users_payload(
    registry: SiteRegistry,
    install: str,
    search: str | None = None,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _admin_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await AdminOps(gw).list_users(search=search)


async def get_user_payload(
    registry: SiteRegistry,
    install: str,
    user_id: int,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _admin_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await AdminOps(gw).get_user(user_id)


async def create_user_payload(
    registry: SiteRegistry,
    install: str,
    username: str,
    email: str,
    password: str,
    roles: list | str | None = None,
    name: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _admin_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await AdminOps(gw).create_user(username, email, password, roles=roles,
                                          name=name, dry_run=dry_run)


async def update_user_payload(
    registry: SiteRegistry,
    install: str,
    user_id: int,
    fields: dict,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    # Fields shape is checked ABOVE the prod gate, matching set_acf_payload: with nothing
    # to write there is no write to gate, so a prod install + empty fields must name the
    # caller's actual mistake instead of sending them back with allow_prod=true. AdminOps
    # keeps its own equivalent validation (defense in depth for direct ops callers).
    if not isinstance(fields, dict) or not fields:
        return {"action": "refused",
                "reason": "no user fields provided (pass an object of field -> value: "
                          "name, email, roles, password, url, description)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _admin_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await AdminOps(gw).update_user(user_id, fields, dry_run=dry_run)


async def delete_user_payload(
    registry: SiteRegistry,
    install: str,
    user_id: int,
    reassign: int = 0,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _admin_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await AdminOps(gw).delete_user(user_id, reassign=reassign, dry_run=dry_run)


async def list_options_payload(
    registry: SiteRegistry,
    install: str,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _options_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await AdminOps(gw).list_options()


async def get_option_payload(
    registry: SiteRegistry,
    install: str,
    name: str,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _options_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await AdminOps(gw).get_option(name)


async def set_option_payload(
    registry: SiteRegistry,
    install: str,
    name: str,
    value,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _options_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await AdminOps(gw).set_option(name, value, dry_run=dry_run)


async def get_theme_file_payload(
    registry: SiteRegistry,
    install: str,
    file: str,
    theme: str | None = None,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _theme_file_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    data = await gw.get_theme_file(file, theme=theme)
    return {"action": "ok", **data}


async def set_theme_file_payload(
    registry: SiteRegistry,
    install: str,
    file: str,
    contents: str,
    theme: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
    allow_create: bool = False,
    allow_other_theme: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _theme_file_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal

    # Always read first: the preview shows what is being overwritten, and on a real write
    # the caller gets the old contents back so a revert needs no second round-trip.
    try:
        before = (await gw.get_theme_file(file, theme=theme)).get("contents")
    except Exception:
        before = None                      # a new file (allow_create) has no "before"

    if dry_run:
        return {"action": "preview", "file": file, "theme": theme,
                "current": before, "requested": contents,
                "current_bytes": 0 if before is None else len(before),
                "requested_bytes": len(contents),
                "unchanged": before == contents}

    data = await gw.set_theme_file(file, contents, theme=theme,
                                   allow_create=allow_create,
                                   allow_other_theme=allow_other_theme)
    return {"action": "applied", **data}


async def delete_option_payload(
    registry: SiteRegistry,
    install: str,
    name: str,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _options_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await AdminOps(gw).delete_option(name, dry_run=dry_run)


async def list_comments_payload(
    registry: SiteRegistry,
    install: str,
    post_id: int | None = None,
    status: str | None = None,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _admin_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await AdminOps(gw).list_comments(post_id=post_id, status=status)


async def moderate_comment_payload(
    registry: SiteRegistry,
    install: str,
    comment_id: int,
    status: str,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _admin_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await AdminOps(gw).moderate_comment(comment_id, status, dry_run=dry_run)


async def delete_comment_payload(
    registry: SiteRegistry,
    install: str,
    comment_id: int,
    force: bool = False,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _admin_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await AdminOps(gw).delete_comment(comment_id, force=force, dry_run=dry_run)


# --- Phase 5k taxonomy/term layer -------------------------------------------
# wp_list_taxonomies / wp_list_terms (read-only) and wp_create_term / wp_update_term /
# wp_delete_term / wp_set_post_terms (writes) manage a site's filing system over WP
# core's taxonomy REST endpoints. Builder-independent like SEO/media/menus/admin (no
# profile or builder precondition); this layer owns the fleet guardrails (unknown
# install, prod gate, transport capability) and TermOps owns validation, the previews,
# the create readback and the set-comparison that catches WP's silently dropped ids.
# The two read-only tools carry NO prod gate, matching wp_list_users/wp_list_menus:
# listing a prod site's taxonomies changes nothing.

async def list_taxonomies_payload(
    registry: SiteRegistry,
    install: str,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _term_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await TermOps(gw).list_taxonomies()


async def list_terms_payload(
    registry: SiteRegistry,
    install: str,
    taxonomy: str,
    search: str | None = None,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _term_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await TermOps(gw).list_terms(taxonomy, search=search)


async def create_term_payload(
    registry: SiteRegistry,
    install: str,
    taxonomy: str,
    name: str,
    slug: str | None = None,
    parent: int | None = None,
    description: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _term_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await TermOps(gw).create_term(taxonomy, name, slug=slug, parent=parent,
                                         description=description, dry_run=dry_run)


async def update_term_payload(
    registry: SiteRegistry,
    install: str,
    taxonomy: str,
    term_id: int,
    fields: dict,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    # Fields shape is checked ABOVE the prod gate, matching update_user_payload: with
    # nothing to write there is no write to gate, so a prod install + empty fields must
    # name the caller's actual mistake instead of sending them back with allow_prod=true.
    # TermOps keeps its own equivalent validation (defense in depth for direct callers).
    if not isinstance(fields, dict) or not fields:
        return {"action": "refused",
                "reason": "no term fields provided (pass an object of field -> value: "
                          "name, slug, parent, description)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _term_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await TermOps(gw).update_term(taxonomy, term_id, fields, dry_run=dry_run)


async def delete_term_payload(
    registry: SiteRegistry,
    install: str,
    taxonomy: str,
    term_id: int,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _term_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await TermOps(gw).delete_term(taxonomy, term_id, dry_run=dry_run)


async def set_post_terms_payload(
    registry: SiteRegistry,
    install: str,
    post_id: int,
    taxonomy: str,
    term_ids: list,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _term_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await TermOps(gw).set_post_terms(post_id, taxonomy, term_ids,
                                            dry_run=dry_run)


# --- Phase 5l plugin layer ---------------------------------------------------
# wp_list_plugins (read-only) and wp_activate_plugin / wp_deactivate_plugin (writes)
# read the installed plugin inventory and toggle activation over WP core's
# /wp/v2/plugins route. Builder-independent like SEO/media/menus/admin/terms (no profile
# or builder precondition); this layer owns the fleet guardrails (unknown install, prod
# gate, transport capability) and PluginOps owns identifier resolution, the previews,
# the status readback and the wp-ops-connect self-lockout refusal. wp_list_plugins
# carries NO prod gate, matching wp_list_users/wp_list_menus/wp_list_terms: reading a
# prod site's plugin inventory changes nothing.

async def list_plugins_payload(
    registry: SiteRegistry,
    install: str,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gw, refusal = _plugin_gateway_or_refusal(site, gateway)
    if refusal is not None:                       # read-only: no prod gate
        return refusal
    return await PluginOps(gw).list_plugins()


async def activate_plugin_payload(
    registry: SiteRegistry,
    install: str,
    plugin: str,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _plugin_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    return await PluginOps(gw).activate(plugin, dry_run=dry_run)


async def deactivate_plugin_payload(
    registry: SiteRegistry,
    install: str,
    plugin: str,
    dry_run: bool = True,
    allow_prod: bool = False,
    gateway=None,
) -> dict:
    site = _site_or_synthetic(registry, install)
    if site is None:
        return {"action": "refused",
                "reason": f"unknown install: {install} (run wp_list_sites to see valid ids)"}
    gate = _prod_write_blocked(site, allow_prod, dry_run=dry_run)
    if gate is not None:
        return gate
    gw, refusal = _plugin_gateway_or_refusal(site, gateway)
    if refusal is not None:
        return refusal
    # The self-lockout refusal itself lives in PluginOps, not here: it must hold for
    # every caller of the ops layer, not just for the ones that come through this tool.
    return await PluginOps(gw).deactivate(plugin, dry_run=dry_run)


@mcp.tool()
async def wp_list_sites(
    account: str | None = None,
    environment: str | None = None,
    query: str | None = None,
) -> dict:
    """List fleet sites (install, account, environment, domain).

    Filter by `account` (hostacct1-6), `environment` (prod|staging), or `query`
    (case-insensitive substring of install name or domain). Returns ids + count only.
    """
    return list_sites_payload(get_registry(), account=account, environment=environment, query=query)


@mcp.tool()
async def wp_site_health(install: str) -> dict:
    """Check one install's reachability: WP REST (if a domain is known) + SSH/WP-CLI.

    `install` is the WP Engine install name (e.g. 'daytonplas1stg').
    """
    return await site_health_payload(get_registry(), install)


@mcp.tool()
async def wp_discover_site(install: str) -> dict:
    """Fingerprint an install (active theme, builder + Divi major version, WP/PHP
    versions, plugin count, multisite) and persist the profile. Transport-aware: an
    install with REST credentials is fingerprinted over the plugin /info endpoint
    (no SSH); otherwise over one SSH/WP-CLI session.

    This is the learning entrypoint: content/settings tools expect a profile to exist.
    `install` is the WP Engine install name.
    """
    return await discover_site_payload(get_registry(), get_store(), install)


@mcp.tool()
async def wp_get_content(install: str, post_type: str = "page", query: str | None = None) -> dict:
    """List posts/pages on an install (lean: id, title, slug, status, type).

    `post_type` defaults to 'page'. `query` is an optional simple search term.
    """
    return await get_content_payload(get_registry(), install, post_type=post_type, query=query)


@mcp.tool()
async def wp_create_content(
    install: str,
    items: list[dict],
    post_type: str = "page",
    dry_run: bool = True,
    allow_prod: bool = False,
    seo: dict | None = None,
) -> dict:
    """Bulk-create posts/pages, rendered into the site's builder (Divi 4 / Gutenberg).

    Each item: {title, blocks:[...], slug?, status?}. Blocks are builder-agnostic:
    {kind:'heading',level,text} | {kind:'paragraph',text} | {kind:'button',text,url} |
    {kind:'image',url,alt?} (use the url returned by wp_upload_media).
    Optional `seo` (an object: title/description/canonical/noindex) is applied to every
    created item's meta (REST transport only). DEFAULTS TO dry_run=true (returns a
    per-item plan + a seo_preview; writes nothing). Pass dry_run=false to apply.
    Idempotent: existing slugs are skipped. Requires a profile (run wp_discover_site
    first). Prod writes require allow_prod=true.
    """
    return await create_content_payload(
        get_registry(), get_store(), install, items,
        post_type=post_type, dry_run=dry_run, allow_prod=allow_prod, seo=seo)


@mcp.tool()
async def wp_find_page(install: str, query: str) -> dict:
    """Find pages AND posts matching `query`, merged (id, title, slug, status, type).

    Read-only. Use this to locate the post_id to feed wp_extract_page / wp_edit_page.
    `install` is the WP Engine install name; `query` is a simple search term.
    """
    return await find_page_payload(get_registry(), install, query)


@mcp.tool()
async def wp_extract_page(install: str, post_id: int) -> dict:
    """Parse one page into an editable element tree (read-only, no writes).

    Returns {builder, roundtrip_ok, tree} where tree is a recursive summary of
    element nodes: address (index path for edit targeting), tag, admin_label,
    text_preview (first 80 chars), children. Requires a profile (wp_discover_site).
    """
    return await extract_page_payload(get_registry(), get_store(), install, post_id)


@mcp.tool()
async def wp_edit_page(
    install: str,
    post_id: int,
    ops: list[dict],
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Edit an existing page via a draft duplicate (the live post is never written).

    Ops (list of dicts): update_element, set_attr, insert_section, insert_module,
    remove_element, duplicate_element. DEFAULTS TO dry_run=true (returns a preview,
    writes nothing). Pass dry_run=false to stage the edit onto a draft duplicate;
    promote it later with wp_publish_swap. Requires a profile (wp_discover_site) and
    an editable builder (Divi 4/5 or Gutenberg). Prod writes require allow_prod=true.
    """
    return await edit_page_payload(
        get_registry(), get_store(), install, post_id, ops,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_publish_swap(
    install: str,
    live_id: int,
    draft_id: int,
    allow_prod: bool = False,
) -> dict:
    """Promote a staged draft onto the live post (WP keeps a revision), then verify.

    Copies the draft's content onto `live_id`, purges cache (Divi), deletes the
    draft, and probes the public URL. Prod writes require allow_prod=true.
    """
    return await publish_swap_payload(
        get_registry(), get_store(), install, live_id, draft_id, allow_prod=allow_prod)


@mcp.tool()
async def wp_discard_draft(install: str, draft_id: int, allow_prod: bool = False) -> dict:
    """Delete a staged wpops draft without publishing. Prod writes require allow_prod=true."""
    return await discard_draft_payload(
        get_registry(), get_store(), install, draft_id, allow_prod=allow_prod)


@mcp.tool()
async def wp_get_seo(install: str, post_id: int) -> dict:
    """Read a page/post's SEO meta: title, description, canonical, noindex.

    Maps the active SEO plugin's meta (SEOPress / Rank Math / Yoast) to generic fields.
    Read-only. Requires the REST transport (the wpcli/SSH gateway has no SEO support).
    `install` is the WP Engine install name; get `post_id` from wp_find_page / wp_get_content.
    """
    return await get_seo_payload(get_registry(), install, post_id)


@mcp.tool()
async def wp_set_seo(
    install: str,
    post_id: int,
    title: str | None = None,
    description: str | None = None,
    canonical: str | None = None,
    noindex: bool | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Set a page/post's SEO meta (title/description/canonical/noindex).

    Pass only the fields to change; omitted (None) fields are left untouched, and
    supplying none at all is refused. Writes to the active SEO plugin's meta (SEOPress
    / Rank Math / Yoast) and is verified by readback. DEFAULTS TO dry_run=true (returns
    a preview; writes nothing). Pass dry_run=false to apply. Requires the REST transport.
    Prod writes require allow_prod=true.
    """
    return await set_seo_payload(
        get_registry(), install, post_id,
        title=title, description=description, canonical=canonical, noindex=noindex,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_upload_media(
    install: str,
    file_path: str,
    alt: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Upload a LOCAL image file into a site's media library, with optional alt text.

    `file_path` is a path on the machine running this server (not a URL). Images only:
    .png/.jpg/.jpeg/.gif/.webp, max 10 MB; .svg is refused (script vector). The
    filename is sanitized and the directory dropped. DEFAULTS TO dry_run=true (returns
    a preview: filename/mime/bytes; uploads nothing). Pass dry_run=false to upload -
    the returned `url` is what you feed to an {kind:'image'} block in wp_create_content
    or wp_edit_page. Requires the REST transport. Prod writes require allow_prod=true.
    """
    return await upload_media_payload(
        get_registry(), install, file_path,
        alt=alt, dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_list_menus(install: str) -> dict:
    """List a site's navigation menus (id, name, slug, theme locations).

    Read-only. Start here: the id or exact name/slug it returns is what you pass as
    `menu` to wp_add_menu_item. Menu items are not fetched. Requires the REST
    transport (the wpcli/SSH gateway has no menu support).
    """
    return await list_menus_payload(get_registry(), install)


@mcp.tool()
async def wp_add_menu_item(
    install: str,
    menu: int | str,
    title: str,
    page_id: int | None = None,
    url: str | None = None,
    parent: int = 0,
    position: int | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Add one item to a navigation menu (a site page, or a custom link).

    `menu` is a menu id, or its EXACT name or slug (from wp_list_menus) - a reference
    matching 0 or 2+ menus is refused and lists the available menus. Pass exactly one
    target: `page_id` (from wp_find_page / wp_get_content) for a page on the site, or
    `url` for a custom link. `parent` is the id of the menu item to nest under (0 =
    top level); `position` is the item's menu_order (omit to append at the end).
    DEFAULTS TO dry_run=true (returns a preview; writes nothing). Pass dry_run=false
    to apply - the add is verified by reading the menu back. Requires the REST
    transport. Prod writes require allow_prod=true.
    """
    return await add_menu_item_payload(
        get_registry(), install, menu, title, page_id=page_id, url=url,
        parent=parent, position=position, dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_update_menu_item(
    install: str,
    item_id: int,
    title: str | None = None,
    position: int | None = None,
    parent: int | None = None,
    url: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Change one existing menu item: rename it, move it, re-nest it, or retarget it.

    `item_id` is the menu ITEM id (from wp_list_menus -> the menu's items), not a page
    id. Only the arguments you pass are changed - omit the rest and they are left
    alone; passing none at all is refused rather than reported as a no-op success.
    `position` is the item's menu_order (0 = first) and `parent` the id of the item to
    nest under (0 = top level); `url` retargets a custom link (use wp_remove_menu_item
    + wp_add_menu_item to repoint an item at a different page). DEFAULTS TO
    dry_run=true (returns a preview of the exact fields; writes nothing). Pass
    dry_run=false to apply. Requires the REST transport. Prod writes require
    allow_prod=true.
    """
    return await update_menu_item_payload(
        get_registry(), install, item_id, title=title, position=position,
        parent=parent, url=url, dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_remove_menu_item(
    install: str,
    item_id: int,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Remove one item from a navigation menu (the page it links to is NOT deleted).

    `item_id` is the menu ITEM id - not a page id. DEFAULTS TO dry_run=true (returns
    a preview; deletes nothing). Pass dry_run=false to apply. Requires the REST
    transport. Prod writes require allow_prod=true.
    """
    return await remove_menu_item_payload(
        get_registry(), install, item_id, dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_change_slug(
    install: str,
    post_id: int,
    new_slug: str,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Rename one page/post's URL slug (the old URL usually starts 404ing - read on).

    CHECK `old_url_redirects` IN THE RESULT. WordPress only 301s the old URL for
    published POSTS; for PAGES (hierarchical, and what most site URLs are) it does not,
    so the old URL 404s until a redirect is added elsewhere. Both the preview and the
    result say which case this is, with a `warning` when the old URL will break.

    `post_id` comes from wp_find_page / wp_get_content. `new_slug` is sanitized to
    WP-safe form (lowercase, a-z 0-9 and hyphens; anything else becomes a hyphen) - a
    value with nothing usable left is refused rather than sent. If the slug is already
    taken, WP stores a uniquified one (about -> about-2): that still succeeds and comes
    back with `uniquified: true` and the ACTUAL slug, so check it. DEFAULTS TO
    dry_run=true (returns a preview with the current slug; writes nothing). Pass
    dry_run=false to apply - the rename is verified by reading the post back. Requires
    the REST transport. Prod writes require allow_prod=true.
    """
    return await change_slug_payload(
        get_registry(), install, post_id, new_slug,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_get_settings(install: str) -> dict:
    """Read a site's editable settings: title, description, timezone, posts_per_page,
    show_on_front, page_on_front, page_for_posts.

    Read-only, and deliberately narrow: only those seven fields are returned (the site
    URL, admin email and language are never exposed or editable through this server).
    Requires the REST transport, and an ADMINISTRATOR app password - WP's settings
    endpoint refuses lesser roles.
    """
    return await get_settings_payload(get_registry(), install)


@mcp.tool()
async def wp_set_setting(
    install: str,
    key: str,
    value: str | int,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Change ONE site setting (see wp_get_settings for the current values).

    `key` must be one of: title, description, timezone (e.g. 'America/Denver'),
    posts_per_page (int), show_on_front ('posts' or 'page'), page_on_front (page id),
    page_for_posts (page id). Any other key is refused - the site URL and admin email
    are not editable here by design. `page_on_front` is NOT checked against the site's
    pages: a wrong id blanks the homepage, so read it back with wp_get_settings and look
    at the site. DEFAULTS TO dry_run=true (returns a preview; writes nothing). Pass
    dry_run=false to apply - the write is verified against what the site reports back,
    and the result carries the site's ACTUAL stored `value` next to what you
    `requested` (WordPress escapes text as it stores it, so a title with '&' or an
    apostrophe comes back encoded - that is the site behaving normally). Requires the
    REST transport and an administrator app password. Prod writes require
    allow_prod=true.
    """
    return await set_setting_payload(
        get_registry(), install, key, value,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_get_acf(install: str, post_id: int) -> dict:
    """Read a page/post's ACF (Advanced Custom Fields) values as a JSON object.

    Returns every ACF field value on the post, keyed by field name - strings, numbers,
    booleans, and lists (repeaters/galleries) pass through untouched. Read-only.
    Requires the REST transport AND ACF active on the site (ACF free or Pro); a site
    without ACF comes back as an error, not a crash. `install` is the WP Engine install
    name; get `post_id` from wp_find_page / wp_get_content.
    """
    return await get_acf_payload(get_registry(), install, post_id)


@mcp.tool()
async def wp_set_acf(
    install: str,
    post_id: int,
    fields: dict,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Set a page/post's ACF field VALUES (not the field-group schema).

    `fields` is an object of {field_name: value}; values are arbitrary JSON (string,
    number, bool, or a list for a repeater/gallery) and are written through ACF's own
    update_field so the field-key linkage is correct. Leading-underscore selectors are
    rejected by the site and reported under `skipped` (the rest still apply, and the
    result comes back as 'partial' rather than 'applied'). DEFAULTS TO dry_run=true
    (returns a preview; writes nothing). Pass dry_run=false to apply - the write is
    verified by reading each value back (ACF's storage normalization, e.g. a true/false
    stored as 1, is tolerated). Only fields on an ACF field group already registered for
    the post take effect. Requires the REST transport and ACF active. Prod writes require
    allow_prod=true.
    """
    return await set_acf_payload(
        get_registry(), install, post_id, fields,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_list_users(install: str, search: str | None = None) -> dict:
    """List a site's WordPress users (id, username, name, email, roles).

    Read-only. `search` is an optional substring WordPress matches against the login,
    name and email. Start here: the `id` it returns is what you pass to wp_get_user /
    wp_update_user / wp_delete_user. Requires the REST transport AND an ADMINISTRATOR
    app password - WP's user endpoints refuse lesser roles (that comes back as an error,
    not a crash).
    """
    return await list_users_payload(get_registry(), install, search=search)


@mcp.tool()
async def wp_get_user(install: str, user_id: int) -> dict:
    """Read one WordPress user by id (username, name, email, roles, url).

    Read-only. Get `user_id` from wp_list_users. Requires the REST transport and an
    administrator app password.
    """
    return await get_user_payload(get_registry(), install, user_id)


@mcp.tool()
async def wp_create_user(
    install: str,
    username: str,
    email: str,
    password: str,
    roles: list[str] | str | None = None,
    name: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Create a WordPress user with a role and a password.

    `username` cannot be changed later (WordPress forbids it) and `email` must not
    already be in use. `roles` is a role slug or a list of them ('subscriber',
    'editor', 'administrator', ...); omit it and WP applies the site's default role.
    THE PASSWORD IS NEVER ECHOED BACK - it is sent to the site and replaced by a marker
    in every preview and result, so it does not end up in this transcript. Choose a
    strong one and record it in your password manager BEFORE calling: this server
    cannot show it to you afterwards. DEFAULTS TO dry_run=true (returns a preview;
    creates nothing). Pass dry_run=false to create. Requires the REST transport and an
    administrator app password. Prod writes require allow_prod=true.
    """
    return await create_user_payload(
        get_registry(), install, username, email, password,
        roles=roles, name=name, dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_update_user(
    install: str,
    user_id: int,
    fields: dict,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Change an existing user's fields.

    `fields` is an object; the editable keys are name, email, roles, password, url,
    description - ANY OTHER KEY IS REFUSED (WordPress silently ignores unknown fields,
    which would report a change that never happened). `username` is not editable.
    Setting `roles` REPLACES the user's roles, so passing ['subscriber'] on an
    administrator demotes them. A `password` here is a real password reset: the user's
    old password stops working immediately, and the new one is masked in the preview
    exactly as in wp_create_user. An empty `fields` is refused rather than reported as
    a successful no-op. DEFAULTS TO dry_run=true (returns a preview; writes nothing).
    Pass dry_run=false to apply. Requires the REST transport and an administrator app
    password. Prod writes require allow_prod=true.
    """
    return await update_user_payload(
        get_registry(), install, user_id, fields,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_delete_user(
    install: str,
    user_id: int,
    reassign: int = 0,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """PERMANENTLY delete a user; their content goes to `reassign` (0 = deleted too).

    There is no trash and no undo for a user: WordPress's REST API only deletes with
    force, so this cannot be reversed from here. `reassign` decides what happens to
    everything they authored - posts, pages, media: the DEFAULT of 0 DELETES THEIR
    CONTENT WITH THEM, while any other user id transfers authorship to that user (get
    one from wp_list_users). If the account wrote anything you want to keep, pass a
    reassign target. DEFAULTS TO dry_run=true (returns a preview that spells out which
    of the two outcomes you are about to get; deletes nothing). Pass dry_run=false to
    delete. Requires the REST transport and an administrator app password. Prod writes
    require allow_prod=true.
    """
    return await delete_user_payload(
        get_registry(), install, user_id, reassign=reassign,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_list_options(install: str) -> dict:
    """List the site's AUTOLOADED option names (names only, no values).

    Read-only. LIMITATION, and it matters: this lists autoloaded options only (WP's
    wp_load_alloptions). An option that is not autoloaded is ABSENT FROM THIS LIST but
    still readable with wp_get_option - so a missing name here does NOT prove the
    option does not exist. Requires the REST transport, wp-ops-connect 1.3.0+ and an
    administrator app password (the endpoint gates on manage_options).
    """
    return await list_options_payload(get_registry(), install)


@mcp.tool()
async def wp_get_option(install: str, name: str) -> dict:
    """Read one wp_options row by exact name (works for non-autoloaded options too).

    Read-only. CHECK `exists`, not the value: an option may legitimately hold '', 0 or
    false, so a falsy `value` on its own does not mean "not set". Values come back as
    the site stores them (WordPress serializes everything to a string, so a number can
    read back quoted). Requires the REST transport, wp-ops-connect 1.3.0+ and an
    administrator app password.
    """
    return await get_option_payload(get_registry(), install, name)


@mcp.tool()
async def wp_set_option(
    install: str,
    name: str,
    value: str | int | float | bool | list | dict,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Write ANY wp_options row - including siteurl/home, where a wrong value can take
    the site offline.

    There is NO allowlist here: this reaches every option a plugin or theme reads,
    including the ones that decide where WordPress thinks it lives. Setting `siteurl`
    or `home` to a wrong value takes the site offline and locks you out of wp-admin
    (recovery needs database or wp-config access, not this server). Treat unfamiliar
    options the same way - many are serialized plugin settings where a partial write
    corrupts the whole structure. READ IT FIRST with wp_get_option and keep the old
    value. The dry-run preview shows the option's CURRENT value next to the new one so
    an overwrite is visible before it happens. DEFAULTS TO dry_run=true (writes
    nothing). Pass dry_run=false to write - the write is verified by re-reading the
    option, tolerating WordPress's storage casts (25 vs '25', true vs 1, '&' escaped to
    '&amp;'), and the result carries the site's ACTUAL stored `value` next to what you
    `requested`. Requires the REST transport, wp-ops-connect 1.3.0+ and an administrator
    app password. Prod writes require allow_prod=true.
    """
    return await set_option_payload(
        get_registry(), install, name, value,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_get_theme_file(
    install: str,
    file: str,
    theme: str | None = None,
) -> dict:
    """Read one theme file (PHP/CSS/JS) from the ACTIVE theme.

    `file` is a path RELATIVE to the theme directory, e.g. "functions.php" or
    "css/custom.css". Absolute paths and ".." are refused. Defaults to the active
    (child) theme; pass `theme` for a specific stylesheet directory. Requires the REST
    transport, wp-ops-connect 1.4.0+, and an app password whose user has `edit_themes`.
    Sites with DISALLOW_FILE_EDIT set refuse this by design.
    """
    return await get_theme_file_payload(get_registry(), install, file, theme=theme)


@mcp.tool()
async def wp_set_theme_file(
    install: str,
    file: str,
    contents: str,
    theme: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
    allow_create: bool = False,
    allow_other_theme: bool = False,
) -> dict:
    """Write one theme file. THIS EXECUTES CODE ON THE SITE - the highest-risk tool here.

    A theme file is PHP. A syntax error does not break a layout, it takes the SITE down.
    Mitigations, in order:
      - the write goes through WordPress core's own theme editor, which makes a loopback
        request after writing and AUTOMATICALLY REVERTS the file if the site fatals;
      - the result carries `previous`, the exact prior contents, so you can revert with a
        second call - KEEP IT;
      - the write is verified by re-reading the file, and a mismatch is an error, never
        a success;
      - path traversal, absolute paths, and non-code extensions are refused, and a
        non-active theme needs allow_other_theme=true (editing a parent theme is how
        changes get lost on the next update - prefer the child theme).
    DEFAULTS TO dry_run=true, which writes nothing and returns the file's CURRENT
    contents next to what you propose. Read it and diff before setting dry_run=false.
    Prod writes require allow_prod=true. CREATING files is not supported - core's
    self-reverting editor only edits files the theme already has; add new files by
    deploy/SFTP first. `allow_create` is accepted but cannot make creation work.
    """
    return await set_theme_file_payload(
        get_registry(), install, file, contents, theme=theme,
        dry_run=dry_run, allow_prod=allow_prod,
        allow_create=allow_create, allow_other_theme=allow_other_theme)


@mcp.tool()
async def wp_delete_option(
    install: str,
    name: str,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Delete a wp_options row entirely (not "set it to empty").

    Whatever reads that option falls back to its default - which for a plugin's settings
    row means every setting it holds is gone at once, and for a core option can change
    how the site behaves. Read it with wp_get_option and keep the value first: nothing
    here can restore it. Deleting an option that was never set is reported as
    `action: "noop"` (nothing to delete), not an error. DEFAULTS TO dry_run=true
    (returns a preview with the current value; deletes nothing). Pass dry_run=false to
    delete. Requires the REST transport, wp-ops-connect 1.3.0+ and an administrator app
    password. Prod writes require allow_prod=true.
    """
    return await delete_option_payload(
        get_registry(), install, name, dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_list_comments(
    install: str,
    post_id: int | None = None,
    status: str | None = None,
) -> dict:
    """List comments (id, post, author, content, status, date).

    Read-only. Filter by `post_id` (from wp_find_page / wp_get_content) and/or `status`
    - one of approved, hold (awaiting moderation), spam, trash. Omit both to see the
    most recent comments across the site. Requires the REST transport and an app
    password with moderate_comments.
    """
    return await list_comments_payload(get_registry(), install,
                                       post_id=post_id, status=status)


@mcp.tool()
async def wp_moderate_comment(
    install: str,
    comment_id: int,
    status: str,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Set one comment's moderation status: approved, hold, spam or trash.

    Those four strings are WordPress's own vocabulary and anything else is refused
    ('approve' and 'APPROVED' are NOT accepted). 'approved' publishes the comment,
    'hold' returns it to the moderation queue, 'spam' teaches the spam filters, 'trash'
    removes it recoverably. `comment_id` comes from wp_list_comments. DEFAULTS TO
    dry_run=true (returns a preview; writes nothing). Pass dry_run=false to apply - the
    result is checked against the status the site reports back. Requires the REST
    transport. Prod writes require allow_prod=true.
    """
    return await moderate_comment_payload(
        get_registry(), install, comment_id, status,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_delete_comment(
    install: str,
    comment_id: int,
    force: bool = False,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Delete a comment: force=true is PERMANENT; the default trashes it (recoverable).

    By default the comment goes to the trash and can be restored from wp-admin -
    that comes back as `action: "trashed"`, which is a success. With `force=true` it is
    erased from the database and nothing here can bring it back. Prefer
    wp_moderate_comment with 'spam' for spam: it trains the filters, where a delete
    teaches them nothing. `comment_id` comes from wp_list_comments. DEFAULTS TO
    dry_run=true (returns a preview stating which of the two outcomes you would get;
    deletes nothing). Pass dry_run=false to apply. Requires the REST transport. Prod
    writes require allow_prod=true.
    """
    return await delete_comment_payload(
        get_registry(), install, comment_id, force=force,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_list_taxonomies(install: str) -> dict:
    """List a site's taxonomies (slug, name, rest_base, hierarchical, post types).

    Read-only. Start here: the `slug` it returns ('category', 'post_tag', or any
    custom taxonomy a plugin/theme registers) is what every other term tool takes as
    `taxonomy`. Only taxonomies registered with show_in_rest appear. Requires the REST
    transport (the wpcli/SSH gateway has no taxonomy support).
    """
    return await list_taxonomies_payload(get_registry(), install)


@mcp.tool()
async def wp_list_terms(install: str, taxonomy: str, search: str | None = None) -> dict:
    """List the terms in one taxonomy (id, name, slug, parent, post count).

    Read-only. `taxonomy` is a slug from wp_list_taxonomies. `search` is an optional
    simple filter on the term name. `parent` comes back as None on a flat taxonomy
    (tags) - that means "no hierarchy", not "top level", which WordPress reports as 0.
    Requires the REST transport.
    """
    return await list_terms_payload(get_registry(), install, taxonomy, search=search)


@mcp.tool()
async def wp_create_term(
    install: str,
    taxonomy: str,
    name: str,
    slug: str | None = None,
    parent: int | None = None,
    description: str | None = None,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Create one term (a category, tag, or any custom taxonomy term).

    `taxonomy` is a slug from wp_list_taxonomies. `slug` is derived from the name when
    omitted. `parent` (a term id from wp_list_terms, 0 = top level) only applies to a
    HIERARCHICAL taxonomy - WordPress ignores it on tags. Creating a term does not
    assign it to anything: use wp_set_post_terms for that. DEFAULTS TO dry_run=true
    (returns a preview; writes nothing). Pass dry_run=false to apply - the create is
    verified by reading the term back. Requires the REST transport. Prod writes require
    allow_prod=true.
    """
    return await create_term_payload(
        get_registry(), install, taxonomy, name, slug=slug, parent=parent,
        description=description, dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_update_term(
    install: str,
    taxonomy: str,
    term_id: int,
    fields: dict,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Rename or re-parent one existing term.

    `fields` is an object of field -> value; only name, slug, parent and description
    can be changed and anything else is refused. CHANGING A SLUG CHANGES THAT TERM'S
    ARCHIVE URL (/category/old/ starts 404ing), so treat it like wp_change_slug.
    Passing no fields at all is refused rather than reported as a no-op success.
    `term_id` comes from wp_list_terms. DEFAULTS TO dry_run=true (returns a preview of
    the exact fields; writes nothing). Pass dry_run=false to apply. Requires the REST
    transport. Prod writes require allow_prod=true.
    """
    return await update_term_payload(
        get_registry(), install, taxonomy, term_id, fields,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_delete_term(
    install: str,
    taxonomy: str,
    term_id: int,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Delete a term: this PERMANENTLY deletes it and unassigns it from every post.

    There is NO TRASH for terms - WordPress requires force on this call and nothing
    here can restore it. The posts keep their content but quietly lose that filing, and
    the term's archive URL (/category/news/) starts 404ing, which is usually noticed
    long after the fact. Check wp_list_terms for the term's post `count` first, and
    prefer re-filing those posts with wp_set_post_terms before deleting. `term_id`
    comes from wp_list_terms. DEFAULTS TO dry_run=true (returns a preview stating that
    outcome; deletes nothing). Pass dry_run=false to apply. Requires the REST
    transport. Prod writes require allow_prod=true.
    """
    return await delete_term_payload(
        get_registry(), install, taxonomy, term_id,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_set_post_terms(
    install: str,
    post_id: int,
    taxonomy: str,
    term_ids: list,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Set a post's terms for one taxonomy - REPLACES them, never appends.

    `term_ids` is the COMPLETE list the post should end up with (ids from
    wp_list_terms); anything not in it is unassigned, and an empty list clears the
    taxonomy on that post. Read the post's current terms first if you mean to add one.
    `post_id` comes from wp_find_page / wp_get_content. The write is verified against
    what the site reports it stored: WordPress silently drops ids the taxonomy does not
    own, and assigning a taxonomy that is not registered for that post's type stores
    NOTHING while still answering 200 - both come back as a TermAssignmentMismatch
    error rather than a false success. DEFAULTS TO dry_run=true (returns a preview;
    writes nothing). Pass dry_run=false to apply. Requires the REST transport. Prod
    writes require allow_prod=true.
    """
    return await set_post_terms_payload(
        get_registry(), install, post_id, taxonomy, term_ids,
        dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_list_plugins(install: str) -> dict:
    """List every installed plugin (id, name, status, version, requirements).

    Read-only. Start here: the `plugin` id it returns ('akismet/akismet') is what the
    two activation tools take, and `status` tells you whether a toggle is even needed.
    Counts come back as `active` / `inactive`. Installing, updating and deleting
    plugins are deliberately NOT offered by this server - use the fleet's plugin-audit
    and MainWP rollout process for those. Requires the REST transport (the wpcli/SSH
    gateway has no plugin support) and WordPress 5.5+.
    """
    return await list_plugins_payload(get_registry(), install)


@mcp.tool()
async def wp_activate_plugin(
    install: str,
    plugin: str,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Activate one already-installed plugin.

    ACTIVATING AN UNTESTED PLUGIN CAN BREAK A LIVE SITE: the activation hook runs
    immediately and can fatal, and the plugin starts filtering the site's output the
    moment it loads. Activate on staging first. `plugin` may be a slug ('akismet'), a
    plugin file ('akismet/akismet') or its .php path - all three resolve to the same
    plugin, and a slug matching two installed plugins is refused with the candidates
    rather than guessed. A plugin that is already active comes back as a no-op, not an
    error. DEFAULTS TO dry_run=true (returns a preview naming the plugin and its current
    status; changes nothing). Pass dry_run=false to apply - the change is verified by
    re-reading the plugin's status. Requires the REST transport. Prod writes require
    allow_prod=true.
    """
    return await activate_plugin_payload(
        get_registry(), install, plugin, dry_run=dry_run, allow_prod=allow_prod)


@mcp.tool()
async def wp_deactivate_plugin(
    install: str,
    plugin: str,
    dry_run: bool = True,
    allow_prod: bool = False,
) -> dict:
    """Deactivate one plugin: this can take site functionality OFFLINE immediately.

    DEACTIVATING A LIVE PLUGIN TAKES WHATEVER IT PROVIDES OFFLINE the instant it lands -
    forms stop submitting, caching stops serving, security rules stop applying, and
    page builders stop rendering their layouts (a Divi/Elementor page can come back as
    raw shortcodes). Check what the plugin does before calling this, and prefer staging.
    Deactivating wp-ops-connect is REFUSED outright: it is the control-plane plugin this
    tool reaches the site through, so deactivating it would sever this tool's own
    connection to the site and nothing here could re-activate it. `plugin` may be a slug
    ('akismet'), a plugin file ('akismet/akismet') or its .php path; a slug matching two
    installed plugins is refused with the candidates rather than guessed. A plugin that
    is already inactive comes back as a no-op, not an error. DEFAULTS TO dry_run=true
    (returns a preview naming the plugin, its current status and what would change;
    changes nothing). Pass dry_run=false to apply - the change is verified by re-reading
    the plugin's status. Requires the REST transport. Prod writes require allow_prod=true.
    """
    return await deactivate_plugin_payload(
        get_registry(), install, plugin, dry_run=dry_run, allow_prod=allow_prod)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

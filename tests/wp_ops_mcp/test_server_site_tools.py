"""Contract tests for the Phase 5e site tools (wp_change_slug / wp_get_settings /
wp_set_setting) on the FastMCP server.

Mirrors test_server_menu_tools.py: fake at the gateway boundary and assert the tool
layer refuses correctly (unknown install, non-REST transport, prod gate) BEFORE a URL
or a site option changes. Slugs and settings are builder-independent, so (like
SEO/media/menus) there is no profile/builder precondition - only the REST transport.
"""
import asyncio

from wp_ops_mcp import server
from wp_ops_mcp.ops.site import SETTINGS_ALLOWLIST
from wp_ops_mcp.registry import SiteRegistry

RAW = [
    {"domain": "a.com", "wpe_account": "hostacct1", "wpe_install": "aprd",
     "cf_zone_id": "z1", "php_version": "8.2", "active": True},   # aprd -> prod
    {"domain": "", "wpe_account": "hostacct1", "wpe_install": "astg",
     "cf_zone_id": "", "php_version": "8.2", "active": True},     # astg -> staging
]

SETTINGS = {"title": "Site", "description": "Tagline",
            "timezone": "America/Denver", "posts_per_page": 10,
            "show_on_front": "page", "page_on_front": 5, "page_for_posts": 9,
            "url": "https://a.com", "email": "admin@a.com"}   # last two: not allowlisted


def _reg():
    return SiteRegistry.from_records(RAW)


class FakeSiteGateway:
    """REST-shaped gateway: exposes update_post_slug/get_settings, records every write."""

    def __init__(self, post_type="page"):
        self.slug = "old-slug"
        self.post_type = post_type
        self.settings = dict(SETTINGS)
        self.slug_writes = []
        self.settings_writes = []

    async def get_post_info(self, post_id):
        return {"id": post_id, "name": self.slug, "status": "publish",
                "type": self.post_type, "title": "T"}

    async def update_post_slug(self, post_id, slug):
        self.slug_writes.append({"post_id": post_id, "slug": slug})
        self.slug = slug
        return {"id": post_id, "slug": slug, "link": f"https://a.com/{slug}/"}

    async def get_settings(self):
        return dict(self.settings)

    async def update_settings(self, fields):
        self.settings_writes.append(dict(fields))
        self.settings.update(fields)
        return dict(self.settings)


class FakeWpcliGateway:
    """wpcli-style gateway: NO update_post_slug, so the site tools must refuse."""

    async def list_posts(self, post_type, query=None):
        return []


# --- registration -----------------------------------------------------------

def test_site_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"wp_change_slug", "wp_get_settings", "wp_set_setting"} <= names


# --- unknown install --------------------------------------------------------

async def test_change_slug_unknown_install():
    gw = FakeSiteGateway()
    payload = await server.change_slug_payload(
        _reg(), "nope", 5, "about-us", dry_run=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]
    assert gw.slug_writes == []


async def test_get_settings_unknown_install():
    payload = await server.get_settings_payload(_reg(), "nope", gateway=FakeSiteGateway())
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]


async def test_set_setting_unknown_install():
    gw = FakeSiteGateway()
    payload = await server.set_setting_payload(
        _reg(), "nope", "title", "New Title", dry_run=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]
    assert gw.settings_writes == []


# --- non-REST transport -----------------------------------------------------

async def test_change_slug_wpcli_gateway_refused():
    payload = await server.change_slug_payload(
        _reg(), "astg", 5, "about-us", dry_run=False, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


async def test_get_settings_wpcli_gateway_refused():
    payload = await server.get_settings_payload(_reg(), "astg", gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


async def test_set_setting_wpcli_gateway_refused():
    payload = await server.set_setting_payload(
        _reg(), "astg", "title", "New Title", dry_run=False, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


# --- prod gate honours dry_run ----------------------------------------------

async def test_change_slug_prod_blocked_without_allow_prod():
    gw = FakeSiteGateway()
    payload = await server.change_slug_payload(
        _reg(), "aprd", 5, "about-us", dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "prod" in payload["reason"].lower()
    assert gw.slug_writes == []              # gate fired before any write


async def test_change_slug_prod_dry_run_passes_gate():
    gw = FakeSiteGateway()
    payload = await server.change_slug_payload(
        _reg(), "aprd", 5, "about-us", dry_run=True, gateway=gw)
    assert payload["action"] == "preview"    # dry-run bypasses the prod gate
    assert gw.slug_writes == []


async def test_change_slug_prod_with_allow_prod_writes():
    gw = FakeSiteGateway()
    payload = await server.change_slug_payload(
        _reg(), "aprd", 5, "about-us", dry_run=False, allow_prod=True, gateway=gw)
    assert payload["action"] == "changed" and payload["verified"] is True
    assert gw.slug_writes == [{"post_id": 5, "slug": "about-us"}]


async def test_set_setting_prod_blocked_without_allow_prod():
    gw = FakeSiteGateway()
    payload = await server.set_setting_payload(
        _reg(), "aprd", "title", "New Title", dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "prod" in payload["reason"].lower()
    assert gw.settings_writes == []


async def test_set_setting_prod_dry_run_passes_gate():
    gw = FakeSiteGateway()
    payload = await server.set_setting_payload(
        _reg(), "aprd", "title", "New Title", dry_run=True, gateway=gw)
    assert payload["action"] == "preview"
    assert gw.settings_writes == []


async def test_set_setting_prod_with_allow_prod_writes():
    gw = FakeSiteGateway()
    payload = await server.set_setting_payload(
        _reg(), "aprd", "title", "New Title", dry_run=False, allow_prod=True, gateway=gw)
    assert payload["action"] == "applied" and payload["verified"] is True
    assert gw.settings_writes == [{"title": "New Title"}]


# --- read-only get has no prod gate -----------------------------------------

async def test_get_settings_happy_path_on_prod_filters_the_allowlist():
    payload = await server.get_settings_payload(_reg(), "aprd", gateway=FakeSiteGateway())
    assert payload == {"action": "ok",
                       "settings": {k: SETTINGS[k] for k in SETTINGS_ALLOWLIST}}
    assert "url" not in payload["settings"] and "email" not in payload["settings"]


# --- happy paths ------------------------------------------------------------

async def test_change_slug_happy_path_sanitizes_through_the_whole_stack():
    gw = FakeSiteGateway()
    payload = await server.change_slug_payload(
        _reg(), "astg", 5, "About Us!", dry_run=False, gateway=gw)
    assert payload["action"] == "changed" and payload["verified"] is True
    assert payload["old_slug"] == "old-slug" and payload["slug"] == "about-us"
    assert payload["uniquified"] is False
    assert gw.slug_writes == [{"post_id": 5, "slug": "about-us"}]


async def test_set_setting_happy_path():
    gw = FakeSiteGateway()
    payload = await server.set_setting_payload(
        _reg(), "astg", "posts_per_page", 25, dry_run=False, gateway=gw)
    assert payload == {"action": "applied", "key": "posts_per_page",
                       "requested": 25, "value": 25, "verified": True}
    assert gw.settings_writes == [{"posts_per_page": 25}]


async def test_change_slug_page_warning_reaches_the_operator():
    """The "the old URL will 404" signal must survive the tool layer, in preview and
    in the applied result - it is the whole reason a page rename is dangerous."""
    gw = FakeSiteGateway(post_type="page")
    preview = await server.change_slug_payload(
        _reg(), "astg", 5, "about-us", dry_run=True, gateway=gw)
    assert preview["old_url_redirects"] is False and "404" in preview["warning"]
    applied = await server.change_slug_payload(
        _reg(), "astg", 5, "about-us", dry_run=False, gateway=gw)
    assert applied["action"] == "changed"
    assert applied["old_url_redirects"] is False and "404" in applied["warning"]


async def test_change_slug_published_post_reports_the_free_redirect():
    gw = FakeSiteGateway(post_type="post")
    payload = await server.change_slug_payload(
        _reg(), "astg", 5, "about-us", dry_run=False, gateway=gw)
    assert payload["old_url_redirects"] is True and "warning" not in payload


# --- ops-level validation still surfaces through the tool -------------------

async def test_set_setting_off_allowlist_key_is_an_error_dict():
    gw = FakeSiteGateway()
    payload = await server.set_setting_payload(
        _reg(), "astg", "url", "https://evil.test", dry_run=False, gateway=gw)
    assert payload["action"] == "error"
    for key in SETTINGS_ALLOWLIST:
        assert key in payload["error"], key
    assert gw.settings_writes == []


async def test_change_slug_bad_post_id_is_an_error_dict():
    gw = FakeSiteGateway()
    payload = await server.change_slug_payload(
        _reg(), "astg", 0, "about-us", dry_run=False, gateway=gw)
    assert payload["action"] == "error"
    assert "post_id must be a positive int" in payload["error"]
    assert gw.slug_writes == []


async def test_change_slug_empty_sanitized_slug_is_an_error_dict():
    gw = FakeSiteGateway()
    payload = await server.change_slug_payload(
        _reg(), "astg", 5, "***", dry_run=False, gateway=gw)
    assert payload["action"] == "error"
    assert "empty after sanitizing" in payload["error"]
    assert gw.slug_writes == []

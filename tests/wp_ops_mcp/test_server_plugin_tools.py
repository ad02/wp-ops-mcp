"""Contract tests for the Phase 5l plugin tools (wp_list_plugins / wp_activate_plugin /
wp_deactivate_plugin) on the FastMCP server.

Mirrors test_server_term_tools.py: fake at the gateway boundary and assert the tool
layer refuses correctly (unknown install, non-REST transport, prod gate) BEFORE any
activation change reaches the site. Plugins are builder-independent, so (like
SEO/media/menus/terms) there is no profile/builder precondition - only the REST
transport. wp_list_plugins carries NO prod gate: reading a prod site's plugin inventory
changes nothing.

The self-lockout refusal is exercised here too, end to end: it is the one guard that
must hold at the tool boundary regardless of environment or dry_run.
"""
import asyncio

from wp_ops_mcp import server
from wp_ops_mcp.registry import SiteRegistry

RAW = [
    {"domain": "a.com", "wpe_account": "hostacct1", "wpe_install": "aprd",
     "cf_zone_id": "z1", "php_version": "8.2", "active": True},   # aprd -> prod
    {"domain": "", "wpe_account": "hostacct1", "wpe_install": "astg",
     "cf_zone_id": "", "php_version": "8.2", "active": True},     # astg -> staging
]

PLUGINS = [
    {"plugin": "akismet/akismet", "name": "Akismet Anti-Spam", "status": "inactive",
     "version": "5.3", "network_only": False, "requires_wp": "5.8",
     "requires_php": "5.6", "textdomain": "akismet", "author": "Automattic"},
    {"plugin": "advanced-custom-fields/acf", "name": "ACF", "status": "active",
     "version": "6.2", "network_only": False, "requires_wp": "5.8",
     "requires_php": "7.4", "textdomain": "acf", "author": "WP Engine"},
    {"plugin": "wp-ops-connect/wp-ops-connect", "name": "WP Ops Connect",
     "status": "active", "version": "1.4.0", "network_only": False,
     "requires_wp": "5.8", "requires_php": "7.4", "textdomain": "wpopsconnect",
     "author": "Example"},
]

WRITES = ("wp_activate_plugin", "wp_deactivate_plugin")


def _reg():
    return SiteRegistry.from_records(RAW)


class FakePluginGateway:
    """REST-shaped gateway: exposes list_plugins, records every status write."""

    def __init__(self):
        self.plugins = [dict(p) for p in PLUGINS]
        self.writes = []

    def _row(self, plugin_id):
        for p in self.plugins:
            if p["plugin"] == plugin_id:
                return p
        raise KeyError(plugin_id)

    async def list_plugins(self):
        return [dict(p) for p in self.plugins]

    async def get_plugin(self, plugin_id):
        return dict(self._row(plugin_id))

    async def set_plugin_status(self, plugin_id, status):
        self.writes.append((plugin_id, status))
        self._row(plugin_id)["status"] = status
        return dict(self._row(plugin_id))


class FakeWpcliGateway:
    """wpcli-style gateway: NO list_plugins, so the plugin tools must refuse."""

    async def list_posts(self, post_type, query=None):
        return []


# --- registration + docstrings ----------------------------------------------

def test_plugin_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"wp_list_plugins", *WRITES} <= names


def test_deactivate_plugin_docstring_states_the_blast_radius_and_the_self_lockout():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    doc = (tools["wp_deactivate_plugin"].description or "").lower()
    assert "offline" in doc
    for word in ("forms", "caching", "security", "page builder"):
        assert word in doc
    assert "wp-ops-connect" in doc and "refused" in doc


def test_activate_plugin_docstring_warns_about_untested_plugins():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    doc = (tools["wp_activate_plugin"].description or "").lower()
    assert "untested" in doc and "break" in doc


# --- unknown install --------------------------------------------------------

async def test_list_plugins_unknown_install():
    payload = await server.list_plugins_payload(_reg(), "nope",
                                                gateway=FakePluginGateway())
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]


async def test_activate_plugin_unknown_install():
    gw = FakePluginGateway()
    payload = await server.activate_plugin_payload(_reg(), "nope", "akismet",
                                                   dry_run=False, gateway=gw)
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]
    assert gw.writes == []


async def test_deactivate_plugin_unknown_install():
    gw = FakePluginGateway()
    payload = await server.deactivate_plugin_payload(_reg(), "nope", "acf",
                                                     dry_run=False, gateway=gw)
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]
    assert gw.writes == []


# --- non-REST transport -----------------------------------------------------

async def test_list_plugins_wpcli_gateway_refused():
    payload = await server.list_plugins_payload(_reg(), "astg",
                                                gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "plugin tools require the REST transport" in payload["reason"]


async def test_activate_plugin_wpcli_gateway_refused():
    payload = await server.activate_plugin_payload(_reg(), "astg", "akismet",
                                                   dry_run=False,
                                                   gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "plugin tools require the REST transport" in payload["reason"]


async def test_deactivate_plugin_wpcli_gateway_refused():
    payload = await server.deactivate_plugin_payload(_reg(), "astg", "acf",
                                                     dry_run=False,
                                                     gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "plugin tools require the REST transport" in payload["reason"]


# --- prod gate honours dry_run (the two writes) -----------------------------

async def test_activate_plugin_prod_blocked_without_allow_prod():
    gw = FakePluginGateway()
    payload = await server.activate_plugin_payload(_reg(), "aprd", "akismet",
                                                   dry_run=False, allow_prod=False,
                                                   gateway=gw)
    assert payload["action"] == "refused" and "prod" in payload["reason"].lower()
    assert gw.writes == []


async def test_activate_plugin_prod_dry_run_passes_gate():
    gw = FakePluginGateway()
    payload = await server.activate_plugin_payload(_reg(), "aprd", "akismet",
                                                   dry_run=True, gateway=gw)
    assert payload["action"] == "preview" and gw.writes == []


async def test_activate_plugin_prod_with_allow_prod_writes():
    gw = FakePluginGateway()
    payload = await server.activate_plugin_payload(_reg(), "aprd", "akismet",
                                                   dry_run=False, allow_prod=True,
                                                   gateway=gw)
    assert payload["action"] == "activated" and payload["verified"] is True
    assert gw.writes == [("akismet/akismet", "active")]


async def test_deactivate_plugin_prod_blocked_without_allow_prod():
    gw = FakePluginGateway()
    payload = await server.deactivate_plugin_payload(_reg(), "aprd", "acf",
                                                     dry_run=False, allow_prod=False,
                                                     gateway=gw)
    assert payload["action"] == "refused" and "prod" in payload["reason"].lower()
    assert gw.writes == []


async def test_deactivate_plugin_prod_dry_run_passes_gate():
    gw = FakePluginGateway()
    payload = await server.deactivate_plugin_payload(_reg(), "aprd", "acf",
                                                     dry_run=True, gateway=gw)
    assert payload["action"] == "preview" and gw.writes == []


# --- read-only tool has no prod gate ----------------------------------------

async def test_list_plugins_happy_path_on_prod():
    payload = await server.list_plugins_payload(_reg(), "aprd",
                                                gateway=FakePluginGateway())
    assert payload["action"] == "ok"
    assert payload["active"] == 2 and payload["inactive"] == 1
    assert {p["plugin"] for p in payload["plugins"]} == {p["plugin"] for p in PLUGINS}


# --- happy paths ------------------------------------------------------------

async def test_deactivate_plugin_happy_path():
    gw = FakePluginGateway()
    payload = await server.deactivate_plugin_payload(_reg(), "astg", "acf",
                                                     dry_run=False, gateway=gw)
    assert payload["action"] == "deactivated" and payload["verified"] is True
    assert gw.writes == [("advanced-custom-fields/acf", "inactive")]


# --- the self-lockout guard holds at the tool boundary ----------------------

async def test_deactivate_plugin_self_lockout_surfaces_through_the_tool():
    gw = FakePluginGateway()
    payload = await server.deactivate_plugin_payload(_reg(), "astg",
                                                     "wp-ops-connect/wp-ops-connect.php",
                                                     dry_run=False, gateway=gw)
    assert payload["action"] == "error" and "PluginSelfLockout" in payload["error"]
    assert gw.writes == []


async def test_deactivate_plugin_self_lockout_holds_on_staging_dry_run_too():
    gw = FakePluginGateway()
    payload = await server.deactivate_plugin_payload(_reg(), "astg", "wp-ops-connect",
                                                     dry_run=True, gateway=gw)
    assert payload["action"] == "error" and "PluginSelfLockout" in payload["error"]
    assert gw.writes == []


async def test_activate_plugin_unknown_plugin_is_an_error_dict():
    gw = FakePluginGateway()
    payload = await server.activate_plugin_payload(_reg(), "astg", "wordfence",
                                                   dry_run=False, gateway=gw)
    assert payload["action"] == "error" and "PluginError" in payload["error"]
    assert gw.writes == []

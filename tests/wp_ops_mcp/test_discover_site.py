"""Tests for the discover_site orchestration."""
import pytest

from skills.wpengine.ssh import SSHResult
from wp_ops_mcp.registry import Site
from wp_ops_mcp.profiles.store import ProfileStore
from wp_ops_mcp.discover import discover_site, rest_discover


DISCOVERY_OUTPUT = """@@WP_VERSION@@
6.5.2
@@CLI_INFO@@
PHP version:	8.2.28
@@TEMPLATE@@
Divi
@@STYLESHEET@@
Divi
@@THEMES@@
[{"name":"Divi","status":"active","version":"4.27.6"}]
@@PLUGINS@@
[{"name":"akismet","status":"active","version":"5.3"}]
@@MULTISITE@@
"""


class FakeTransport:
    def __init__(self, output):
        self._output = output
        self.last_command = None

    async def run_raw(self, command, timeout=90):
        self.last_command = command
        return SSHResult(stdout=self._output, stderr="", exit_code=0)


def make_site(install="dermwellstg"):
    return Site(install=install, account="hostacct1", domain="",
                environment="staging", php_version="8.2", cf_zone_id="")


class TestDiscoverSite:
    async def test_builds_and_persists_profile(self, tmp_path):
        store = ProfileStore(tmp_path / "p.db")
        site = make_site()
        transport = FakeTransport(DISCOVERY_OUTPUT)
        profile = await discover_site(site, transport, store, now="2026-06-15T00:00:00")

        assert profile.install == "dermwellstg"
        assert profile.builder == "divi"
        assert profile.divi_major == 4
        assert profile.wp_version == "6.5.2"
        assert profile.php_version == "8.2.28"
        assert profile.active_theme == "Divi"
        assert profile.active_theme_version == "4.27.6"
        assert profile.discovered_at == "2026-06-15T00:00:00"

        # persisted
        assert store.get("dermwellstg").divi_major == 4

    async def test_uses_single_combined_command(self, tmp_path):
        store = ProfileStore(tmp_path / "p.db")
        transport = FakeTransport(DISCOVERY_OUTPUT)
        await discover_site(make_site(), transport, store, now="t")
        # one combined command containing the install path and sentinels
        assert "~/sites/dermwellstg" in transport.last_command
        assert "@@PLUGINS@@" in transport.last_command

    async def test_carries_site_identity_into_profile(self, tmp_path):
        store = ProfileStore(tmp_path / "p.db")
        site = Site(install="aprd", account="hostacct3", domain="a.com",
                    environment="prod", php_version="8.1", cf_zone_id="z3")
        transport = FakeTransport(DISCOVERY_OUTPUT)
        profile = await discover_site(site, transport, store, now="t")
        assert profile.account == "hostacct3"
        assert profile.environment == "prod"
        assert profile.domain == "a.com"


class FakeInfoGateway:
    """A REST content gateway stub exposing only ensure_plugin() -> /info payload.

    rest_discover duck-types the gateway, so this stub needs nothing more.
    """
    def __init__(self, info):
        self._info = info
        self.calls = 0

    async def ensure_plugin(self):
        self.calls += 1
        return self._info


class TestRestDiscover:
    """rest_discover maps the plugin /info payload to a SiteProfile with no SSH.

    /info carries less than the SSH fingerprint, so php_version, active_theme_version,
    plugins and multisite are honestly blank (REST can't see them). It is a pure
    builder: it does NOT persist - discover_site_payload owns the store.upsert.
    """
    async def test_maps_divi_info_to_profile(self):
        gw = FakeInfoGateway({"wp": "6.9", "theme": "Divi", "divi": "4.24.0",
                              "seo_plugin": "seopress", "plugin_version": "1.1.0"})
        site = make_site(install="teamsitestg")
        profile = await rest_discover(gw, site, now="2026-07-28T00:00:00")

        assert profile.install == "teamsitestg"
        assert profile.builder == "divi"
        assert profile.divi_major == 4                 # first dotted component of "4.24.0"
        assert profile.wp_version == "6.9"
        assert profile.active_theme == "Divi"
        assert profile.php_version == ""               # honest empty - not in /info
        assert profile.active_theme_version is None    # /info gives no theme version
        assert profile.multisite is False
        assert profile.plugins == []                   # /info gives no plugin list
        assert profile.discovered_at == "2026-07-28T00:00:00"
        assert profile.raw["info"]["seo_plugin"] == "seopress"   # raw carries /info verbatim
        assert gw.calls == 1                           # /info fetched once via ensure_plugin

    async def test_maps_null_divi_to_gutenberg(self):
        gw = FakeInfoGateway({"wp": "6.8", "theme": "twentytwentyfour", "divi": None,
                              "seo_plugin": None, "plugin_version": "1.1.0"})
        profile = await rest_discover(gw, make_site(), now="t")
        assert profile.builder == "gutenberg"
        assert profile.divi_major is None
        assert profile.active_theme == "twentytwentyfour"

    async def test_divi_major_is_first_dotted_component(self):
        gw = FakeInfoGateway({"wp": "6.9", "theme": "Divi", "divi": "5.1.2",
                              "plugin_version": "1.1.0"})
        site = Site(install="aprd", account="hostacct3", domain="a.com",
                    environment="prod", php_version="8.1", cf_zone_id="z3")
        profile = await rest_discover(gw, site, now="t")
        assert profile.divi_major == 5
        # identity comes from the resolved Site (real fleet identity here)
        assert profile.account == "hostacct3"
        assert profile.environment == "prod"
        assert profile.domain == "a.com"

    async def test_nonnumeric_divi_version_keeps_builder_divi_major_none(self):
        # wp-ops-connect falls back to $theme->get('Version'), which can be free-text
        # ("v5.0.1", or a child theme's "beta"). A Divi site we cannot version is still
        # Divi - builder stays "divi" with divi_major None (the edit layer maps that to
        # a safe refusal), never a crash on int("v5").
        for bad in ("v5.0.1", "beta"):
            gw = FakeInfoGateway({"wp": "6.9", "theme": "Divi", "divi": bad,
                                  "plugin_version": "1.1.0"})
            profile = await rest_discover(gw, make_site(), now="t")
            assert profile.builder == "divi"
            assert profile.divi_major is None

    async def test_divi_version_leading_numeric_still_parses(self):
        # "5.0.0-beta": the first dotted component is numeric, so the major reads 5.
        gw = FakeInfoGateway({"wp": "6.9", "theme": "Divi", "divi": "5.0.0-beta",
                              "plugin_version": "1.1.0"})
        profile = await rest_discover(gw, make_site(), now="t")
        assert profile.builder == "divi"
        assert profile.divi_major == 5

    async def test_real_clock_when_now_omitted(self):
        gw = FakeInfoGateway({"wp": "6.9", "theme": "Divi", "divi": "4.0",
                              "plugin_version": "1.1.0"})
        profile = await rest_discover(gw, make_site())
        assert profile.discovered_at        # non-empty ISO timestamp from the real clock

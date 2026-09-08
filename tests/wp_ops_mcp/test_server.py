"""Tests for the wp_ops_mcp FastMCP server tools."""
import pytest

from wp_ops_mcp.registry import SiteRegistry
from wp_ops_mcp.transport.rest import RestProbe
from wp_ops_mcp.transport.wpcli import SSHProbe
from wp_ops_mcp import server

RAW = [
    {"domain": "a.com", "wpe_account": "hostacct1", "wpe_install": "aprd",
     "cf_zone_id": "z1", "php_version": "8.2", "active": True},
    {"domain": "", "wpe_account": "hostacct1", "wpe_install": "astg",
     "cf_zone_id": "", "php_version": "8.2", "active": True},
    {"domain": "b.com", "wpe_account": "hostacct3", "wpe_install": "bprd",
     "cf_zone_id": "z3", "php_version": "8.1", "active": True},
]


class FakeRest:
    def __init__(self, probe):
        self._probe = probe
    async def probe(self):
        return self._probe


class FakeCli:
    def __init__(self, probe):
        self._probe = probe
    async def probe(self):
        return self._probe


def test_list_sites_payload_is_lean_and_counted():
    reg = SiteRegistry.from_records(RAW)
    payload = server.list_sites_payload(reg)
    assert payload["count"] == 3
    first = payload["sites"][0]
    # token-lean: only the identity fields, no nested probe junk
    assert set(first) == {"install", "account", "environment", "domain"}


def test_list_sites_payload_filters():
    reg = SiteRegistry.from_records(RAW)
    payload = server.list_sites_payload(reg, account="hostacct3")
    assert payload["count"] == 1
    assert payload["sites"][0]["install"] == "bprd"


async def test_site_health_payload_unknown_install():
    reg = SiteRegistry.from_records(RAW)
    payload = await server.site_health_payload(reg, "nope")
    assert "error" in payload
    assert "nope" in payload["error"]


async def test_site_health_payload_known_install():
    reg = SiteRegistry.from_records(RAW)
    rest = FakeRest(RestProbe(reachable=True, status_code=200, detail="ok"))
    cli = FakeCli(SSHProbe(reachable=True, wp_cli=True, detail="ok"))
    payload = await server.site_health_payload(reg, "aprd", rest_client=rest, cli_transport=cli)
    assert payload["install"] == "aprd"
    assert payload["rest_reachable"] is True
    assert payload["ssh_reachable"] is True


async def test_tools_registered_on_fastmcp_app():
    tools = {t.name for t in await server.mcp.list_tools()}
    assert {"wp_list_sites", "wp_site_health", "wp_discover_site"}.issubset(tools)


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
[{"name":"akismet","status":"active","version":"5.3"},{"name":"wordfence","status":"active","version":"7.0"}]
@@MULTISITE@@
"""


class FakeTransport:
    def __init__(self, output):
        self._output = output
    async def run_raw(self, command, timeout=90):
        from skills.wpengine.ssh import SSHResult
        return SSHResult(stdout=self._output, stderr="", exit_code=0)


async def test_discover_site_payload_unknown_install(tmp_path):
    from wp_ops_mcp.profiles.store import ProfileStore
    reg = SiteRegistry.from_records(RAW)
    store = ProfileStore(tmp_path / "p.db")
    payload = await server.discover_site_payload(reg, store, "nope")
    assert "error" in payload


async def test_discover_site_payload_is_lean(tmp_path, monkeypatch):
    # Pin the SSH/WP-CLI path deterministically: discovery now routes via
    # _content_gateway, so force wpcli mode and an absent credentials file so no
    # ambient REST creds can flip this into the REST branch.
    monkeypatch.setenv("WPOPS_TRANSPORT", "wpcli")
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(tmp_path / "absent.json"))
    from wp_ops_mcp.profiles.store import ProfileStore
    reg = SiteRegistry.from_records(RAW)
    store = ProfileStore(tmp_path / "p.db")
    payload = await server.discover_site_payload(
        reg, store, "aprd", transport=FakeTransport(DISCOVERY_OUTPUT), now="t",
    )
    assert payload["install"] == "aprd"
    assert payload["builder"] == "divi"
    assert payload["divi_major"] == 4
    assert payload["wp_version"] == "6.5.2"
    assert payload["plugin_count"] == 2
    assert payload["transport"] == "wpcli"      # SSH/WP-CLI path is tagged
    # lean: must NOT dump the full plugin list
    assert "plugins" not in payload


class _InfoClient:
    """Minimal WPRestClient stand-in: serves the plugin /info payload (HTTP 200) so a
    REAL RestContentGateway (and thus the isinstance routing check) exercises the REST
    discovery branch of discover_site_payload end-to-end."""
    def __init__(self, info):
        self._info = info
        self.calls = []

    async def request(self, method, path, *, params=None, json_body=None, http=None):
        self.calls.append((method, path))
        return (200, self._info)


def _rest_gw(info):
    from wp_ops_mcp.ops.rest_gateway import RestContentGateway
    return RestContentGateway(_InfoClient(info))


async def test_discover_site_payload_routes_to_rest(tmp_path):
    # An injected RestContentGateway wins in _content_gateway (explicit gateway seam),
    # so discovery takes the REST branch: profile from /info, transport tagged "rest",
    # persisted through the SAME store path the SSH discovery uses.
    from wp_ops_mcp.profiles.store import ProfileStore
    reg = SiteRegistry.from_records(RAW)
    store = ProfileStore(tmp_path / "p.db")
    gw = _rest_gw({"wp": "6.9", "theme": "Divi", "divi": "4.24.0",
                   "seo_plugin": "seopress", "plugin_version": "1.1.0"})
    payload = await server.discover_site_payload(reg, store, "aprd", now="t", gateway=gw)
    assert payload["transport"] == "rest"
    assert payload["install"] == "aprd"
    assert payload["builder"] == "divi"
    assert payload["divi_major"] == 4
    assert payload["wp_version"] == "6.9"
    assert payload["php_version"] == ""          # honest empty over REST
    assert payload["plugin_count"] == 0          # /info carries no plugin list
    assert "plugins" not in payload              # lean shape preserved
    # persisted via the SAME store path SSH discovery uses (_resolve_editable reads it)
    assert store.get("aprd").builder == "divi"
    assert store.get("aprd").divi_major == 4


async def test_discover_site_payload_rest_null_divi_is_gutenberg(tmp_path):
    from wp_ops_mcp.profiles.store import ProfileStore
    reg = SiteRegistry.from_records(RAW)
    store = ProfileStore(tmp_path / "p.db")
    gw = _rest_gw({"wp": "6.8", "theme": "twentytwentyfour", "divi": None,
                   "plugin_version": "1.1.0"})
    payload = await server.discover_site_payload(reg, store, "aprd", now="t", gateway=gw)
    assert payload["transport"] == "rest"
    assert payload["builder"] == "gutenberg"
    assert payload["divi_major"] is None


async def test_discover_site_payload_rest_discovers_synthetic_install(tmp_path, monkeypatch):
    # A credentialed install absent from the registry resolves to a synthetic Site and is
    # discoverable over REST (the whole point of 5f: REST-only sites get a profile, no SSH).
    import json
    from wp_ops_mcp.profiles.store import ProfileStore
    creds = tmp_path / "c.json"
    creds.write_text(json.dumps({"teamsitestg": {"base_url": "https://team.example.com",
                                                 "username": "u", "app_password": "p"}}),
                     encoding="utf-8")
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(creds))
    reg = SiteRegistry.from_records(RAW)          # does NOT contain teamsitestg
    store = ProfileStore(tmp_path / "p.db")
    gw = _rest_gw({"wp": "6.9", "theme": "Divi", "divi": "5.0.1",
                   "plugin_version": "1.1.0"})
    payload = await server.discover_site_payload(reg, store, "teamsitestg", now="t", gateway=gw)
    assert payload["transport"] == "rest"
    assert payload["install"] == "teamsitestg"
    assert payload["account"] == "rest-only"      # synthetic identity
    assert payload["environment"] == "staging"    # derived from the ...stg name
    assert payload["builder"] == "divi" and payload["divi_major"] == 5
    assert store.get("teamsitestg") is not None   # persisted


async def test_discover_site_payload_fails_closed_when_gateway_none(tmp_path, monkeypatch):
    # rest-mode with no REST credentials -> _content_gateway returns None. Use a REGISTERED
    # install so the site resolves (else the earlier site-is-None branch fires): we want to
    # reach the gw-is-None branch, which must fail CLOSED with an error dict (never falling
    # through to import/load the SSH transport) rather than raise.
    monkeypatch.setenv("WPOPS_TRANSPORT", "rest")
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(tmp_path / "absent.json"))
    from wp_ops_mcp.profiles.store import ProfileStore
    reg = SiteRegistry.from_records(RAW)
    store = ProfileStore(tmp_path / "p.db")
    payload = await server.discover_site_payload(reg, store, "aprd")
    assert "error" in payload
    assert "transport unavailable" in payload["error"]   # the gw-None branch, not unknown-install


async def test_content_tools_registered():
    tools = {t.name for t in await server.mcp.list_tools()}
    assert {"wp_get_content", "wp_create_content"}.issubset(tools)


class FakeContentGateway:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.created = []
    async def list_posts(self, post_type, query=None):
        return list(self.existing)
    async def create_post(self, post_type, title, slug, status, content, meta):
        self.created.append({"slug": slug, "content": content, "meta": meta})
        return 4242


def _profile(store, install, builder="divi", divi_major=4):
    from wp_ops_mcp.profiles.store import SiteProfile
    store.upsert(SiteProfile(
        install=install, account="hostacct1", environment="x", domain="",
        discovered_at="t", wp_version="6.5", php_version="8.2", active_theme="Divi",
        active_theme_version="4.27", builder=builder, divi_major=divi_major,
        multisite=False, plugins=[], raw={}))


async def test_get_content_payload_unknown_install():
    reg = SiteRegistry.from_records(RAW)
    payload = await server.get_content_payload(reg, "nope", gateway=FakeContentGateway())
    assert "error" in payload


async def test_get_content_payload_lists():
    reg = SiteRegistry.from_records(RAW)
    gw = FakeContentGateway(existing=[{"id": 1, "title": "Home", "slug": "home",
                                       "status": "publish", "type": "page"}])
    payload = await server.get_content_payload(reg, "aprd", gateway=gw)
    assert payload["count"] == 1


async def test_create_requires_profile_first(tmp_path):
    from wp_ops_mcp.profiles.store import ProfileStore
    reg = SiteRegistry.from_records(RAW)
    store = ProfileStore(tmp_path / "p.db")  # empty
    payload = await server.create_content_payload(
        reg, store, "aprd", [{"title": "X", "blocks": []}], gateway=FakeContentGateway())
    assert "error" in payload
    assert "wp_discover_site" in payload["error"]


async def test_create_dry_run_with_profile(tmp_path):
    from wp_ops_mcp.profiles.store import ProfileStore
    reg = SiteRegistry.from_records(RAW)
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg", builder="gutenberg", divi_major=None)
    gw = FakeContentGateway()
    payload = await server.create_content_payload(
        reg, store, "astg",
        [{"title": "New Page", "blocks": [{"kind": "paragraph", "text": "hi"}]}],
        dry_run=True, gateway=gw)
    assert payload["dry_run"] is True
    assert gw.created == []


async def test_prod_apply_blocked_without_override(tmp_path):
    from wp_ops_mcp.profiles.store import ProfileStore
    reg = SiteRegistry.from_records(RAW)
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd")  # aprd derives environment=prod
    payload = await server.create_content_payload(
        reg, store, "aprd", [{"title": "X", "blocks": []}],
        dry_run=False, allow_prod=False, gateway=FakeContentGateway())
    assert "error" in payload
    assert "prod" in payload["error"].lower()


async def test_prod_apply_allowed_with_override(tmp_path):
    from wp_ops_mcp.profiles.store import ProfileStore
    reg = SiteRegistry.from_records(RAW)
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="gutenberg", divi_major=None)
    gw = FakeContentGateway()
    payload = await server.create_content_payload(
        reg, store, "aprd",
        [{"title": "Real", "blocks": [{"kind": "paragraph", "text": "x"}]}],
        dry_run=False, allow_prod=True, gateway=gw)
    assert payload["dry_run"] is False
    assert len(gw.created) == 1


# --- theme file tools ----------------------------------------------------------------
# Writing a theme file executes code on the site. These pin the guardrails: unknown
# install refused, prod gated, dry-run writes nothing and shows the current file.

class _ThemeGw:
    def __init__(self):
        self.wrote = None
        self.current = "<?php // old\n"

    async def get_theme_file(self, file, theme=None):
        return {"theme": "Divi-child", "file": file, "contents": self.current,
                "bytes": len(self.current), "writable": True}

    async def set_theme_file(self, file, contents, theme=None, allow_create=False,
                             allow_other_theme=False):
        self.wrote = contents
        prev, self.current = self.current, contents
        return {"theme": "Divi-child", "file": file, "written": True, "created": False,
                "bytes": len(contents), "verified": True, "previous": prev,
                "previous_bytes": len(prev)}


def _reg_with(install="acmestg", environment="staging"):
    from wp_ops_mcp.registry import SiteRegistry
    return SiteRegistry.from_records([{
        "wpe_install": install, "wpe_account": "a", "domain": "x.test", "active": True,
        "php_version": "", "cf_zone_id": "", "environment": environment}])


def test_set_theme_file_refuses_unknown_install():
    import asyncio
    from wp_ops_mcp import server
    r = asyncio.run(server.set_theme_file_payload(
        _reg_with(), "nope", "style.css", "body{}", dry_run=False, gateway=_ThemeGw()))
    assert r["action"] == "refused" and "unknown install" in r["reason"]


def test_set_theme_file_dry_run_writes_nothing_and_shows_current():
    import asyncio
    from wp_ops_mcp import server
    gw = _ThemeGw()
    r = asyncio.run(server.set_theme_file_payload(
        _reg_with(), "acmestg", "functions.php", "<?php // new\n",
        dry_run=True, gateway=gw))
    assert r["action"] == "preview"
    assert gw.wrote is None, "dry run must not write"
    assert r["current"] == "<?php // old\n"


def test_set_theme_file_blocks_prod_without_allow_prod():
    import asyncio
    from wp_ops_mcp import server
    gw = _ThemeGw()
    r = asyncio.run(server.set_theme_file_payload(
        _reg_with("acmeprd", "prod"), "acmeprd", "functions.php", "<?php x",
        dry_run=False, gateway=gw))
    assert r["action"] == "refused" and gw.wrote is None


def test_set_theme_file_writes_on_staging_and_returns_previous():
    import asyncio
    from wp_ops_mcp import server
    gw = _ThemeGw()
    r = asyncio.run(server.set_theme_file_payload(
        _reg_with(), "acmestg", "functions.php", "<?php // new\n",
        dry_run=False, gateway=gw))
    assert r["action"] == "applied"
    assert gw.wrote == "<?php // new\n"
    assert r["previous"] == "<?php // old\n"


def test_get_theme_file_reads():
    import asyncio
    from wp_ops_mcp import server
    r = asyncio.run(server.get_theme_file_payload(
        _reg_with(), "acmestg", "style.css", gateway=_ThemeGw()))
    assert r["action"] == "ok" and r["contents"] == "<?php // old\n"

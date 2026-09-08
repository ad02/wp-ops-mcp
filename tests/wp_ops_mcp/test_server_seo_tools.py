"""Contract tests for the Phase 5a SEO tools (wp_get_seo / wp_set_seo) and the
optional create-time SEO fields on the FastMCP server.

Mirrors test_server_edit_tools.py: fake at the gateway boundary and assert the
tool layer refuses correctly (unknown install, wpcli transport, no fields, prod
gate) BEFORE any write, and that wp_create_content threads SEO through to the
gateway. SEO is builder-independent, so (unlike the edit layer) there is no
profile/builder precondition on the get/set tools.
"""
import asyncio

from wp_ops_mcp import server
from wp_ops_mcp.registry import SiteRegistry
from wp_ops_mcp.profiles.store import ProfileStore, SiteProfile

RAW = [
    {"domain": "a.com", "wpe_account": "hostacct1", "wpe_install": "aprd",
     "cf_zone_id": "z1", "php_version": "8.2", "active": True},   # aprd -> prod
    {"domain": "", "wpe_account": "hostacct1", "wpe_install": "astg",
     "cf_zone_id": "", "php_version": "8.2", "active": True},     # astg -> staging
]

PARAGRAPH = [{"title": "New Page", "blocks": [{"kind": "paragraph", "text": "hi"}]}]


def _reg():
    return SiteRegistry.from_records(RAW)


def _profile(store, install, builder="gutenberg", divi_major=None):
    store.upsert(SiteProfile(
        install=install, account="hostacct1", environment="x", domain="",
        discovered_at="t", wp_version="6.5", php_version="8.2", active_theme="Divi",
        active_theme_version="4.27", builder=builder, divi_major=divi_major,
        multisite=False, plugins=[], raw={}))


class FakeSeoGateway:
    """SEO-capable content gateway (exposes ensure_seo_capable) + the create surface.

    set_calls records every set_post_meta call so tests can assert SEO reached the
    gateway (and which post id it targeted).
    """

    def __init__(self, plugin="seopress", existing=None):
        self.info = {"plugin_version": "1.1.0", "seo_plugin": plugin}
        self.meta = {}
        self.set_calls = []                 # [(post_id, meta), ...]
        self.existing = existing or []
        self.created = []

    async def ensure_seo_capable(self):
        return self.info

    async def get_post_meta(self, post_id):
        return dict(self.meta)

    async def set_post_meta(self, post_id, meta):
        self.set_calls.append((post_id, dict(meta)))
        self.meta.update(meta)
        return list(meta)

    async def list_posts(self, post_type, query=None):
        return list(self.existing)

    async def create_post(self, post_type, title, slug, status, content, meta):
        self.created.append({"slug": slug})
        return 4242


class FakeWpcliGateway:
    """wpcli-style gateway: NO ensure_seo_capable, so SEO tools must refuse."""

    def __init__(self, existing=None):
        self.existing = existing or []
        self.created = []

    async def list_posts(self, post_type, query=None):
        return list(self.existing)

    async def create_post(self, post_type, title, slug, status, content, meta):
        self.created.append({"slug": slug})
        return 4242


# --- registration -----------------------------------------------------------

def test_seo_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"wp_get_seo", "wp_set_seo"} <= names


# --- unknown install (both tools) -------------------------------------------

async def test_get_seo_unknown_install():
    payload = await server.get_seo_payload(_reg(), "nope", 10, gateway=FakeSeoGateway())
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]


async def test_set_seo_unknown_install():
    payload = await server.set_seo_payload(_reg(), "nope", 10, title="T",
                                           gateway=FakeSeoGateway())
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]


# --- wpcli gateway has no SEO support (both tools) --------------------------

async def test_get_seo_wpcli_gateway_refused():
    payload = await server.get_seo_payload(_reg(), "astg", 10, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


async def test_set_seo_wpcli_gateway_refused():
    payload = await server.set_seo_payload(_reg(), "astg", 10, title="T",
                                           gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


# --- set: no fields ---------------------------------------------------------

async def test_set_seo_no_fields_refused():
    payload = await server.set_seo_payload(_reg(), "astg", 10, gateway=FakeSeoGateway())
    assert payload["action"] == "refused"
    assert "no SEO fields" in payload["reason"]


# --- set: prod gate honours dry_run -----------------------------------------

async def test_set_seo_prod_blocked_without_allow_prod():
    gw = FakeSeoGateway()
    payload = await server.set_seo_payload(_reg(), "aprd", 10, title="T",
                                           dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "prod" in payload["reason"].lower()
    assert gw.set_calls == []              # gate fired before any write


async def test_set_seo_prod_dry_run_passes_gate():
    gw = FakeSeoGateway()
    payload = await server.set_seo_payload(_reg(), "aprd", 10, title="T",
                                           dry_run=True, gateway=gw)
    assert payload["action"] == "preview"  # dry-run bypasses the prod gate
    assert gw.set_calls == []


# --- set: dry-run preview passthrough + real apply --------------------------

async def test_set_seo_dry_run_preview_passthrough():
    gw = FakeSeoGateway()
    payload = await server.set_seo_payload(_reg(), "astg", 10, title="T", noindex=True,
                                           dry_run=True, gateway=gw)
    assert payload["action"] == "preview"
    assert payload["plugin"] == "seopress"
    assert gw.set_calls == []


async def test_set_seo_applies_on_real_run():
    gw = FakeSeoGateway()
    payload = await server.set_seo_payload(_reg(), "astg", 10, title="T",
                                           dry_run=False, gateway=gw)
    assert payload["action"] == "applied" and payload["verified"] is True
    assert gw.set_calls and gw.set_calls[0][0] == 10


# --- get: happy path --------------------------------------------------------

async def test_get_seo_returns_fields():
    gw = FakeSeoGateway()
    gw.meta = {"_seopress_titles_title": "Hello"}
    payload = await server.get_seo_payload(_reg(), "astg", 10, gateway=gw)
    assert payload["plugin"] == "seopress"
    assert payload["fields"]["title"] == "Hello"


# --- create-with-seo --------------------------------------------------------

async def test_create_with_seo_attaches_result(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg")
    gw = FakeSeoGateway()
    payload = await server.create_content_payload(
        _reg(), store, "astg", PARAGRAPH, dry_run=False,
        seo={"title": "SEO T"}, gateway=gw)
    created = [i for i in payload["items"] if i["action"] == "created"]
    assert created and created[0]["seo"]["action"] == "applied"
    assert gw.set_calls and gw.set_calls[0][0] == 4242   # SEO write hit the new post id


async def test_create_with_seo_dry_run_previews(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg")
    gw = FakeSeoGateway()
    payload = await server.create_content_payload(
        _reg(), store, "astg", PARAGRAPH, dry_run=True,
        seo={"title": "SEO T"}, gateway=gw)
    assert payload["seo_preview"] == {"title": "SEO T"}
    assert gw.set_calls == []              # dry-run writes nothing


async def test_create_with_seo_bad_type_rejected(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg")
    payload = await server.create_content_payload(
        _reg(), store, "astg", [{"title": "X", "blocks": []}],
        dry_run=True, seo="notadict", gateway=FakeSeoGateway())
    assert payload["error"] == "seo must be an object"


async def test_create_with_seo_wpcli_gateway_refused(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg")
    gw = FakeWpcliGateway()
    payload = await server.create_content_payload(
        _reg(), store, "astg", PARAGRAPH, dry_run=False,
        seo={"title": "SEO T"}, gateway=gw)
    created = [i for i in payload["items"] if i["action"] == "created"]
    assert created and created[0]["seo"] == {
        "action": "refused", "reason": "SEO requires REST transport"}

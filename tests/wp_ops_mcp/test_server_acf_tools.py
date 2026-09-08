"""Contract tests for the Phase 5i ACF tools (wp_get_acf / wp_set_acf) on the
FastMCP server.

Mirrors test_server_site_tools.py: fake at the gateway boundary and assert the tool
layer refuses correctly (unknown install, non-REST transport, prod gate) BEFORE any
ACF value changes, and that ops-level outcomes (readback-verified apply, ACF-inactive,
bad fields) surface through the tool. ACF values are builder-independent, so (like
SEO/media/menus/site) there is no profile/builder precondition - only the REST transport.
"""
import asyncio

from wp_ops_mcp import server
from wp_ops_mcp.registry import SiteRegistry
from wp_ops_mcp.transport.rest import RestError

RAW = [
    {"domain": "a.com", "wpe_account": "hostacct1", "wpe_install": "aprd",
     "cf_zone_id": "z1", "php_version": "8.2", "active": True},   # aprd -> prod
    {"domain": "", "wpe_account": "hostacct1", "wpe_install": "astg",
     "cf_zone_id": "", "php_version": "8.2", "active": True},     # astg -> staging
]


def _reg():
    return SiteRegistry.from_records(RAW)


class FakeAcfGateway:
    """ACF-capable REST-shaped gateway; records every set_acf_fields call."""

    def __init__(self):
        self.info = {"plugin_version": "1.2.0", "acf": "6.2.1"}
        self.store = {}
        self.set_calls = []

    async def ensure_acf_capable(self):
        return self.info

    async def get_acf_fields(self, post_id):
        return dict(self.store)

    async def set_acf_fields(self, post_id, fields):
        self.set_calls.append((post_id, dict(fields)))
        self.store.update(fields)
        return {"applied": list(fields), "skipped": [], "acf": dict(self.store)}


class InactiveAcfGateway(FakeAcfGateway):
    """ACF plugin present but ACF itself not active: capability probe raises."""

    async def ensure_acf_capable(self):
        raise RestError("ACF not active on this site (wp-ops-connect reports acf=null)")


class FakeWpcliGateway:
    """wpcli-style gateway: NO get_acf_fields, so the ACF tools must refuse."""

    async def list_posts(self, post_type, query=None):
        return []


# --- registration -----------------------------------------------------------

def test_acf_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"wp_get_acf", "wp_set_acf"} <= names


# --- unknown install --------------------------------------------------------

async def test_get_acf_unknown_install():
    payload = await server.get_acf_payload(_reg(), "nope", 10, gateway=FakeAcfGateway())
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]


async def test_set_acf_unknown_install():
    gw = FakeAcfGateway()
    payload = await server.set_acf_payload(
        _reg(), "nope", 10, {"headline": "T"}, dry_run=False, gateway=gw)
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]
    assert gw.set_calls == []


# --- non-REST transport -----------------------------------------------------

async def test_get_acf_wpcli_gateway_refused():
    payload = await server.get_acf_payload(_reg(), "astg", 10, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused" and "REST transport" in payload["reason"]


async def test_set_acf_wpcli_gateway_refused():
    payload = await server.set_acf_payload(
        _reg(), "astg", 10, {"headline": "T"}, dry_run=False, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused" and "REST transport" in payload["reason"]


# --- prod gate honours dry_run ----------------------------------------------

async def test_set_acf_prod_blocked_without_allow_prod():
    gw = FakeAcfGateway()
    payload = await server.set_acf_payload(
        _reg(), "aprd", 10, {"headline": "T"}, dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused" and "prod" in payload["reason"].lower()
    assert gw.set_calls == []                  # gate fired before any write


async def test_set_acf_prod_dry_run_passes_gate():
    gw = FakeAcfGateway()
    payload = await server.set_acf_payload(
        _reg(), "aprd", 10, {"headline": "T"}, dry_run=True, gateway=gw)
    assert payload["action"] == "preview"      # dry-run bypasses the prod gate
    assert gw.set_calls == []


async def test_set_acf_prod_with_allow_prod_writes():
    gw = FakeAcfGateway()
    payload = await server.set_acf_payload(
        _reg(), "aprd", 10, {"headline": "T"}, dry_run=False, allow_prod=True, gateway=gw)
    assert payload["action"] == "applied" and payload["verified"] is True
    assert gw.set_calls and gw.set_calls[0][0] == 10


# --- get: happy path (read-only, no prod gate) ------------------------------

async def test_get_acf_returns_fields():
    gw = FakeAcfGateway()
    gw.store = {"headline": "Hello", "count": 3}
    payload = await server.get_acf_payload(_reg(), "astg", 10, gateway=gw)
    assert payload["action"] == "ok"
    assert payload["acf"] == {"headline": "Hello", "count": 3}


async def test_get_acf_inactive_is_error_dict_not_refusal():
    # ACF not active is an ops-level error dict (the transport IS REST), distinct from
    # the wpcli transport refusal - that difference is what Task 4 live-validates.
    payload = await server.get_acf_payload(_reg(), "astg", 10, gateway=InactiveAcfGateway())
    assert payload["action"] == "error" and "ACF not active" in payload["error"]


# --- set: dry-run preview + real apply --------------------------------------

async def test_set_acf_dry_run_preview_passthrough():
    gw = FakeAcfGateway()
    payload = await server.set_acf_payload(
        _reg(), "astg", 10, {"headline": "T"}, dry_run=True, gateway=gw)
    assert payload["action"] == "preview" and payload["fields"] == {"headline": "T"}
    assert gw.set_calls == []


async def test_set_acf_applies_on_real_run():
    gw = FakeAcfGateway()
    payload = await server.set_acf_payload(
        _reg(), "astg", 10, {"headline": "T"}, dry_run=False, gateway=gw)
    assert payload["action"] == "applied" and payload["verified"] is True
    assert gw.set_calls and gw.set_calls[0][0] == 10


# --- fields shape is refused before any other write gate --------------------
# Mirrors set_seo_payload: the caller's own mistake (nothing to write) is reported
# before the fleet gates, so a prod site with empty fields says what is actually
# wrong instead of sending the caller off to re-run with allow_prod.

async def test_set_acf_empty_fields_refused():
    gw = FakeAcfGateway()
    payload = await server.set_acf_payload(_reg(), "astg", 10, {}, dry_run=False, gateway=gw)
    assert payload["action"] == "refused" and "no ACF fields provided" in payload["reason"]
    assert gw.set_calls == []


async def test_set_acf_non_dict_fields_refused():
    gw = FakeAcfGateway()
    payload = await server.set_acf_payload(
        _reg(), "astg", 10, "notadict", dry_run=False, gateway=gw)
    assert payload["action"] == "refused" and "no ACF fields provided" in payload["reason"]
    assert gw.set_calls == []


async def test_set_acf_prod_empty_fields_refuses_on_fields_not_prod():
    # Ordering: fields shape is checked ABOVE the prod gate, so prod + empty fields
    # names the real problem (nothing to write) instead of the prod refusal.
    gw = FakeAcfGateway()
    payload = await server.set_acf_payload(
        _reg(), "aprd", 10, {}, dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "no ACF fields provided" in payload["reason"]
    assert "prod" not in payload["reason"].lower()
    assert gw.set_calls == []

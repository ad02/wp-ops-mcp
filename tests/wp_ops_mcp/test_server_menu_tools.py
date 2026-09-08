"""Contract tests for the Phase 5d menu tools (wp_list_menus / wp_add_menu_item /
wp_update_menu_item / wp_remove_menu_item) on the FastMCP server.

Mirrors test_server_media_tool.py: fake at the gateway boundary and assert the tool
layer refuses correctly (unknown install, non-REST transport, prod gate) BEFORE any
nav change reaches the site. Menus are builder-independent, so (like SEO/media)
there is no profile/builder precondition - only the REST transport.
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

MENUS = [{"id": 3, "name": "Main Menu", "slug": "main-menu", "locations": ["primary"]}]


def _reg():
    return SiteRegistry.from_records(RAW)


class FakeMenuGateway:
    """REST-shaped gateway: exposes list_menus, records every write."""

    def __init__(self):
        self.items = []
        self.adds = []
        self.updates = []
        self.deletes = []

    async def list_menus(self):
        return [dict(m) for m in MENUS]

    async def list_menu_items(self, menu_id):
        return [dict(i) for i in self.items]

    async def add_menu_item(self, menu_id, title, page_id=None, url=None,
                            parent=0, menu_order=None):
        self.adds.append({"menu_id": menu_id, "title": title, "page_id": page_id,
                          "url": url, "parent": parent, "menu_order": menu_order})
        item = {"id": 99, "title": title, "url": url or f"/?page_id={page_id}",
                "menu_order": menu_order, "parent": parent, "object": "page",
                "object_id": page_id, "type": "post_type", "status": "publish"}
        self.items.append(dict(item))
        return item

    async def update_menu_item(self, item_id, fields):
        self.updates.append({"item_id": item_id, "fields": dict(fields)})
        row = {"id": item_id, "title": "About", "url": "/about/", "menu_order": 1,
               "parent": 0, "object": "page", "object_id": 12,
               "type": "post_type", "status": "publish"}
        row.update(fields)
        return row

    async def delete_menu_item(self, item_id):
        self.deletes.append(item_id)


class FakeWpcliGateway:
    """wpcli-style gateway: NO list_menus, so the menu tools must refuse."""

    async def list_posts(self, post_type, query=None):
        return []


# --- registration -----------------------------------------------------------

def test_menu_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"wp_list_menus", "wp_add_menu_item", "wp_update_menu_item",
            "wp_remove_menu_item"} <= names


# --- unknown install --------------------------------------------------------

async def test_list_menus_unknown_install():
    payload = await server.list_menus_payload(_reg(), "nope", gateway=FakeMenuGateway())
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]


async def test_add_menu_item_unknown_install():
    gw = FakeMenuGateway()
    payload = await server.add_menu_item_payload(
        _reg(), "nope", "Main Menu", "About", page_id=12, dry_run=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]
    assert gw.adds == []


async def test_update_menu_item_unknown_install():
    gw = FakeMenuGateway()
    payload = await server.update_menu_item_payload(
        _reg(), "nope", 99, title="About Us", dry_run=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]
    assert gw.updates == []


async def test_remove_menu_item_unknown_install():
    gw = FakeMenuGateway()
    payload = await server.remove_menu_item_payload(
        _reg(), "nope", 99, dry_run=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]
    assert gw.deletes == []


# --- non-REST transport -----------------------------------------------------

async def test_list_menus_wpcli_gateway_refused():
    payload = await server.list_menus_payload(_reg(), "astg", gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


async def test_add_menu_item_wpcli_gateway_refused():
    payload = await server.add_menu_item_payload(
        _reg(), "astg", "Main Menu", "About", page_id=12,
        dry_run=False, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


async def test_update_menu_item_wpcli_gateway_refused():
    payload = await server.update_menu_item_payload(
        _reg(), "astg", 99, title="About Us", dry_run=False, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


async def test_remove_menu_item_wpcli_gateway_refused():
    payload = await server.remove_menu_item_payload(
        _reg(), "astg", 99, dry_run=False, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


# --- prod gate honours dry_run ----------------------------------------------

async def test_add_menu_item_prod_blocked_without_allow_prod():
    gw = FakeMenuGateway()
    payload = await server.add_menu_item_payload(
        _reg(), "aprd", "Main Menu", "About", page_id=12,
        dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "prod" in payload["reason"].lower()
    assert gw.adds == []               # gate fired before any write


async def test_add_menu_item_prod_dry_run_passes_gate():
    gw = FakeMenuGateway()
    payload = await server.add_menu_item_payload(
        _reg(), "aprd", "Main Menu", "About", page_id=12, dry_run=True, gateway=gw)
    assert payload["action"] == "preview"      # dry-run bypasses the prod gate
    assert gw.adds == []


async def test_add_menu_item_prod_with_allow_prod_writes():
    gw = FakeMenuGateway()
    payload = await server.add_menu_item_payload(
        _reg(), "aprd", "Main Menu", "About", page_id=12,
        dry_run=False, allow_prod=True, gateway=gw)
    assert payload["action"] == "added" and payload["verified"] is True
    assert len(gw.adds) == 1


async def test_update_menu_item_prod_blocked_without_allow_prod():
    gw = FakeMenuGateway()
    payload = await server.update_menu_item_payload(
        _reg(), "aprd", 99, title="About Us", dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "prod" in payload["reason"].lower()
    assert gw.updates == []             # gate fired before any write


async def test_update_menu_item_prod_dry_run_passes_gate():
    gw = FakeMenuGateway()
    payload = await server.update_menu_item_payload(
        _reg(), "aprd", 99, title="About Us", dry_run=True, gateway=gw)
    assert payload["action"] == "preview"
    assert gw.updates == []


async def test_remove_menu_item_prod_blocked_without_allow_prod():
    gw = FakeMenuGateway()
    payload = await server.remove_menu_item_payload(
        _reg(), "aprd", 99, dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "prod" in payload["reason"].lower()
    assert gw.deletes == []


async def test_remove_menu_item_prod_dry_run_passes_gate():
    gw = FakeMenuGateway()
    payload = await server.remove_menu_item_payload(
        _reg(), "aprd", 99, dry_run=True, gateway=gw)
    assert payload["action"] == "preview"
    assert gw.deletes == []


# --- read-only list has no prod gate ----------------------------------------

async def test_list_menus_happy_path_on_prod():
    payload = await server.list_menus_payload(_reg(), "aprd", gateway=FakeMenuGateway())
    assert payload == {"action": "ok", "menus": MENUS}


# --- happy paths ------------------------------------------------------------

async def test_add_menu_item_happy_path():
    gw = FakeMenuGateway()
    payload = await server.add_menu_item_payload(
        _reg(), "astg", "Main Menu", "About", page_id=12, parent=4, position=2,
        dry_run=False, gateway=gw)
    assert payload["action"] == "added" and payload["verified"] is True
    assert payload["item"]["id"] == 99
    assert gw.adds == [{"menu_id": 3, "title": "About", "page_id": 12,
                        "url": None, "parent": 4, "menu_order": 2}]


async def test_add_menu_item_custom_url_happy_path():
    gw = FakeMenuGateway()
    payload = await server.add_menu_item_payload(
        _reg(), "astg", 3, "Blog", url="https://a.com/blog", dry_run=False, gateway=gw)
    assert payload["action"] == "added"
    assert gw.adds[0]["url"] == "https://a.com/blog"


async def test_update_menu_item_happy_path():
    gw = FakeMenuGateway()
    payload = await server.update_menu_item_payload(
        _reg(), "astg", 99, title="About Us", position=3, dry_run=False, gateway=gw)
    assert payload["action"] == "updated"
    assert payload["item"]["id"] == 99 and payload["item"]["title"] == "About Us"
    assert gw.updates == [{"item_id": 99,
                           "fields": {"title": "About Us", "menu_order": 3}}]


async def test_remove_menu_item_happy_path():
    gw = FakeMenuGateway()
    payload = await server.remove_menu_item_payload(
        _reg(), "astg", 99, dry_run=False, gateway=gw)
    assert payload == {"action": "removed", "item_id": 99}
    assert gw.deletes == [99]


# --- ops-level validation still surfaces through the tool -------------------

async def test_add_menu_item_without_a_target_is_an_error_dict():
    gw = FakeMenuGateway()
    payload = await server.add_menu_item_payload(
        _reg(), "astg", "Main Menu", "About", dry_run=False, gateway=gw)
    assert payload["action"] == "error"
    assert "exactly one of page_id or url" in payload["error"]
    assert gw.adds == []


async def test_update_menu_item_without_fields_is_an_error_dict():
    gw = FakeMenuGateway()
    payload = await server.update_menu_item_payload(
        _reg(), "astg", 99, dry_run=False, gateway=gw)
    assert payload["action"] == "error"
    assert "no fields provided" in payload["error"]
    assert gw.updates == []


async def test_add_menu_item_unknown_menu_is_an_error_dict():
    gw = FakeMenuGateway()
    payload = await server.add_menu_item_payload(
        _reg(), "astg", "Nope", "About", page_id=12, dry_run=False, gateway=gw)
    assert payload["action"] == "error"
    assert "available menus:" in payload["error"]
    assert gw.adds == []

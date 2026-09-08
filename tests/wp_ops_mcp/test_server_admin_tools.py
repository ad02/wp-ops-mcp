"""Contract tests for the Phase 5j admin tools (users / options / comments) on the
FastMCP server.

Mirrors test_server_menu_tools.py: fake at the gateway boundary and assert the tool
layer refuses correctly (unknown install, non-REST transport, prod gate) BEFORE any
user deletion or option write reaches the site. Admin surfaces are
builder-independent, so - like SEO/media/menus - there is no profile/builder
precondition; only the REST transport.

The read-only tools (list/get) deliberately have NO prod gate: reading a prod site's
users, options or comments changes nothing, and gating it would only push operators
toward passing allow_prod habitually.
"""
import asyncio

import pytest

from wp_ops_mcp import server
from wp_ops_mcp.registry import SiteRegistry

RAW = [
    {"domain": "a.com", "wpe_account": "hostacct1", "wpe_install": "aprd",
     "cf_zone_id": "z1", "php_version": "8.2", "active": True},   # aprd -> prod
    {"domain": "", "wpe_account": "hostacct1", "wpe_install": "astg",
     "cf_zone_id": "", "php_version": "8.2", "active": True},     # astg -> staging
]

SECRET = "S3cret-passw0rd!"

TOOL_NAMES = {
    "wp_list_users", "wp_get_user", "wp_create_user", "wp_update_user", "wp_delete_user",
    "wp_list_options", "wp_get_option", "wp_set_option", "wp_delete_option",
    "wp_list_comments", "wp_moderate_comment", "wp_delete_comment",
}


def _reg():
    return SiteRegistry.from_records(RAW)


class FakeAdminGateway:
    """REST-shaped admin gateway: exposes list_users AND get_option, records writes."""

    def __init__(self):
        self.options = {"blogname": "Site"}
        self.writes = []

    # users
    async def list_users(self, search=None):
        return [{"id": 1, "username": "admin", "name": "Admin", "email": "a@x.test",
                 "roles": ["administrator"], "url": ""}]

    async def get_user(self, user_id):
        return {"id": user_id, "username": "admin", "name": "Admin",
                "email": "a@x.test", "roles": ["administrator"], "url": ""}

    async def create_user(self, username, email, password, roles=None, name=None):
        self.writes.append(("create_user", username))
        return {"id": 42, "username": username, "name": name or username,
                "email": email, "roles": [roles] if isinstance(roles, str) else list(roles or []),
                "url": ""}

    async def update_user(self, user_id, fields):
        self.writes.append(("update_user", user_id, dict(fields)))
        return {"id": user_id, "username": "admin", "name": fields.get("name", "Admin"),
                "email": "a@x.test", "roles": ["administrator"], "url": ""}

    async def delete_user(self, user_id, reassign=0):
        self.writes.append(("delete_user", user_id, reassign))
        return {"deleted": True, "previous": user_id}

    # options
    async def ensure_options_capable(self):
        return {"plugin_version": "1.3.0"}

    async def list_option_names(self):
        return sorted(self.options)

    async def get_option(self, name):
        if name in self.options:
            return {"name": name, "value": self.options[name], "exists": True}
        return {"name": name, "value": None, "exists": False}

    async def set_option(self, name, value):
        self.writes.append(("set_option", name, value))
        self.options[name] = value
        return {"name": name, "value": value}

    async def delete_option(self, name):
        self.writes.append(("delete_option", name))
        return self.options.pop(name, None) is not None

    # comments
    async def list_comments(self, post_id=None, status=None):
        return [{"id": 11, "post": 5, "author_name": "Bot", "content": "x",
                 "status": "hold", "date": "2026-07-01T00:00:00"}]

    async def update_comment(self, comment_id, fields):
        self.writes.append(("update_comment", comment_id, dict(fields)))
        return {"id": comment_id, "post": 5, "author_name": "Bot", "content": "x",
                "status": fields.get("status", "hold"), "date": "2026-07-01T00:00:00"}

    async def delete_comment(self, comment_id, force=False):
        self.writes.append(("delete_comment", comment_id, force))
        if force:
            return {"deleted": True, "id": comment_id, "status": "deleted"}
        return {"deleted": False, "id": comment_id, "status": "trash"}


class FakeWpcliGateway:
    """wpcli-style gateway: NO list_users and NO get_option, so admin tools refuse."""

    async def list_posts(self, post_type, query=None):
        return []


# Every admin payload, bound to its arguments, so the three refusal families
# (unknown install, non-REST transport, prod gate) are asserted for ALL 12 tools
# instead of a representative sample.
READ_CALLS = {
    "list_users": lambda reg, install, gw: server.list_users_payload(reg, install, gateway=gw),
    "get_user": lambda reg, install, gw: server.get_user_payload(reg, install, 1, gateway=gw),
    "list_options": lambda reg, install, gw: server.list_options_payload(reg, install, gateway=gw),
    "get_option": lambda reg, install, gw: server.get_option_payload(
        reg, install, "blogname", gateway=gw),
    "list_comments": lambda reg, install, gw: server.list_comments_payload(
        reg, install, gateway=gw),
}

WRITE_CALLS = {
    "create_user": lambda reg, install, gw, **kw: server.create_user_payload(
        reg, install, "newbie", "n@x.test", SECRET, gateway=gw, **kw),
    "update_user": lambda reg, install, gw, **kw: server.update_user_payload(
        reg, install, 7, {"name": "Ed"}, gateway=gw, **kw),
    "delete_user": lambda reg, install, gw, **kw: server.delete_user_payload(
        reg, install, 7, gateway=gw, **kw),
    "set_option": lambda reg, install, gw, **kw: server.set_option_payload(
        reg, install, "wpops_test", 1, gateway=gw, **kw),
    "delete_option": lambda reg, install, gw, **kw: server.delete_option_payload(
        reg, install, "blogname", gateway=gw, **kw),
    "moderate_comment": lambda reg, install, gw, **kw: server.moderate_comment_payload(
        reg, install, 11, "spam", gateway=gw, **kw),
    "delete_comment": lambda reg, install, gw, **kw: server.delete_comment_payload(
        reg, install, 11, gateway=gw, **kw),
}


# --- registration -----------------------------------------------------------

def test_admin_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert TOOL_NAMES <= names


def test_dangerous_tool_docstrings_state_the_blast_radius():
    tools = {t.name: (t.description or "") for t in asyncio.run(server.mcp.list_tools())}
    assert "siteurl" in tools["wp_set_option"] and "offline" in tools["wp_set_option"]
    assert "reassign" in tools["wp_delete_user"]
    assert "permanent" in tools["wp_delete_user"].lower()
    assert "force" in tools["wp_delete_comment"] and "trash" in tools["wp_delete_comment"].lower()


def test_option_listing_limitation_is_documented():
    tools = {t.name: (t.description or "") for t in asyncio.run(server.mcp.list_tools())}
    assert "autoload" in tools["wp_list_options"].lower()


# --- unknown install --------------------------------------------------------

@pytest.mark.parametrize("name", sorted(READ_CALLS))
async def test_read_tools_refuse_unknown_install(name):
    payload = await READ_CALLS[name](_reg(), "nope", FakeAdminGateway())
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]


@pytest.mark.parametrize("name", sorted(WRITE_CALLS))
async def test_write_tools_refuse_unknown_install(name):
    gw = FakeAdminGateway()
    payload = await WRITE_CALLS[name](_reg(), "nope", gw, dry_run=False)
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]
    assert gw.writes == []


# --- non-REST transport -----------------------------------------------------

@pytest.mark.parametrize("name", sorted(READ_CALLS))
async def test_read_tools_refuse_wpcli_gateway(name):
    payload = await READ_CALLS[name](_reg(), "astg", FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


@pytest.mark.parametrize("name", sorted(WRITE_CALLS))
async def test_write_tools_refuse_wpcli_gateway(name):
    payload = await WRITE_CALLS[name](_reg(), "astg", FakeWpcliGateway(), dry_run=False)
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


# --- prod gate honours dry_run ----------------------------------------------

@pytest.mark.parametrize("name", sorted(WRITE_CALLS))
async def test_write_tools_blocked_on_prod_without_allow_prod(name):
    gw = FakeAdminGateway()
    payload = await WRITE_CALLS[name](_reg(), "aprd", gw, dry_run=False, allow_prod=False)
    assert payload["action"] == "refused"
    assert "prod" in payload["reason"].lower()
    assert gw.writes == []                     # gate fired before any write


@pytest.mark.parametrize("name", sorted(WRITE_CALLS))
async def test_write_tools_dry_run_passes_the_prod_gate(name):
    gw = FakeAdminGateway()
    payload = await WRITE_CALLS[name](_reg(), "aprd", gw, dry_run=True)
    assert payload["action"] == "preview"
    assert gw.writes == []


@pytest.mark.parametrize("name", sorted(READ_CALLS))
async def test_read_tools_have_no_prod_gate(name):
    payload = await READ_CALLS[name](_reg(), "aprd", FakeAdminGateway())
    assert payload["action"] == "ok"


# --- happy paths ------------------------------------------------------------

async def test_list_users_happy_path():
    payload = await server.list_users_payload(_reg(), "astg", gateway=FakeAdminGateway())
    assert payload["count"] == 1 and payload["users"][0]["username"] == "admin"


async def test_create_user_happy_path_never_echoes_the_password():
    gw = FakeAdminGateway()
    payload = await server.create_user_payload(
        _reg(), "astg", "newbie", "n@x.test", SECRET, roles="editor",
        dry_run=False, gateway=gw)
    assert payload["action"] == "created" and payload["user"]["id"] == 42
    assert SECRET not in repr(payload)
    assert gw.writes == [("create_user", "newbie")]


async def test_create_user_preview_never_echoes_the_password():
    payload = await server.create_user_payload(
        _reg(), "aprd", "newbie", "n@x.test", SECRET, gateway=FakeAdminGateway())
    assert SECRET not in repr(payload)


async def test_update_user_empty_fields_refused_before_the_prod_gate():
    gw = FakeAdminGateway()
    payload = await server.update_user_payload(
        _reg(), "aprd", 7, {}, dry_run=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "no user fields" in payload["reason"]      # names the real mistake, not prod
    assert gw.writes == []


async def test_delete_user_happy_path_passes_reassign_through():
    gw = FakeAdminGateway()
    payload = await server.delete_user_payload(
        _reg(), "astg", 7, reassign=1, dry_run=False, gateway=gw)
    assert payload["action"] == "deleted" and payload["reassign"] == 1
    assert gw.writes == [("delete_user", 7, 1)]


async def test_set_option_happy_path_verifies_by_readback():
    gw = FakeAdminGateway()
    payload = await server.set_option_payload(
        _reg(), "astg", "wpops_test", "hello", dry_run=False, gateway=gw)
    assert payload["action"] == "applied" and payload["verified"] is True
    assert gw.options["wpops_test"] == "hello"


async def test_delete_option_absent_is_a_noop():
    gw = FakeAdminGateway()
    payload = await server.delete_option_payload(
        _reg(), "astg", "never_existed", dry_run=False, gateway=gw)
    assert payload["action"] == "noop"


async def test_moderate_comment_happy_path():
    gw = FakeAdminGateway()
    payload = await server.moderate_comment_payload(
        _reg(), "astg", 11, "spam", dry_run=False, gateway=gw)
    assert payload["action"] == "moderated" and payload["comment"]["status"] == "spam"


async def test_moderate_comment_bad_status_is_an_error_dict():
    gw = FakeAdminGateway()
    payload = await server.moderate_comment_payload(
        _reg(), "astg", 11, "approve", dry_run=False, gateway=gw)
    assert payload["action"] == "error" and "approved" in payload["error"]
    assert gw.writes == []


async def test_delete_comment_default_trashes():
    gw = FakeAdminGateway()
    payload = await server.delete_comment_payload(
        _reg(), "astg", 11, dry_run=False, gateway=gw)
    assert payload["action"] == "trashed" and payload["recoverable"] is True
    assert gw.writes == [("delete_comment", 11, False)]


async def test_delete_comment_force_deletes():
    gw = FakeAdminGateway()
    payload = await server.delete_comment_payload(
        _reg(), "astg", 11, force=True, dry_run=False, gateway=gw)
    assert payload["action"] == "deleted"
    assert gw.writes == [("delete_comment", 11, True)]

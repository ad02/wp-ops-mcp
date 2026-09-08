"""Contract tests for the Phase 4a edit tools on the FastMCP server.

The edit tools are thin wrappers over EditOps; these tests fake at the gateway /
profile boundary and assert the tool layer refuses correctly BEFORE any I/O and
shapes results (preview_hint, merged find, probe) the way the brief specifies.
"""
import asyncio

import pytest

from wp_ops_mcp import server
from wp_ops_mcp.registry import SiteRegistry
from wp_ops_mcp.profiles.store import ProfileStore, SiteProfile

RAW = [
    {"domain": "a.com", "wpe_account": "hostacct1", "wpe_install": "aprd",
     "cf_zone_id": "z1", "php_version": "8.2", "active": True},
    {"domain": "", "wpe_account": "hostacct1", "wpe_install": "astg",
     "cf_zone_id": "", "php_version": "8.2", "active": True},
]

# A divi4 page that round-trips (reused from the EditOps orchestration tests).
DIVI4 = ('[et_pb_section][et_pb_row][et_pb_column type="4_4"]'
         '[et_pb_text admin_label="Body"]<p>old</p>[/et_pb_text]'
         '[/et_pb_column][/et_pb_row][/et_pb_section]')
UPDATE = [{"op": "update_element", "target": {"admin_label": "Body"},
           "content": "<p>new</p>"}]


def _profile(store, install, builder="divi", divi_major=4, domain=""):
    store.upsert(SiteProfile(
        install=install, account="hostacct1", environment="x", domain=domain,
        discovered_at="t", wp_version="6.5", php_version="8.2", active_theme="Divi",
        active_theme_version="4.27", builder=builder, divi_major=divi_major,
        multisite=False, plugins=[], raw={}))


class FakeEditGateway:
    """In-memory content gateway supporting the full edit surface."""

    def __init__(self, content=None, by_type=None):
        self.content = dict(content or {})          # post_id -> content str
        self.by_type = by_type or {"page": [], "post": []}
        self.deleted = []
        self.purged = []
        self._next = 9001

    async def list_posts(self, post_type, query=None):
        return list(self.by_type.get(post_type, []))

    async def get_post_content(self, post_id):
        if post_id not in self.content:
            raise RuntimeError("post not found")
        return self.content[post_id]

    async def duplicate_post(self, post_id):
        nid = self._next
        self._next += 1
        self.content[nid] = self.content[post_id]
        return nid

    async def update_post_content(self, post_id, content):
        self.content[post_id] = content
        return post_id

    async def purge_et_cache(self, post_id):
        self.purged.append(post_id)

    async def delete_post(self, post_id):
        self.deleted.append(post_id)
        self.content.pop(post_id, None)

    async def get_post_info(self, post_id):
        # In these contract tests the id passed as draft_id is always a staged
        # wpops draft, so report the draft-marker shape the guard expects.
        return {"name": f"page-wpops-draft-{post_id}", "status": "draft"}


def _reg():
    return SiteRegistry.from_records(RAW)


# --- registration + pure mapping (from the brief) ---------------------------

def test_edit_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"wp_find_page", "wp_extract_page", "wp_edit_page",
            "wp_publish_swap", "wp_discard_draft"} <= names


def test_builder_mapping_refuses_elementor():
    assert server._edit_builder_for({"builder": "elementor", "divi_major": None}) is None
    assert server._edit_builder_for({"builder": "divi", "divi_major": 4}) == "divi4"
    assert server._edit_builder_for({"builder": "divi", "divi_major": 5}) == "divi5"
    assert server._edit_builder_for({"builder": "gutenberg", "divi_major": None}) == "gutenberg"


def test_builder_mapping_reads_real_siteprofile():
    # The real runtime shape is a SiteProfile dataclass (attribute access), not a dict.
    prof = SiteProfile(
        install="x", account="a", environment="prod", domain="", discovered_at="t",
        wp_version="6.5", php_version="8.2", active_theme="Divi", active_theme_version="5",
        builder="divi", divi_major=5, multisite=False, plugins=[], raw={})
    assert server._edit_builder_for(prof) == "divi5"


# --- wp_find_page -----------------------------------------------------------

async def test_find_page_merges_pages_and_posts():
    gw = FakeEditGateway(by_type={
        "page": [{"id": 1, "title": "Home", "slug": "home", "status": "publish", "type": "page"}],
        "post": [{"id": 2, "title": "Hello", "slug": "hello", "status": "publish", "type": "post"}],
    })
    payload = await server.find_page_payload(_reg(), "aprd", "h", gateway=gw)
    assert payload["count"] == 2
    assert {i["id"] for i in payload["items"]} == {1, 2}


async def test_find_page_unknown_install():
    payload = await server.find_page_payload(_reg(), "nope", "x", gateway=FakeEditGateway())
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]


# --- wp_extract_page (read-only, no prod gate) ------------------------------

async def test_extract_refuses_without_profile(tmp_path):
    store = ProfileStore(tmp_path / "p.db")  # empty
    payload = await server.extract_page_payload(_reg(), store, "aprd", 10,
                                                gateway=FakeEditGateway())
    assert payload["action"] == "refused"
    assert "wp_discover_site" in payload["reason"]


async def test_extract_refuses_non_editable_builder(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="elementor", divi_major=None)
    payload = await server.extract_page_payload(_reg(), store, "aprd", 10,
                                                gateway=FakeEditGateway())
    assert payload["action"] == "refused"
    assert "elementor" in payload["reason"]


async def test_extract_returns_tree(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="divi", divi_major=4)
    gw = FakeEditGateway(content={10: DIVI4})
    payload = await server.extract_page_payload(_reg(), store, "aprd", 10, gateway=gw)
    assert payload["builder"] == "divi4"
    assert payload["roundtrip_ok"] is True
    assert payload["tree"][0]["tag"] == "et_pb_section"


async def test_extract_missing_post_returns_error(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="divi", divi_major=4)
    payload = await server.extract_page_payload(_reg(), store, "aprd", 999,
                                                gateway=FakeEditGateway())
    assert payload["action"] == "error"


async def test_extract_malformed_content_refuses(tmp_path):
    # Malformed Divi (mismatched close tag) does not round-trip; extract must refuse
    # via the rt_ok gate rather than letting parse() raise past the tool boundary.
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="divi", divi_major=4)
    gw = FakeEditGateway(
        content={10: "[et_pb_section][et_pb_row][/et_pb_row_extra][/et_pb_section]"})
    payload = await server.extract_page_payload(_reg(), store, "aprd", 10, gateway=gw)
    assert payload["action"] == "refused"
    assert "round-trip" in payload["reason"]


# --- wp_edit_page -----------------------------------------------------------

async def test_edit_dry_run_previews(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg", builder="divi", divi_major=4)
    gw = FakeEditGateway(content={10: DIVI4})
    payload = await server.edit_page_payload(_reg(), store, "astg", 10, UPDATE,
                                             dry_run=True, gateway=gw)
    assert payload["action"] == "preview"
    assert gw.content[10] == DIVI4          # nothing written


async def test_edit_staged_adds_preview_hint(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg", builder="divi", divi_major=4)
    gw = FakeEditGateway(
        content={10: DIVI4},
        by_type={"page": [{"id": 10, "title": "Contact", "slug": "contact"}], "post": []})
    payload = await server.edit_page_payload(_reg(), store, "astg", 10, UPDATE,
                                             dry_run=False, gateway=gw)
    assert payload["action"] == "staged"
    assert "Contact [wpops draft]" in payload["preview_hint"]
    assert str(payload["draft_id"]) in payload["preview_hint"]


async def test_edit_refuses_without_profile(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    payload = await server.edit_page_payload(_reg(), store, "aprd", 10, UPDATE,
                                             dry_run=False, gateway=FakeEditGateway())
    assert payload["action"] == "refused"


async def test_edit_prod_blocked_without_allow_prod(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="divi", divi_major=4)   # aprd derives prod
    gw = FakeEditGateway(content={10: DIVI4})
    payload = await server.edit_page_payload(_reg(), store, "aprd", 10, UPDATE,
                                             dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "prod" in payload["reason"].lower()
    assert gw.deleted == [] and gw.content[10] == DIVI4     # no I/O happened


async def test_edit_prod_allowed_with_allow_prod(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="divi", divi_major=4)
    gw = FakeEditGateway(
        content={10: DIVI4},
        by_type={"page": [{"id": 10, "title": "Home", "slug": "home"}], "post": []})
    payload = await server.edit_page_payload(_reg(), store, "aprd", 10, UPDATE,
                                             dry_run=False, allow_prod=True, gateway=gw)
    assert payload["action"] == "staged"


async def test_edit_prod_dry_run_allowed(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="divi", divi_major=4)
    gw = FakeEditGateway(content={10: DIVI4})
    payload = await server.edit_page_payload(_reg(), store, "aprd", 10, UPDATE,
                                             dry_run=True, gateway=gw)
    assert payload["action"] == "preview"          # dry-run bypasses the prod gate


# --- wp_publish_swap --------------------------------------------------------

async def test_publish_swap_prod_blocked(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="divi", divi_major=4, domain="a.com")
    payload = await server.publish_swap_payload(_reg(), store, "aprd", 10, 11,
                                                allow_prod=False, gateway=FakeEditGateway())
    assert payload["action"] == "refused"


async def test_publish_swap_probes_on_success(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg", builder="divi", divi_major=4, domain="staging.example")
    gw = FakeEditGateway(content={10: DIVI4, 11: DIVI4})
    seen = {}

    async def fake_probe(url):
        seen["url"] = url
        return {"url": url, "status": 200, "ok": True}

    payload = await server.publish_swap_payload(_reg(), store, "astg", 10, 11,
                                                gateway=gw, probe=fake_probe)
    assert payload["action"] == "published"
    assert payload["probe"]["ok"] is True
    assert seen["url"] == "https://staging.example/?p=10"


async def test_publish_swap_probe_error_is_wrapped(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg", builder="divi", divi_major=4, domain="staging.example")
    gw = FakeEditGateway(content={10: DIVI4, 11: DIVI4})

    async def boom_probe(url):
        raise RuntimeError("connection reset")

    payload = await server.publish_swap_payload(_reg(), store, "astg", 10, 11,
                                                gateway=gw, probe=boom_probe)
    assert payload["action"] == "published"        # publish still succeeded
    assert payload["probe"]["ok"] is False
    assert "connection reset" in payload["probe"]["error"]


async def test_publish_swap_no_domain_skips_probe(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg", builder="divi", divi_major=4, domain="")
    gw = FakeEditGateway(content={10: DIVI4, 11: DIVI4})
    payload = await server.publish_swap_payload(_reg(), store, "astg", 10, 11, gateway=gw)
    assert payload["action"] == "published"
    assert "probe" not in payload


# --- wp_discard_draft -------------------------------------------------------

async def test_discard_draft(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "astg", builder="divi", divi_major=4)
    gw = FakeEditGateway(content={11: DIVI4})
    payload = await server.discard_draft_payload(_reg(), store, "astg", 11, gateway=gw)
    assert payload["action"] == "discarded"
    assert 11 in gw.deleted


async def test_discard_prod_blocked(tmp_path):
    store = ProfileStore(tmp_path / "p.db")
    _profile(store, "aprd", builder="divi", divi_major=4)
    gw = FakeEditGateway(content={11: DIVI4})
    payload = await server.discard_draft_payload(_reg(), store, "aprd", 11,
                                                 allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert gw.deleted == []


# --- unknown install (mirrors wp_find_page's test) --------------------------

@pytest.mark.parametrize("call", [
    lambda reg, store: server.extract_page_payload(
        reg, store, "nope", 10, gateway=FakeEditGateway()),
    lambda reg, store: server.edit_page_payload(
        reg, store, "nope", 10, UPDATE, dry_run=False, gateway=FakeEditGateway()),
    lambda reg, store: server.publish_swap_payload(
        reg, store, "nope", 10, 11, gateway=FakeEditGateway()),
    lambda reg, store: server.discard_draft_payload(
        reg, store, "nope", 11, gateway=FakeEditGateway()),
], ids=["extract", "edit", "publish_swap", "discard"])
async def test_edit_tools_unknown_install(call, tmp_path):
    store = ProfileStore(tmp_path / "p.db")  # empty; unknown install resolves first
    payload = await call(_reg(), store)
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]

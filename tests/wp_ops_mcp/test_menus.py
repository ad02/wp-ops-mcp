"""Unit tests for MenuOps (ops/menus.py): menu resolution, target validation,
add-with-readback, update, remove.

Fakes at the gateway boundary (RestContentGateway's menu surface) so the tests
pin the two disciplines that keep a client's navigation from drifting:

  1. single-match resolution - a menu reference that matches 0 or 2+ menus is an
     error listing what IS available, never a guess at which nav to edit;
  2. targets are validated BEFORE the gateway - an empty/zero/blank target must
     not reach the wire at all (the gateway is checked for "no calls", not just
     "no writes").
"""
from wp_ops_mcp.ops.menus import MenuError, MenuOps

MENUS = [
    {"id": 3, "name": "Main Menu", "slug": "main-menu", "locations": ["primary"]},
    {"id": 7, "name": "Footer", "slug": "footer", "locations": []},
]


class FakeMenuGateway:
    """REST-shaped menu gateway that records every call that reached the wire.

    `calls` is the assertion surface for "never reached the gateway" claims;
    `adds`/`updates`/`deletes` record the write arguments (including the position
    -> menu_order rename).
    """

    def __init__(self, menus=None, items=None, error=None, readback_miss=False):
        self.menus = [dict(m) for m in (MENUS if menus is None else menus)]
        self.items = [dict(i) for i in (items or [])]
        self.error = error
        self.readback_miss = readback_miss      # add succeeds but the item never lands
        self.calls = []
        self.adds = []
        self.updates = []
        self.deletes = []
        self.new_id = 99

    def _boom(self):
        if self.error is not None:
            raise self.error

    async def list_menus(self):
        self.calls.append("list_menus")
        self._boom()
        return [dict(m) for m in self.menus]

    async def list_menu_items(self, menu_id):
        self.calls.append(("list_menu_items", menu_id))
        self._boom()
        return [dict(i) for i in self.items]

    async def add_menu_item(self, menu_id, title, page_id=None, url=None,
                            parent=0, menu_order=None):
        self.calls.append("add_menu_item")
        self._boom()
        self.adds.append({"menu_id": menu_id, "title": title, "page_id": page_id,
                          "url": url, "parent": parent, "menu_order": menu_order})
        item = {"id": self.new_id, "title": title,
                "url": url or f"/?page_id={page_id}",
                "menu_order": menu_order if menu_order is not None else len(self.items) + 1,
                "parent": parent,
                "object": "page" if page_id else "custom",
                "object_id": page_id,
                "type": "post_type" if page_id else "custom",
                "status": "publish"}
        if not self.readback_miss:
            self.items.append(dict(item))
        return item

    async def update_menu_item(self, item_id, fields):
        self.calls.append("update_menu_item")
        self._boom()
        self.updates.append({"item_id": item_id, "fields": dict(fields)})
        row = {"id": item_id, "title": "About", "url": "/about/", "menu_order": 1,
               "parent": 0, "object": "page", "object_id": 12,
               "type": "post_type", "status": "publish"}
        row.update(fields)                      # mapped row reflects the change
        return row

    async def delete_menu_item(self, item_id):
        self.calls.append("delete_menu_item")
        self._boom()
        self.deletes.append(item_id)


# --- resolution -------------------------------------------------------------

async def test_resolve_menu_by_id():
    assert (await MenuOps(FakeMenuGateway()).resolve_menu(7))["name"] == "Footer"


async def test_resolve_menu_by_exact_name():
    assert (await MenuOps(FakeMenuGateway()).resolve_menu("Main Menu"))["id"] == 3


async def test_resolve_menu_by_exact_slug():
    assert (await MenuOps(FakeMenuGateway()).resolve_menu("footer"))["id"] == 7


async def test_resolve_menu_no_match_lists_available():
    try:
        await MenuOps(FakeMenuGateway()).resolve_menu("Nav")
    except MenuError as e:
        msg = str(e)
        assert "available menus:" in msg
        assert "Main Menu (3)" in msg and "Footer (7)" in msg
    else:
        raise AssertionError("expected MenuError")


async def test_resolve_menu_is_exact_not_fuzzy():
    """'main menu' (wrong case) is a miss, not a lucky hit on 'Main Menu'."""
    try:
        await MenuOps(FakeMenuGateway()).resolve_menu("main menu")
    except MenuError as e:
        assert "available menus:" in str(e)
    else:
        raise AssertionError("expected MenuError")


async def test_resolve_menu_ambiguous_name_refuses():
    gw = FakeMenuGateway(menus=[{"id": 1, "name": "Nav", "slug": "nav-a", "locations": []},
                                {"id": 2, "name": "Nav", "slug": "nav-b", "locations": []}])
    try:
        await MenuOps(gw).resolve_menu("Nav")
    except MenuError as e:
        msg = str(e)
        assert "available menus:" in msg
        assert "Nav (1)" in msg and "Nav (2)" in msg
    else:
        raise AssertionError("expected MenuError")


async def test_resolve_menu_ambiguous_across_name_and_slug_refuses():
    """name-OR-slug matching can collide across menus; that is still ambiguous."""
    gw = FakeMenuGateway(menus=[{"id": 1, "name": "footer", "slug": "bottom", "locations": []},
                                {"id": 2, "name": "Footer Nav", "slug": "footer", "locations": []}])
    try:
        await MenuOps(gw).resolve_menu("footer")
    except MenuError as e:
        assert "available menus:" in str(e)
    else:
        raise AssertionError("expected MenuError")


async def test_resolve_menu_bool_is_not_an_id():
    """True == 1 in Python; a bool must not silently resolve to menu id 1."""
    gw = FakeMenuGateway(menus=[{"id": 1, "name": "Nav", "slug": "nav", "locations": []}])
    try:
        await MenuOps(gw).resolve_menu(True)
    except MenuError as e:
        assert "available menus:" in str(e)
    else:
        raise AssertionError("expected MenuError")


# --- list -------------------------------------------------------------------

async def test_list_returns_menus_without_fetching_items():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).list()
    # "action": "ok" is part of the contract - every successful read in this server
    # carries it, so a client switching on result["action"] never KeyErrors.
    assert out == {"action": "ok", "menus": MENUS}
    assert gw.calls == ["list_menus"]           # items are NOT fetched


async def test_list_gateway_failure_is_an_error_dict():
    out = await MenuOps(FakeMenuGateway(error=RuntimeError("boom"))).list()
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


# --- add: target validation happens BEFORE the gateway ----------------------

async def test_add_without_a_target_never_reaches_the_gateway():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).add("Main Menu", "About", dry_run=False)
    assert out["action"] == "error" and "exactly one of page_id or url" in out["error"]
    assert gw.calls == []


async def test_add_with_both_targets_never_reaches_the_gateway():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).add("Main Menu", "About", page_id=12,
                                url="https://a.com/x", dry_run=False)
    assert out["action"] == "error" and "exactly one of page_id or url" in out["error"]
    assert gw.calls == []


async def test_add_rejects_empty_page_ids_before_the_gateway():
    """0, negatives, a numeric string and True are all NOT a page id."""
    for bad in (0, -1, "12", True, 1.5):
        gw = FakeMenuGateway()
        out = await MenuOps(gw).add("Main Menu", "About", page_id=bad, dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "page_id must be a positive int" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_add_rejects_empty_urls_before_the_gateway():
    for bad in ("", "   ", 123, b"https://a.com"):
        gw = FakeMenuGateway()
        out = await MenuOps(gw).add("Main Menu", "About", url=bad, dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "url must be a non-empty string" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_add_validates_targets_in_dry_run_too():
    """A preview that would fail for real is a lie; dry-run refuses identically."""
    gw = FakeMenuGateway()
    out = await MenuOps(gw).add("Main Menu", "About", page_id=0, dry_run=True)
    assert out["action"] == "error" and "page_id" in out["error"]
    assert gw.calls == []


# --- add: dry run -----------------------------------------------------------

async def test_add_dry_run_previews_and_writes_nothing():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).add("Main Menu", "About", page_id=12, position=2)
    assert out == {"action": "preview", "menu": "Main Menu", "menu_id": 3,
                   "item": {"title": "About", "page_id": 12, "parent": 0, "position": 2}}
    assert gw.adds == [] and "add_menu_item" not in gw.calls


async def test_add_dry_run_still_resolves_the_menu():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).add("no-such-menu", "About", page_id=12)
    assert out["action"] == "error" and "available menus:" in out["error"]
    assert gw.adds == []


# --- add: real run + readback ----------------------------------------------

async def test_add_page_item_passes_position_through_as_menu_order():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).add(3, "About", page_id=12, parent=5, position=2, dry_run=False)
    assert out["action"] == "added" and out["verified"] is True
    assert out["menu"] == "Main Menu"
    assert out["item"]["id"] == 99 and out["item"]["title"] == "About"
    assert gw.adds == [{"menu_id": 3, "title": "About", "page_id": 12,
                        "url": None, "parent": 5, "menu_order": 2}]


async def test_add_without_position_leaves_menu_order_unset():
    gw = FakeMenuGateway()
    await MenuOps(gw).add("Footer", "About", page_id=12, dry_run=False)
    assert gw.adds[0]["menu_id"] == 7 and gw.adds[0]["menu_order"] is None


async def test_add_custom_url_item():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).add("Footer", "Blog", url="https://a.com/blog", dry_run=False)
    assert out["action"] == "added"
    assert gw.adds[0]["url"] == "https://a.com/blog" and gw.adds[0]["page_id"] is None


async def test_add_verifies_by_reading_the_menu_back():
    gw = FakeMenuGateway()
    await MenuOps(gw).add(3, "About", page_id=12, dry_run=False)
    assert ("list_menu_items", 3) in gw.calls        # readback targeted the same menu


async def test_add_readback_miss_is_an_error_naming_the_item():
    gw = FakeMenuGateway(readback_miss=True)
    out = await MenuOps(gw).add(3, "About", page_id=12, dry_run=False)
    assert out["action"] == "error"
    assert "99" in out["error"] and "Main Menu" in out["error"]


async def test_add_gateway_failure_is_an_error_dict():
    gw = FakeMenuGateway(error=RuntimeError("boom"))
    out = await MenuOps(gw).add(3, "About", page_id=12, dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


async def test_add_never_raises_on_pathological_menu_refs():
    gw = FakeMenuGateway()
    for bad in (None, 3.5, ["Main Menu"], {"id": 3}):
        out = await MenuOps(gw).add(bad, "About", page_id=12, dry_run=False)
        assert isinstance(out, dict) and out["action"] == "error", (bad, out)
    assert gw.adds == []


# --- update -----------------------------------------------------------------

async def test_update_sends_the_given_fields_with_position_as_menu_order():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).update(91, title="Our Team", position=2, parent=5,
                                   url="https://a.com/team", dry_run=False)
    assert out["action"] == "updated"
    assert out["item"]["id"] == 91 and out["item"]["title"] == "Our Team"
    assert gw.updates == [{"item_id": 91,
                           "fields": {"title": "Our Team", "menu_order": 2,
                                      "parent": 5, "url": "https://a.com/team"}}]


async def test_update_sends_only_the_fields_that_were_given():
    """A partial update must not resend (and so overwrite) the untouched fields."""
    gw = FakeMenuGateway()
    await MenuOps(gw).update(91, title="Our Team", dry_run=False)
    assert gw.updates[0]["fields"] == {"title": "Our Team"}


async def test_update_treats_zero_position_and_parent_as_real_values():
    """position=0 (first slot) and parent=0 (un-nest to top level) are changes,
    not 'unset' - only None means 'leave this alone'."""
    gw = FakeMenuGateway()
    await MenuOps(gw).update(91, position=0, parent=0, dry_run=False)
    assert gw.updates[0]["fields"] == {"menu_order": 0, "parent": 0}


async def test_update_without_any_fields_never_reaches_the_gateway():
    """An empty POST would report success for a change that never happened."""
    gw = FakeMenuGateway()
    out = await MenuOps(gw).update(91, dry_run=False)
    assert out == {"action": "error",
                   "error": "MenuError: no fields provided (title/position/parent/url)"}
    assert gw.calls == []


async def test_update_rejects_empty_item_ids_before_the_gateway():
    for bad in (0, -1, None, "91", True):
        gw = FakeMenuGateway()
        out = await MenuOps(gw).update(bad, title="Our Team", dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "item_id must be a positive int" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_update_rejects_empty_urls_before_the_gateway():
    """Same discipline as add: retargeting an item at nothing is refused here."""
    for bad in ("", "   ", 123, b"https://a.com"):
        gw = FakeMenuGateway()
        out = await MenuOps(gw).update(91, url=bad, dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "url must be a non-empty string" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_update_dry_run_previews_the_exact_fields_and_writes_nothing():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).update(91, title="Our Team", position=2)
    assert out == {"action": "preview", "item_id": 91,
                   "fields": {"title": "Our Team", "menu_order": 2}}
    assert gw.calls == []


async def test_update_validates_in_dry_run_too():
    """A preview that would fail for real is a lie; dry-run refuses identically."""
    gw = FakeMenuGateway()
    out = await MenuOps(gw).update(0, title="Our Team", dry_run=True)
    assert out["action"] == "error" and "item_id must be a positive int" in out["error"]
    assert gw.calls == []


async def test_update_gateway_failure_is_an_error_dict():
    gw = FakeMenuGateway(error=RuntimeError("boom"))
    out = await MenuOps(gw).update(91, title="Our Team", dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


# --- remove -----------------------------------------------------------------

async def test_remove_dry_run_previews_and_touches_nothing():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).remove(99)
    assert out == {"action": "preview", "item_id": 99}
    assert gw.calls == []                      # no extra fetch, no delete


async def test_remove_deletes_on_real_run():
    gw = FakeMenuGateway()
    out = await MenuOps(gw).remove(99, dry_run=False)
    assert out == {"action": "removed", "item_id": 99}
    assert gw.deletes == [99]


async def test_remove_rejects_empty_item_ids_before_the_gateway():
    for bad in (0, -1, None, "99", True):
        gw = FakeMenuGateway()
        out = await MenuOps(gw).remove(bad, dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "item_id must be a positive int" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_remove_gateway_failure_is_an_error_dict():
    gw = FakeMenuGateway(error=RuntimeError("boom"))
    out = await MenuOps(gw).remove(99, dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]

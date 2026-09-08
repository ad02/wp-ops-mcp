import asyncio
import pytest
from wp_ops_mcp.ops.rest_gateway import RestContentGateway
from wp_ops_mcp.transport.rest import WPRestClient, RestError


class ScriptedClient(WPRestClient):
    """WPRestClient whose request() is served from a scripted {(method, path): (status, data)} map."""

    def __init__(self, script):
        super().__init__("https://site.test", app_user="u", app_password="p")
        self.script, self.calls, self.uploads = script, [], []

    async def request(self, method, path, *, params=None, json_body=None, http=None):
        self.calls.append((method, path, params, json_body))
        key = (method, path)
        if key not in self.script:
            raise AssertionError(f"unexpected call {key}")
        return self.script[key]

    async def upload(self, path, filename, content, mime, http=None):
        # Binary uploads are scripted under a synthetic "UPLOAD" method so one script
        # map can hold both the upload and the follow-up JSON calls.
        self.uploads.append((path, filename, content, mime))
        key = ("UPLOAD", path)
        if key not in self.script:
            raise AssertionError(f"unexpected upload {key}")
        return self.script[key]


INFO_SITE = ("GET", "/wp-json/wpops/v1/info")
INFO_P5 = ("GET", "/wp-json/wpops/v1/info?post_id=5")


def _gw(script):
    return RestContentGateway(ScriptedClient(script))


def test_ensure_plugin_ok_and_version_gate():
    gw = _gw({INFO_SITE: (200, {"wp": "6.9", "theme": "Divi", "divi": "4.27",
                                "plugin_version": "1.0.0"})})
    info = asyncio.run(gw.ensure_plugin())
    assert info["plugin_version"] == "1.0.0"
    gw2 = _gw({INFO_SITE: (200, {"plugin_version": "0.9.0"})})
    with pytest.raises(RestError):
        asyncio.run(gw2.ensure_plugin())
    gw3 = _gw({INFO_SITE: (404, {"code": "rest_no_route"})})
    with pytest.raises(RestError):
        asyncio.run(gw3.ensure_plugin())


def test_get_post_info_and_type_cache():
    gw = _gw({INFO_P5: (200, {"id": 5, "type": "page", "slug": "home",
                              "status": "publish", "title": "Home"})})
    info = asyncio.run(gw.get_post_info(5))
    assert info["name"] == "home" and info["status"] == "publish"
    assert gw._type_cache[5] == "page"


def test_list_posts_maps_fields():
    gw = _gw({("GET", "/wp-json/wp/v2/pages"): (200, [
        {"id": 1, "title": {"raw": "Home", "rendered": "Home"}, "slug": "home",
         "status": "publish", "type": "page"}])})
    rows = asyncio.run(gw.list_posts("page"))
    assert rows == [{"id": 1, "title": "Home", "slug": "home",
                     "status": "publish", "type": "page"}]
    method, path, params, _ = gw.c.calls[0]
    assert params == {"status": "any", "per_page": 100, "context": "edit",
                      "_fields": "id,title,slug,status,type", "page": 1}
    assert gw._type_cache[1] == "page"  # list warms the type cache (saves an /info round-trip)


def test_get_post_content_returns_raw_exact():
    gw = _gw({INFO_P5: (200, {"id": 5, "type": "page", "slug": "s", "status": "draft",
                              "title": "T"}),
              ("GET", "/wp-json/wp/v2/pages/5"): (200, {
                  "content": {"raw": '[et_pb_text]a "b"\n[/et_pb_text]'}})})
    c = asyncio.run(gw.get_post_content(5))
    assert c == '[et_pb_text]a "b"\n[/et_pb_text]'
    # The content read must go through context=edit so WP returns content.raw
    # (rendered context omits .raw and the read would fail).
    content_call = next(call for call in gw.c.calls if call[1] == "/wp-json/wp/v2/pages/5")
    assert content_call[2] == {"context": "edit"}


def test_missing_post_raises_resterror():
    gw = _gw({INFO_P5: (404, {"code": "wpops_not_found", "message": "post not found"})})
    with pytest.raises(RestError, match="post not found"):
        asyncio.run(gw.get_post_info(5))


DUP = ("POST", "/wp-json/wpops/v1/duplicate")
PURGE = ("POST", "/wp-json/wpops/v1/purge-cache")
META = ("POST", "/wp-json/wpops/v1/meta")
_PLUGIN_OK = {INFO_SITE: (200, {"plugin_version": "1.0.0"})}


def test_ensure_plugin_accepts_short_version():
    # "1.0" must satisfy MIN "1.0.0": versions zero-pad to equal length before comparing.
    gw = _gw({INFO_SITE: (200, {"plugin_version": "1.0"})})
    info = asyncio.run(gw.ensure_plugin())
    assert info["plugin_version"] == "1.0"


def test_create_post_posts_and_applies_meta_via_plugin():
    gw = _gw({**_PLUGIN_OK,
              ("POST", "/wp-json/wp/v2/pages"): (201, {"id": 42}),
              META: (200, {"applied": ["_et_pb_use_builder"]})})
    pid = asyncio.run(gw.create_post("page", "T", "t", "draft",
                                     "[et_pb_section][/et_pb_section]",
                                     {"_et_pb_use_builder": "on"}))
    assert pid == 42
    meta_call = [c for c in gw.c.calls if c[1] == "/wp-json/wpops/v1/meta"][0]
    assert meta_call[3] == {"post_id": 42, "meta": {"_et_pb_use_builder": "on"}}


def test_create_without_meta_skips_plugin():
    gw = _gw({("POST", "/wp-json/wp/v2/posts"): (201, {"id": 7})})
    assert asyncio.run(gw.create_post("post", "T", "t", "draft", "<p>x</p>", {})) == 7
    assert all(c[1] != "/wp-json/wpops/v1/info" for c in gw.c.calls)


def test_update_delete_duplicate_purge():
    gw = _gw({**_PLUGIN_OK,
              INFO_P5: (200, {"id": 5, "type": "page", "slug": "s", "status": "draft", "title": "T"}),
              ("POST", "/wp-json/wp/v2/pages/5"): (200, {"id": 5}),
              ("DELETE", "/wp-json/wp/v2/pages/5"): (200, {"deleted": True}),
              DUP: (200, {"draft_id": 99}),
              PURGE: (200, {"purged": True})})
    assert asyncio.run(gw.update_post_content(5, "new")) == 5
    asyncio.run(gw.delete_post(5))
    assert asyncio.run(gw.duplicate_post(5)) == 99
    asyncio.run(gw.purge_et_cache(5))
    dele = [c for c in gw.c.calls if c[0] == "DELETE"][0]
    assert dele[2] == {"force": "true"}


def test_write_error_statuses_raise():
    gw = _gw({**_PLUGIN_OK, DUP: (403, {"code": "rest_forbidden", "message": "no cap"})})
    with pytest.raises(RestError, match="no cap"):
        asyncio.run(gw.duplicate_post(5))


def test_create_meta_failure_mentions_post_id():
    # Core create succeeds (post 42 now exists) but the /meta apply 500s. The raised
    # error must name the orphaned post id so it is findable, not just "meta failed".
    gw = _gw({**_PLUGIN_OK,
              ("POST", "/wp-json/wp/v2/pages"): (201, {"id": 42}),
              META: (500, {"message": "db write failed"})})
    with pytest.raises(RestError, match="42"):
        asyncio.run(gw.create_post("page", "T", "t", "draft", "<p>x</p>",
                                   {"_et_pb_use_builder": "on"}))


def test_create_against_site_without_plugin_mentions_post_id():
    # Core create succeeds (post 43 exists) but ensure_plugin() fails because the
    # site has no wp-ops-connect. The orphan id must still surface.
    gw = _gw({INFO_SITE: (404, {"code": "rest_no_route"}),
              ("POST", "/wp-json/wp/v2/pages"): (201, {"id": 43})})
    with pytest.raises(RestError, match="43"):
        asyncio.run(gw.create_post("page", "T", "t", "draft", "<p>x</p>",
                                   {"_et_pb_use_builder": "on"}))


def test_update_and_delete_and_purge_error_statuses_raise():
    P5 = {"id": 5, "type": "page", "slug": "s", "status": "draft", "title": "T"}
    up = _gw({INFO_P5: (200, P5),
              ("POST", "/wp-json/wp/v2/pages/5"): (403, {"message": "forbidden"})})
    with pytest.raises(RestError, match="forbidden"):
        asyncio.run(up.update_post_content(5, "new"))
    de = _gw({INFO_P5: (200, P5),
              ("DELETE", "/wp-json/wp/v2/pages/5"): (500, {"message": "boom"})})
    with pytest.raises(RestError, match="boom"):
        asyncio.run(de.delete_post(5))
    pu = _gw({**_PLUGIN_OK, PURGE: (500, {"message": "cache down"})})
    with pytest.raises(RestError, match="cache down"):
        asyncio.run(pu.purge_et_cache(5))


def test_duplicate_warms_draft_type_cache():
    # Source page id 5 is already type-cached (e.g. from a prior list/info). Duplicating
    # it must copy the source's type onto the new draft with NO extra get_post_info call.
    gw = _gw({**_PLUGIN_OK, DUP: (200, {"draft_id": 99})})
    gw._type_cache[5] = "page"
    assert asyncio.run(gw.duplicate_post(5)) == 99
    assert gw._type_cache[99] == "page"
    assert not any("info?post_id" in c[1] for c in gw.c.calls)


def test_get_and_set_post_meta_and_seo_gate():
    gw = _gw({INFO_SITE: (200, {"plugin_version": "1.1.0", "seo_plugin": "seopress"}),
              ("GET", "/wp-json/wpops/v1/meta?post_id=5"): (200, {"meta": {"_seopress_titles_title": "T"}}),
              META: (200, {"applied": ["_seopress_titles_title"]})})
    info = asyncio.run(gw.ensure_seo_capable())
    assert info["seo_plugin"] == "seopress"
    assert asyncio.run(gw.get_post_meta(5)) == {"_seopress_titles_title": "T"}
    assert asyncio.run(gw.set_post_meta(5, {"_seopress_titles_title": "New"})) == ["_seopress_titles_title"]


def test_seo_gate_refuses_old_plugin():
    gw = _gw({INFO_SITE: (200, {"plugin_version": "1.0.0"})})
    with pytest.raises(RestError, match="1.1.0"):
        asyncio.run(gw.ensure_seo_capable())


UPLOAD = ("UPLOAD", "/wp-json/wp/v2/media")


def test_upload_media_with_alt_and_orphan_naming():
    gw = _gw({UPLOAD: (201, {"id": 77, "source_url": "https://site.test/up/a.png"}),
              ("POST", "/wp-json/wp/v2/media/77"): (200, {"id": 77})})
    out = asyncio.run(gw.upload_media("a.png", b"\x89PNG", "image/png", alt="A cat"))
    assert out == {"id": 77, "url": "https://site.test/up/a.png", "alt": "A cat"}
    assert gw.c.uploads == [("/wp-json/wp/v2/media", "a.png", b"\x89PNG", "image/png")]
    alt_call = [c for c in gw.c.calls if c[1] == "/wp-json/wp/v2/media/77"][0]
    assert alt_call[3] == {"alt_text": "A cat"}

    # Attachment 77 now exists on the site but the alt update 500s. The error must name
    # the created attachment so the orphan is findable (mirrors create_post's meta path).
    gw2 = _gw({UPLOAD: (201, {"id": 77, "source_url": "https://site.test/up/a.png"}),
               ("POST", "/wp-json/wp/v2/media/77"): (500, {"message": "db write failed"})})
    with pytest.raises(RestError, match="77"):
        asyncio.run(gw2.upload_media("a.png", b"x", "image/png", alt="A cat"))


def test_upload_media_without_alt_and_failure_statuses():
    gw = _gw({UPLOAD: (201, {"id": 5, "source_url": "https://site.test/up/b.png"})})
    assert asyncio.run(gw.upload_media("b.png", b"x", "image/png")) == {
        "id": 5, "url": "https://site.test/up/b.png", "alt": ""}
    assert gw.c.calls == []                     # no alt -> no follow-up request at all
    gw2 = _gw({UPLOAD: (413, {"message": "file too large"})})
    with pytest.raises(RestError, match="file too large"):
        asyncio.run(gw2.upload_media("b.png", b"x", "image/png"))
    # 201 without source_url is unusable downstream (no URL to place in content).
    gw3 = _gw({UPLOAD: (201, {"id": 5})})
    with pytest.raises(RestError):
        asyncio.run(gw3.upload_media("b.png", b"x", "image/png"))


def test_delete_media():
    gw = _gw({("DELETE", "/wp-json/wp/v2/media/77"): (200, {"deleted": True})})
    asyncio.run(gw.delete_media(77))
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == ("DELETE", "/wp-json/wp/v2/media/77")
    assert params == {"force": "true"}          # attachments need force - no trash step
    gw2 = _gw({("DELETE", "/wp-json/wp/v2/media/77"): (500, {"message": "boom"})})
    with pytest.raises(RestError, match="boom"):
        asyncio.run(gw2.delete_media(77))


# -- menus --------------------------------------------------------------------
MENUS = ("GET", "/wp-json/wp/v2/menus")
ITEMS = ("GET", "/wp-json/wp/v2/menu-items")
ADD_ITEM = ("POST", "/wp-json/wp/v2/menu-items")
UPD_ITEM = ("POST", "/wp-json/wp/v2/menu-items/91")
DEL_ITEM = ("DELETE", "/wp-json/wp/v2/menu-items/91")

_ITEM = {"id": 91, "title": {"raw": "About Us", "rendered": "About &amp; Us"},
         "url": "https://site.test/about/", "menu_order": 2, "parent": 0,
         "object": "page", "object_id": 12, "type": "post_type",
         "status": "publish", "description": "dropped by the mapping"}

_MAPPED_ITEM = {"id": 91, "title": "About Us", "url": "https://site.test/about/",
                "menu_order": 2, "parent": 0, "object": "page", "object_id": 12,
                "type": "post_type", "status": "publish"}


def test_list_menus_maps_fields():
    gw = _gw({MENUS: (200, [
        {"id": 3, "name": "Main Menu", "slug": "main-menu", "locations": ["primary"],
         "description": "", "meta": []},
        {"id": 4, "name": "Footer", "slug": "footer"}])})
    rows = asyncio.run(gw.list_menus())
    # Menu name comes plain (no raw/rendered dance); locations always a list.
    assert rows == [{"id": 3, "name": "Main Menu", "slug": "main-menu", "locations": ["primary"]},
                    {"id": 4, "name": "Footer", "slug": "footer", "locations": []}]
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == MENUS
    assert params == {"context": "edit", "per_page": 100}


def test_list_menus_error_status_raises():
    gw = _gw({MENUS: (403, {"code": "rest_forbidden", "message": "no cap"})})
    with pytest.raises(RestError, match="no cap"):
        asyncio.run(gw.list_menus())


def test_list_menu_items_maps_fields_title_raw_first():
    gw = _gw({ITEMS: (200, [_ITEM])})
    assert asyncio.run(gw.list_menu_items(3)) == [_MAPPED_ITEM]
    _, _, params, _ = gw.c.calls[0]
    assert params == {"menus": 3, "per_page": 100, "context": "edit"}


def test_list_menu_items_falls_back_to_rendered_title():
    gw = _gw({ITEMS: (200, [{**_ITEM, "title": {"raw": "", "rendered": "Rendered"}}])})
    assert asyncio.run(gw.list_menu_items(3))[0]["title"] == "Rendered"


def test_list_menu_items_error_status_raises():
    gw = _gw({ITEMS: (500, {"message": "boom"})})
    with pytest.raises(RestError, match="boom"):
        asyncio.run(gw.list_menu_items(3))


def test_add_menu_item_page_posts_post_type_body_and_returns_mapped_row():
    gw = _gw({ADD_ITEM: (201, _ITEM)})
    assert asyncio.run(gw.add_menu_item(3, "About Us", page_id=12, menu_order=5)) == _MAPPED_ITEM
    method, path, _, body = gw.c.calls[0]
    assert (method, path) == ADD_ITEM
    assert body == {"menus": 3, "title": "About Us", "status": "publish", "parent": 0,
                    "type": "post_type", "object": "page", "object_id": 12,
                    "menu_order": 5}


def test_add_menu_item_custom_url_body_and_omits_menu_order_when_unset():
    gw = _gw({ADD_ITEM: (201, {"id": 93, "title": {"raw": "Call"}, "url": "tel:+15551234",
                               "menu_order": 1, "parent": 7, "object": "custom",
                               "object_id": 93, "type": "custom", "status": "publish"})})
    row = asyncio.run(gw.add_menu_item(3, "Call", url="tel:+15551234", parent=7))
    assert row["id"] == 93 and row["type"] == "custom" and row["url"] == "tel:+15551234"
    _, _, _, body = gw.c.calls[0]
    assert body == {"menus": 3, "title": "Call", "status": "publish", "parent": 7,
                    "type": "custom", "url": "tel:+15551234"}
    # No menu_order in the body -> WP appends at the end of the menu.
    assert "menu_order" not in body


def test_add_menu_item_requires_exactly_one_of_page_id_url():
    gw = _gw({})
    with pytest.raises(ValueError, match="page_id"):
        asyncio.run(gw.add_menu_item(3, "T"))
    with pytest.raises(ValueError, match="page_id"):
        asyncio.run(gw.add_menu_item(3, "T", page_id=12, url="https://site.test/x/"))
    assert gw.c.calls == []                     # validated before any request goes out


def test_add_menu_item_error_statuses_raise():
    gw = _gw({ADD_ITEM: (400, {"message": "invalid menu"})})
    with pytest.raises(RestError, match="invalid menu"):
        asyncio.run(gw.add_menu_item(3, "T", page_id=12))
    # 201 without an id is unusable downstream (ops verifies the add by readback on id).
    gw2 = _gw({ADD_ITEM: (201, {"title": {"raw": "T"}})})
    with pytest.raises(RestError):
        asyncio.run(gw2.add_menu_item(3, "T", page_id=12))


def test_update_menu_item_posts_allowed_fields_and_returns_mapped_row():
    gw = _gw({UPD_ITEM: (200, _ITEM)})
    fields = {"title": "About Us", "menu_order": 4, "parent": 2,
              "url": "https://site.test/about-us/"}
    assert asyncio.run(gw.update_menu_item(91, fields)) == _MAPPED_ITEM
    method, path, _, body = gw.c.calls[0]
    assert (method, path) == UPD_ITEM
    assert body == fields


def test_update_menu_item_rejects_unknown_fields():
    gw = _gw({})
    with pytest.raises(ValueError, match="status"):
        asyncio.run(gw.update_menu_item(91, {"title": "New", "status": "draft"}))
    assert gw.c.calls == []                     # nothing is sent when a key is not allow-listed


def test_update_menu_item_error_status_raises():
    gw = _gw({UPD_ITEM: (404, {"message": "item not found"})})
    with pytest.raises(RestError, match="item not found"):
        asyncio.run(gw.update_menu_item(91, {"title": "New"}))


def test_delete_menu_item_forces_and_errors_raise():
    gw = _gw({DEL_ITEM: (200, {"deleted": True})})
    assert asyncio.run(gw.delete_menu_item(91)) is None
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == DEL_ITEM
    assert params == {"force": "true"}          # menu items have no meaningful trash state
    gw2 = _gw({DEL_ITEM: (500, {"message": "boom"})})
    with pytest.raises(RestError, match="boom"):
        asyncio.run(gw2.delete_menu_item(91))


# -- slug ---------------------------------------------------------------------
_P5 = {"id": 5, "type": "page", "slug": "old-slug", "status": "publish", "title": "T"}
SLUG_P5 = ("POST", "/wp-json/wp/v2/pages/5")


def test_update_post_slug_posts_slug_only_and_maps_response():
    gw = _gw({INFO_P5: (200, _P5),
              SLUG_P5: (200, {"id": 5, "slug": "new-slug",
                              "link": "https://site.test/new-slug/"})})
    assert asyncio.run(gw.update_post_slug(5, "new-slug")) == {
        "id": 5, "slug": "new-slug", "link": "https://site.test/new-slug/"}
    method, path, _, body = [c for c in gw.c.calls if c[0] == "POST"][0]
    assert (method, path) == SLUG_P5           # route resolved from the post's type
    assert body == {"slug": "new-slug"}        # slug only - never touch title/content


def test_update_post_slug_returns_wp_uniquified_slug():
    # WP appends -2 when the requested slug collides with an existing one. Return WP's
    # ACTUAL slug so ops can flag the rename as uniquified instead of claiming an exact
    # rename that never happened.
    gw = _gw({("POST", "/wp-json/wp/v2/posts/5"): (200, {
        "id": 5, "slug": "about-2", "link": "https://site.test/about-2/"})})
    gw._type_cache[5] = "post"                 # warmed cache -> no /info round-trip
    out = asyncio.run(gw.update_post_slug(5, "about"))
    assert out["slug"] == "about-2" and out["link"] == "https://site.test/about-2/"
    assert not any("info" in c[1] for c in gw.c.calls)


def test_update_post_slug_defaults_missing_link():
    gw = _gw({SLUG_P5: (200, {"id": 5, "slug": "s"})})
    gw._type_cache[5] = "page"
    assert asyncio.run(gw.update_post_slug(5, "s")) == {"id": 5, "slug": "s", "link": ""}


def test_update_post_slug_uncached_type_info_failure_raises_before_the_write():
    # Cold cache: the route comes from /info, so a post that cannot be resolved (404 -
    # wrong id, or a type this transport does not support) must fail there. The POST is
    # not in the script at all: if the gateway attempted one anyway, ScriptedClient
    # would raise AssertionError instead of RestError and this test would fail.
    gw = _gw({INFO_P5: (404, {"code": "rest_post_invalid_id", "message": "Invalid post ID."})})
    with pytest.raises(RestError, match="Invalid post ID"):
        asyncio.run(gw.update_post_slug(5, "new-slug"))
    assert [c for c in gw.c.calls if c[0] == "POST"] == []


def test_update_post_slug_error_statuses_raise():
    gw = _gw({INFO_P5: (200, _P5),
              SLUG_P5: (403, {"code": "rest_forbidden", "message": "no cap"})})
    with pytest.raises(RestError, match="no cap"):
        asyncio.run(gw.update_post_slug(5, "new-slug"))
    # A 200 with no slug in it is unusable downstream: ops compares the returned slug
    # against the requested one to detect uniquification, so silence is not success.
    gw2 = _gw({INFO_P5: (200, _P5), SLUG_P5: (200, {"id": 5})})
    with pytest.raises(RestError):
        asyncio.run(gw2.update_post_slug(5, "new-slug"))
    gw3 = _gw({INFO_P5: (200, _P5), SLUG_P5: (200, ["not a post object"])})
    with pytest.raises(RestError):
        asyncio.run(gw3.update_post_slug(5, "new-slug"))


# -- settings -----------------------------------------------------------------
SETTINGS_GET = ("GET", "/wp-json/wp/v2/settings")
SETTINGS_POST = ("POST", "/wp-json/wp/v2/settings")


def test_get_settings_passthrough():
    gw = _gw({SETTINGS_GET: (200, {"title": "Site", "description": "Tag",
                                   "url": "https://site.test", "posts_per_page": 10})})
    # Full passthrough - the allowlist filter lives in the ops layer, not the gateway.
    assert asyncio.run(gw.get_settings()) == {"title": "Site", "description": "Tag",
                                              "url": "https://site.test", "posts_per_page": 10}
    method, path, params, body = gw.c.calls[0]
    assert (method, path) == SETTINGS_GET and params is None and body is None


def test_get_settings_forbidden_surfaces_wp_detail():
    # /wp/v2/settings is admin-only: a non-admin app password 403s and the operator needs
    # WP's own reason (wrong role) rather than a generic failure.
    gw = _gw({SETTINGS_GET: (403, {"code": "rest_forbidden",
                                   "message": "Sorry, you are not allowed to edit settings."})})
    with pytest.raises(RestError, match="not allowed to edit settings"):
        asyncio.run(gw.get_settings())
    gw2 = _gw({SETTINGS_GET: (200, ["not a settings object"])})
    with pytest.raises(RestError):
        asyncio.run(gw2.get_settings())


def test_update_settings_posts_fields_and_returns_response():
    gw = _gw({SETTINGS_POST: (200, {"title": "New Title", "description": "Tag"})})
    assert asyncio.run(gw.update_settings({"title": "New Title"})) == {
        "title": "New Title", "description": "Tag"}
    method, path, _, body = gw.c.calls[0]
    assert (method, path) == SETTINGS_POST
    assert body == {"title": "New Title"}


def test_update_settings_rejects_empty_fields():
    gw = _gw({})
    with pytest.raises(ValueError, match="no settings"):
        asyncio.run(gw.update_settings({}))
    assert gw.c.calls == []                    # validated before any request goes out


def test_update_settings_error_statuses_raise():
    gw = _gw({SETTINGS_POST: (403, {"code": "rest_forbidden", "message": "no cap"})})
    with pytest.raises(RestError, match="no cap"):
        asyncio.run(gw.update_settings({"title": "x"}))
    gw2 = _gw({SETTINGS_POST: (200, "ok")})
    with pytest.raises(RestError):
        asyncio.run(gw2.update_settings({"title": "x"}))


# -- list pagination ----------------------------------------------------------
class PagedClient(WPRestClient):
    """Serves list_posts pages from a {page_number: response} script.

    A response is a list of rows (returned as HTTP 200), a (status, data) tuple, or
    an Exception instance to raise (a simulated network RestError mid-loop). Records
    the page numbers requested so tests can assert where the loop stopped.
    """

    def __init__(self, pages):
        super().__init__("https://site.test", app_user="u", app_password="p")
        self.pages = pages
        self.requested_pages = []

    async def request(self, method, path, *, params=None, json_body=None, http=None):
        page = (params or {}).get("page", 1)
        self.requested_pages.append(page)
        resp = self.pages[page]
        if isinstance(resp, Exception):
            raise resp
        return resp if isinstance(resp, tuple) else (200, resp)


def _page_rows(n, start=1):
    return [{"id": i, "title": {"raw": f"P{i}"}, "slug": f"p{i}",
             "status": "publish", "type": "page"} for i in range(start, start + n)]


def test_list_posts_paginates_until_short_page_not_truncated():
    # 100 (full) + 37 (short) -> 137 rows, stops after the short page 2, not truncated.
    gw = RestContentGateway(PagedClient({1: _page_rows(100), 2: _page_rows(37, start=101)}))
    rows = asyncio.run(gw.list_posts("page"))
    assert len(rows) == 137
    assert rows[0]["id"] == 1 and rows[-1]["id"] == 137   # both pages accumulated, in order
    assert gw.last_list_truncated is False
    assert gw.c.requested_pages == [1, 2]                 # never asked for a third page


def test_list_posts_truncates_at_ten_page_cap():
    # 10 full pages -> capped at the 10-page limit, truncated True, page 11 never asked.
    gw = RestContentGateway(PagedClient(
        {p: _page_rows(100, start=(p - 1) * 100 + 1) for p in range(1, 11)}))
    rows = asyncio.run(gw.list_posts("page"))
    assert len(rows) == 1000
    assert gw.last_list_truncated is True
    assert gw.c.requested_pages == list(range(1, 11))


def test_list_posts_page_two_error_fails_open_with_truncated():
    # Page 1 ok (100), page 2 returns non-200: keep page 1's rows, flag truncated, no raise.
    gw = RestContentGateway(PagedClient({1: _page_rows(100), 2: (500, {"message": "boom"})}))
    rows = asyncio.run(gw.list_posts("page"))
    assert len(rows) == 100
    assert gw.last_list_truncated is True


def test_list_posts_page_two_network_error_fails_open_with_truncated():
    # A RestError raised past page 1 (network blip) is also fail-open, not re-raised.
    gw = RestContentGateway(PagedClient({1: _page_rows(100), 2: RestError("network down")}))
    rows = asyncio.run(gw.list_posts("page"))
    assert len(rows) == 100
    assert gw.last_list_truncated is True


def test_list_posts_page_one_error_still_raises():
    # A first-page failure is a real, empty result - raise exactly as before (no fail-open).
    gw = RestContentGateway(PagedClient({1: (500, {"message": "boom"})}))
    with pytest.raises(RestError, match="boom"):
        asyncio.run(gw.list_posts("page"))


def test_list_posts_truncated_resets_each_call():
    # last_list_truncated reflects only the latest call: a truncated call followed by a
    # clean one must read False (init False in __init__ + reset at the top of each call).
    client = PagedClient({p: _page_rows(100, start=(p - 1) * 100 + 1) for p in range(1, 11)})
    gw = RestContentGateway(client)
    asyncio.run(gw.list_posts("page"))
    assert gw.last_list_truncated is True
    client.pages = {1: _page_rows(5)}          # re-script the same client with one short page
    client.requested_pages = []
    asyncio.run(gw.list_posts("page"))
    assert gw.last_list_truncated is False


# -- acf ----------------------------------------------------------------------
ACF_GET = ("GET", "/wp-json/wpops/v1/acf?post_id=5")
ACF_POST = ("POST", "/wp-json/wpops/v1/acf")


def test_ensure_acf_capable_presence_gate():
    # info.get("acf") is ACF_VERSION (a string) when ACF is loaded -> capable.
    gw = _gw({INFO_SITE: (200, {"plugin_version": "1.2.0", "acf": "6.2.1"})})
    info = asyncio.run(gw.ensure_acf_capable())
    assert info["acf"] == "6.2.1"
    # function_exists('acf') with no ACF_VERSION const reports True - still capable.
    gw2 = _gw({INFO_SITE: (200, {"plugin_version": "1.2.0", "acf": True})})
    assert asyncio.run(gw2.ensure_acf_capable())["acf"] is True
    # acf null (ACF not installed) -> presence gate refuses; NOT a version floor.
    gw3 = _gw({INFO_SITE: (200, {"plugin_version": "1.2.0", "acf": None})})
    with pytest.raises(RestError, match="ACF not active"):
        asyncio.run(gw3.ensure_acf_capable())


def test_get_acf_fields_returns_dict_including_empty():
    gw = _gw({ACF_GET: (200, {"acf": {"headline": "Hi", "count": 3, "gallery": [1, 2]}})})
    assert asyncio.run(gw.get_acf_fields(5)) == {"headline": "Hi", "count": 3, "gallery": [1, 2]}
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == ACF_GET          # post_id rides the query string
    # An empty {} is a valid result (the post has no ACF values), not an error.
    gw2 = _gw({ACF_GET: (200, {"acf": {}})})
    assert asyncio.run(gw2.get_acf_fields(5)) == {}


def test_get_acf_fields_501_inactive_raises_with_detail():
    # Plugin's function_exists('get_fields') guard 501s when ACF is uninstalled; the
    # plugin's message must surface (via _err_detail) so the cause is obvious.
    gw = _gw({ACF_GET: (501, {"code": "wpops_acf_inactive", "message": "ACF not active"})})
    with pytest.raises(RestError, match="ACF not active"):
        asyncio.run(gw.get_acf_fields(5))


def test_get_acf_fields_missing_key_200_raises():
    # A 200 with no "acf" key is unusable downstream - refuse, don't return None.
    gw = _gw({ACF_GET: (200, {"other": {}})})
    with pytest.raises(RestError):
        asyncio.run(gw.get_acf_fields(5))
    gw2 = _gw({ACF_GET: (200, ["not an object"])})
    with pytest.raises(RestError):
        asyncio.run(gw2.get_acf_fields(5))


def test_set_acf_fields_posts_body_and_returns_applied_skipped_acf():
    gw = _gw({ACF_POST: (200, {"applied": ["headline"], "skipped": ["_hidden"],
                               "acf": {"headline": "New"}})})
    out = asyncio.run(gw.set_acf_fields(5, {"headline": "New", "_hidden": "x"}))
    assert out == {"applied": ["headline"], "skipped": ["_hidden"],
                   "acf": {"headline": "New"}}
    method, path, _, body = gw.c.calls[0]
    assert (method, path) == ACF_POST
    assert body == {"post_id": 5, "fields": {"headline": "New", "_hidden": "x"}}


def test_set_acf_fields_defaults_skipped_when_absent():
    # The plugin may omit "skipped" when nothing was rejected; default it to [].
    gw = _gw({ACF_POST: (200, {"applied": ["headline"], "acf": {"headline": "New"}})})
    assert asyncio.run(gw.set_acf_fields(5, {"headline": "New"})) == {
        "applied": ["headline"], "skipped": [], "acf": {"headline": "New"}}


def test_set_acf_fields_400_bad_fields_raises():
    gw = _gw({ACF_POST: (400, {"code": "wpops_bad_fields",
                               "message": "fields must be an object"})})
    with pytest.raises(RestError, match="fields must be an object"):
        asyncio.run(gw.set_acf_fields(5, {"x": 1}))


def test_set_acf_fields_missing_applied_200_raises():
    # A 200 without "applied" cannot confirm the write - refuse rather than claim success.
    gw = _gw({ACF_POST: (200, {"acf": {}})})
    with pytest.raises(RestError):
        asyncio.run(gw.set_acf_fields(5, {"x": 1}))


# -- users --------------------------------------------------------------------
USERS = ("GET", "/wp-json/wp/v2/users")
USER_7 = ("GET", "/wp-json/wp/v2/users/7")
CREATE_USER = ("POST", "/wp-json/wp/v2/users")
UPD_USER = ("POST", "/wp-json/wp/v2/users/7")
DEL_USER = ("DELETE", "/wp-json/wp/v2/users/7")

_USER = {"id": 7, "username": "jdoe", "name": "J Doe", "email": "j@site.test",
         "roles": ["editor"], "url": "https://site.test/author/jdoe/",
         "capabilities": {"edit_posts": True}, "extra_capabilities": {}}

_MAPPED_USER = {"id": 7, "username": "jdoe", "name": "J Doe", "email": "j@site.test",
                "roles": ["editor"], "url": "https://site.test/author/jdoe/"}


def test_list_users_maps_fields_and_params():
    gw = _gw({USERS: (200, [_USER])})
    assert asyncio.run(gw.list_users()) == [_MAPPED_USER]
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == USERS
    # context=edit is what makes WP return username/email/roles at all.
    assert params == {"context": "edit", "per_page": 100}


def test_list_users_passes_search_and_per_page():
    gw = _gw({USERS: (200, [])})
    assert asyncio.run(gw.list_users(search="doe", per_page=25)) == []
    _, _, params, _ = gw.c.calls[0]
    assert params == {"context": "edit", "per_page": 25, "search": "doe"}


def test_list_users_defaults_roles_and_falls_back_to_slug():
    # context=view has no "username" - the slug is the closest public equivalent.
    gw = _gw({USERS: (200, [{"id": 9, "slug": "editorbot", "name": "Bot"}])})
    assert asyncio.run(gw.list_users()) == [
        {"id": 9, "username": "editorbot", "name": "Bot", "email": None,
         "roles": [], "url": None}]


def test_list_users_error_status_raises():
    gw = _gw({USERS: (403, {"code": "rest_user_cannot_view", "message": "no cap"})})
    with pytest.raises(RestError, match="no cap"):
        asyncio.run(gw.list_users())


def test_get_user_maps_and_requests_edit_context():
    gw = _gw({USER_7: (200, _USER)})
    assert asyncio.run(gw.get_user(7)) == _MAPPED_USER
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == USER_7
    assert params == {"context": "edit"}


def test_get_user_error_statuses_raise():
    gw = _gw({USER_7: (404, {"message": "Invalid user ID."})})
    with pytest.raises(RestError, match="Invalid user ID"):
        asyncio.run(gw.get_user(7))
    gw2 = _gw({USER_7: (200, ["not an object"])})
    with pytest.raises(RestError):
        asyncio.run(gw2.get_user(7))


def test_create_user_posts_minimal_body():
    gw = _gw({CREATE_USER: (201, _USER)})
    assert asyncio.run(gw.create_user("jdoe", "j@site.test", "s3cret")) == _MAPPED_USER
    method, path, _, body = gw.c.calls[0]
    assert (method, path) == CREATE_USER
    assert body == {"username": "jdoe", "email": "j@site.test", "password": "s3cret"}


def test_create_user_includes_roles_and_name_when_given():
    gw = _gw({CREATE_USER: (201, _USER)})
    asyncio.run(gw.create_user("jdoe", "j@site.test", "s3cret",
                               roles=["editor"], name="J Doe"))
    _, _, _, body = gw.c.calls[0]
    assert body == {"username": "jdoe", "email": "j@site.test", "password": "s3cret",
                    "roles": ["editor"], "name": "J Doe"}


def test_create_user_wraps_a_single_role_string():
    # roles="editor" is the natural call; sending list("editor") would ship characters.
    gw = _gw({CREATE_USER: (201, _USER)})
    asyncio.run(gw.create_user("jdoe", "j@site.test", "s3cret", roles="editor"))
    assert gw.c.calls[0][3]["roles"] == ["editor"]


def test_create_user_error_statuses_raise():
    gw = _gw({CREATE_USER: (400, {"code": "existing_user_login",
                                  "message": "Sorry, that username already exists!"})})
    with pytest.raises(RestError, match="already exists"):
        asyncio.run(gw.create_user("jdoe", "j@site.test", "s3cret"))
    # 201 without an id cannot be verified downstream.
    gw2 = _gw({CREATE_USER: (201, {"username": "jdoe"})})
    with pytest.raises(RestError):
        asyncio.run(gw2.create_user("jdoe", "j@site.test", "s3cret"))


def test_update_user_posts_allowed_fields_and_returns_mapped_row():
    gw = _gw({UPD_USER: (200, _USER)})
    fields = {"name": "J Doe", "email": "j@site.test", "roles": ["editor"],
              "password": "n3w", "url": "https://x.test/", "description": "bio"}
    assert asyncio.run(gw.update_user(7, fields)) == _MAPPED_USER
    method, path, _, body = gw.c.calls[0]
    assert (method, path) == UPD_USER
    assert body == fields


def test_update_user_rejects_unknown_fields():
    gw = _gw({})
    with pytest.raises(ValueError, match="username"):
        asyncio.run(gw.update_user(7, {"name": "New", "username": "nope"}))
    assert gw.c.calls == []                     # nothing is sent when a key is not allow-listed


def test_update_user_error_status_raises():
    gw = _gw({UPD_USER: (403, {"message": "Sorry, you are not allowed to edit this user."})})
    with pytest.raises(RestError, match="not allowed to edit"):
        asyncio.run(gw.update_user(7, {"name": "New"}))


def test_delete_user_forces_and_reassigns_to_zero_by_default():
    gw = _gw({DEL_USER: (200, {"deleted": True, "previous": {"id": 7, "name": "J Doe"}})})
    assert asyncio.run(gw.delete_user(7)) == {"deleted": True, "previous": 7}
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == DEL_USER
    # WP core refuses a user delete without BOTH force and reassign.
    assert params == {"force": "true", "reassign": 0}


def test_delete_user_passes_reassign_target_through():
    gw = _gw({DEL_USER: (200, {"deleted": True, "previous": {"id": 7}})})
    asyncio.run(gw.delete_user(7, reassign=3))
    assert gw.c.calls[0][2] == {"force": "true", "reassign": 3}


def test_delete_user_tolerates_missing_previous():
    gw = _gw({DEL_USER: (200, {"deleted": True})})
    assert asyncio.run(gw.delete_user(7)) == {"deleted": True, "previous": None}


def test_delete_user_error_status_raises():
    gw = _gw({DEL_USER: (400, {"code": "rest_user_invalid_reassign",
                               "message": "Invalid user ID for reassignment."})})
    with pytest.raises(RestError, match="reassignment"):
        asyncio.run(gw.delete_user(7, reassign=99))


# -- comments -----------------------------------------------------------------
COMMENTS = ("GET", "/wp-json/wp/v2/comments")
UPD_COMMENT = ("POST", "/wp-json/wp/v2/comments/44")
DEL_COMMENT = ("DELETE", "/wp-json/wp/v2/comments/44")

_COMMENT = {"id": 44, "post": 5, "author_name": "Spammer",
            "content": {"raw": "buy pills", "rendered": "<p>buy pills</p>"},
            "status": "hold", "date": "2026-07-28T10:00:00", "link": "dropped"}

_MAPPED_COMMENT = {"id": 44, "post": 5, "author_name": "Spammer", "content": "buy pills",
                   "status": "hold", "date": "2026-07-28T10:00:00"}


def test_list_comments_maps_fields_and_params():
    gw = _gw({COMMENTS: (200, [_COMMENT])})
    assert asyncio.run(gw.list_comments()) == [_MAPPED_COMMENT]
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == COMMENTS
    assert params == {"context": "edit", "per_page": 100}


def test_list_comments_filters_by_post_and_status():
    gw = _gw({COMMENTS: (200, [])})
    assert asyncio.run(gw.list_comments(post_id=5, status="hold", per_page=10)) == []
    _, _, params, _ = gw.c.calls[0]
    assert params == {"context": "edit", "per_page": 10, "post": 5, "status": "hold"}


def test_list_comments_content_falls_back_to_rendered():
    gw = _gw({COMMENTS: (200, [{**_COMMENT, "content": {"rendered": "<p>hi</p>"}}])})
    assert asyncio.run(gw.list_comments())[0]["content"] == "<p>hi</p>"


def test_list_comments_error_status_raises():
    gw = _gw({COMMENTS: (500, {"message": "boom"})})
    with pytest.raises(RestError, match="boom"):
        asyncio.run(gw.list_comments())


def test_update_comment_posts_allowed_fields_and_returns_mapped_row():
    gw = _gw({UPD_COMMENT: (200, _COMMENT)})
    fields = {"status": "spam", "content": "edited", "author_name": "Someone"}
    assert asyncio.run(gw.update_comment(44, fields)) == _MAPPED_COMMENT
    method, path, _, body = gw.c.calls[0]
    assert (method, path) == UPD_COMMENT
    assert body == fields


def test_update_comment_rejects_unknown_fields():
    gw = _gw({})
    with pytest.raises(ValueError, match="post"):
        asyncio.run(gw.update_comment(44, {"status": "approved", "post": 9}))
    assert gw.c.calls == []


def test_update_comment_error_status_raises():
    gw = _gw({UPD_COMMENT: (404, {"message": "Invalid comment ID."})})
    with pytest.raises(RestError, match="Invalid comment ID"):
        asyncio.run(gw.update_comment(44, {"status": "spam"}))


def test_delete_comment_trashes_without_force():
    gw = _gw({DEL_COMMENT: (200, {**_COMMENT, "status": "trash"})})
    assert asyncio.run(gw.delete_comment(44)) == {"deleted": False, "id": 44,
                                                  "status": "trash"}
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == DEL_COMMENT
    assert params is None                       # no force -> WP trashes, recoverable


def test_delete_comment_force_reports_permanent_delete():
    gw = _gw({DEL_COMMENT: (200, {"deleted": True, "previous": {"id": 44}})})
    assert asyncio.run(gw.delete_comment(44, force=True)) == {
        "deleted": True, "id": 44, "status": "deleted"}
    assert gw.c.calls[0][2] == {"force": "true"}


def test_delete_comment_error_statuses_raise():
    gw = _gw({DEL_COMMENT: (403, {"message": "Sorry, you are not allowed to delete this comment."})})
    with pytest.raises(RestError, match="not allowed to delete"):
        asyncio.run(gw.delete_comment(44))
    gw2 = _gw({DEL_COMMENT: (200, ["not an object"])})
    with pytest.raises(RestError):
        asyncio.run(gw2.delete_comment(44))


# -- options (wp-ops-connect v1.3.0) -------------------------------------------
# One plugin route serves all four verbs, so every option call shares this path and
# only the method (and params/body) differs.
OPT_GET = ("GET", "/wp-json/wpops/v1/option")
OPT_SET = ("POST", "/wp-json/wpops/v1/option")
OPT_DEL = ("DELETE", "/wp-json/wpops/v1/option")


def test_options_gate_accepts_1_3_0_and_returns_info():
    gw = _gw({INFO_SITE: (200, {"plugin_version": "1.3.0", "wp": "6.9"})})
    info = asyncio.run(gw.ensure_options_capable())
    assert info["plugin_version"] == "1.3.0"


def test_options_gate_refuses_older_plugin():
    gw = _gw({INFO_SITE: (200, {"plugin_version": "1.2.0"})})
    with pytest.raises(RestError, match="1.3.0"):
        asyncio.run(gw.ensure_options_capable())


def test_list_option_names_returns_names():
    gw = _gw({OPT_GET: (200, {"names": ["siteurl", "home", "blogname"]})})
    assert asyncio.run(gw.list_option_names()) == ["siteurl", "home", "blogname"]
    method, path, params, body = gw.c.calls[0]
    assert (method, path) == OPT_GET
    # No name at all is what makes the plugin list instead of read.
    assert params is None and body is None


def test_list_option_names_error_and_missing_key_raise():
    gw = _gw({OPT_GET: (403, {"message": "Sorry, you are not allowed to do that."})})
    with pytest.raises(RestError, match="not allowed"):
        asyncio.run(gw.list_option_names())
    gw2 = _gw({OPT_GET: (200, {"name": "blogname"})})   # a read response, not a list
    with pytest.raises(RestError):
        asyncio.run(gw2.list_option_names())
    gw3 = _gw({OPT_GET: (200, {"names": "siteurl"})})   # not a list -> not char-split
    with pytest.raises(RestError):
        asyncio.run(gw3.list_option_names())


def test_get_option_existing_sends_name_param():
    gw = _gw({OPT_GET: (200, {"name": "blogname", "value": "My Site", "exists": True})})
    assert asyncio.run(gw.get_option("blogname")) == {
        "name": "blogname", "value": "My Site", "exists": True}
    _, _, params, body = gw.c.calls[0]
    # params, not an f-string path: httpx percent-encodes arbitrary option names for us.
    assert params == {"name": "blogname"} and body is None


def test_get_option_absent_reports_none_and_false():
    gw = _gw({OPT_GET: (200, {"name": "nope", "value": None, "exists": False})})
    assert asyncio.run(gw.get_option("nope")) == {
        "name": "nope", "value": None, "exists": False}


def test_get_option_falsy_value_is_not_absent():
    # An option may legitimately hold "" / 0 / False - only `exists` decides.
    gw = _gw({OPT_GET: (200, {"name": "blog_public", "value": 0, "exists": True})})
    out = asyncio.run(gw.get_option("blog_public"))
    assert out["value"] == 0 and out["exists"] is True


def test_get_option_missing_keys_raise():
    gw = _gw({OPT_GET: (200, {"name": "blogname"})})
    with pytest.raises(RestError):
        asyncio.run(gw.get_option("blogname"))


def test_set_option_posts_name_and_value():
    gw = _gw({OPT_SET: (200, {"name": "wpops_test", "value": {"a": 1}, "updated": True})})
    assert asyncio.run(gw.set_option("wpops_test", {"a": 1})) == {
        "name": "wpops_test", "value": {"a": 1}}
    method, path, params, body = gw.c.calls[0]
    assert (method, path) == OPT_SET
    assert params is None and body == {"name": "wpops_test", "value": {"a": 1}}


def test_set_option_error_statuses_raise():
    gw = _gw({OPT_SET: (403, {"message": "Sorry, you are not allowed to do that."})})
    with pytest.raises(RestError, match="not allowed"):
        asyncio.run(gw.set_option("blogname", "X"))
    gw2 = _gw({OPT_SET: (200, {"name": "blogname", "updated": True})})  # no value echoed
    with pytest.raises(RestError):
        asyncio.run(gw2.set_option("blogname", "X"))


def test_delete_option_returns_bool_both_ways():
    gw = _gw({OPT_DEL: (200, {"name": "wpops_test", "deleted": True})})
    assert asyncio.run(gw.delete_option("wpops_test")) is True
    gw2 = _gw({OPT_DEL: (200, {"name": "gone", "deleted": False})})
    assert asyncio.run(gw2.delete_option("gone")) is False


def test_delete_option_sends_name_as_query_param_not_body():
    """Request-shape lock. The transport CAN put a JSON body on DELETE (httpx sets
    content-type: application/json, which WP's get_param would read), but a DELETE
    body is the shape most likely to be dropped in transit - CDNs, proxies and some
    server configs discard it, and every other DELETE in this gateway uses params.
    The plugin reads $req->get_param('name'), which always covers the query string,
    so the query param is the form that provably works end to end."""
    gw = _gw({OPT_DEL: (200, {"name": "wpops_test", "deleted": True})})
    asyncio.run(gw.delete_option("wpops_test"))
    method, path, params, body = gw.c.calls[0]
    assert (method, path) == OPT_DEL
    assert params == {"name": "wpops_test"}
    assert body is None


def test_delete_option_error_and_missing_key_raise():
    gw = _gw({OPT_DEL: (403, {"message": "Sorry, you are not allowed to do that."})})
    with pytest.raises(RestError, match="not allowed"):
        asyncio.run(gw.delete_option("blogname"))
    gw2 = _gw({OPT_DEL: (200, {"name": "blogname"})})
    with pytest.raises(RestError):
        asyncio.run(gw2.delete_option("blogname"))


def test_option_bad_name_400_surfaces_plugin_detail():
    """The plugin 400s wpops_bad_request on an empty/non-string name - on every verb."""
    bad = (400, {"code": "wpops_bad_request", "message": "name must be a non-empty string"})
    gw = _gw({OPT_GET: bad})
    with pytest.raises(RestError, match="name must be a non-empty string"):
        asyncio.run(gw.get_option(""))
    gw2 = _gw({OPT_SET: bad})
    with pytest.raises(RestError, match="name must be a non-empty string"):
        asyncio.run(gw2.set_option("", "X"))
    gw3 = _gw({OPT_DEL: bad})
    with pytest.raises(RestError, match="name must be a non-empty string"):
        asyncio.run(gw3.delete_option(""))


# -- post type discovery (phase 5k task 1) ------------------------------------
TYPES = ("GET", "/wp-json/wp/v2/types")

# Shape of a real /wp/v2/types?context=edit body (trimmed to the fields we map).
TYPES_BODY = {
    "post": {"slug": "post", "rest_base": "posts", "hierarchical": False,
             "taxonomies": ["category", "post_tag"], "name": "Posts"},
    "page": {"slug": "page", "rest_base": "pages", "hierarchical": True,
             "taxonomies": [], "name": "Pages"},
    "project": {"slug": "project", "rest_base": "projects", "hierarchical": False,
                "taxonomies": ["project_type"], "name": "Projects"},
}


def test_route_page_post_never_touches_types():
    """The static map is the fast path: page/post resolve with ZERO HTTP calls, so an
    empty script (which AssertionErrors on any call) is enough to run them."""
    gw = _gw({})
    assert asyncio.run(gw._route_async("page")) == "pages"
    assert asyncio.run(gw._route_async("post")) == "posts"
    assert gw.c.calls == []


def test_get_types_maps_records_and_caches():
    gw = _gw({TYPES: (200, TYPES_BODY)})
    types = asyncio.run(gw.get_types())
    assert types["project"] == {"rest_base": "projects", "hierarchical": False,
                                "taxonomies": ["project_type"], "name": "Projects"}
    assert types["page"]["hierarchical"] is True
    assert asyncio.run(gw.get_types()) == types      # cached: no second fetch
    assert len(gw.c.calls) == 1
    assert gw.c.calls[0][2] == {"context": "edit"}


def test_unknown_type_resolves_via_one_cached_types_fetch():
    gw = _gw({TYPES: (200, TYPES_BODY)})
    assert asyncio.run(gw._route_async("project")) == "projects"
    assert len(gw.c.calls) == 1
    assert asyncio.run(gw._route_async("project")) == "projects"
    assert asyncio.run(gw.get_type_info("project"))["taxonomies"] == ["project_type"]
    assert len(gw.c.calls) == 1                      # still one /types fetch


def test_unknown_and_absent_type_raises_naming_it():
    body = {k: v for k, v in TYPES_BODY.items() if k != "project"}
    gw = _gw({TYPES: (200, body)})
    with pytest.raises(RestError, match="project"):
        asyncio.run(gw._route_async("project"))
    gw2 = _gw({TYPES: (200, body)})
    with pytest.raises(RestError, match="project"):
        asyncio.run(gw2.get_type_info("project"))


def test_types_unreachable_still_resolves_page_and_post():
    """Discovery is additive: a site whose /types 500s keeps every page/post tool
    working off the static map, and only genuinely new types fail."""
    gw = _gw({TYPES: (500, {"code": "internal_server_error"})})
    assert asyncio.run(gw._route_async("page")) == "pages"
    assert asyncio.run(gw._route_async("post")) == "posts"
    assert gw.c.calls == []
    with pytest.raises(RestError):
        asyncio.run(gw._route_async("project"))
    assert gw._route("page") == "pages"              # sync fallback unchanged
    with pytest.raises(RestError):
        gw._route("project")


def test_list_posts_uses_discovered_rest_base():
    gw = _gw({TYPES: (200, TYPES_BODY),
              ("GET", "/wp-json/wp/v2/projects"): (200, [
                  {"id": 7, "title": {"raw": "Clinic"}, "slug": "clinic",
                   "status": "publish", "type": "project"}])})
    rows = asyncio.run(gw.list_posts("project"))
    assert rows == [{"id": 7, "title": "Clinic", "slug": "clinic",
                     "status": "publish", "type": "project"}]
    assert [c[1] for c in gw.c.calls] == ["/wp-json/wp/v2/types",
                                          "/wp-json/wp/v2/projects"]


def test_route_for_id_resolves_cpt_route():
    gw = _gw({INFO_P5: (200, {"id": 5, "type": "project", "slug": "clinic",
                              "status": "publish", "title": "Clinic"}),
              TYPES: (200, TYPES_BODY),
              ("GET", "/wp-json/wp/v2/projects/5"): (200, {"content": {"raw": "<p>x</p>"}})})
    assert asyncio.run(gw.get_post_content(5)) == "<p>x</p>"


def test_create_post_uses_discovered_rest_base():
    gw = _gw({TYPES: (200, TYPES_BODY),
              ("POST", "/wp-json/wp/v2/projects"): (201, {"id": 9})})
    assert asyncio.run(gw.create_post("project", "T", "t", "draft", "<p>x</p>", {})) == 9
    assert gw._type_cache[9] == "project"


# -- taxonomies + terms (phase 5k task 2) -------------------------------------
TAXONOMIES = ("GET", "/wp-json/wp/v2/taxonomies")
CATEGORIES = ("GET", "/wp-json/wp/v2/categories")
TAGS = ("GET", "/wp-json/wp/v2/tags")

# Shape of a real /wp/v2/taxonomies?context=edit body. Both built-ins are the reason
# rest_base matters at all: NEITHER route matches its slug (category -> categories,
# post_tag -> tags). A CPT taxonomy usually registers rest_base == slug.
TAX_BODY = {
    "category": {"slug": "category", "name": "Categories", "rest_base": "categories",
                 "hierarchical": True, "types": ["post"]},
    "post_tag": {"slug": "post_tag", "name": "Tags", "rest_base": "tags",
                 "hierarchical": False, "types": ["post"]},
    "project_type": {"slug": "project_type", "name": "Project Types",
                     "rest_base": "project_type", "hierarchical": True,
                     "types": ["project"]},
}

_TERM = {"id": 3, "name": "News", "slug": "news", "parent": 0, "count": 12,
         "taxonomy": "category", "description": "d", "link": "dropped"}
_MAPPED_TERM = {"id": 3, "name": "News", "slug": "news", "parent": 0, "count": 12,
                "taxonomy": "category"}


def test_list_taxonomies_maps_rows_and_requests_edit_context():
    gw = _gw({TAXONOMIES: (200, TAX_BODY)})
    rows = asyncio.run(gw.list_taxonomies())
    assert {"slug": "category", "name": "Categories", "rest_base": "categories",
            "hierarchical": True, "types": ["post"]} in rows
    assert {r["slug"]: r["rest_base"] for r in rows} == {
        "category": "categories", "post_tag": "tags", "project_type": "project_type"}
    method, path, params, _ = gw.c.calls[0]
    assert (method, path) == TAXONOMIES
    assert params == {"context": "edit"}


def test_list_taxonomies_caches_the_map():
    gw = _gw({TAXONOMIES: (200, TAX_BODY)})
    first = asyncio.run(gw.list_taxonomies())
    assert asyncio.run(gw.list_taxonomies()) == first
    assert len(gw.c.calls) == 1                      # cached: no second fetch


def test_list_taxonomies_error_and_bad_body_raise():
    gw = _gw({TAXONOMIES: (403, {"message": "Sorry, you are not allowed to edit terms."})})
    with pytest.raises(RestError, match="not allowed to edit terms"):
        asyncio.run(gw.list_taxonomies())
    gw2 = _gw({TAXONOMIES: (200, ["not an object"])})
    with pytest.raises(RestError):
        asyncio.run(gw2.list_taxonomies())


def test_list_terms_uses_rest_base_not_slug():
    gw = _gw({TAXONOMIES: (200, TAX_BODY), CATEGORIES: (200, [_TERM])})
    assert asyncio.run(gw.list_terms("category")) == [_MAPPED_TERM]
    assert [c[1] for c in gw.c.calls] == ["/wp-json/wp/v2/taxonomies",
                                          "/wp-json/wp/v2/categories"]
    assert gw.c.calls[1][2] == {"per_page": 100}


def test_list_terms_post_tag_resolves_to_tags_route():
    """The single most likely bug in this phase: post_tag's route is /tags, never
    /post_tag. Same for the field name it writes on a post object."""
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              TAGS: (200, [{**_TERM, "id": 8, "name": "botox", "slug": "botox",
                            "taxonomy": "post_tag", "count": 2}])})
    rows = asyncio.run(gw.list_terms("post_tag"))
    assert rows == [{"id": 8, "name": "botox", "slug": "botox", "parent": 0,
                     "count": 2, "taxonomy": "post_tag"}]
    assert gw.c.calls[1][1] == "/wp-json/wp/v2/tags"


def test_list_terms_passes_search_and_per_page():
    gw = _gw({TAXONOMIES: (200, TAX_BODY), CATEGORIES: (200, [])})
    assert asyncio.run(gw.list_terms("category", search="new", per_page=10)) == []
    assert gw.c.calls[1][2] == {"per_page": 10, "search": "new"}


def test_list_terms_tolerates_missing_parent_on_flat_taxonomy():
    # WP omits `parent` for non-hierarchical taxonomies (tags) entirely.
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              TAGS: (200, [{"id": 8, "name": "botox", "slug": "botox", "count": 2,
                            "taxonomy": "post_tag"}])})
    assert asyncio.run(gw.list_terms("post_tag"))[0]["parent"] is None


def test_unknown_taxonomy_raises_naming_it_and_listing_available():
    gw = _gw({TAXONOMIES: (200, TAX_BODY)})
    with pytest.raises(RestError) as exc:
        asyncio.run(gw.list_terms("product_cat"))
    msg = str(exc.value)
    # Names the bad slug AND the ones that do exist - the fix is usually a rename.
    assert "product_cat" in msg and "category" in msg and "post_tag" in msg


def test_list_terms_error_status_raises():
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              CATEGORIES: (500, {"message": "boom"})})
    with pytest.raises(RestError, match="boom"):
        asyncio.run(gw.list_terms("category"))


def test_create_term_posts_minimal_body_and_maps_row():
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("POST", "/wp-json/wp/v2/categories"): (201, _TERM)})
    assert asyncio.run(gw.create_term("category", "News")) == _MAPPED_TERM
    method, path, _, body = gw.c.calls[1]
    assert (method, path) == ("POST", "/wp-json/wp/v2/categories")
    assert body == {"name": "News"}                  # optional keys omitted, not None


def test_create_term_includes_optional_fields():
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("POST", "/wp-json/wp/v2/categories"): (201, _TERM)})
    asyncio.run(gw.create_term("category", "News", slug="news", parent=2,
                               description="d"))
    assert gw.c.calls[1][3] == {"name": "News", "slug": "news", "parent": 2,
                                "description": "d"}


def test_create_term_error_statuses_raise():
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("POST", "/wp-json/wp/v2/categories"): (400, {
                  "code": "term_exists", "message": "A term with the name provided "
                                                    "already exists with this parent."})})
    with pytest.raises(RestError, match="already exists"):
        asyncio.run(gw.create_term("category", "News"))
    gw2 = _gw({TAXONOMIES: (200, TAX_BODY),
               ("POST", "/wp-json/wp/v2/categories"): (200, _TERM)})  # 200, not 201
    with pytest.raises(RestError):
        asyncio.run(gw2.create_term("category", "News"))


def test_update_term_posts_allowed_fields_and_returns_mapped_row():
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("POST", "/wp-json/wp/v2/categories/3"): (200, _TERM)})
    fields = {"name": "News", "slug": "news", "parent": 0, "description": "d"}
    assert asyncio.run(gw.update_term("category", 3, fields)) == _MAPPED_TERM
    method, path, _, body = gw.c.calls[1]
    assert (method, path) == ("POST", "/wp-json/wp/v2/categories/3")
    assert body == fields


def test_update_term_rejects_unknown_fields_before_any_request():
    gw = _gw({})
    with pytest.raises(ValueError, match="count"):
        asyncio.run(gw.update_term("category", 3, {"name": "New", "count": 99}))
    assert gw.c.calls == []      # validated before the taxonomy lookup - nothing is sent


def test_update_term_error_status_raises():
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("POST", "/wp-json/wp/v2/categories/3"): (404, {
                  "message": "Term does not exist."})})
    with pytest.raises(RestError, match="does not exist"):
        asyncio.run(gw.update_term("category", 3, {"name": "New"}))


def test_delete_term_sends_force_and_reports_previous_id():
    """WP REFUSES a term delete without force - terms have no trash state."""
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("DELETE", "/wp-json/wp/v2/categories/3"): (200, {
                  "deleted": True, "previous": _TERM})})
    assert asyncio.run(gw.delete_term("category", 3)) == {"deleted": True, "term": 3}
    method, path, params, body = gw.c.calls[1]
    assert (method, path) == ("DELETE", "/wp-json/wp/v2/categories/3")
    assert params == {"force": "true"} and body is None


def test_delete_term_tolerates_missing_previous():
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("DELETE", "/wp-json/wp/v2/tags/8"): (200, {"deleted": True})})
    assert asyncio.run(gw.delete_term("post_tag", 8)) == {"deleted": True, "term": None}


def test_delete_term_error_statuses_raise():
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("DELETE", "/wp-json/wp/v2/categories/3"): (403, {
                  "message": "Sorry, you are not allowed to delete this term."})})
    with pytest.raises(RestError, match="not allowed to delete"):
        asyncio.run(gw.delete_term("category", 3))
    gw2 = _gw({TAXONOMIES: (200, TAX_BODY),
               ("DELETE", "/wp-json/wp/v2/categories/3"): (200, ["not an object"])})
    with pytest.raises(RestError):
        asyncio.run(gw2.delete_term("category", 3))


def test_set_post_terms_writes_the_rest_base_keyed_field_on_the_post():
    """Term assignment is a POST to the POST object with the taxonomy's rest_base as
    the field name ({"tags": [...]}, never {"post_tag": [...]})."""
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              INFO_P5: (200, {"id": 5, "type": "post", "slug": "s",
                              "status": "publish", "title": "T"}),
              ("POST", "/wp-json/wp/v2/posts/5"): (200, {"id": 5, "tags": [8, 9]})})
    out = asyncio.run(gw.set_post_terms(5, "post_tag", [8, 9]))
    assert out == {"post_id": 5, "taxonomy": "post_tag", "terms": [8, 9]}
    method, path, _, body = gw.c.calls[-1]
    assert (method, path) == ("POST", "/wp-json/wp/v2/posts/5")
    assert body == {"tags": [8, 9]}


def test_set_post_terms_reports_what_wp_actually_stored():
    # WP silently drops ids the taxonomy does not own - report its list, not ours.
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("POST", "/wp-json/wp/v2/posts/5"): (200, {"id": 5, "categories": [3]})})
    gw._type_cache[5] = "post"                       # warm: no /info round-trip
    out = asyncio.run(gw.set_post_terms(5, "category", [3, 999]))
    assert out["terms"] == [3]
    assert gw.c.calls[-1][3] == {"categories": [3, 999]}


def test_set_post_terms_resolves_a_cpt_route():
    gw = _gw({TAXONOMIES: (200, TAX_BODY), TYPES: (200, TYPES_BODY),
              ("POST", "/wp-json/wp/v2/projects/7"): (200, {"id": 7,
                                                            "project_type": [4]})})
    gw._type_cache[7] = "project"
    out = asyncio.run(gw.set_post_terms(7, "project_type", [4]))
    assert out == {"post_id": 7, "taxonomy": "project_type", "terms": [4]}
    assert gw.c.calls[-1][3] == {"project_type": [4]}


def test_set_post_terms_unknown_taxonomy_raises_before_touching_the_post():
    gw = _gw({TAXONOMIES: (200, TAX_BODY)})
    with pytest.raises(RestError, match="product_cat"):
        asyncio.run(gw.set_post_terms(5, "product_cat", [1]))
    assert [c[1] for c in gw.c.calls] == ["/wp-json/wp/v2/taxonomies"]


def test_set_post_terms_error_statuses_raise():
    gw = _gw({TAXONOMIES: (200, TAX_BODY),
              ("POST", "/wp-json/wp/v2/posts/5"): (403, {
                  "message": "Sorry, you are not allowed to assign this term."})})
    gw._type_cache[5] = "post"
    with pytest.raises(RestError, match="not allowed to assign"):
        asyncio.run(gw.set_post_terms(5, "category", [3]))
    gw2 = _gw({TAXONOMIES: (200, TAX_BODY),
               ("POST", "/wp-json/wp/v2/posts/5"): (200, ["not an object"])})
    gw2._type_cache[5] = "post"
    with pytest.raises(RestError):
        asyncio.run(gw2.set_post_terms(5, "category", [3]))


def test_taxonomy_cache_is_shared_across_term_methods():
    gw = _gw({TAXONOMIES: (200, TAX_BODY), CATEGORIES: (200, [_TERM]),
              ("POST", "/wp-json/wp/v2/categories"): (201, _TERM),
              ("DELETE", "/wp-json/wp/v2/categories/3"): (200, {"deleted": True,
                                                                "previous": _TERM})})
    asyncio.run(gw.list_terms("category"))
    asyncio.run(gw.create_term("category", "News"))
    asyncio.run(gw.delete_term("category", 3))
    assert [c[1] for c in gw.c.calls].count("/wp-json/wp/v2/taxonomies") == 1


# -- plugins (phase 5l task 1) ------------------------------------------------
PLUGINS = ("GET", "/wp-json/wp/v2/plugins")
# The REST id is `folder/file` and its slash MUST arrive RAW - the plugins controller
# registers its item route as (?P<plugin>[^.\/]+(?:\/[^.\/]+)?), a pattern that
# deliberately matches a literal slash. Live-verified 2026-07-29 on examplestg:
#   %2F -> 404 rest_plugin_not_found ; raw slash -> 200.
# (Most WP REST id params are NOT like this, which is why %2F looked plausible.)
AKISMET_ID = "akismet/akismet"
AKISMET_PATH = "/wp-json/wp/v2/plugins/akismet/akismet"

# A real /wp/v2/plugins?context=edit row, trimmed to the fields we map plus a couple we
# deliberately drop. `author` is a STRING carrying the plugin header's markup verbatim -
# WP does not strip it, and neither do we.
PLUGIN_ROW = {
    "plugin": "akismet/akismet", "status": "inactive", "name": "Akismet Anti-spam",
    "plugin_uri": "https://akismet.com/", "version": "5.3.2",
    "author": '<a href="https://automattic.com">Automattic</a>',
    "author_uri": "https://automattic.com/wordpress-plugins/",
    "description": {"raw": "Spam protection.", "rendered": "Spam protection."},
    "network_only": False, "requires_wp": "5.8", "requires_php": "5.6",
    "textdomain": "akismet",
}
PLUGIN_MAPPED = {
    "plugin": "akismet/akismet", "name": "Akismet Anti-spam", "status": "inactive",
    "version": "5.3.2", "network_only": False, "requires_wp": "5.8",
    "requires_php": "5.6", "textdomain": "akismet",
    "author": '<a href="https://automattic.com">Automattic</a>',
}


def test_list_plugins_maps_rows_and_uses_edit_context():
    gw = _gw({PLUGINS: (200, [PLUGIN_ROW,
                              {"plugin": "hello", "status": "active", "name": "Hello Dolly",
                               "version": "1.7.2", "network_only": False,
                               "requires_wp": "", "requires_php": "",
                               "textdomain": "hello-dolly", "author": "Matt Mullenweg"}])})
    rows = asyncio.run(gw.list_plugins())
    assert rows[0] == PLUGIN_MAPPED
    # A single-file plugin's REST id has no slash at all - it passes through unchanged.
    assert rows[1]["plugin"] == "hello" and rows[1]["status"] == "active"
    method, path, params, body = gw.c.calls[0]
    assert (method, path) == PLUGINS and params == {"context": "edit"} and body is None


def test_list_plugins_error_status_and_non_list_raise():
    gw = _gw({PLUGINS: (403, {"message": "Sorry, you are not allowed to manage plugins "
                              "for this site."})})
    with pytest.raises(RestError, match="not allowed to manage plugins"):
        asyncio.run(gw.list_plugins())
    gw2 = _gw({PLUGINS: (200, {"plugin": "akismet/akismet"})})   # object, not a list
    with pytest.raises(RestError):
        asyncio.run(gw2.list_plugins())


def test_list_plugins_404_says_the_endpoint_may_be_unavailable():
    """WP < 5.5 has no /wp/v2/plugins route at all. A bare 'HTTP 404: rest_no_route'
    reads like a bad request; name the actual cause."""
    gw = _gw({PLUGINS: (404, {"code": "rest_no_route",
                              "message": "No route was found matching the URL and "
                                         "request method."})})
    with pytest.raises(RestError, match="may be unavailable") as e:
        asyncio.run(gw.list_plugins())
    assert "5.5" in str(e.value)


def test_get_plugin_sends_the_id_slash_raw_not_percent_encoded():
    """Do NOT "fix" this back to %2F. The plan for phase 5l asserted the slash had to be
    percent-encoded; live validation on examplestg (2026-07-29) proved the opposite:

        404  /wp-json/wp/v2/plugins/what-the-file%2Fwhat-the-file  rest_plugin_not_found
        200  /wp-json/wp/v2/plugins/what-the-file/what-the-file

    WP's plugins controller registers the item route as
    (?P<plugin>[^.\\/]+(?:\\/[^.\\/]+)?) - the regex matches a literal slash on purpose,
    so the id must be sent with a raw one."""
    gw = _gw({("GET", AKISMET_PATH): (200, PLUGIN_ROW)})
    assert asyncio.run(gw.get_plugin(AKISMET_ID)) == PLUGIN_MAPPED
    method, path, params, _ = gw.c.calls[0]
    assert path == AKISMET_PATH
    segment = path.split("/wp-json/wp/v2/plugins/", 1)[1]
    assert segment == "akismet/akismet" and "%2F" not in path.upper()
    assert params == {"context": "edit"}


def test_get_plugin_still_encodes_other_unsafe_characters():
    """Raw slash is a targeted exemption, not "stop encoding". Everything else a plugin
    file name could contain - spaces, ?, # - still has to be escaped or it would split
    the query string / fragment off the path."""
    weird_id = "odd plugin/what?#file"
    weird_path = "/wp-json/wp/v2/plugins/odd%20plugin/what%3F%23file"
    gw = _gw({("GET", weird_path): (200, dict(PLUGIN_ROW, plugin=weird_id))})
    assert asyncio.run(gw.get_plugin(weird_id))["plugin"] == weird_id
    path = gw.c.calls[0][1]
    assert path == weird_path
    assert " " not in path and "?" not in path and "#" not in path
    assert path.count("/") == 6            # 5 route separators + the one inside the id


def test_get_plugin_missing_plugin_404_is_not_blamed_on_the_wp_version():
    """A 404 with rest_plugin_not_found means the ID is wrong, NOT that the endpoint
    is missing - saying 'requires WP 5.5' there would send the caller down a dead end."""
    gw = _gw({("GET", "/wp-json/wp/v2/plugins/nope/nope"): (404, {
        "code": "rest_plugin_not_found", "message": "Plugin file does not exist."})})
    with pytest.raises(RestError, match="not installed") as e:
        asyncio.run(gw.get_plugin("nope/nope"))
    assert "may be unavailable" not in str(e.value)


def test_set_plugin_status_sends_raw_slash_id_and_posts_status_body():
    """Same live-verified contract as the GET (examplestg, 2026-07-29): the write
    route is the same item route, so %2F would 404 here too."""
    activated = dict(PLUGIN_ROW, status="active")
    gw = _gw({("POST", AKISMET_PATH): (200, activated)})
    out = asyncio.run(gw.set_plugin_status(AKISMET_ID, "active"))
    assert out["status"] == "active" and out["plugin"] == AKISMET_ID
    method, path, params, body = gw.c.calls[0]
    assert (method, path) == ("POST", AKISMET_PATH)
    assert path.split("/wp-json/wp/v2/plugins/", 1)[1] == "akismet/akismet"
    assert "%2F" not in path.upper()
    assert body == {"status": "active"} and params is None


def test_set_plugin_status_deactivate_posts_inactive():
    gw = _gw({("POST", AKISMET_PATH): (200, dict(PLUGIN_ROW, status="inactive"))})
    assert asyncio.run(gw.set_plugin_status(AKISMET_ID, "inactive"))["status"] == "inactive"
    assert gw.c.calls[0][3] == {"status": "inactive"}


def test_set_plugin_status_rejects_bad_status_before_any_request():
    """Validate first: WP would happily 400, but only after a round-trip, and a typo
    like 'activate' must never reach a live site as an ambiguous write."""
    for bad in ("activate", "ACTIVE", "", None, "deleted"):
        gw = _gw({})                       # empty script: any call AssertionErrors
        with pytest.raises(ValueError, match="active"):
            asyncio.run(gw.set_plugin_status(AKISMET_ID, bad))
        assert gw.c.calls == []


def test_set_plugin_status_error_statuses_raise_with_detail():
    gw = _gw({("POST", AKISMET_PATH): (403, {
        "code": "rest_cannot_manage_plugins",
        "message": "Sorry, you are not allowed to activate this plugin."})})
    with pytest.raises(RestError, match="not allowed to activate"):
        asyncio.run(gw.set_plugin_status(AKISMET_ID, "active"))
    gw2 = _gw({("POST", AKISMET_PATH): (200, ["not an object"])})
    with pytest.raises(RestError):
        asyncio.run(gw2.set_plugin_status(AKISMET_ID, "active"))
    gw3 = _gw({("POST", AKISMET_PATH): (500, {
        "code": "unable_to_connect_to_filesystem",
        "message": "Unable to connect to the filesystem."})})
    with pytest.raises(RestError, match="Unable to connect to the filesystem"):
        asyncio.run(gw3.set_plugin_status(AKISMET_ID, "active"))


def test_plugin_reads_are_not_cached():
    """Activation changes the inventory, so a second read must hit the site again."""
    gw = _gw({PLUGINS: (200, [PLUGIN_ROW])})
    asyncio.run(gw.list_plugins())
    asyncio.run(gw.list_plugins())
    assert len(gw.c.calls) == 2


# --- theme file read/write -----------------------------------------------------------
# Theme files are executable PHP. The gateway must surface the plugin's guardrails as
# errors rather than pretending a refused write succeeded.

class _ThemeClient:
    """Stub WPRestClient recording calls and returning canned (status, data)."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.base_url = "https://s.test"

    async def request(self, method, path, *, params=None, json_body=None, http=None):
        # Signature mirrors WPRestClient.request EXACTLY (keyword-only, json_body not
        # json). A looser stub accepted json= and hid a real TypeError until live test.
        self.calls.append({"method": method, "path": path, "params": params,
                           "json": json_body})
        return self.responses.pop(0)


def _theme_gw(responses):
    from wp_ops_mcp.ops.rest_gateway import RestContentGateway
    gw = RestContentGateway(_ThemeClient(responses))
    gw._plugin_info = {"plugin_version": "1.4.0"}      # skip the discovery round-trip
    return gw


def test_get_theme_file_returns_contents():
    import asyncio
    gw = _theme_gw([(200, {"theme": "Divi-child", "file": "style.css",
                     "contents": "body{}", "bytes": 6, "writable": True})])
    r = asyncio.run(gw.get_theme_file("style.css"))
    assert r["contents"] == "body{}"
    assert gw.c.calls[0]["path"].endswith("/theme-file")


def test_set_theme_file_returns_previous_so_a_revert_is_possible():
    import asyncio
    gw = _theme_gw([(200, {"theme": "Divi-child", "file": "functions.php", "written": True,
                     "created": False, "bytes": 20, "verified": True,
                     "previous": "<?php // old", "previous_bytes": 12})])
    r = asyncio.run(gw.set_theme_file("functions.php", "<?php // new"))
    assert r["written"] is True and r["verified"] is True
    assert r["previous"] == "<?php // old"


def test_set_theme_file_raises_when_core_reverted_a_fatal():
    """A write that fataled is REVERTED by core - it must not read as success."""
    import asyncio
    import pytest
    from wp_ops_mcp.ops.rest_gateway import RestError
    gw = _theme_gw([(400, {"code": "wpops_write_failed",
                     "message": "Your PHP code changes were not applied due to an error",
                     "data": {"reverted": True}})])
    with pytest.raises(RestError) as e:
        asyncio.run(gw.set_theme_file("functions.php", "<?php syntax error"))
    assert "revert" in str(e.value).lower() or "not applied" in str(e.value).lower()


def test_set_theme_file_raises_when_write_not_verified():
    """verified=False means disk does not match what we sent - never report success."""
    import asyncio
    import pytest
    from wp_ops_mcp.ops.rest_gateway import RestError
    gw = _theme_gw([(200, {"written": True, "verified": False, "bytes": 3,
                     "previous": "x", "previous_bytes": 1})])
    with pytest.raises(RestError):
        asyncio.run(gw.set_theme_file("style.css", "body{}"))

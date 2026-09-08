"""REST implementation of the content gateway (WP core REST + wp-ops-connect).

Same method surface as WPCliContentGateway, so ContentOps/EditOps/tools are unchanged.
No SSH anywhere on this path (spec: cloud deployments run REST-only).
"""
from __future__ import annotations

from urllib.parse import quote

from ..transport.rest import WPRestClient, RestError

# Static route map for WP's two built-in post types. It is BOTH the fast path (a page
# or post never costs a /wp/v2/types round-trip) and the offline fallback (page/post keep
# working when discovery is unreachable). Every other type is discovered - see get_types.
_ROUTES = {"page": "pages", "post": "posts"}
_TYPES_ROUTE = "/wp-json/wp/v2/types"
_TAXONOMIES_ROUTE = "/wp-json/wp/v2/taxonomies"

# list_posts pagination: 100 rows/page, walk up to 10 pages (1000 rows) then stop and
# flag truncation. The cap bounds a runaway list on a site with thousands of posts.
_LIST_PER_PAGE = 100
_LIST_PAGE_CAP = 10


def _ver(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def _ver_lt(a: str, b: str) -> bool:
    """True if version a < version b, zero-padding the shorter so 1.0 == 1.0.0."""
    ta, tb = _ver(a), _ver(b)
    n = max(len(ta), len(tb))
    return ta + (0,) * (n - len(ta)) < tb + (0,) * (n - len(tb))


def _err_detail(data) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("code") or data)[:200]
    return str(data)[:200]


def _title(value) -> str:
    """WP titles arrive as {"raw","rendered"} under context=edit; raw is the editable
    text (rendered is entity-encoded), so prefer it and fall back to rendered."""
    if isinstance(value, dict):
        return value.get("raw") or value.get("rendered", "")
    return "" if value is None else str(value)


_MENU_ITEM_FIELDS = ("url", "menu_order", "parent", "object", "object_id", "type", "status")
# Menu-item keys a caller may change; anything else is rejected (see update_menu_item).
_MENU_ITEM_EDITABLE = ("title", "menu_order", "parent", "url")


def _menu_item(row: dict) -> dict:
    """One menu-item row, mapped identically wherever it comes from (list/add/update)."""
    out = {"id": row.get("id"), "title": _title(row.get("title"))}
    out.update({f: row.get(f) for f in _MENU_ITEM_FIELDS})
    return out


# User fields a caller may change; anything else is rejected (see update_user). username
# is deliberately absent - WP itself refuses to change a login after creation.
_USER_EDITABLE = ("name", "email", "roles", "password", "url", "description")
# Comment fields a caller may change (see update_comment).
_COMMENT_EDITABLE = ("status", "content", "author_name")


def _user(row: dict) -> dict:
    """One user row, mapped identically wherever it comes from (list/get/create/update).
    context=edit returns `username`; a view-context row only has `slug`, so fall back to
    it rather than reporting no login at all."""
    return {"id": row.get("id"), "username": row.get("username") or row.get("slug"),
            "name": row.get("name"), "email": row.get("email"),
            "roles": list(row.get("roles") or []), "url": row.get("url")}


def _comment(row: dict) -> dict:
    """One comment row. content arrives as {"raw","rendered"} exactly like a title, so
    the same raw-then-rendered helper applies."""
    return {"id": row.get("id"), "post": row.get("post"),
            "author_name": row.get("author_name"), "content": _title(row.get("content")),
            "status": row.get("status"), "date": row.get("date")}


_OPTION_ROUTE = "/wp-json/wpops/v1/option"
_THEME_FILE_ROUTE = "/wp-json/wpops/v1/theme-file"

# Term fields a caller may change; anything else is rejected (see update_term).
_TERM_EDITABLE = ("name", "slug", "parent", "description")


def _term(row: dict, taxonomy: str) -> dict:
    """One term row, mapped identically wherever it comes from (list/create/update).
    `parent` is absent entirely on non-hierarchical taxonomies (tags), so None here
    means "flat taxonomy", not "top level" (which WP reports as 0)."""
    return {"id": row.get("id"), "name": row.get("name"), "slug": row.get("slug"),
            "parent": row.get("parent"), "count": row.get("count"),
            "taxonomy": row.get("taxonomy") or taxonomy}


_PLUGINS_ROUTE = "/wp-json/wp/v2/plugins"
# The only two statuses core accepts on a single-site install (network-activate is a
# multisite-only third value we deliberately do not expose - see the plan's scope note).
_PLUGIN_STATUSES = ("active", "inactive")


def _plugin_path(plugin_id: str) -> str:
    """Item route for one plugin.

    A plugin's REST id is its "plugin file" - `folder/file` (akismet/akismet), and that
    slash must be sent RAW. Unlike most WP REST id params, the plugins controller
    registers its item route as `(?P<plugin>[^.\\/]+(?:\\/[^.\\/]+)?)` - a pattern that
    deliberately matches a literal slash - so percent-encoding it does NOT match.
    Live-verified 2026-07-29 on examplestg:

        404  /wp-json/wp/v2/plugins/what-the-file%2Fwhat-the-file  rest_plugin_not_found
        200  /wp-json/wp/v2/plugins/what-the-file/what-the-file

    safe="/" therefore keeps the slash intact while still escaping anything else unsafe
    (spaces, ?, # ...) that would otherwise break the path.
    """
    return f"{_PLUGINS_ROUTE}/{quote(str(plugin_id), safe='/')}"


def _plugin(row: dict) -> dict:
    """One plugin row, mapped identically wherever it comes from (list/get/status).

    `author` is a plain STRING lifted from the plugin header and it routinely contains
    an anchor tag - WP does not strip it and neither do we: half-sanitizing markup here
    would silently change what the site reports. Callers render it as untrusted text.
    """
    return {"plugin": row.get("plugin"), "name": row.get("name"),
            "status": row.get("status"), "version": row.get("version"),
            "network_only": bool(row.get("network_only")),
            "requires_wp": row.get("requires_wp"),
            "requires_php": row.get("requires_php"),
            "textdomain": row.get("textdomain"), "author": row.get("author")}


def _plugin_error(action: str, status: int, data) -> RestError:
    """A 404 on this route has two very different causes, and guessing wrong sends the
    caller down a dead end: rest_plugin_not_found means the ID is wrong, anything else
    (rest_no_route) means the site has no /wp/v2/plugins route - WP < 5.5, or the route
    filtered off. Say which."""
    hint = ""
    if status == 404:
        code = data.get("code") if isinstance(data, dict) else None
        if code == "rest_plugin_not_found":
            hint = " - plugin not installed on this site"
        else:
            hint = (f" - the {_PLUGINS_ROUTE} endpoint may be unavailable on this site "
                    f"(it requires WordPress 5.5+)")
    return RestError(f"{action} failed (HTTP {status}: {_err_detail(data)}){hint}")


class RestContentGateway:
    MIN_PLUGIN_VERSION = "1.0.0"
    SEO_MIN_VERSION = "1.1.0"
    OPTIONS_MIN_VERSION = "1.3.0"
    THEME_FILES_MIN_VERSION = "1.4.0"

    def __init__(self, client: WPRestClient):
        self.c = client
        self._type_cache: dict[int, str] = {}      # post id -> post type
        self._types_cache: dict | None = None      # post type slug -> discovery record
        self._tax_cache: dict | None = None        # taxonomy slug -> discovery record
        self._plugin_info: dict | None = None
        # Set by each list_posts call: True when the 10-page cap was hit or a page past
        # the first errored (so the returned list may be incomplete). find reads this.
        self.last_list_truncated = False

    def preview_url(self, draft_id: int) -> str | None:
        """Human-viewable URL for a staged draft, or None if we cannot build one.

        `?p=<id>&preview=true` works for any post type and does not need the permalink,
        which we may not have fetched. Viewing a draft still requires being logged in -
        this is a link for a person, not an anonymous fetch.
        """
        base = getattr(self.c, "base_url", "") or ""
        if not base:
            return None
        return f"{base.rstrip('/')}/?p={int(draft_id)}&preview=true"

    # -- plugin ---------------------------------------------------------------
    async def ensure_plugin(self) -> dict:
        if self._plugin_info is not None:
            return self._plugin_info
        status, data = await self.c.request("GET", "/wp-json/wpops/v1/info")
        if status != 200 or not isinstance(data, dict):
            raise RestError(f"wp-ops-connect plugin unavailable (HTTP {status}: {_err_detail(data)})")
        if _ver_lt(data.get("plugin_version", "0"), self.MIN_PLUGIN_VERSION):
            raise RestError(f"wp-ops-connect {data.get('plugin_version')} < required {self.MIN_PLUGIN_VERSION}")
        self._plugin_info = data
        return data

    async def ensure_seo_capable(self) -> dict:
        """ensure_plugin() plus a floor of SEO_MIN_VERSION; returns the cached info
        (callers read info.get("seo_plugin"))."""
        info = await self.ensure_plugin()
        if _ver_lt(info.get("plugin_version", "0"), self.SEO_MIN_VERSION):
            raise RestError(f"wp-ops-connect {info.get('plugin_version')} < required "
                            f"{self.SEO_MIN_VERSION} for SEO - update the plugin")
        return info

    async def ensure_options_capable(self) -> dict:
        """ensure_plugin() plus a floor of OPTIONS_MIN_VERSION - the /option route only
        exists from wp-ops-connect 1.3.0 (an older plugin 404s rest_no_route, which is a
        far less obvious error than saying so up front). Returns the cached info."""
        info = await self.ensure_plugin()
        if _ver_lt(info.get("plugin_version", "0"), self.OPTIONS_MIN_VERSION):
            raise RestError(f"wp-ops-connect {info.get('plugin_version')} < required "
                            f"{self.OPTIONS_MIN_VERSION} for options - update the plugin")
        return info

    async def ensure_theme_files_capable(self) -> dict:
        """ensure_plugin() plus a floor of THEME_FILES_MIN_VERSION - /theme-file only
        exists from wp-ops-connect 1.4.0."""
        info = await self.ensure_plugin()
        if _ver_lt(info.get("plugin_version", "0"), self.THEME_FILES_MIN_VERSION):
            raise RestError(f"wp-ops-connect {info.get('plugin_version')} < required "
                            f"{self.THEME_FILES_MIN_VERSION} for theme files - update the plugin")
        return info

    async def get_theme_file(self, file: str, theme: str | None = None) -> dict:
        """Read one theme file. Returns {"theme","file","contents","bytes","writable"}."""
        await self.ensure_theme_files_capable()
        params = {"file": file}
        if theme:
            params["theme"] = theme
        st, data = await self.c.request("GET", _THEME_FILE_ROUTE, params=params)
        if st != 200 or not isinstance(data, dict) or "contents" not in data:
            raise RestError(f"get theme file failed (HTTP {st}: {_err_detail(data)})")
        return data

    async def set_theme_file(self, file: str, contents: str, theme: str | None = None,
                             allow_create: bool = False,
                             allow_other_theme: bool = False) -> dict:
        """Write one theme file through WordPress core's self-reverting editor.

        Two distinct failures are BOTH raised, never returned as success:
          - core rejected/reverted the write (a PHP fatal was detected on loopback)
          - the write landed but the readback does not match what we sent (verified=False)
        On success the response carries `previous`, so the caller can revert deliberately.
        """
        await self.ensure_theme_files_capable()
        body = {"file": file, "contents": contents,
                "allow_create": bool(allow_create),
                "allow_other_theme": bool(allow_other_theme)}
        if theme:
            body["theme"] = theme
        st, data = await self.c.request("POST", _THEME_FILE_ROUTE, json_body=body)
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"set theme file failed (HTTP {st}: {_err_detail(data)})")
        if not data.get("written"):
            raise RestError(f"set theme file did not write (HTTP {st}: {_err_detail(data)})")
        if not data.get("verified"):
            raise RestError("theme file readback does not match what was sent - "
                            "the file on disk is NOT the content you supplied")
        return data

    # -- info / type resolution ----------------------------------------------
    async def get_post_info(self, post_id: int) -> dict:
        status, data = await self.c.request("GET", f"/wp-json/wpops/v1/info?post_id={int(post_id)}")
        if status != 200 or not isinstance(data, dict):
            raise RestError(f"post info failed (HTTP {status}: {_err_detail(data)})")
        self._type_cache[int(post_id)] = data["type"]
        return {"id": data["id"], "name": data["slug"], "status": data["status"],
                "type": data["type"], "title": data.get("title", "")}

    async def get_types(self) -> dict:
        """Every REST-enabled post type on the site, keyed by slug:
        `{slug: {"rest_base", "hierarchical", "taxonomies", "name"}}`.

        /wp/v2/types only lists types registered with show_in_rest, which is exactly the
        set the REST tools can act on. Cached per gateway instance: the map is stable for
        a session, and _route_async() would otherwise re-fetch it on every CPT call.
        """
        if self._types_cache is not None:
            return self._types_cache
        st, data = await self.c.request("GET", _TYPES_ROUTE, params={"context": "edit"})
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"list post types failed (HTTP {st}: {_err_detail(data)})")
        types = {}
        for slug, row in data.items():
            if not isinstance(row, dict):
                continue
            # rest_base is the URL segment and is NOT the slug for many types
            # (post -> posts, post_tag -> tags); slug is only a last-resort default.
            types[slug] = {"rest_base": row.get("rest_base") or slug,
                           "hierarchical": bool(row.get("hierarchical")),
                           "taxonomies": list(row.get("taxonomies") or []),
                           "name": row.get("name", slug)}
        self._types_cache = types
        return types

    async def get_type_info(self, post_type: str) -> dict:
        """One type's discovery record; RestError naming the type (and what IS available)
        when it is not registered or not exposed over REST."""
        types = await self.get_types()
        info = types.get(post_type)
        if info is None:
            raise RestError(f"post type {post_type!r} is not exposed over REST on this "
                            f"site (available: {', '.join(sorted(types)) or 'none'})")
        return info

    def _route(self, post_type: str) -> str:
        """Static, no-I/O route lookup - page/post only.

        Kept as-is for the offline path: it is the fallback semantics _route_async()
        preserves, and the only lookup available to a caller that cannot await. All
        in-gateway call sites are async and use _route_async().
        """
        route = _ROUTES.get(post_type)
        if route is None:
            raise RestError(f"unsupported post type {post_type!r} for REST transport")
        return route

    async def _route_async(self, post_type: str) -> str:
        """Route for any post type: built-ins from the static map with no HTTP at all,
        anything else from cached discovery."""
        route = _ROUTES.get(post_type)
        if route is not None:
            return route
        return (await self.get_type_info(post_type))["rest_base"]

    async def _route_for_id(self, post_id: int) -> str:
        ptype = self._type_cache.get(int(post_id))
        if ptype is None:
            ptype = (await self.get_post_info(post_id))["type"]
        return await self._route_async(ptype)

    # -- reads ----------------------------------------------------------------
    async def list_posts(self, post_type: str, query: str | None = None) -> list[dict]:
        # _fields keeps the response to metadata - without it WP returns every page's
        # full raw content (context=edit), which times out on content-heavy sites.
        # Paginate (per_page=100, page=1..) so sites with >100 pages/posts list fully;
        # cap at _LIST_PAGE_CAP pages and set last_list_truncated when the cap is hit
        # (the last page was still full) or a page past the first errors. Fail-open on
        # a mid-loop error: listing is read-only, so a partial list + the truncated flag
        # beats raising and losing the rows we already have. A first-page error is a real
        # empty result and still raises, exactly as before.
        route = await self._route_async(post_type)
        self.last_list_truncated = False
        out: list[dict] = []
        for page in range(1, _LIST_PAGE_CAP + 1):
            params = {"status": "any", "per_page": _LIST_PER_PAGE, "context": "edit",
                      "_fields": "id,title,slug,status,type", "page": page}
            if query:
                params["search"] = query
            try:
                status, data = await self.c.request(
                    "GET", f"/wp-json/wp/v2/{route}", params=params)
            except RestError:
                if page == 1:
                    raise
                self.last_list_truncated = True
                break
            if status != 200 or not isinstance(data, list):
                if page == 1:
                    raise RestError(f"list failed (HTTP {status}: {_err_detail(data)})")
                self.last_list_truncated = True
                break
            for p in data:
                title = _title(p.get("title"))
                pid, ptype = p.get("id"), p.get("type", post_type)
                if pid is not None:
                    self._type_cache[int(pid)] = ptype  # warm cache: skip an /info round-trip
                out.append({"id": pid, "title": title, "slug": p.get("slug"),
                            "status": p.get("status"), "type": ptype})
            if len(data) < _LIST_PER_PAGE:
                break                            # a short page is the last page
        else:
            # Ran the full page range with no short-page break: the cap was hit and the
            # final page was full, so more rows may exist beyond it.
            self.last_list_truncated = True
        return out

    async def get_post_content(self, post_id: int) -> str:
        route = await self._route_for_id(post_id)
        status, data = await self.c.request("GET", f"/wp-json/wp/v2/{route}/{int(post_id)}",
                                            params={"context": "edit"})
        if status != 200 or not isinstance(data, dict):
            raise RestError(f"get content failed (HTTP {status}: {_err_detail(data)})")
        raw = (data.get("content") or {}).get("raw")
        if raw is None:
            raise RestError("no content.raw in response (auth/context problem?)")
        return raw

    async def get_post_meta(self, post_id: int) -> dict:
        status, data = await self.c.request("GET", f"/wp-json/wpops/v1/meta?post_id={int(post_id)}")
        if status != 200 or not isinstance(data, dict) or "meta" not in data:
            raise RestError(f"get meta failed (HTTP {status}: {_err_detail(data)})")
        return data["meta"]

    # -- writes ---------------------------------------------------------------
    async def create_post(self, post_type: str, title: str, slug: str, status: str,
                          content: str, meta: dict) -> int:
        route = await self._route_async(post_type)
        st, data = await self.c.request("POST", f"/wp-json/wp/v2/{route}",
                                        json_body={"title": title, "slug": slug,
                                                   "status": status, "content": content})
        if st != 201 or not isinstance(data, dict) or "id" not in data:
            raise RestError(f"create failed (HTTP {st}: {_err_detail(data)})")
        post_id = int(data["id"])
        self._type_cache[post_id] = post_type
        if meta:
            # The core post already exists; if the plugin check or meta apply fails now,
            # name post_id in the error so the orphan draft is findable (not silently lost).
            try:
                await self.ensure_plugin()
                await self.set_post_meta(post_id, meta)
            except RestError as e:
                raise RestError(f"post {post_id} created but meta apply failed: {e}") from e
        return post_id

    async def set_post_meta(self, post_id: int, meta: dict) -> list[str]:
        st, data = await self.c.request("POST", "/wp-json/wpops/v1/meta",
                                        json_body={"post_id": int(post_id), "meta": meta})
        if st != 200 or not isinstance(data, dict) or "applied" not in data:
            raise RestError(f"meta apply failed (HTTP {st}: {_err_detail(data)})")
        return data["applied"]

    async def update_post_content(self, post_id: int, content: str) -> int:
        route = await self._route_for_id(post_id)
        st, data = await self.c.request("POST", f"/wp-json/wp/v2/{route}/{int(post_id)}",
                                        json_body={"content": content})
        if st != 200:
            raise RestError(f"update failed (HTTP {st}: {_err_detail(data)})")
        return int(post_id)

    async def update_post_slug(self, post_id: int, slug: str) -> dict:
        """Rename a post's slug; returns {"id", "slug", "link"}.

        WP core handles the SEO side by itself: saving a new slug records the old one in
        _wp_old_slug and wp_old_slug_redirect() 301s the old URL. It also UNIQUIFIES on
        collision (about -> about-2), so the returned slug is WP's actual one, not the
        requested one - callers compare the two and report the difference.
        """
        route = await self._route_for_id(post_id)
        st, data = await self.c.request("POST", f"/wp-json/wp/v2/{route}/{int(post_id)}",
                                        json_body={"slug": slug})
        if st != 200 or not isinstance(data, dict) or "slug" not in data:
            raise RestError(f"slug update failed (HTTP {st}: {_err_detail(data)})")
        return {"id": int(data.get("id") or post_id), "slug": data["slug"],
                "link": data.get("link", "")}

    async def delete_post(self, post_id: int) -> None:
        route = await self._route_for_id(post_id)
        st, data = await self.c.request("DELETE", f"/wp-json/wp/v2/{route}/{int(post_id)}",
                                        params={"force": "true"})
        if st != 200:
            raise RestError(f"delete failed (HTTP {st}: {_err_detail(data)})")

    async def duplicate_post(self, post_id: int) -> int:
        await self.ensure_plugin()
        st, data = await self.c.request("POST", "/wp-json/wpops/v1/duplicate",
                                        json_body={"post_id": int(post_id)})
        if st != 200 or not isinstance(data, dict) or "draft_id" not in data:
            raise RestError(f"duplicate failed (HTTP {st}: {_err_detail(data)})")
        draft_id = int(data["draft_id"])
        # A duplicate shares its source's post type; warm the draft's cache from the
        # source when known so the next edit cycle skips an /info round-trip.
        if int(post_id) in self._type_cache:
            self._type_cache[draft_id] = self._type_cache[int(post_id)]
        return draft_id

    async def purge_et_cache(self, post_id: int) -> None:
        await self.ensure_plugin()
        st, data = await self.c.request("POST", "/wp-json/wpops/v1/purge-cache",
                                        json_body={"post_id": int(post_id)})
        if st != 200:
            raise RestError(f"purge failed (HTTP {st}: {_err_detail(data)})")

    # -- media ----------------------------------------------------------------
    async def upload_media(self, filename: str, content: bytes, mime: str,
                           alt: str | None = None) -> dict:
        """Upload bytes to the media library; returns {"id", "url", "alt"}.

        Core REST only - no wp-ops-connect needed, so this works on any site with an
        app password. Callers validate the extension/size before getting here.
        """
        st, data = await self.c.upload("/wp-json/wp/v2/media", filename, content, mime)
        if st != 201 or not isinstance(data, dict) or "id" not in data or "source_url" not in data:
            raise RestError(f"media upload failed (HTTP {st}: {_err_detail(data)})")
        media_id = int(data["id"])
        if alt:
            # The attachment already exists; if the alt update fails now, name media_id
            # in the error so the orphan is findable (same pattern as create_post).
            st2, d2 = await self.c.request("POST", f"/wp-json/wp/v2/media/{media_id}",
                                           json_body={"alt_text": alt})
            if st2 != 200:
                raise RestError(f"media {media_id} uploaded but alt text failed "
                                f"(HTTP {st2}: {_err_detail(d2)})")
        return {"id": media_id, "url": data["source_url"], "alt": alt or ""}

    async def delete_media(self, media_id: int) -> None:
        # force=true is mandatory for attachments: WP refuses to trash them.
        st, data = await self.c.request("DELETE", f"/wp-json/wp/v2/media/{int(media_id)}",
                                        params={"force": "true"})
        if st != 200:
            raise RestError(f"media delete failed (HTTP {st}: {_err_detail(data)})")

    # -- menus ----------------------------------------------------------------
    # Core REST only (/wp/v2/menus + /wp/v2/menu-items): the app-password user has
    # edit_theme_options, so no wp-ops-connect version gate is needed here.
    async def list_menus(self) -> list[dict]:
        st, data = await self.c.request("GET", "/wp-json/wp/v2/menus",
                                        params={"context": "edit", "per_page": 100})
        if st != 200 or not isinstance(data, list):
            raise RestError(f"list menus failed (HTTP {st}: {_err_detail(data)})")
        return [{"id": m.get("id"), "name": m.get("name"), "slug": m.get("slug"),
                 "locations": list(m.get("locations") or [])} for m in data]

    async def list_menu_items(self, menu_id: int) -> list[dict]:
        st, data = await self.c.request("GET", "/wp-json/wp/v2/menu-items",
                                        params={"menus": int(menu_id), "per_page": 100,
                                                "context": "edit"})
        if st != 200 or not isinstance(data, list):
            raise RestError(f"list menu items failed (HTTP {st}: {_err_detail(data)})")
        return [_menu_item(i) for i in data]

    async def add_menu_item(self, menu_id: int, title: str, page_id: int | None = None,
                            url: str | None = None, parent: int = 0,
                            menu_order: int | None = None) -> dict:
        if (page_id is None) == (url is None):
            raise ValueError("exactly one of page_id or url is required")
        body = {"menus": int(menu_id), "title": title, "status": "publish",
                "parent": int(parent)}
        if page_id is not None:
            body.update({"type": "post_type", "object": "page", "object_id": int(page_id)})
        else:
            body.update({"type": "custom", "url": url})
        if menu_order is not None:
            body["menu_order"] = int(menu_order)  # omitted -> WP appends at the end
        st, data = await self.c.request("POST", "/wp-json/wp/v2/menu-items", json_body=body)
        if st != 201 or not isinstance(data, dict) or "id" not in data:
            raise RestError(f"add menu item failed (HTTP {st}: {_err_detail(data)})")
        return _menu_item(data)

    async def update_menu_item(self, item_id: int, fields: dict) -> dict:
        # Allow-list, not a silent filter: a typo'd key must fail loudly rather than
        # report success for a change that never reached the site.
        bad = [k for k in fields if k not in _MENU_ITEM_EDITABLE]
        if bad:
            raise ValueError(f"unsupported menu item field(s) {', '.join(sorted(bad))} - "
                             f"allowed: {', '.join(_MENU_ITEM_EDITABLE)}")
        st, data = await self.c.request("POST", f"/wp-json/wp/v2/menu-items/{int(item_id)}",
                                        json_body=dict(fields))
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"update menu item failed (HTTP {st}: {_err_detail(data)})")
        return _menu_item(data)

    async def delete_menu_item(self, item_id: int) -> None:
        # force=true: menu items have no meaningful trashed state.
        st, data = await self.c.request("DELETE", f"/wp-json/wp/v2/menu-items/{int(item_id)}",
                                        params={"force": "true"})
        if st != 200:
            raise RestError(f"delete menu item failed (HTTP {st}: {_err_detail(data)})")

    # -- settings -------------------------------------------------------------
    # Core REST only (/wp/v2/settings). Admin-only endpoint: a non-administrator app
    # password gets 403 rest_forbidden, which surfaces verbatim so the cause is obvious.
    # The gateway passes the whole payload through in both directions - the field
    # allowlist is an ops-layer policy, deliberately not duplicated here.
    async def get_settings(self) -> dict:
        st, data = await self.c.request("GET", "/wp-json/wp/v2/settings")
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"get settings failed (HTTP {st}: {_err_detail(data)})")
        return data

    async def update_settings(self, fields: dict) -> dict:
        # An empty POST would 200 with the current settings and read as a successful
        # write of nothing - refuse instead of reporting a change that never happened.
        if not fields:
            raise ValueError("no settings fields to update")
        st, data = await self.c.request("POST", "/wp-json/wp/v2/settings",
                                        json_body=dict(fields))
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"settings update failed (HTTP {st}: {_err_detail(data)})")
        return data

    # -- users ----------------------------------------------------------------
    # Core REST only (/wp/v2/users). context=edit is what makes WP return username,
    # email and roles - without it the rows come back public-shaped (slug only).
    # The app-password user needs list_users/edit_users/create_users/delete_users, i.e.
    # an administrator; anything less surfaces verbatim as a 403 rest_forbidden.
    async def list_users(self, search: str | None = None, per_page: int = 100) -> list[dict]:
        params = {"context": "edit", "per_page": int(per_page)}
        if search:
            params["search"] = search
        st, data = await self.c.request("GET", "/wp-json/wp/v2/users", params=params)
        if st != 200 or not isinstance(data, list):
            raise RestError(f"list users failed (HTTP {st}: {_err_detail(data)})")
        return [_user(u) for u in data]

    async def get_user(self, user_id: int) -> dict:
        st, data = await self.c.request("GET", f"/wp-json/wp/v2/users/{int(user_id)}",
                                        params={"context": "edit"})
        if st != 200 or not isinstance(data, dict) or "id" not in data:
            raise RestError(f"get user failed (HTTP {st}: {_err_detail(data)})")
        return _user(data)

    async def create_user(self, username: str, email: str, password: str,
                          roles: list | str | None = None,
                          name: str | None = None) -> dict:
        body = {"username": username, "email": email, "password": password}
        if roles is not None:
            # A bare "editor" is the natural call; list() on it would ship characters.
            body["roles"] = [roles] if isinstance(roles, str) else list(roles)
        if name is not None:
            body["name"] = name
        st, data = await self.c.request("POST", "/wp-json/wp/v2/users", json_body=body)
        if st != 201 or not isinstance(data, dict) or "id" not in data:
            raise RestError(f"create user failed (HTTP {st}: {_err_detail(data)})")
        return _user(data)

    async def update_user(self, user_id: int, fields: dict) -> dict:
        # Allow-list, not a silent filter: WP ignores unknown keys, so a typo'd field
        # would report success for a change that never happened.
        bad = [k for k in fields if k not in _USER_EDITABLE]
        if bad:
            raise ValueError(f"unsupported user field(s) {', '.join(sorted(bad))} - "
                             f"allowed: {', '.join(_USER_EDITABLE)}")
        st, data = await self.c.request("POST", f"/wp-json/wp/v2/users/{int(user_id)}",
                                        json_body=dict(fields))
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"update user failed (HTTP {st}: {_err_detail(data)})")
        return _user(data)

    async def delete_user(self, user_id: int, reassign: int = 0) -> dict:
        """Permanently delete a user. WP core REST refuses this without BOTH force=true
        and reassign - the user's posts must go somewhere. reassign=0 deletes their
        content with them; any other id transfers authorship to that user."""
        st, data = await self.c.request("DELETE", f"/wp-json/wp/v2/users/{int(user_id)}",
                                        params={"force": "true", "reassign": int(reassign)})
        if st != 200:
            raise RestError(f"delete user failed (HTTP {st}: {_err_detail(data)})")
        prev = data.get("previous") if isinstance(data, dict) else None
        return {"deleted": True,
                "previous": prev.get("id") if isinstance(prev, dict) else None}

    # -- comments --------------------------------------------------------------
    # Core REST only (/wp/v2/comments). Statuses are WP's own strings: approved, hold,
    # spam, trash. context=edit returns content.raw (the unrendered comment text).
    async def list_comments(self, post_id: int | None = None, status: str | None = None,
                            per_page: int = 100) -> list[dict]:
        params = {"context": "edit", "per_page": int(per_page)}
        if post_id is not None:
            params["post"] = int(post_id)
        if status:
            params["status"] = status
        st, data = await self.c.request("GET", "/wp-json/wp/v2/comments", params=params)
        if st != 200 or not isinstance(data, list):
            raise RestError(f"list comments failed (HTTP {st}: {_err_detail(data)})")
        return [_comment(c) for c in data]

    async def update_comment(self, comment_id: int, fields: dict) -> dict:
        bad = [k for k in fields if k not in _COMMENT_EDITABLE]
        if bad:
            raise ValueError(f"unsupported comment field(s) {', '.join(sorted(bad))} - "
                             f"allowed: {', '.join(_COMMENT_EDITABLE)}")
        st, data = await self.c.request("POST", f"/wp-json/wp/v2/comments/{int(comment_id)}",
                                        json_body=dict(fields))
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"update comment failed (HTTP {st}: {_err_detail(data)})")
        return _comment(data)

    async def delete_comment(self, comment_id: int, force: bool = False) -> dict:
        """Trash a comment (recoverable) or, with force, delete it permanently.
        Returns {"deleted", "id", "status"} either way: WP answers a trash with the
        comment itself (status "trash") and a force with {"deleted", "previous"}."""
        st, data = await self.c.request("DELETE", f"/wp-json/wp/v2/comments/{int(comment_id)}",
                                        params={"force": "true"} if force else None)
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"delete comment failed (HTTP {st}: {_err_detail(data)})")
        if force:
            prev = data.get("previous")
            prev_id = prev.get("id") if isinstance(prev, dict) else None
            return {"deleted": True, "id": prev_id or int(comment_id), "status": "deleted"}
        return {"deleted": False, "id": data.get("id", int(comment_id)),
                "status": data.get("status", "trash")}

    # -- options ---------------------------------------------------------------
    # Arbitrary wp_options through wp-ops-connect's /option route (core REST's
    # /wp/v2/settings only reaches registered options). One route, four verbs, gated
    # server-side on manage_options. Values are arbitrary JSON and pass through
    # untouched in both directions - update_option() does the serialization.
    # Callers gate on ensure_options_capable() first; these stay thin like the meta/acf
    # methods so the ops layer owns the preview/prod policy.
    async def list_option_names(self) -> list[str]:
        # No name param at all is what makes the plugin list rather than read. The list
        # is autoloaded names only (wp_load_alloptions); non-autoloaded options are
        # still readable by name through get_option.
        st, data = await self.c.request("GET", _OPTION_ROUTE)
        # isinstance(list), not just presence: list() over a stray string would return
        # one entry per character and read as a plausible - but wrong - option list.
        if st != 200 or not isinstance(data, dict) or not isinstance(data.get("names"), list):
            raise RestError(f"list options failed (HTTP {st}: {_err_detail(data)})")
        return list(data["names"])

    async def get_option(self, name: str) -> dict:
        """Read one option; returns {"name", "value", "exists"}.

        `exists` is the only absence signal: an option may legitimately hold ""/0/false,
        so a falsy value says nothing (the plugin uses a sentinel default to tell the
        two apart). value is None when the option is not set.
        """
        st, data = await self.c.request("GET", _OPTION_ROUTE, params={"name": name})
        if st != 200 or not isinstance(data, dict) or "exists" not in data or "value" not in data:
            raise RestError(f"get option failed (HTTP {st}: {_err_detail(data)})")
        return {"name": data.get("name", name), "value": data["value"],
                "exists": bool(data["exists"])}

    async def set_option(self, name: str, value) -> dict:
        """Write ANY option (including siteurl/home - no allowlist). Returns
        {"name", "value"} where value is what the site actually stored, re-read after
        the write: update_option() returns false for an unchanged value, so the stored
        value - not the plugin's return - is the only honest confirmation."""
        st, data = await self.c.request("POST", _OPTION_ROUTE,
                                        json_body={"name": name, "value": value})
        if st != 200 or not isinstance(data, dict) or "value" not in data:
            raise RestError(f"set option failed (HTTP {st}: {_err_detail(data)})")
        return {"name": data.get("name", name), "value": data["value"]}

    async def delete_option(self, name: str) -> bool:
        """Delete an option; True when a row was removed, False when there was nothing
        to remove (delete_option() returns false for an absent option - not an error).

        name goes in the QUERY STRING, not a JSON body. The transport would happily send
        a DELETE body (httpx sets content-type: application/json, which WP's get_param
        reads), but DELETE bodies are the shape most often dropped by CDNs/proxies in
        front of the fleet, and $req->get_param('name') always sees the query string.
        Every other DELETE in this gateway passes params for the same reason.
        """
        st, data = await self.c.request("DELETE", _OPTION_ROUTE, params={"name": name})
        if st != 200 or not isinstance(data, dict) or "deleted" not in data:
            raise RestError(f"delete option failed (HTTP {st}: {_err_detail(data)})")
        return bool(data["deleted"])

    # -- acf ------------------------------------------------------------------
    # ACF (Advanced Custom Fields) values through wp-ops-connect's dedicated /acf route,
    # which calls ACF's own get_fields/update_field so field-key linkage stays correct
    # (raw post meta would orphan the field key). Presence-gated, not version-gated:
    # ACF free and Pro both qualify. Values are arbitrary JSON (strings, numbers, bools,
    # arrays for repeaters/galleries) and pass through untouched.
    async def ensure_acf_capable(self) -> dict:
        """ensure_plugin() plus a presence gate on ACF being active. info.get("acf") is
        ACF_VERSION (or True) when ACF is loaded and None when it is not - a presence
        check, not a version floor. Returns the cached info like ensure_seo_capable."""
        info = await self.ensure_plugin()
        if not info.get("acf"):
            raise RestError("ACF not active on this site (wp-ops-connect reports acf=null)")
        return info

    async def get_acf_fields(self, post_id: int) -> dict:
        # data["acf"] is the field map (an object; {} when the post has no ACF values).
        # A 501 wpops_acf_inactive body (ACF uninstalled) surfaces here via _err_detail.
        status, data = await self.c.request("GET", f"/wp-json/wpops/v1/acf?post_id={int(post_id)}")
        if status != 200 or not isinstance(data, dict) or "acf" not in data:
            raise RestError(f"get acf failed (HTTP {status}: {_err_detail(data)})")
        return data["acf"]

    async def set_acf_fields(self, post_id: int, fields: dict) -> dict:
        # The plugin writes each field via ACF's update_field then re-reads. "skipped"
        # holds keys it refused (leading-underscore field keys) - pass it through so ops
        # can report them instead of claiming a silent success. A 400 (bad fields) or a
        # 501 (ACF inactive) surfaces as a RestError via _err_detail.
        st, data = await self.c.request("POST", "/wp-json/wpops/v1/acf",
                                        json_body={"post_id": int(post_id), "fields": fields})
        if st != 200 or not isinstance(data, dict) or "applied" not in data:
            raise RestError(f"acf apply failed (HTTP {st}: {_err_detail(data)})")
        return {"applied": data["applied"], "skipped": data.get("skipped", []),
                "acf": data["acf"]}

    # -- taxonomies + terms ----------------------------------------------------
    # Core REST only. Every term route is DISCOVERED from /wp/v2/taxonomies, never
    # built from the taxonomy slug: WP's two built-ins both differ (category ->
    # /categories, post_tag -> /tags) and a CPT taxonomy may declare any rest_base.
    # The same rest_base is also the taxonomy's FIELD NAME on a post object, which is
    # what set_post_terms writes.
    async def list_taxonomies(self) -> list[dict]:
        """Every REST-enabled taxonomy: rows `{slug, name, rest_base, hierarchical,
        types}`. Cached per gateway instance (`self._tax_cache`) - the map is stable
        for a session and every term call would otherwise re-fetch it."""
        if self._tax_cache is None:
            st, data = await self.c.request("GET", _TAXONOMIES_ROUTE,
                                            params={"context": "edit"})
            if st != 200 or not isinstance(data, dict):
                raise RestError(f"list taxonomies failed (HTTP {st}: {_err_detail(data)})")
            tax = {}
            for slug, row in data.items():
                if not isinstance(row, dict):
                    continue
                tax[slug] = {"slug": slug, "name": row.get("name", slug),
                             "rest_base": row.get("rest_base") or slug,
                             "hierarchical": bool(row.get("hierarchical")),
                             "types": list(row.get("types") or [])}
            self._tax_cache = tax
        return list(self._tax_cache.values())

    async def _tax_rest_base(self, taxonomy: str) -> str:
        """The taxonomy's rest_base - its term route AND its field name on a post.
        RestError naming the slug (and the ones that do exist) when it is unknown."""
        await self.list_taxonomies()
        info = (self._tax_cache or {}).get(taxonomy)
        if info is None:
            raise RestError(f"taxonomy {taxonomy!r} is not exposed over REST on this "
                            f"site (available: "
                            f"{', '.join(sorted(self._tax_cache or {})) or 'none'})")
        return info["rest_base"]

    async def list_terms(self, taxonomy: str, search: str | None = None,
                         per_page: int = 100) -> list[dict]:
        # No context=edit: every mapped field is in the default view context, and edit
        # would demand edit_terms for what is a read-only call.
        base = await self._tax_rest_base(taxonomy)
        params = {"per_page": int(per_page)}
        if search:
            params["search"] = search
        st, data = await self.c.request("GET", f"/wp-json/wp/v2/{base}", params=params)
        if st != 200 or not isinstance(data, list):
            raise RestError(f"list terms failed (HTTP {st}: {_err_detail(data)})")
        return [_term(t, taxonomy) for t in data]

    async def create_term(self, taxonomy: str, name: str, slug: str | None = None,
                          parent: int | None = None,
                          description: str | None = None) -> dict:
        base = await self._tax_rest_base(taxonomy)
        body = {"name": name}
        # Omit the optionals rather than send nulls: WP would reject parent=None on a
        # hierarchical taxonomy and overwrite a slug with "" on a later update.
        if slug is not None:
            body["slug"] = slug
        if parent is not None:
            body["parent"] = int(parent)
        if description is not None:
            body["description"] = description
        st, data = await self.c.request("POST", f"/wp-json/wp/v2/{base}", json_body=body)
        if st != 201 or not isinstance(data, dict) or "id" not in data:
            raise RestError(f"create term failed (HTTP {st}: {_err_detail(data)})")
        return _term(data, taxonomy)

    async def update_term(self, taxonomy: str, term_id: int, fields: dict) -> dict:
        # Allow-list first, before any HTTP: WP ignores unknown keys, so a typo'd field
        # would report success for a change that never happened.
        bad = [k for k in fields if k not in _TERM_EDITABLE]
        if bad:
            raise ValueError(f"unsupported term field(s) {', '.join(sorted(bad))} - "
                             f"allowed: {', '.join(_TERM_EDITABLE)}")
        base = await self._tax_rest_base(taxonomy)
        st, data = await self.c.request("POST", f"/wp-json/wp/v2/{base}/{int(term_id)}",
                                        json_body=dict(fields))
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"update term failed (HTTP {st}: {_err_detail(data)})")
        return _term(data, taxonomy)

    async def delete_term(self, taxonomy: str, term_id: int) -> dict:
        """Permanently delete a term (and unassign it from every post). force=true is
        mandatory: terms have no trash state and WP refuses the call without it."""
        base = await self._tax_rest_base(taxonomy)
        st, data = await self.c.request("DELETE", f"/wp-json/wp/v2/{base}/{int(term_id)}",
                                        params={"force": "true"})
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"delete term failed (HTTP {st}: {_err_detail(data)})")
        prev = data.get("previous")
        return {"deleted": True,
                "term": prev.get("id") if isinstance(prev, dict) else None}

    async def set_post_terms(self, post_id: int, taxonomy: str,
                             term_ids: list) -> dict:
        """Assign a post's terms for one taxonomy (replaces, not appends).

        This is a write to the POST object - `{"tags": [8, 9]}` on /wp/v2/posts/5 -
        keyed by the taxonomy's rest_base, not a term-side call. WP silently drops ids
        the taxonomy does not own, so `terms` is what it actually stored.
        """
        base = await self._tax_rest_base(taxonomy)
        route = await self._route_for_id(post_id)
        st, data = await self.c.request("POST", f"/wp-json/wp/v2/{route}/{int(post_id)}",
                                        json_body={base: [int(t) for t in term_ids]})
        if st != 200 or not isinstance(data, dict):
            raise RestError(f"set post terms failed (HTTP {st}: {_err_detail(data)})")
        return {"post_id": int(post_id), "taxonomy": taxonomy,
                "terms": data.get(base, [])}

    # -- plugins ---------------------------------------------------------------
    # Core REST only (/wp/v2/plugins, WP 5.5+). The app-password user needs
    # activate_plugins; anything less surfaces verbatim as a 403 rest_cannot_*.
    # Deliberately NOT cached: activating a plugin changes the very inventory these
    # reads report, and a stale row would make a readback "confirm" a write that never
    # landed. Every call is a fresh round-trip.
    async def list_plugins(self) -> list[dict]:
        """Every installed plugin, active or not. context=edit is what returns the
        header fields (version, requires_wp, textdomain) alongside the status."""
        st, data = await self.c.request("GET", _PLUGINS_ROUTE, params={"context": "edit"})
        if st != 200 or not isinstance(data, list):
            raise _plugin_error("list plugins", st, data)
        return [_plugin(p) for p in data if isinstance(p, dict)]

    async def get_plugin(self, plugin_id: str) -> dict:
        """One plugin by REST id (`akismet/akismet`). The id's slash is sent raw by
        _plugin_path - see its docstring for why that is load-bearing."""
        st, data = await self.c.request("GET", _plugin_path(plugin_id),
                                        params={"context": "edit"})
        if st != 200 or not isinstance(data, dict) or "plugin" not in data:
            raise _plugin_error(f"get plugin {plugin_id!r}", st, data)
        return _plugin(data)

    async def set_plugin_status(self, plugin_id: str, status: str) -> dict:
        """Activate or deactivate one plugin; returns the plugin row WP reports after
        the change.

        DEACTIVATING A LIVE PLUGIN CAN TAKE SITE FUNCTIONALITY OFFLINE (forms, caching,
        security, page builders) the instant this returns. The ops layer owns the
        dry-run preview and the readback; this method just performs the write.
        """
        # Validate before any HTTP: WP would 400 a bad status, but only after a
        # round-trip, and a typo ("activate") must never reach a live site as an
        # ambiguous write.
        if status not in _PLUGIN_STATUSES:
            raise ValueError(f"status must be one of {', '.join(_PLUGIN_STATUSES)} - "
                             f"got {status!r}")
        st, data = await self.c.request("POST", _plugin_path(plugin_id),
                                        json_body={"status": status})
        if st != 200 or not isinstance(data, dict) or "plugin" not in data:
            raise _plugin_error(f"set plugin {plugin_id!r} to {status}", st, data)
        return _plugin(data)

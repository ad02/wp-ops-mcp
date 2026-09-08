"""Navigation management: resolve a menu, list it, add/update/remove an item.

Two disciplines are load-bearing here, because navigation is the most visible thing
on a client site and a wrong edit is a wrong edit on every page at once:

  1. **Single-match resolution.** ``menu`` accepts an id, an exact name or an exact
     slug; 0 or 2+ matches is a ``MenuError`` that LISTS the available menus rather
     than a guess. Sites routinely carry "Main Menu" plus a near-duplicate footer or
     mobile nav - picking one on a prefix/case hunch is how the wrong nav gets edited.
  2. **Targets are validated before the gateway.** A menu item points at exactly one
     thing: a page id or a custom URL. Empty-ish targets (``0``, ``""``, ``"  "``,
     ``True``, a numeric *string*) are refused here, so they never reach the wire -
     WP would happily create an item pointing at nothing, and a blank nav entry on a
     live site is indistinguishable from a broken theme.

Like SeoOps/MediaOps, every public method NEVER raises: anticipated failures and
unanticipated ones alike come back as ``{"action": "error", "error": "<Type>: <msg>"}``
so an MCP tool returns a value instead of unwinding. ``resolve_menu`` is the one
exception - it is the internal primitive and raises ``MenuError``, which the public
methods convert.

Writes are verified: ``add`` re-reads the menu and confirms the new item id is really
in it before reporting success (WP can 201 an item that a plugin then filters out).
``update`` needs no extra read - WP's 200 response IS the stored row after the write,
and the gateway maps it, so what comes back is the item as it now exists.
"""
from __future__ import annotations


class MenuError(RuntimeError):
    """Unresolvable menu reference or an invalid menu-item target."""


def _listing(menus: list[dict]) -> str:
    """"Name (id), Name (id)" - what the operator can pick from, in one line."""
    return ", ".join(f"{m.get('name')} ({m.get('id')})" for m in menus) or "(none)"


def _positive_id(value, field: str) -> int:
    """A WordPress object id: a genuine positive int.

    ``bool`` is excluded explicitly - it subclasses int, so ``True`` would otherwise
    pass as id 1 and silently target a real menu/page.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MenuError(f"{field} must be a positive int, got {value!r}")
    return value


def _url(value) -> str:
    """A custom-link destination: a genuine non-empty string.

    Shared by ``add`` (via ``_target``) and ``update`` so retargeting an existing item
    is held to the same rule as creating one - a blank nav entry on a live site is
    indistinguishable from a broken theme either way.
    """
    if not isinstance(value, str) or not value.strip():
        raise MenuError(f"url must be a non-empty string, got {value!r}")
    return value


def _target(page_id, url) -> dict:
    """The one thing this item points at, validated: {"page_id": n} or {"url": s}."""
    if (page_id is None) == (url is None):
        raise MenuError("exactly one of page_id or url is required "
                        "(page_id for a page on the site, url for a custom link)")
    if page_id is not None:
        return {"page_id": _positive_id(page_id, "page_id")}
    return {"url": _url(url)}


class MenuOps:
    """Orchestrates menu reads/writes over a REST content gateway.

    The gateway must expose ``list_menus`` / ``list_menu_items`` / ``add_menu_item`` /
    ``update_menu_item`` / ``delete_menu_item`` (RestContentGateway does; the SSH/wpcli
    gateway does not - the tool layer refuses that transport before getting here).
    """

    def __init__(self, gateway):
        self.gw = gateway

    async def resolve_menu(self, menu: int | str) -> dict:
        """Resolve an id / exact name / exact slug to exactly one menu dict.

        Raises MenuError (listing the available menus) on 0 or 2+ matches. Anything
        that is not an int or str - including a bool - matches nothing and gets the
        same listing, so a malformed reference reads as "here is what exists".
        """
        menus = await self.gw.list_menus()
        if isinstance(menu, bool):
            matches = []                      # True == 1: never let it mean "menu 1"
        elif isinstance(menu, int):
            matches = [m for m in menus if m.get("id") == menu]
        elif isinstance(menu, str):
            matches = [m for m in menus if menu in (m.get("name"), m.get("slug"))]
        else:
            matches = []
        if len(matches) == 1:
            return matches[0]
        got = "no menu matched" if not matches else f"{len(matches)} menus match"
        raise MenuError(f"{got} {menu!r} - available menus: {_listing(menus)}")

    async def list(self) -> dict:
        """All menus (id, name, slug, locations). Items are NOT fetched - one call.

        Carries "action": "ok" like every other successful read in this server: a client
        that switches on result["action"] must not hit a KeyError on this one tool.
        """
        try:
            return {"action": "ok", "menus": await self.gw.list_menus()}
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

    async def add(self, menu: int | str, title: str, page_id: int | None = None,
                  url: str | None = None, parent: int = 0, position: int | None = None,
                  dry_run: bool = True) -> dict:
        """Add one item to a menu; ``position`` is the item's menu_order.

        Order is deliberate: the target is validated FIRST (an invalid one never
        reaches the gateway, in dry-run and for real alike, so a preview cannot
        promise a write that would fail), then the menu is resolved, then the write
        happens and is confirmed by reading the menu back.
        """
        try:
            target = _target(page_id, url)
            m = await self.resolve_menu(menu)
            if dry_run:
                return {"action": "preview", "menu": m["name"], "menu_id": m["id"],
                        "item": {"title": title, **target,
                                 "parent": parent, "position": position}}
            # **target, not the raw params: the VALIDATED target is the only thing
            # that reaches the wire, and it carries exactly one of page_id/url.
            item = await self.gw.add_menu_item(m["id"], title, **target,
                                               parent=parent, menu_order=position)
            # Readback: a 201 is the site's intent, not proof. Confirm the id is in
            # the menu before telling the operator their nav changed.
            items = await self.gw.list_menu_items(m["id"])
            if not any(i.get("id") == item.get("id") for i in items):
                return {"action": "error",
                        "error": f"MenuReadbackMissing: item {item.get('id')} is not in "
                                 f"menu {m['name']} ({m['id']}) after the add"}
            return {"action": "added", "menu": m["name"], "item": item, "verified": True}
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

    async def update(self, item_id: int, title: str | None = None,
                     position: int | None = None, parent: int | None = None,
                     url: str | None = None, dry_run: bool = True) -> dict:
        """Change one existing menu item in place: rename, reposition, re-nest, retarget.

        Partial by design: ``None`` means "leave this alone" and only the params that
        were actually passed reach the wire, so renaming an item cannot silently reset
        its position. ``None`` is the ONLY way to say "unchanged" - ``position=0``
        (first slot) and ``parent=0`` (un-nest to top level) are real edits and are
        sent. Refusing an all-``None`` call matters: WP answers an empty POST with 200
        and the unchanged row, which would read as a successful edit that never
        happened.
        """
        try:
            _positive_id(item_id, "item_id")
            fields = {}
            if title is not None:
                fields["title"] = title
            if position is not None:
                fields["menu_order"] = position   # caller-facing name for menu_order
            if parent is not None:
                fields["parent"] = parent
            if url is not None:
                fields["url"] = _url(url)
            if not fields:
                raise MenuError("no fields provided (title/position/parent/url)")
            if dry_run:
                return {"action": "preview", "item_id": item_id, "fields": fields}
            return {"action": "updated",
                    "item": await self.gw.update_menu_item(item_id, fields)}
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

    async def remove(self, item_id: int, dry_run: bool = True) -> dict:
        """Delete one menu item by id (the linked page itself is untouched)."""
        try:
            _positive_id(item_id, "item_id")
            if dry_run:
                return {"action": "preview", "item_id": item_id}
            await self.gw.delete_menu_item(item_id)
            return {"action": "removed", "item_id": item_id}
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

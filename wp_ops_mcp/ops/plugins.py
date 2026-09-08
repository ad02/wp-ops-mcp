"""Plugin operations: read the installed inventory, activate and deactivate.

A plugin toggle is the highest-leverage single call in this whole server - one POST can
take a site's forms, caching, security or page builder offline - so three disciplines
carry the weight here:

  1. **Single-match resolution.** A plugin's REST id is its "plugin file"
     (``akismet/akismet``), but callers naturally reach for a slug (``akismet``) or the
     full path (``akismet/akismet.php``). All three normalize to the one id, matched in
     descending order of precision: exact REST id, then folder segment, then textdomain.
     A tier that matches 2+ plugins is a ``PluginError`` LISTING them, never a guess -
     two SEO plugins can ship the same textdomain, and toggling the wrong one is
     invisible until something stops working.
  2. **The self-lockout guard.** ``wp-ops-connect`` is the control-plane plugin this
     tool reaches the site through. Deactivating it would sever that connection, and
     nothing here could re-activate it afterwards - the tool would have to be repaired
     by hand in wp-admin. So ``deactivate`` refuses it, twice: once on the raw
     identifier (so the refusal costs no request at all) and once on the resolved id
     (so reaching it by textdomain is caught too). This is a correctness property, not
     a policy gate: a tool that can permanently disable itself mid-operation is broken.
     ``activate`` is deliberately NOT guarded - re-activating it can only restore the
     connection.
  3. **Writes are read back.** WP answers the status POST with a row of its own making;
     the status is re-read with a fresh ``get_plugin`` and compared. A disagreement is
     an error carrying ``write_landed`` (plus the resolved ``plugin`` id) - the POST did
     reach the site, and an operator must not be told "nothing happened" when something
     did. The same holds when the readback itself FAILS - a timeout or a 5xx after the
     write landed is still a changed site, so that error carries ``write_landed`` too.

Already-in-the-target-state is a ``noop``, not an error: asking for a state the site is
already in is a legitimate call and reporting it as a failure would make retries unsafe.

Like SeoOps/MenuOps/TermOps, every public method NEVER raises: anticipated failures and
unanticipated ones alike come back as ``{"action": "error", "error": "<Type>: <msg>"}``
so an MCP tool returns a value instead of unwinding. ``resolve`` is the one exception -
it is the internal primitive and raises ``PluginError``, which the public methods convert.
"""
from __future__ import annotations

# The MCP's own control plane. Matched as a SUBSTRING of the identifier and of the
# resolved id/folder, so `wp-ops-connect`, `wp-ops-connect/wp-ops-connect` and
# `wp-ops-connect/wp-ops-connect.php` are all caught - as is any casing of them.
SELF_PLUGIN = "wp-ops-connect"

_LISTING_CAP = 20        # installed-plugin listings are for reading, not for completeness


class PluginError(RuntimeError):
    """Unresolvable, ambiguous or malformed plugin reference."""


class PluginSelfLockout(PluginError):
    """Refused: the call would deactivate this tool's own control-plane plugin."""


def _error(e: Exception, **extra) -> dict:
    return {"action": "error", "error": f"{type(e).__name__}: {e}", **extra}


def _identifier(value) -> str:
    """A plugin reference: a genuine non-empty string, with any ``.php`` suffix removed.

    WP's REST id never carries the extension (``akismet/akismet``), so stripping it here
    means the caller's most natural form - the path they see in wp-admin - resolves like
    every other. ``bool`` is excluded with the rest of the non-strings.
    """
    if not isinstance(value, str) or not value.strip():
        raise PluginError(f"plugin must be a non-empty string, got {value!r}")
    ident = value.strip()
    if ident.lower().endswith(".php"):
        ident = ident[:-4]
    if not ident:
        raise PluginError(f"plugin must be a non-empty string, got {value!r}")
    return ident


def _is_self(value: str) -> bool:
    """Does this identifier or REST id refer to the control-plane plugin?"""
    return SELF_PLUGIN in str(value).lower()


_LOCKOUT_MSG = (
    "refusing to deactivate {ref} - " + SELF_PLUGIN + " is the control-plane plugin "
    "this tool reaches the site through. Deactivating it would sever that connection "
    "and no tool here could re-activate it; it would have to be re-enabled by hand in "
    "wp-admin"
)


def _effect(row: dict, current, target: str) -> str:
    """The preview sentence: which plugin, what it is now, what would change."""
    verb = "activated" if target == "active" else "deactivated"
    tail = (" - activating an untested plugin can break a live site"
            if target == "active" else
            " - this can take site functionality offline (forms, caching, security, "
            "page builders) the instant it lands")
    return (f"{row.get('name')} ({row.get('plugin')}) is currently {current}; "
            f"it would be {verb}{tail}")


def _listing(plugins: list[dict]) -> str:
    """"a/a, b/b, ... (+N more)" - what the operator can pick from, bounded.

    A fleet site runs 30-60 plugins; an unbounded listing turns a one-line "no such
    plugin" into a wall of text nobody reads. The cap keeps the actionable part visible
    and says how much was elided.
    """
    ids = [str(p.get("plugin")) for p in plugins]
    if not ids:
        return "(none)"
    shown = ", ".join(ids[:_LISTING_CAP])
    if len(ids) > _LISTING_CAP:
        shown += f" (+{len(ids) - _LISTING_CAP} more - run wp_list_plugins for the rest)"
    return shown


class PluginOps:
    """Orchestrates plugin reads and activation writes over a REST content gateway.

    The gateway must expose ``list_plugins`` / ``get_plugin`` / ``set_plugin_status``
    (RestContentGateway does; the SSH/wpcli gateway does not - the tool layer refuses
    that transport before getting here). Nothing here builds a URL: the encoding of the
    plugin id (whose ``/`` must stay raw) lives in the gateway.
    """

    def __init__(self, gateway):
        self.gw = gateway

    # -- resolution ------------------------------------------------------------
    async def _resolve_row(self, identifier) -> dict:
        """Resolve an identifier to exactly one plugin ROW (the id plus its status).

        The row is what activate/deactivate need anyway - the current status comes from
        this same listing, so a toggle costs one read, not two.
        """
        ident = _identifier(identifier)
        folder = ident.split("/")[0].lower()
        low = ident.lower()
        plugins = await self.gw.list_plugins()
        # Descending precision. The first tier that matches ANYTHING decides: a tier
        # with 2+ hits is ambiguous and refused rather than falling through to a
        # vaguer tier that might happen to have exactly one.
        for tier in (
            lambda p: str(p.get("plugin", "")).lower() == low,
            lambda p: str(p.get("plugin", "")).split("/")[0].lower() == folder,
            lambda p: str(p.get("textdomain", "")).lower() == low,
        ):
            matches = [p for p in plugins if tier(p)]
            if len(matches) == 1:
                return matches[0]
            if matches:
                cands = ", ".join(str(p.get("plugin")) for p in matches)
                raise PluginError(f"{len(matches)} plugins match {identifier!r} - "
                                  f"pass the exact plugin id: {cands}")
        raise PluginError(f"no installed plugin matches {identifier!r} - "
                          f"installed: {_listing(plugins)}")

    async def resolve(self, identifier) -> str:
        """Resolve a slug / folder-file / .php path to exactly one plugin REST id.

        Raises PluginError (listing the installed plugins, or the candidates) on 0 or
        2+ matches. This is the internal primitive - the public methods convert it.
        """
        return str((await self._resolve_row(identifier)).get("plugin"))

    # -- reads -----------------------------------------------------------------
    async def list_plugins(self) -> dict:
        """Every installed plugin (id, name, status, version, requirements). Read-only.

        ``active``/``inactive`` are counted from the reported status rather than derived
        from each other, so a multisite "network-active" row inflates neither count.
        """
        try:
            rows = await self.gw.list_plugins()
            return {"action": "ok", "count": len(rows), "plugins": rows,
                    "active": sum(1 for r in rows if r.get("status") == "active"),
                    "inactive": sum(1 for r in rows if r.get("status") == "inactive")}
        except Exception as e:
            return _error(e)

    # -- writes ----------------------------------------------------------------
    async def _apply(self, identifier, target: str, dry_run: bool) -> dict:
        """Resolve -> noop check -> preview -> write -> readback. Shared by both verbs.

        ``wrote`` is the same flag AdminOps.set_option / SiteOps.set_setting carry: once
        the status POST returns, the site HAS changed, so EVERY exit from here on - a
        status disagreement AND a readback that times out or 5xxs - has to say
        ``write_landed``. It is handled here rather than in activate/deactivate because
        this is where the write lives; their handlers stay the catch-all for everything
        before it (resolution, the lockout refusal, a bad identifier), which raises with
        ``wrote`` still False and so is reported unchanged.
        """
        wrote = False
        try:
            row = await self._resolve_row(identifier)
            plugin_id = str(row.get("plugin"))
            if _is_self(plugin_id) and target == "inactive":
                # Second guard: the raw identifier was innocent (a textdomain, say) but
                # it resolved to the control plane. One read has happened; no write will.
                raise PluginSelfLockout(_LOCKOUT_MSG.format(ref=plugin_id))
            current = row.get("status")
            if current == target:
                return {"action": "noop", "plugin": row, "reason": f"already {target}"}
            if dry_run:
                return {"action": "preview", "plugin": row, "current_status": current,
                        "target_status": target, "effect": _effect(row, current, target)}
            await self.gw.set_plugin_status(plugin_id, target)
            wrote = True
            # Readback: WP's 200 is the row IT built from the write it thinks it made. A
            # must-use plugin or a fatal in the plugin's own activation hook can leave the
            # real status elsewhere, so the verdict comes from a fresh read.
            after = await self.gw.get_plugin(plugin_id)
            if after.get("status") != target:
                # `plugin` is the resolved id here, not the row: on an error path the row
                # is what is in doubt, and the id is what an operator reverts by.
                return {"action": "error", "plugin": plugin_id, "write_landed": True,
                        "error": f"PluginStatusMismatch: requested {target}, site reports "
                                 f"{after.get('status')!s} for {plugin_id}"}
            return {"action": "activated" if target == "active" else "deactivated",
                    "plugin": after, "verified": True}
        except Exception as e:
            if not wrote:
                raise                    # nothing changed - the caller reports it plainly
            # The toggle already landed; an error that does not say so tells an operator
            # "nothing happened" about a site whose plugin just went on or offline.
            return _error(e, plugin=plugin_id, write_landed=True)

    async def activate(self, identifier, dry_run: bool = True) -> dict:
        """Activate one installed plugin, verified by reading its status back.

        ACTIVATING AN UNTESTED PLUGIN CAN BREAK A LIVE SITE: activation hooks run
        immediately and can fatal, and a plugin can start filtering output the moment it
        loads. Not guarded for ``wp-ops-connect`` - see the module docstring.
        """
        try:
            return await self._apply(identifier, "active", dry_run)
        except Exception as e:
            return _error(e)

    async def deactivate(self, identifier, dry_run: bool = True) -> dict:
        """Deactivate one installed plugin, verified by reading its status back.

        DEACTIVATING A LIVE PLUGIN CAN TAKE SITE FUNCTIONALITY OFFLINE (forms, caching,
        security, page builders) the instant it lands. ``wp-ops-connect`` is refused
        outright, before any request: it is the control plane this tool speaks through.
        """
        try:
            ident = _identifier(identifier)
            # First guard, before ANY gateway call: the refusal must not depend on the
            # site being reachable, and it must cost nothing.
            if _is_self(ident):
                raise PluginSelfLockout(_LOCKOUT_MSG.format(ref=ident))
            return await self._apply(ident, "inactive", dry_run)
        except Exception as e:
            return _error(e)

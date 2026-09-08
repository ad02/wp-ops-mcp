"""Unit tests for PluginOps (ops/plugins.py): plugin inventory + activation toggles.

Fakes at the gateway boundary (RestContentGateway's plugin surface, Task 1) so the
tests pin the three disciplines that keep a plugin toggle honest:

  1. **Single-match resolution.** A caller will pass `akismet`, `akismet/akismet` or
     `akismet/akismet.php` for the same plugin; all three must land on the one REST id,
     and a slug that matches two installed plugins is refused with the candidates rather
     than guessed at - toggling the wrong plugin on a live site is invisible until a
     form or a cache stops working.
  2. **The self-lockout guard.** `wp-ops-connect` is the control plane this tool reaches
     the site through. Deactivating it would sever that connection and nothing here
     could re-activate it, so the refusal happens BEFORE any gateway call - the fake's
     `calls` list is the assertion surface for that.
  3. **Writes are read back.** WordPress answers a status POST with the row it says it
     stored; the status is confirmed by a fresh get_plugin, and a disagreement is an
     error carrying `write_landed` (the POST reached the site) rather than a false
     success.
"""
import pytest

from wp_ops_mcp.ops.plugins import PluginError, PluginOps

PLUGINS = [
    {"plugin": "akismet/akismet", "name": "Akismet Anti-Spam", "status": "inactive",
     "version": "5.3", "network_only": False, "requires_wp": "5.8",
     "requires_php": "5.6", "textdomain": "akismet", "author": "Automattic"},
    {"plugin": "advanced-custom-fields/acf", "name": "ACF", "status": "active",
     "version": "6.2", "network_only": False, "requires_wp": "5.8",
     "requires_php": "7.4", "textdomain": "acf", "author": "WP Engine"},
    # textdomain deliberately does NOT contain the folder name: it proves the
    # post-resolution self-lockout guard, not just the raw-identifier one.
    {"plugin": "wp-ops-connect/wp-ops-connect", "name": "WP Ops Connect",
     "status": "active", "version": "1.4.0", "network_only": False,
     "requires_wp": "5.8", "requires_php": "7.4", "textdomain": "wpopsconnect",
     "author": "Example"},
    # Two plugins shipping the same textdomain - the realistic ambiguity case.
    {"plugin": "seo-pack/seo-pack", "name": "SEO Pack", "status": "active",
     "version": "1.0", "network_only": False, "requires_wp": "5.8",
     "requires_php": "7.4", "textdomain": "seo", "author": "A"},
    {"plugin": "seo-lite/seo-lite", "name": "SEO Lite", "status": "inactive",
     "version": "2.0", "network_only": False, "requires_wp": "5.8",
     "requires_php": "7.4", "textdomain": "seo", "author": "B"},
]


class FakePluginGateway:
    """REST-shaped plugin gateway that records every call that reached the wire.

    `calls` is the assertion surface for "never reached the gateway" claims; `writes`
    records the status POSTs. `readback_status` forces get_plugin to report a status
    other than the one that was written (WP 200s, a must-use filter reverts it).
    `readback_error` makes get_plugin RAISE while the write itself still lands - the
    timeout/5xx case where the site HAS changed but the verification never came back.
    """

    def __init__(self, plugins=None, error=None, readback_status=None,
                 readback_error=None):
        self.plugins = [dict(p) for p in (PLUGINS if plugins is None else plugins)]
        self.error = error
        self.readback_status = readback_status
        self.readback_error = readback_error
        self.calls = []
        self.writes = []

    def _boom(self):
        if self.error is not None:
            raise self.error

    def _row(self, plugin_id):
        for p in self.plugins:
            if p["plugin"] == plugin_id:
                return p
        raise KeyError(f"no plugin {plugin_id!r}")

    async def list_plugins(self):
        self.calls.append("list_plugins")
        self._boom()
        return [dict(p) for p in self.plugins]

    async def get_plugin(self, plugin_id):
        self.calls.append(("get_plugin", plugin_id))
        self._boom()
        if self.readback_error is not None:
            raise self.readback_error
        row = dict(self._row(plugin_id))
        if self.readback_status is not None:
            row["status"] = self.readback_status
        return row

    async def set_plugin_status(self, plugin_id, status):
        self.calls.append(("set_plugin_status", plugin_id, status))
        self._boom()
        self.writes.append((plugin_id, status))
        self._row(plugin_id)["status"] = status
        return dict(self._row(plugin_id))


def _ops(**kw):
    gw = FakePluginGateway(**kw)
    return PluginOps(gw), gw


# --- resolve: the three identifier forms ------------------------------------

@pytest.mark.parametrize("identifier", ["akismet", "akismet/akismet",
                                        "akismet/akismet.php", "Akismet/Akismet.PHP"])
async def test_resolve_accepts_every_identifier_form(identifier):
    ops, _ = _ops()
    assert await ops.resolve(identifier) == "akismet/akismet"


async def test_resolve_falls_back_to_textdomain():
    """`acf` is neither the REST id nor the folder of advanced-custom-fields/acf."""
    ops, _ = _ops()
    assert await ops.resolve("acf") == "advanced-custom-fields/acf"


async def test_resolve_prefers_the_exact_rest_id_over_a_textdomain_match():
    ops, _ = _ops()
    assert await ops.resolve("seo-pack/seo-pack") == "seo-pack/seo-pack"


async def test_resolve_unknown_names_it_and_lists_what_is_installed():
    ops, _ = _ops()
    with pytest.raises(PluginError) as e:
        await ops.resolve("wordfence")
    assert "wordfence" in str(e.value) and "akismet/akismet" in str(e.value)


async def test_resolve_ambiguous_lists_the_candidates():
    ops, _ = _ops()
    with pytest.raises(PluginError) as e:
        await ops.resolve("seo")
    msg = str(e.value)
    assert "seo-pack/seo-pack" in msg and "seo-lite/seo-lite" in msg


async def test_resolve_blank_identifier_is_refused_before_the_gateway():
    ops, gw = _ops()
    with pytest.raises(PluginError):
        await ops.resolve("   ")
    assert gw.calls == []


async def test_resolve_not_found_listing_is_capped():
    many = [{"plugin": f"p{i}/p{i}", "name": f"P{i}", "status": "inactive",
             "textdomain": f"p{i}"} for i in range(40)]
    ops, _ = _ops(plugins=many)
    with pytest.raises(PluginError) as e:
        await ops.resolve("nope")
    msg = str(e.value)
    assert "p0/p0" in msg and "p39/p39" not in msg and "more" in msg


# --- list -------------------------------------------------------------------

async def test_list_plugins_counts_active_and_inactive():
    ops, _ = _ops()
    out = await ops.list_plugins()
    assert out["action"] == "ok"
    assert len(out["plugins"]) == len(PLUGINS)
    assert out["active"] == 3 and out["inactive"] == 2


async def test_list_plugins_never_raises():
    ops, _ = _ops(error=RuntimeError("boom"))
    out = await ops.list_plugins()
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


# --- activate ---------------------------------------------------------------

async def test_activate_dry_run_previews_without_writing():
    ops, gw = _ops()
    out = await ops.activate("akismet")
    assert out["action"] == "preview"
    assert out["plugin"]["plugin"] == "akismet/akismet"
    assert out["current_status"] == "inactive" and out["target_status"] == "active"
    assert "Akismet Anti-Spam" in out["effect"] and "inactive" in out["effect"]
    assert gw.writes == []


async def test_activate_writes_and_reads_back():
    ops, gw = _ops()
    out = await ops.activate("akismet", dry_run=False)
    assert out["action"] == "activated" and out["verified"] is True
    assert out["plugin"]["status"] == "active"
    assert gw.writes == [("akismet/akismet", "active")]
    assert ("get_plugin", "akismet/akismet") in gw.calls


async def test_activate_already_active_is_a_noop_not_an_error():
    ops, gw = _ops()
    out = await ops.activate("acf", dry_run=False)
    assert out["action"] == "noop" and out["reason"] == "already active"
    assert out["plugin"]["plugin"] == "advanced-custom-fields/acf"
    assert gw.writes == []


async def test_activate_readback_mismatch_is_an_error_carrying_write_landed():
    ops, gw = _ops(readback_status="inactive")
    out = await ops.activate("akismet", dry_run=False)
    assert out["action"] == "error"
    assert "PluginStatusMismatch: requested active, site reports inactive" in out["error"]
    assert out["write_landed"] is True
    assert gw.writes == [("akismet/akismet", "active")]


async def test_activate_never_raises():
    ops, _ = _ops(error=RuntimeError("boom"))
    out = await ops.activate("akismet", dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


async def test_activate_unknown_plugin_is_an_error_dict():
    ops, gw = _ops()
    out = await ops.activate("wordfence", dry_run=False)
    assert out["action"] == "error" and "PluginError" in out["error"]
    assert gw.writes == []


# --- deactivate -------------------------------------------------------------

async def test_deactivate_dry_run_previews_and_states_the_blast_radius():
    ops, gw = _ops()
    out = await ops.deactivate("acf")
    assert out["action"] == "preview"
    assert out["current_status"] == "active" and out["target_status"] == "inactive"
    assert "ACF" in out["effect"]
    assert "offline" in out["effect"].lower()
    assert gw.writes == []


async def test_deactivate_writes_and_reads_back():
    ops, gw = _ops()
    out = await ops.deactivate("acf", dry_run=False)
    assert out["action"] == "deactivated" and out["verified"] is True
    assert out["plugin"]["status"] == "inactive"
    assert gw.writes == [("advanced-custom-fields/acf", "inactive")]


async def test_deactivate_already_inactive_is_a_noop():
    ops, gw = _ops()
    out = await ops.deactivate("akismet", dry_run=False)
    assert out["action"] == "noop" and out["reason"] == "already inactive"
    assert gw.writes == []


async def test_deactivate_readback_mismatch_is_an_error_carrying_write_landed():
    ops, gw = _ops(readback_status="active")
    out = await ops.deactivate("acf", dry_run=False)
    assert out["action"] == "error"
    assert ("PluginStatusMismatch: requested inactive, site reports active"
            in out["error"])
    assert out["write_landed"] is True


async def test_deactivate_never_raises():
    ops, _ = _ops(error=RuntimeError("boom"))
    out = await ops.deactivate("acf", dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


# --- the self-lockout guard -------------------------------------------------

@pytest.mark.parametrize("identifier", [
    "wp-ops-connect",
    "wp-ops-connect/wp-ops-connect",
    "wp-ops-connect/wp-ops-connect.php",
    "WP-Ops-Connect",
])
@pytest.mark.parametrize("dry_run", [True, False])
async def test_deactivate_refuses_the_control_plane_plugin_before_any_gateway_call(
        identifier, dry_run):
    ops, gw = _ops()
    out = await ops.deactivate(identifier, dry_run=dry_run)
    assert out["action"] == "error"
    assert "PluginSelfLockout" in out["error"]
    assert "wp-ops-connect" in out["error"]
    assert gw.calls == []                       # not even a read reached the site
    assert gw.writes == []


async def test_deactivate_refuses_the_control_plane_plugin_reached_by_textdomain():
    """The second guard: the raw identifier is innocent, the RESOLVED id is not."""
    ops, gw = _ops()
    out = await ops.deactivate("wpopsconnect", dry_run=False)
    assert out["action"] == "error" and "PluginSelfLockout" in out["error"]
    assert gw.writes == []                      # resolution read, but no write


async def test_activating_the_control_plane_plugin_is_not_refused():
    """The guard is deactivate-only: activating it can only restore the connection."""
    ops, _ = _ops()
    out = await ops.activate("wp-ops-connect", dry_run=False)
    assert out["action"] == "noop" and out["reason"] == "already active"


# --- the write landed but could not be verified -----------------------------
# A readback that TIMES OUT is not the same as a write that failed: the plugin is
# already toggled on the live site. An error dict without `write_landed` would tell an
# operator "nothing happened" about a site whose forms/caching just went offline.

async def test_readback_exception_after_successful_write_reports_write_landed():
    ops, gw = _ops(readback_error=RuntimeError("readback timed out"))
    out = await ops.deactivate("acf", dry_run=False)
    assert out["action"] == "error"
    assert "RuntimeError: readback timed out" in out["error"]
    assert out["write_landed"] is True
    assert gw.writes == [("advanced-custom-fields/acf", "inactive")]


async def test_activate_readback_exception_after_successful_write_reports_write_landed():
    ops, gw = _ops(readback_error=RuntimeError("readback timed out"))
    out = await ops.activate("akismet", dry_run=False)
    assert out["action"] == "error"
    assert "RuntimeError: readback timed out" in out["error"]
    assert out["write_landed"] is True
    assert gw.writes == [("akismet/akismet", "active")]


async def test_status_mismatch_carries_plugin_id():
    """The resolved id is a FIELD, not only prose - the caller reverts by id."""
    ops, _ = _ops(readback_status="inactive")
    out = await ops.activate("akismet", dry_run=False)
    assert out["action"] == "error" and out["write_landed"] is True
    assert out["plugin"] == "akismet/akismet"

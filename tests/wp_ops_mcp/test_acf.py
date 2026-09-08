# tests/wp_ops_mcp/test_acf.py
"""Unit tests for AcfOps + the _acf_equal tolerance helper.

Mirrors test_seo_ops.py: fake at the gateway boundary and assert that every path
comes back as a value (never an exception), that dry-run writes nothing, that a real
apply is confirmed by readback, and that ACF's storage normalization (True<->1,
5<->"5", ints<->str in arrays) does NOT make a landed write read as a mismatch.
"""
import asyncio

from wp_ops_mcp.ops.acf import AcfOps, _acf_equal
from wp_ops_mcp.transport.rest import RestError


# --- _acf_equal: tolerant equality -----------------------------------------

def test_acf_equal_bool_normalization():
    # ACF stores a true/false field as 1/0 (and "" is its other falsey form).
    assert _acf_equal(True, 1)
    assert _acf_equal(1, True)
    assert _acf_equal(False, 0)
    assert _acf_equal(0, False)
    assert _acf_equal(False, "")
    assert not _acf_equal(True, 0)
    assert not _acf_equal(True, False)


def test_acf_equal_scalar_str_int():
    assert _acf_equal("5", 5)
    assert _acf_equal(5, "5")
    assert _acf_equal("abc", "abc")
    assert not _acf_equal("5", 6)


def test_acf_equal_lists_elementwise():
    assert _acf_equal([1, 2], ["1", "2"])
    assert not _acf_equal([1, 2], [1, 2, 3])   # length differs
    assert not _acf_equal([1, 2], [1, 3])      # element differs


def test_acf_equal_nested_dict():
    # keys must match; values compare with the same tolerance (recursively).
    assert _acf_equal({"a": 1, "b": [True]}, {"a": "1", "b": [1]})
    assert not _acf_equal({"a": 1}, {"b": 1})  # different keys
    assert not _acf_equal({"a": 1}, {"a": 2})  # different value


def test_acf_equal_unequal_and_mixed_types():
    assert not _acf_equal("x", "y")
    assert not _acf_equal(None, "")            # None only equals None
    assert _acf_equal(None, None)
    assert not _acf_equal([1], {"a": 1})       # list vs dict


# --- AcfOps.get -------------------------------------------------------------

class RaisingAcfGateway:
    """Gateway whose capability probe fails - stands in for a plugin/network error."""

    async def ensure_acf_capable(self):
        raise RestError("plugin down")

    async def get_acf_fields(self, post_id):
        raise RestError("plugin down")

    async def set_acf_fields(self, post_id, fields):
        raise RestError("plugin down")


class FakeAcfGateway:
    """ACF-capable content gateway with an in-memory field store.

    `skipped` are the selectors the plugin refuses (leading-underscore field keys);
    `readback` overrides the acf map the write echoes back (to force a mismatch or an
    ACF-normalized readback).
    """

    def __init__(self, acf_active=True, skipped=None, readback=None):
        self.info = {"plugin_version": "1.2.0", "acf": "6.2.1" if acf_active else None}
        self.acf_active = acf_active
        self.store = {}
        self.set_calls = []
        self.skipped = list(skipped or [])
        self.readback = readback

    async def ensure_acf_capable(self):
        if not self.acf_active:
            raise RestError("ACF not active on this site (wp-ops-connect reports acf=null)")
        return self.info

    async def get_acf_fields(self, post_id):
        return dict(self.store)

    async def set_acf_fields(self, post_id, fields):
        self.set_calls.append((post_id, dict(fields)))
        applied = [k for k in fields if k not in self.skipped]
        for k in applied:
            self.store[k] = fields[k]
        acf = self.readback if self.readback is not None else dict(self.store)
        return {"applied": applied, "skipped": list(self.skipped), "acf": acf}


def test_get_ok_returns_acf_map():
    gw = FakeAcfGateway()
    gw.store = {"headline": "Hi", "count": 3, "gallery": [1, 2]}
    r = asyncio.run(AcfOps(gw).get(5))
    assert r == {"action": "ok", "acf": {"headline": "Hi", "count": 3, "gallery": [1, 2]}}


def test_get_acf_inactive_is_error_dict():
    r = asyncio.run(AcfOps(FakeAcfGateway(acf_active=False)).get(5))
    assert r["action"] == "error" and "ACF not active" in r["error"]


def test_get_never_raises_on_raising_gateway():
    r = asyncio.run(AcfOps(RaisingAcfGateway()).get(5))
    assert r["action"] == "error" and "RestError" in r["error"]


# --- AcfOps.set -------------------------------------------------------------

def test_set_dry_run_previews_and_writes_nothing():
    gw = FakeAcfGateway()
    r = asyncio.run(AcfOps(gw).set(5, {"headline": "New"}))
    assert r["action"] == "preview" and r["fields"] == {"headline": "New"}
    assert gw.set_calls == []                  # dry-run never touched the gateway


def test_set_applied_and_verified():
    gw = FakeAcfGateway()
    r = asyncio.run(AcfOps(gw).set(5, {"headline": "New", "count": 3}, dry_run=False))
    assert r["action"] == "applied" and r["verified"] is True
    assert r["applied"] == ["headline", "count"] and r["skipped"] == []
    assert "warning" not in r
    assert r["acf"] == {"headline": "New", "count": 3}
    assert gw.set_calls and gw.set_calls[0][0] == 5


def test_set_verifies_through_acf_normalization():
    # ACF echoes a true/false as 1 and a number as a string; _acf_equal absorbs both
    # so a landed write is NOT reported as a readback mismatch.
    gw = FakeAcfGateway(readback={"flag": 1, "n": "7"})
    r = asyncio.run(AcfOps(gw).set(5, {"flag": True, "n": 7}, dry_run=False))
    assert r["action"] == "applied" and r["verified"] is True


def test_set_readback_mismatch_is_error():
    gw = FakeAcfGateway(readback={"headline": "Different"})
    r = asyncio.run(AcfOps(gw).set(5, {"headline": "New"}, dry_run=False))
    assert r["action"] == "error"
    assert "AcfReadbackMismatch" in r["error"] and "headline" in r["error"]


def test_set_absent_readback_key_is_mismatch_even_for_false():
    # A silently-dropped write of literal False must NOT verify. The key is absent from
    # the readback, and `acf.get(k)` -> None would compare _acf_equal(None, False) ==
    # True (bool(None) == bool(False)), wrongly reporting the write as landed. Absence
    # is detected with a sentinel at the call site, so it fails as a mismatch and the
    # message says the value was absent rather than printing a fabricated None.
    gw = FakeAcfGateway(readback={})
    r = asyncio.run(AcfOps(gw).set(5, {"flag": False}, dry_run=False))
    assert r["action"] == "error"
    assert "AcfReadbackMismatch" in r["error"] and "flag" in r["error"]
    assert "<absent>" in r["error"]


def test_set_skipped_nonempty_is_partial_with_warning():
    # A leading-underscore selector is skipped by the plugin; the applied keys still
    # land and verify, but the result is action "partial" (NOT "applied") so a
    # partly-applied write is never mistaken for a clean one - plus the carried
    # `skipped` list and a warning naming it.
    gw = FakeAcfGateway(skipped=["_hidden"])
    r = asyncio.run(AcfOps(gw).set(5, {"headline": "New", "_hidden": "x"}, dry_run=False))
    assert r["action"] == "partial" and r["verified"] is True
    assert r["applied"] == ["headline"] and r["skipped"] == ["_hidden"]
    assert "warning" in r and "_hidden" in r["warning"] and "skipped" in r["warning"]


def test_set_empty_fields_is_error_dict():
    gw = FakeAcfGateway()
    r = asyncio.run(AcfOps(gw).set(5, {}, dry_run=False))
    assert r["action"] == "error" and "no ACF fields provided" in r["error"]
    assert gw.set_calls == []


def test_set_non_dict_fields_is_error_dict():
    gw = FakeAcfGateway()
    r = asyncio.run(AcfOps(gw).set(5, ["not", "a", "dict"], dry_run=False))
    assert r["action"] == "error" and "fields must be an object" in r["error"]
    assert gw.set_calls == []


def test_set_never_raises_on_raising_gateway():
    r = asyncio.run(AcfOps(RaisingAcfGateway()).set(5, {"headline": "New"}, dry_run=False))
    assert r["action"] == "error" and "RestError" in r["error"]

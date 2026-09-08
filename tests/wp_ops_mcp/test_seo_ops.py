# tests/wp_ops_mcp/test_seo_ops.py
import asyncio
from wp_ops_mcp.ops.seo import SeoOps
from wp_ops_mcp.transport.rest import RestError


class RaisingSeoGateway:
    """Gateway whose capability probe fails - stands in for a plugin/network error."""

    async def ensure_seo_capable(self):
        raise RestError("plugin down")


def test_gateway_raise_becomes_error_dict():
    # A RestError from the gateway must surface as an {"action": "error"} value,
    # never propagate out of SeoOps - both on the read and the write path.
    gw = RaisingSeoGateway()
    g = asyncio.run(SeoOps(gw).get_seo(5))
    assert g["action"] == "error" and "RestError" in g["error"]
    s = asyncio.run(SeoOps(gw).set_seo(5, {"title": "T"}, dry_run=False))
    assert s["action"] == "error" and "RestError" in s["error"]


class FakeSeoGateway:
    def __init__(self, plugin="seopress", fail_applied=False):
        self.info = {"plugin_version": "1.1.0", "seo_plugin": plugin}
        self.meta = {}
        self.fail_applied = fail_applied

    async def ensure_seo_capable(self):
        return self.info

    async def get_post_meta(self, post_id):
        return dict(self.meta)

    async def set_post_meta(self, post_id, meta):
        if self.fail_applied:
            return list(meta)          # claims applied but doesn't store
        self.meta.update(meta)
        return list(meta)


def test_set_and_get_roundtrip():
    gw = FakeSeoGateway()
    ops = SeoOps(gw)
    r = asyncio.run(ops.set_seo(5, {"title": "T", "noindex": True}, dry_run=False))
    assert r["action"] == "applied" and r["verified"] is True
    g = asyncio.run(ops.get_seo(5))
    assert g["fields"]["title"] == "T" and g["fields"]["noindex"] is True


def test_dry_run_writes_nothing():
    gw = FakeSeoGateway()
    r = asyncio.run(SeoOps(gw).set_seo(5, {"title": "T"}))
    assert r["action"] == "preview" and gw.meta == {}


def test_readback_mismatch_is_error():
    gw = FakeSeoGateway(fail_applied=True)
    r = asyncio.run(SeoOps(gw).set_seo(5, {"title": "T"}, dry_run=False))
    assert r["action"] == "error" and "SeoReadbackMismatch" in r["error"]


def test_no_seo_plugin_is_an_error_dict():
    r = asyncio.run(SeoOps(FakeSeoGateway(plugin=None)).set_seo(5, {"title": "T"}, dry_run=False))
    assert r["action"] == "error" and "no supported SEO plugin" in r["error"]


def test_rankmath_noindex_is_supported_and_no_longer_an_error():
    """Was an error dict while rank_math_robots was out of scope; it now writes."""
    r = asyncio.run(SeoOps(FakeSeoGateway(plugin="rank-math")).set_seo(
        5, {"noindex": True}, dry_run=True))
    assert r["action"] != "error", r
    assert r["meta"]["rank_math_robots"] == ["noindex"]


def test_clearing_value_verifies_against_absent_key():
    gw = FakeSeoGateway()
    gw.meta = {"_seopress_titles_title": "old"}

    async def set_and_drop(post_id, meta):
        gw.meta.pop("_seopress_titles_title", None)   # WP clears empty meta
        return list(meta)

    gw.set_post_meta = set_and_drop
    r = asyncio.run(SeoOps(gw).set_seo(5, {"title": ""}, dry_run=False))
    assert r["action"] == "applied" and r["verified"] is True


def test_partial_apply_is_error():
    # The plugin's /meta POST silently skips non-whitelisted keys, returning a shorter
    # "applied" list with HTTP 200. SeoOps must catch that BEFORE the readback net.
    gw = FakeSeoGateway()

    async def set_subset(post_id, meta):
        # Only the whitelisted key survives; the noindex key is silently dropped.
        subset = {k: v for k, v in meta.items() if k == "_seopress_titles_title"}
        gw.meta.update(subset)   # stores only the subset...
        return list(subset)      # ...and reports only that as applied

    gw.set_post_meta = set_subset
    r = asyncio.run(SeoOps(gw).set_seo(5, {"title": "T", "noindex": True}, dry_run=False))
    assert r["action"] == "error" and "SeoPartialApply" in r["error"]


def test_get_seo_validates_plugin_before_meta_fetch():
    # get_seo checks plugin support BEFORE the meta round-trip: an unsupported plugin
    # must fail fast without ever calling get_post_meta (saves a wasted request).
    gw = FakeSeoGateway(plugin="wpforms")     # not a supported SEO plugin

    async def boom(post_id):
        raise AssertionError("get_post_meta must not run for an unsupported plugin")

    gw.get_post_meta = boom
    r = asyncio.run(SeoOps(gw).get_seo(5))
    assert r["action"] == "error" and "no supported SEO plugin" in r["error"]

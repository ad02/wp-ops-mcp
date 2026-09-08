"""Config-driven transport selection in server.py (lazy wpcli import).

Verifies WPOPS_TRANSPORT drives _content_gateway (rest / auto / wpcli), that the
explicit gateway= seam still wins, that server.py imports wpcli lazily (the cloud
REST-only rule), and that an edit payload refuses cleanly with a dict when neither
a gateway nor REST credentials are available.
"""
import importlib
import json
import sys

import pytest

from wp_ops_mcp import server
from wp_ops_mcp.ops.rest_gateway import RestContentGateway
from wp_ops_mcp.registry import Site, SiteRegistry


def _site():
    # The real Site dataclass (registry.py) is frozen with every field required;
    # only .install is read by _content_gateway, the rest are filler.
    return Site(install="examplestg", account="hostacct9",
                domain="examplestg.example.com",
                environment="staging", php_version="8.2", cf_zone_id="")


def _creds(tmp_path, monkeypatch, install="examplestg"):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({install: {"base_url": "https://x.test",
                                       "username": "u", "app_password": "p"}}), encoding="utf-8")
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(p))


def test_rest_mode_with_creds_returns_rest_gateway(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setenv("WPOPS_TRANSPORT", "rest")
    gw = server._content_gateway(_site())
    assert isinstance(gw, RestContentGateway)


def test_rest_mode_without_creds_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(tmp_path / "missing.json"))
    monkeypatch.setenv("WPOPS_TRANSPORT", "rest")
    assert server._content_gateway(_site()) is None


def test_auto_prefers_rest_when_creds_exist(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setenv("WPOPS_TRANSPORT", "auto")
    assert isinstance(server._content_gateway(_site()), RestContentGateway)


def test_explicit_gateway_arg_still_wins(tmp_path, monkeypatch):
    sentinel = object()
    assert server._content_gateway(_site(), gateway=sentinel) is sentinel


def test_wpcli_not_imported_at_module_import_time(monkeypatch):
    saved = {k: v for k, v in sys.modules.items() if "wp_ops_mcp" in k or "wpengine" in k}
    for k in list(saved):
        sys.modules.pop(k, None)
    try:
        importlib.import_module("wp_ops_mcp.server")
        assert not any("transport.wpcli" in m for m in sys.modules), \
            "server.py must import wpcli lazily (cloud REST-only rule)"
    finally:
        sys.modules.update(saved)


def test_edit_payload_refuses_cleanly_without_creds(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(tmp_path / "missing.json"))
    monkeypatch.setenv("WPOPS_TRANSPORT", "rest")
    # The live registry has no "examplestg", so the refusal may come from the
    # unknown-install / missing-profile precondition rather than the credentials
    # check - both are {"action": "refused", ...} dicts. Assert only the shape.
    r = asyncio.run(server.edit_page_payload(server.get_registry(), server.get_store(),
                                             "examplestg", 1, [], dry_run=True))
    assert r.get("action") == "refused"


def test_malformed_transport_env_fails_closed(tmp_path, monkeypatch):
    """Unknown WPOPS_TRANSPORT values FAIL CLOSED to None and never load the SSH
    (wpcli) transport - the cloud REST-only guarantee. A typo or unrelated word must
    not silently fall through to the SSH path. ("REST " is NOT malformed - it
    normalizes to rest; covered by test_case_and_whitespace_normalized.)
    Isolated from any real default credentials file via WPOPS_CREDENTIALS."""
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(tmp_path / "absent.json"))
    for value in ("restt", "ssh", "rest wpcli"):
        monkeypatch.setenv("WPOPS_TRANSPORT", value)
        # Start each case with the SSH transport unloaded so the assertion below
        # proves THIS call didn't import it (not a leftover from an earlier test).
        for k in [m for m in list(sys.modules) if "wpcli" in m]:
            sys.modules.pop(k, None)
        assert server._content_gateway(_site()) is None, f"{value!r} should fail closed"
        assert not any("transport.wpcli" in m for m in sys.modules), \
            f"{value!r} must not load the SSH transport"


def test_case_and_whitespace_normalized(tmp_path, monkeypatch):
    """WPOPS_TRANSPORT is normalized (strip + lowercase): ' REST ' -> rest mode."""
    _creds(tmp_path, monkeypatch)
    monkeypatch.setenv("WPOPS_TRANSPORT", " REST ")
    assert isinstance(server._content_gateway(_site()), RestContentGateway)


def test_wpcli_mode_explicit(monkeypatch):
    """WPOPS_TRANSPORT=wpcli selects the SSH/WP-CLI gateway (import inside the test
    for the isinstance check; pop the modules after so other tests start clean)."""
    from wp_ops_mcp.ops.wpcli_gateway import WPCliContentGateway
    monkeypatch.setenv("WPOPS_TRANSPORT", "wpcli")
    try:
        assert isinstance(server._content_gateway(_site()), WPCliContentGateway)
    finally:
        for k in [m for m in list(sys.modules) if "wpcli" in m]:
            sys.modules.pop(k, None)


# -- synthetic sites for credentialed-but-unregistered installs ---------------
# _site_or_synthetic resolves an install to a registry Site when the fleet inventory
# knows it, else a SYNTHETIC Site when REST credentials exist for it (so REST-only
# stagings/team sites absent from sites.json still resolve and tools stop refusing them).

def _reg(*sites):
    return SiteRegistry(list(sites))


def _creds_file(tmp_path, monkeypatch, mapping):
    p = tmp_path / "synthetic-creds.json"
    p.write_text(json.dumps(mapping), encoding="utf-8")
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(p))
    return p


def test_site_or_synthetic_returns_registry_site_when_known():
    site = _site()
    assert server._site_or_synthetic(_reg(site), "examplestg") is site


def test_site_or_synthetic_synthesizes_credentialed_install(tmp_path, monkeypatch):
    _creds_file(tmp_path, monkeypatch, {"teamsitestg": {
        "base_url": "https://team-staging.example.com",
        "username": "u", "app_password": "p"}})
    site = server._site_or_synthetic(_reg(), "teamsitestg")   # unknown to the registry
    assert site is not None
    assert site.install == "teamsitestg"
    assert site.account == "rest-only"
    assert site.domain == "team-staging.example.com"          # host of the creds base_url
    assert site.environment == "staging"                      # derived from the name (...stg)
    assert site.php_version == "" and site.cf_zone_id == ""   # honest: not knowable over REST


def test_site_or_synthetic_env_prod_default(tmp_path, monkeypatch):
    _creds_file(tmp_path, monkeypatch, {"acmeclinic": {
        "base_url": "https://acme.example.com", "username": "u", "app_password": "p"}})
    assert server._site_or_synthetic(_reg(), "acmeclinic").environment == "prod"


def test_site_or_synthetic_none_when_uncredentialed(tmp_path, monkeypatch):
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(tmp_path / "absent.json"))
    assert server._site_or_synthetic(_reg(), "ghostinstall") is None


class _FakeSeoGateway:
    """SEO-capable gateway - just enough for set_seo's dry-run to reach a preview."""
    async def ensure_seo_capable(self):
        return {"plugin_version": "1.1.0", "seo_plugin": "seopress"}


def test_write_tool_serves_credentialed_synthetic_site(tmp_path, monkeypatch):
    # A credentialed install the fleet inventory does not list resolves to a synthetic
    # Site, so the write tool RUNS (reaches the gateway) instead of refusing "unknown".
    import asyncio
    _creds_file(tmp_path, monkeypatch, {"teamsitestg": {
        "base_url": "https://team-staging.example.com",
        "username": "u", "app_password": "p"}})
    payload = asyncio.run(server.set_seo_payload(_reg(), "teamsitestg", 10, title="T",
                                                 dry_run=True, gateway=_FakeSeoGateway()))
    assert payload["action"] == "preview"      # NOT the unknown-install refusal
    assert payload["plugin"] == "seopress"


def test_write_tool_still_refuses_uncredentialed_unknown(tmp_path, monkeypatch):
    # No credentials + not in the registry -> the unknown-install refusal is unchanged.
    import asyncio
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(tmp_path / "absent.json"))
    payload = asyncio.run(server.set_seo_payload(_reg(), "ghostinstall", 10, title="T",
                                                 dry_run=True, gateway=_FakeSeoGateway()))
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]

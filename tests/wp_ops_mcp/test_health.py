"""Tests for wp_ops_mcp per-site health aggregation."""
import pytest

from wp_ops_mcp.registry import Site
from wp_ops_mcp.transport.rest import RestProbe
from wp_ops_mcp.transport.wpcli import SSHProbe
from wp_ops_mcp.health import SiteHealth, check_site_health


def make_site(install="exampleprd", domain="example.com", env="prod"):
    return Site(install=install, account="hostacct1", domain=domain,
                environment=env, php_version="8.2", cf_zone_id="z1")


class FakeRest:
    def __init__(self, probe):
        self._probe = probe
    async def probe(self):
        return self._probe


class FakeCli:
    def __init__(self, probe):
        self._probe = probe
    async def probe(self):
        return self._probe


class TestCheckSiteHealth:
    async def test_both_reachable(self):
        site = make_site()
        rest = FakeRest(RestProbe(reachable=True, status_code=200, detail="ok"))
        cli = FakeCli(SSHProbe(reachable=True, wp_cli=True, detail="ok"))
        h = await check_site_health(site, rest_client=rest, cli_transport=cli)
        assert isinstance(h, SiteHealth)
        assert h.rest_reachable is True
        assert h.ssh_reachable is True
        assert h.wp_cli is True
        assert h.install == "exampleprd"
        assert h.environment == "prod"

    async def test_no_domain_skips_rest_probe(self):
        site = make_site(install="parkedstg", domain="", env="staging")
        cli = FakeCli(SSHProbe(reachable=True, wp_cli=True, detail="ok"))
        # rest_client must NOT be called when there is no domain.
        rest = FakeRest(RestProbe(reachable=True, status_code=200, detail="should not be used"))
        h = await check_site_health(site, rest_client=rest, cli_transport=cli)
        assert h.rest_reachable is None
        assert "no domain" in h.rest_detail.lower()
        assert h.ssh_reachable is True

    async def test_ssh_unreachable_reflected(self):
        site = make_site()
        rest = FakeRest(RestProbe(reachable=True, status_code=200, detail="ok"))
        cli = FakeCli(SSHProbe(reachable=False, wp_cli=False, detail="SSH auth failed"))
        h = await check_site_health(site, rest_client=rest, cli_transport=cli)
        assert h.ssh_reachable is False
        assert h.wp_cli is False
        assert "auth" in h.ssh_detail.lower()

    async def test_to_dict_is_token_lean(self):
        site = make_site()
        rest = FakeRest(RestProbe(reachable=True, status_code=200, detail="ok"))
        cli = FakeCli(SSHProbe(reachable=True, wp_cli=True, detail="ok"))
        h = await check_site_health(site, rest_client=rest, cli_transport=cli)
        d = h.to_dict()
        assert d["install"] == "exampleprd"
        assert d["rest_reachable"] is True
        assert d["ssh_reachable"] is True
        assert set(d).issuperset({"install", "account", "environment", "rest_reachable", "ssh_reachable", "wp_cli"})

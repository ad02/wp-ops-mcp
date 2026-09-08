"""Tests for the wp_ops_mcp site registry."""
import pytest

from wp_ops_mcp.registry import Site, SiteRegistry, derive_environment


class TestDeriveEnvironment:
    @pytest.mark.parametrize("install", [
        "daytonplas1stg",
        "dermwellstg",
        "georgousaesstg",
        "clientsite1stg",
        "testinstadev",
        "greenbergtest",
        "divi5demostg",
    ])
    def test_staging_markers(self, install):
        assert derive_environment(install) == "staging"

    @pytest.mark.parametrize("install", [
        "dentalsedation",
        "clientsite1prd",
        "thecosmetiprd",
        "hostacct1",
    ])
    def test_defaults_to_prod(self, install):
        assert derive_environment(install) == "prod"


# Minimal raw records shaped like data/sites.json entries.
RAW = [
    {"domain": "example.com", "wpe_account": "hostacct1", "wpe_install": "exampleprd",
     "cf_zone_id": "z1", "php_version": "8.2", "active": True},
    {"domain": "", "wpe_account": "hostacct1", "wpe_install": "examplestg",
     "cf_zone_id": "", "php_version": "8.2", "active": True},
    {"domain": "other.com", "wpe_account": "hostacct3", "wpe_install": "otherprd",
     "cf_zone_id": "z3", "php_version": "8.1", "active": True},
    {"domain": "dead.com", "wpe_account": "hostacct3", "wpe_install": "deadprd",
     "cf_zone_id": "", "php_version": "8.0", "active": False},
]


class TestSiteRegistry:
    def test_from_records_builds_sites_and_skips_inactive(self):
        reg = SiteRegistry.from_records(RAW)
        installs = {s.install for s in reg.all()}
        assert installs == {"exampleprd", "examplestg", "otherprd"}
        assert "deadprd" not in installs

    def test_site_carries_derived_environment(self):
        reg = SiteRegistry.from_records(RAW)
        assert reg.get("examplestg").environment == "staging"
        assert reg.get("exampleprd").environment == "prod"

    def test_site_fields_mapped(self):
        reg = SiteRegistry.from_records(RAW)
        s = reg.get("exampleprd")
        assert isinstance(s, Site)
        assert s.account == "hostacct1"
        assert s.domain == "example.com"
        assert s.php_version == "8.2"
        assert s.cf_zone_id == "z1"

    def test_get_unknown_returns_none(self):
        reg = SiteRegistry.from_records(RAW)
        assert reg.get("nope") is None

    def test_filter_by_account(self):
        reg = SiteRegistry.from_records(RAW)
        got = {s.install for s in reg.filter(account="hostacct3")}
        assert got == {"otherprd"}

    def test_filter_by_environment(self):
        reg = SiteRegistry.from_records(RAW)
        got = {s.install for s in reg.filter(environment="staging")}
        assert got == {"examplestg"}

    def test_filter_by_domain_substring(self):
        reg = SiteRegistry.from_records(RAW)
        got = {s.install for s in reg.filter(query="other")}
        assert got == {"otherprd"}

    def test_filter_query_matches_install_name(self):
        reg = SiteRegistry.from_records(RAW)
        got = {s.install for s in reg.filter(query="stg")}
        assert got == {"examplestg"}

    def test_filters_combine_as_and(self):
        reg = SiteRegistry.from_records(RAW)
        got = {s.install for s in reg.filter(account="hostacct1", environment="prod")}
        assert got == {"exampleprd"}


# --- explicit environment beats the name heuristic ----------------------------------
# derive_environment() reads the install NAME. `demositestg` is a WPE STAGING install
# whose name carries no stg/dev/test marker, so it was classified "prod" and the
# prod write-guard misfired on it (2026-08-26). An explicit value must win.

def test_explicit_environment_in_record_wins_over_name_heuristic():
    from wp_ops_mcp.registry import SiteRegistry
    reg = SiteRegistry.from_records([{
        "wpe_install": "demositestg", "wpe_account": "hostacct1",
        "domain": "demositestg.example.com", "active": True,
        "php_version": "", "cf_zone_id": "", "environment": "staging"}])
    assert reg.get("demositestg").environment == "staging"


def test_name_heuristic_still_used_when_no_explicit_environment():
    from wp_ops_mcp.registry import SiteRegistry
    reg = SiteRegistry.from_records([
        {"wpe_install": "acmeprd", "wpe_account": "a", "domain": "", "active": True,
         "php_version": "", "cf_zone_id": ""},
        {"wpe_install": "acmestg", "wpe_account": "a", "domain": "", "active": True,
         "php_version": "", "cf_zone_id": ""}])
    assert reg.get("acmeprd").environment == "prod"
    assert reg.get("acmestg").environment == "staging"


def test_blank_or_bogus_environment_falls_back_to_heuristic_not_prod_by_accident():
    """A blank value must not silently become a real environment string."""
    from wp_ops_mcp.registry import SiteRegistry
    reg = SiteRegistry.from_records([
        {"wpe_install": "acmestg", "wpe_account": "a", "domain": "", "active": True,
         "php_version": "", "cf_zone_id": "", "environment": ""},
        {"wpe_install": "acmestg2", "wpe_account": "a", "domain": "", "active": True,
         "php_version": "", "cf_zone_id": "", "environment": "banana"}])
    assert reg.get("acmestg").environment == "staging"
    assert reg.get("acmestg2").environment == "staging"

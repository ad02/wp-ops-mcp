"""Tests for the SQLite site_profiles store."""
import pytest

from wp_ops_mcp.profiles.store import ProfileStore, SiteProfile


def make_profile(install="dermwellstg", builder="divi", divi_major=4):
    return SiteProfile(
        install=install,
        account="hostacct1",
        environment="staging",
        domain="",
        discovered_at="2026-06-15T09:00:00",
        wp_version="6.5.2",
        php_version="8.2.28",
        active_theme="child-theme",
        active_theme_version="1.0",
        builder=builder,
        divi_major=divi_major,
        multisite=False,
        plugins=[{"name": "akismet", "status": "active", "version": "5.3"}],
        raw={"template": "Divi", "stylesheet": "child-theme"},
    )


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles.db")


class TestProfileStore:
    def test_upsert_then_get_round_trips(self, store):
        p = make_profile()
        store.upsert(p)
        got = store.get("dermwellstg")
        assert got is not None
        assert got.install == "dermwellstg"
        assert got.builder == "divi"
        assert got.divi_major == 4
        assert got.multisite is False
        assert got.plugins == [{"name": "akismet", "status": "active", "version": "5.3"}]
        assert got.raw["template"] == "Divi"

    def test_get_unknown_returns_none(self, store):
        assert store.get("nope") is None

    def test_upsert_updates_in_place_no_duplicate(self, store):
        store.upsert(make_profile(divi_major=4))
        store.upsert(make_profile(divi_major=5))  # same install, drift to Divi 5
        all_profiles = store.all()
        assert len(all_profiles) == 1
        assert store.get("dermwellstg").divi_major == 5

    def test_all_returns_every_profile(self, store):
        store.upsert(make_profile(install="aprd"))
        store.upsert(make_profile(install="bprd"))
        assert {p.install for p in store.all()} == {"aprd", "bprd"}

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "profiles.db"
        ProfileStore(path).upsert(make_profile())
        reopened = ProfileStore(path)
        assert reopened.get("dermwellstg") is not None

    def test_multisite_true_round_trips(self, store):
        p = make_profile()
        p.multisite = True
        store.upsert(p)
        assert store.get("dermwellstg").multisite is True

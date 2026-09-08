"""The REST client's default timeout.

Live finding (2026-07-28, examplestg): /wp/v2/taxonomies?context=edit answered in
18.6s, past the old 15s default, so a working-but-slow admin endpoint surfaced as a bare
"network error". The default is now 45s, overridable per environment with WPOPS_TIMEOUT.
"""
import importlib

from wp_ops_mcp.transport import rest


def test_default_timeout_covers_slow_admin_endpoints():
    assert rest.DEFAULT_TIMEOUT >= 30.0
    assert rest.WPRestClient("https://x.test")._timeout == rest.DEFAULT_TIMEOUT


def test_explicit_timeout_still_wins():
    assert rest.WPRestClient("https://x.test", timeout=5.0)._timeout == 5.0


def test_env_override(monkeypatch):
    monkeypatch.setenv("WPOPS_TIMEOUT", "90")
    reloaded = importlib.reload(rest)
    try:
        assert reloaded.DEFAULT_TIMEOUT == 90.0
        assert reloaded.WPRestClient("https://x.test")._timeout == 90.0
    finally:
        monkeypatch.delenv("WPOPS_TIMEOUT", raising=False)
        importlib.reload(rest)   # restore the module for every other test

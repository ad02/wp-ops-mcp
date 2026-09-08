import json
from wp_ops_mcp.ops.credentials import SiteCredentials, load_credentials


def _write(tmp_path, data):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_load_returns_credentials_with_stripped_password(tmp_path):
    p = _write(tmp_path, {"examplestg": {
        "base_url": "https://examplestg.example.com/",
        "username": "wpops-mcp", "app_password": "abcd efgh ijkl"}})
    c = load_credentials("examplestg", path=p)
    assert isinstance(c, SiteCredentials)
    assert c.base_url == "https://examplestg.example.com"  # trailing slash normalized off
    assert c.app_password == "abcdefghijkl"          # spaces stripped


def test_missing_install_and_missing_file_return_none(tmp_path):
    p = _write(tmp_path, {"other": {"base_url": "x", "username": "u", "app_password": "p"}})
    assert load_credentials("examplestg", path=p) is None
    assert load_credentials("examplestg", path=tmp_path / "nope.json") is None


def test_malformed_json_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_credentials("any", path=p) is None


def test_env_var_path_used_when_no_arg(tmp_path, monkeypatch):
    p = _write(tmp_path, {"i1": {"base_url": "https://x", "username": "u", "app_password": "p"}})
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(p))
    assert load_credentials("i1").username == "u"


def test_null_or_nonstring_field_returns_none(tmp_path):
    # A present-but-null/non-string/empty field must behave like a missing key
    # (no str() coercion producing "None"/"5"/"").
    for bad in (None, 5, ""):
        p = _write(tmp_path, {"i1": {"base_url": bad, "username": "u", "app_password": "p"}})
        assert load_credentials("i1", path=p) is None


def test_explicit_empty_path_does_not_fall_through(monkeypatch, tmp_path):
    # Explicit path arg wins even when empty; it must NOT silently fall through to env/default.
    p = _write(tmp_path, {"i1": {"base_url": "https://x", "username": "u", "app_password": "p"}})
    monkeypatch.setenv("WPOPS_CREDENTIALS", str(p))
    assert load_credentials("i1", path="") is None


def test_empty_env_var_falls_back_to_default(monkeypatch):
    # Empty env var is treated as unset -> default path (absent in test cwd) -> None, no raise.
    monkeypatch.setenv("WPOPS_CREDENTIALS", "")
    assert load_credentials("i1") is None


# --- optional per-site user_agent ---------------------------------------------------
# Some CF zones 301 the fleet-scanner UA (demositestg, 2026-08-26), so the UA has to
# be settable per site. Absent/blank/non-string behaves like every other optional field.

def _write_entry(tmp_path, entry):
    import json
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"inst": entry}), encoding="utf-8")
    return p


_BASE = {"base_url": "https://s.test", "username": "u", "app_password": "a b c"}


def test_user_agent_is_none_when_absent(tmp_path):
    from wp_ops_mcp.ops.credentials import load_credentials
    c = load_credentials("inst", _write_entry(tmp_path, dict(_BASE)))
    assert c is not None and c.user_agent is None


def test_user_agent_is_read_when_present(tmp_path):
    from wp_ops_mcp.ops.credentials import load_credentials
    c = load_credentials("inst", _write_entry(tmp_path, dict(_BASE, user_agent="WP-Ops-MCP/1.0")))
    assert c.user_agent == "WP-Ops-MCP/1.0"


def test_blank_or_non_string_user_agent_is_ignored_not_sent(tmp_path):
    """A blank UA must not become the literal header - fall back to the default."""
    from wp_ops_mcp.ops.credentials import load_credentials
    assert load_credentials("inst", _write_entry(tmp_path, dict(_BASE, user_agent=""))).user_agent is None
    assert load_credentials("inst", _write_entry(tmp_path, dict(_BASE, user_agent=5))).user_agent is None


def test_user_agent_does_not_make_credentials_required(tmp_path):
    """Adding the field must not turn a previously-valid entry invalid."""
    from wp_ops_mcp.ops.credentials import load_credentials
    assert load_credentials("inst", _write_entry(tmp_path, dict(_BASE))) is not None


# --- optional per-site environment ---------------------------------------------------
# The synthetic Site built for credentialed-but-unregistered installs derived environment
# from the NAME, so a real staging install (demositestg) was guarded as production.

def test_environment_is_none_when_absent(tmp_path):
    from wp_ops_mcp.ops.credentials import load_credentials
    c = load_credentials("inst", _write_entry(tmp_path, dict(_BASE)))
    assert c.environment is None


def test_environment_is_read_when_present(tmp_path):
    from wp_ops_mcp.ops.credentials import load_credentials
    c = load_credentials("inst", _write_entry(tmp_path, dict(_BASE, environment="staging")))
    assert c.environment == "staging"


def test_blank_or_non_string_environment_is_ignored(tmp_path):
    from wp_ops_mcp.ops.credentials import load_credentials
    assert load_credentials("inst", _write_entry(tmp_path, dict(_BASE, environment=""))).environment is None
    assert load_credentials("inst", _write_entry(tmp_path, dict(_BASE, environment=7))).environment is None

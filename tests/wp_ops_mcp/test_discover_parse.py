"""Tests for the discovery command builder and output parser (pure)."""
import json

from wp_ops_mcp.discover import build_discovery_command, parse_discovery


class TestBuildDiscoveryCommand:
    def test_targets_install_dir(self):
        cmd = build_discovery_command("dermwellstg")
        assert "~/sites/dermwellstg" in cmd

    def test_contains_all_sentinels(self):
        cmd = build_discovery_command("x")
        for s in ("@@WP_VERSION@@", "@@CLI_INFO@@", "@@TEMPLATE@@",
                  "@@STYLESHEET@@", "@@THEMES@@", "@@PLUGINS@@", "@@MULTISITE@@"):
            assert s in cmd

    def test_no_single_quotes(self):
        # paramiko mangles nested single quotes through the WPE gateway.
        cmd = build_discovery_command("x")
        assert "'" not in cmd

    def test_single_line_semicolon_separated(self):
        # The WPE SSH gateway collapses newlines to spaces, which makes a following
        # `wp ...` command swallow the next `echo @@X@@` as positional args. Keep it
        # one line with ; separators (proven to work in Phase 1).
        cmd = build_discovery_command("x")
        assert "\n" not in cmd
        assert "; " in cmd


SAMPLE = """@@WP_VERSION@@
6.5.2
@@CLI_INFO@@
OS:  Linux
PHP binary:	/usr/bin/php8.2
PHP version:	8.2.28
WP-CLI version:	2.12.0
@@TEMPLATE@@
Divi
@@STYLESHEET@@
child-theme
@@THEMES@@
[{"name":"Divi","status":"inactive","version":"4.27.6"},{"name":"child-theme","status":"active","version":"1.0"}]
@@PLUGINS@@
[{"name":"akismet","status":"active","version":"5.3"},{"name":"elementor","status":"inactive","version":"3.21.0"}]
@@MULTISITE@@
"""


class TestParseDiscovery:
    def test_parses_versions(self):
        d = parse_discovery(SAMPLE)
        assert d["wp_version"] == "6.5.2"
        assert d["php_version"] == "8.2.28"

    def test_parses_template_and_stylesheet(self):
        d = parse_discovery(SAMPLE)
        assert d["template"] == "Divi"
        assert d["stylesheet"] == "child-theme"

    def test_parses_theme_and_plugin_json(self):
        d = parse_discovery(SAMPLE)
        assert isinstance(d["themes"], list)
        assert {t["name"] for t in d["themes"]} == {"Divi", "child-theme"}
        assert {p["name"] for p in d["plugins"]} == {"akismet", "elementor"}

    def test_multisite_false_when_empty(self):
        d = parse_discovery(SAMPLE)
        assert d["multisite"] is False

    def test_multisite_true_when_one(self):
        raw = SAMPLE.replace("@@MULTISITE@@\n", "@@MULTISITE@@\n1\n")
        d = parse_discovery(raw)
        assert d["multisite"] is True

    def test_malformed_json_yields_empty_lists(self):
        raw = SAMPLE.replace(
            '[{"name":"akismet","status":"active","version":"5.3"},{"name":"elementor","status":"inactive","version":"3.21.0"}]',
            "Error: WP-CLI exploded",
        )
        d = parse_discovery(raw)
        assert d["plugins"] == []

    def test_json_with_leading_php_warning_still_parses(self):
        # PHP 7.4 + WP 6.9 installs emit deprecation notices that get prepended to
        # the --format=json output. Extract the JSON array, don't choke on the noise.
        raw = SAMPLE.replace(
            '[{"name":"akismet","status":"active","version":"5.3"},{"name":"elementor","status":"inactive","version":"3.21.0"}]',
            'PHP Deprecated:  Creation of dynamic property Foo::$bar is deprecated in /x.php on line 12\n'
            '[{"name":"akismet","status":"active","version":"5.3"}]',
        )
        d = parse_discovery(raw)
        assert [p["name"] for p in d["plugins"]] == ["akismet"]

    def test_sentinel_concatenated_to_json_line(self):
        # wp --format=json prints no trailing newline, so the next `echo @@X@@`
        # lands on the same line as the closing ']'. Splitter must handle that.
        raw = (
            "@@WP_VERSION@@\n6.5.2\n@@CLI_INFO@@\nPHP version:\t8.2.0\n"
            "@@TEMPLATE@@\nDivi\n@@STYLESHEET@@\nDivi\n"
            '@@THEMES@@\n[{"name":"Divi","status":"active","version":"4.0"}]@@PLUGINS@@\n'
            '[{"name":"akismet","status":"active","version":"5.3"}]@@MULTISITE@@\n'
        )
        d = parse_discovery(raw)
        assert [t["name"] for t in d["themes"]] == ["Divi"]
        assert [p["name"] for p in d["plugins"]] == ["akismet"]
        assert d["wp_version"] == "6.5.2"

    def test_error_lines_treated_as_missing(self):
        raw = "@@WP_VERSION@@\nError: not a wp install\n@@CLI_INFO@@\n@@TEMPLATE@@\n@@STYLESHEET@@\n@@THEMES@@\n@@PLUGINS@@\n@@MULTISITE@@\n"
        d = parse_discovery(raw)
        assert d["wp_version"] is None
        assert d["template"] is None
        assert d["themes"] == []

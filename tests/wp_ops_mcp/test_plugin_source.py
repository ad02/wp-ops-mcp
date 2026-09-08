"""Static guards on the plugin source: every route capability-gated, whitelist intact.
(No PHP runtime in this repo - live behavior is validated in the staging task.)"""
import pathlib
import re

SRC = pathlib.Path("wp_ops_mcp/plugin/wp-ops-connect/wp-ops-connect.php")


def _src():
    return SRC.read_text(encoding="utf-8")


def test_plugin_exists_and_is_small():
    text = _src()
    # Still a guard, just raised for the 1.4.0 /theme-file route. Keep it tight: this
    # plugin runs on every fleet site and must stay auditable by reading it.
    assert len(text.splitlines()) <= 360
    assert "WP_OPS_CONNECT_VERSION" in text


def test_every_route_has_capability_permission_callback():
    text = _src()
    routes = re.findall(r"register_rest_route\(", text)
    assert len(routes) == 7      # info, duplicate, purge-cache, meta, acf, option, theme-file
    # 5 content routes on edit_others_pages, 1 option route on manage_options,
    # 1 theme-file route on edit_themes (executable code = the highest gate we have).
    assert text.count("'permission_callback' => 'wpops_can'") == 5
    assert text.count("'permission_callback' => 'wpops_can_admin'") == 1
    assert text.count("'permission_callback' => 'wpops_can_themes'") == 1
    assert len(re.findall(r"'permission_callback' =>", text)) == 7
    assert "edit_others_pages" in text
    assert "__return_true" not in text            # no open endpoints, ever


def test_duplicate_uses_wpops_naming_and_meta_whitelist_present():
    text = _src()
    assert "-wpops-draft-" in text
    assert "[wpops draft]" in text
    assert "_et_pb_use_builder" in text and "_et_pb_page_layout" in text
    assert "wp_slash" in text                     # slashing on server-side insert
    assert "ABSPATH" in text                      # direct-load guard


def test_security_load_bearing_details_pinned():
    text = _src()
    assert text.count("(int)") >= 4                       # every post_id int-cast
    assert "is_scalar" in text
    assert "in_array( $k, WPOPS_META_WHITELIST, true )" in text or "in_array($k, WPOPS_META_WHITELIST, true)" in text
    assert "wpops_editable" in text                     # per-post least-privilege guard
    assert "current_user_can( 'edit_post'" in text or "current_user_can('edit_post'" in text


def test_meta_whitelist_contains_only_known_prefixes():
    text = _src()
    block = re.search(r"WPOPS_META_WHITELIST\s*=\s*array\((.*?)\)", text, re.S).group(1)
    keys = re.findall(r"'([^']+)'", block)
    prefixes = ("_et_pb_", "_seopress_", "rank_math_", "_yoast_wpseo_")
    assert keys and all(k.startswith(prefixes) for k in keys)
    assert len(keys) == 18                # closed: 6 divi + 4 seopress + 4 rank math + 4 yoast


def test_version_and_content_contract_pins():
    text = _src()
    assert "'1.4.0'" in text
    assert "'1.3.0'" not in text          # no stale version left in the header/define
    assert "seo_plugin" in text
    assert "_seopress_titles_title" in text and "rank_math_title" in text and "_yoast_wpseo_title" in text
    assert "GET, POST" in text            # /meta and /acf serve both verbs, still one gated route each
    assert "get_post_meta" in text        # read path exists


def test_acf_endpoint_contract():
    """v1.2.0 ACF endpoint: capability-gated, ACF's own API, honest 501 when inactive, empty -> {}."""
    text = _src()
    assert "'/acf'" in text                              # acf route registered
    assert "wpops_acf" in text                         # its callback exists
    assert "function_exists( 'get_fields' )" in text     # inactive guard covering both verbs
    assert "wpops_acf_inactive" in text                    # honest 501 when ACF absent, not a fatal
    assert "get_fields( $p->ID )" in text                # ACF read on the guarded post
    assert "update_field( $k, $v, $p->ID )" in text      # ACF's own writer (field-key linkage), not raw post-meta
    assert "new stdClass()" in text                      # empty ACF serializes as {} not []
    assert "'acf' =>" in text                            # acf key in /info and the acf responses


def test_acf_post_rejects_leading_underscore_selectors():
    """v1.2.0 hardening: /acf POST skips leading-underscore selectors so a caller can't ride
    update_field's unregistered-selector fallthrough into raw update_metadata on protected core
    keys (_wp_page_template, _thumbnail_id, other plugins' internal meta)."""
    text = _src()
    # the guard rejects any selector that starts with '_', before update_field runs
    assert re.search(r"strpos\(\s*\(string\)\s*\$k,\s*'_'\s*\)\s*===\s*0", text)
    assert "continue" in text                            # rejected key is skipped, never written
    assert "$skipped" in text                            # skipped keys are tracked
    assert "'skipped' =>" in text                        # ...and surfaced in the response


def test_option_endpoint_gated_on_manage_options():
    """v1.3.0 arbitrary options: gated on WP's OWN options capability (manage_options), which is
    strictly higher than the content routes' edit_others_pages. No allowlist by design (full
    parity) - the blast-radius contract lives in the MCP layer (dry_run + prod gate)."""
    text = _src()
    assert "'/option'" in text                                  # option route registered
    assert re.search(r"function wpops_can_admin\(\)", text)    # its own capability helper
    assert "current_user_can( 'manage_options' )" in text or "current_user_can('manage_options')" in text
    # defined once, referenced once (the option route) - never leaks onto a content route
    assert len(re.findall(r"function wpops_can_admin\(", text)) == 1
    assert text.count("'wpops_can_admin'") == 1
    assert "'methods' => 'GET, POST, DELETE'" in text           # one route, three verbs
    assert "wpops_option" in text                              # its callback exists


def test_option_endpoint_crud_and_validation():
    """GET (list + single), POST (write then re-read), DELETE - with a sentinel so an option
    holding false/''/0 is not mistaken for a missing one, and a 400 on a bad name."""
    text = _src()
    assert "wp_load_alloptions()" in text        # list = autoloaded names
    assert "array_keys(" in text
    assert "get_option(" in text
    assert "update_option(" in text
    assert "delete_option(" in text
    assert "__wpops_absent__" in text              # sentinel distinguishes absent from false/empty
    assert "'exists' =>" in text
    assert "'deleted' =>" in text
    assert "'updated' => true" in text           # update_option returns false when unchanged - not a failure
    # name validation: non-empty string required for POST/DELETE and single GET
    assert "is_string(" in text
    assert text.count("wpops_bad_request") >= 3    # meta, acf, option
    assert "name must be a non-empty string" in text


def test_option_endpoint_does_not_weaken_content_routes():
    """The option route must not become the gate for anything else: the content callbacks keep
    their per-post guard and wpops_can, and wpops_option must NOT use wpops_editable."""
    text = _src()
    body = re.search(r"function wpops_option\(.*?\n\}\n", text, re.S).group(0)
    assert "wpops_editable" not in body        # options are not post-scoped
    assert "current_user_can" not in body        # the gate is the route's permission_callback
    for cb in ("wpops_info", "wpops_duplicate", "wpops_purge", "wpops_meta", "wpops_acf"):
        assert cb in text


def test_meta_whitelist_exactly_matches_adapters():
    import re
    from wp_ops_mcp.ops.seo import _KEYS
    text = _src()
    block = re.search(r"WPOPS_META_WHITELIST\s*=\s*array\((.*?)\)", text, re.S).group(1)
    plugin_keys = set(re.findall(r"'([^']+)'", block))
    seo_keys = {k for keys in _KEYS.values() for k in keys.values()}
    et_pb_keys = {k for k in plugin_keys if k.startswith("_et_pb_")}
    assert seo_keys <= plugin_keys, f"missing from plugin whitelist: {seo_keys - plugin_keys}"
    assert plugin_keys == et_pb_keys | seo_keys, f"unexpected extra keys: {plugin_keys - et_pb_keys - seo_keys}"


def test_theme_file_route_guardrails_pinned():
    """v1.4.0 /theme-file writes EXECUTABLE code - its guards are load-bearing."""
    text = _src()
    assert "edit_themes" in text                       # highest capability, not edit_posts
    assert "DISALLOW_FILE_EDIT" in text                # honours the site switching it off
    assert "DISALLOW_FILE_MODS" in text
    assert "wp_edit_theme_plugin_file" in text         # core's SELF-REVERTING writer
    assert "realpath" in text                          # containment, not string prefixing
    assert "allow_other_theme" in text                 # parent theme needs an explicit ask
    assert "allow_create" in text                      # no accidental file creation
    assert "'previous'" in text                        # caller can always revert
    # Extension allowlist is closed, and must not admit config/executables by accident.
    block = re.search(r"\$allowed_ext\s*=\s*array\((.*?)\)", text, re.S).group(1)
    exts = re.findall(r"'([^']+)'", block)
    assert exts and set(exts) <= {"php", "css", "js", "json", "txt", "html", "svg", "md"}
    assert "htaccess" not in exts and "ini" not in exts


def test_theme_file_creation_is_refused_not_half_supported():
    """Core cannot create theme files; the plugin must say so rather than pass through
    core's opaque "Sorry, that file cannot be edited." (verified live 2026-08-31)."""
    text = _src()
    assert "Creating theme files is not supported" in text
    assert "allow_create=true to create it" not in text     # the old misleading promise


def test_rank_math_robots_array_is_validated_not_silently_dropped():
    """rank_math_robots is the only ARRAY-valued whitelisted key. is_scalar() alone would
    drop it silently with a 200, which is how a "successful" no-op noindex would happen."""
    text = _src()
    assert "'rank_math_robots'" in text
    assert "$robot_directives" in text
    assert "'skipped'" in text            # rejected keys are reported, not swallowed

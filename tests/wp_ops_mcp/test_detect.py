"""Tests for builder + Divi-major-version detection."""
from wp_ops_mcp.builders.detect import detect_builder, BuilderInfo


def themes(*entries):
    # entries: (name, status, version)
    return [{"name": n, "status": s, "version": v} for n, s, v in entries]


def plugins(*entries):
    return [{"name": n, "status": s, "version": v} for n, s, v in entries]


class TestDetectBuilder:
    def test_divi_theme_v4(self):
        info = detect_builder(
            template="Divi", stylesheet="Divi",
            themes=themes(("Divi", "active", "4.27.6")),
            plugins=plugins(),
        )
        assert info.builder == "divi"
        assert info.divi_major == 4
        assert info.source == "theme"
        assert info.version == "4.27.6"

    def test_divi_theme_v5(self):
        info = detect_builder(
            template="Divi", stylesheet="Divi",
            themes=themes(("Divi", "active", "5.1.1")),
            plugins=plugins(),
        )
        assert info.builder == "divi"
        assert info.divi_major == 5

    def test_divi_child_theme_detected_via_template(self):
        # Child theme active (stylesheet), but parent template is Divi.
        info = detect_builder(
            template="Divi", stylesheet="child-theme",
            themes=themes(("Divi", "inactive", "4.25.0"), ("child-theme", "active", "1.0")),
            plugins=plugins(),
        )
        assert info.builder == "divi"
        assert info.divi_major == 4
        assert info.source == "theme"

    def test_divi_builder_plugin_with_other_theme(self):
        info = detect_builder(
            template="astra", stylesheet="astra",
            themes=themes(("astra", "active", "4.1")),
            plugins=plugins(("divi-builder", "active", "4.27.6")),
        )
        assert info.builder == "divi"
        assert info.divi_major == 4
        assert info.source == "plugin"

    def test_elementor_when_no_divi(self):
        info = detect_builder(
            template="hello-elementor", stylesheet="hello-elementor",
            themes=themes(("hello-elementor", "active", "3.0")),
            plugins=plugins(("elementor", "active", "3.21.0")),
        )
        assert info.builder == "elementor"
        assert info.divi_major is None
        assert info.version == "3.21.0"

    def test_gutenberg_default(self):
        info = detect_builder(
            template="twentytwentyfour", stylesheet="twentytwentyfour",
            themes=themes(("twentytwentyfour", "active", "1.2")),
            plugins=plugins(("akismet", "active", "5.3")),
        )
        assert info.builder == "gutenberg"
        assert info.divi_major is None

    def test_divi_wins_over_elementor_when_both_present(self):
        info = detect_builder(
            template="Divi", stylesheet="Divi",
            themes=themes(("Divi", "active", "4.27.6")),
            plugins=plugins(("elementor", "active", "3.21.0")),
        )
        assert info.builder == "divi"

    def test_inactive_elementor_ignored(self):
        info = detect_builder(
            template="twentytwentyfour", stylesheet="twentytwentyfour",
            themes=themes(("twentytwentyfour", "active", "1.2")),
            plugins=plugins(("elementor", "inactive", "3.21.0")),
        )
        assert info.builder == "gutenberg"

    def test_returns_builderinfo(self):
        info = detect_builder("Divi", "Divi", themes(("Divi", "active", "4.0")), plugins())
        assert isinstance(info, BuilderInfo)

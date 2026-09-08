"""Tests for the builder-aware render dispatcher."""
import pytest

from wp_ops_mcp.builders.render import render_content, UnsupportedBuilderError

BLOCKS = [{"kind": "paragraph", "text": "hi"}]


def test_divi4_routes_to_shortcodes():
    r = render_content(BLOCKS, builder="divi", divi_major=4)
    assert r.content.startswith("[et_pb_section")
    assert r.meta.get("_et_pb_use_builder") == "on"


def test_gutenberg_routes_to_block_markup():
    r = render_content(BLOCKS, builder="gutenberg", divi_major=None)
    assert "<!-- wp:paragraph -->" in r.content
    assert r.meta == {}


def test_divi5_is_deferred_not_guessed():
    with pytest.raises(UnsupportedBuilderError) as exc:
        render_content(BLOCKS, builder="divi", divi_major=5)
    assert "5" in str(exc.value)


def test_elementor_is_deferred():
    with pytest.raises(UnsupportedBuilderError):
        render_content(BLOCKS, builder="elementor", divi_major=None)


def test_divi_unknown_major_raises_rather_than_guess():
    with pytest.raises(UnsupportedBuilderError):
        render_content(BLOCKS, builder="divi", divi_major=None)


def test_unknown_builder_falls_back_to_gutenberg():
    r = render_content(BLOCKS, builder="something-else", divi_major=None)
    assert "<!-- wp:paragraph -->" in r.content

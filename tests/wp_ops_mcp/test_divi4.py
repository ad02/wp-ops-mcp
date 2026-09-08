"""Tests for Divi 4 (shortcode) rendering."""
from wp_ops_mcp.builders.divi4 import render_divi4
from wp_ops_mcp.builders.divi4_parse import parse_divi4, serialize_divi4
from wp_ops_mcp.builders.rendered import RenderedContent


def test_returns_rendered_content():
    r = render_divi4([{"kind": "paragraph", "text": "hi"}])
    assert isinstance(r, RenderedContent)


def test_wraps_in_section_row_column():
    r = render_divi4([{"kind": "paragraph", "text": "hi"}])
    assert r.content.startswith("[et_pb_section")
    assert r.content.rstrip().endswith("[/et_pb_section]")
    assert "[et_pb_row" in r.content
    assert '[et_pb_column type="4_4"' in r.content


def test_sets_divi_builder_meta():
    r = render_divi4([])
    assert r.meta.get("_et_pb_use_builder") == "on"
    assert "_et_pb_old_content" in r.meta


def test_heading_renders_et_pb_text_with_h_tag():
    r = render_divi4([{"kind": "heading", "level": 2, "text": "Our Services"}])
    assert "[et_pb_text" in r.content
    assert "<h2>Our Services</h2>" in r.content
    assert "[/et_pb_text]" in r.content


def test_heading_default_level_is_2():
    r = render_divi4([{"kind": "heading", "text": "T"}])
    assert "<h2>T</h2>" in r.content


def test_paragraph_renders_p_in_et_pb_text():
    r = render_divi4([{"kind": "paragraph", "text": "Hello world"}])
    assert "<p>Hello world</p>" in r.content


def test_button_renders_self_closing_et_pb_button():
    r = render_divi4([{"kind": "button", "text": "Book Now", "url": "https://x.com/book"}])
    assert "[et_pb_button" in r.content
    assert 'button_text="Book Now"' in r.content
    assert 'button_url="https://x.com/book"' in r.content
    assert "/]" in r.content  # self-closing


def test_html_escapes_text_content():
    r = render_divi4([{"kind": "paragraph", "text": "A & B <script>"}])
    assert "A &amp; B &lt;script&gt;" in r.content
    assert "<script>" not in r.content


def test_escapes_quotes_in_button_attributes():
    r = render_divi4([{"kind": "button", "text": 'Say "hi"', "url": "https://x"}])
    # a raw double-quote in an attr value would break the shortcode
    assert 'button_text="Say "hi""' not in r.content
    assert "&quot;" in r.content


def test_multiple_blocks_render_in_order():
    r = render_divi4([
        {"kind": "heading", "level": 1, "text": "Title"},
        {"kind": "paragraph", "text": "Body"},
    ])
    assert r.content.index("<h1>Title</h1>") < r.content.index("<p>Body</p>")


def test_unknown_block_kind_is_skipped_safely():
    r = render_divi4([{"kind": "carousel", "text": "x"}, {"kind": "paragraph", "text": "ok"}])
    assert "<p>ok</p>" in r.content


def test_image_renders_self_closing_et_pb_image_with_src_and_alt():
    r = render_divi4([{"kind": "image", "url": "https://x.com/a.png", "alt": "A photo"}])
    assert "[et_pb_image" in r.content
    assert 'src="https://x.com/a.png"' in r.content
    assert 'alt="A photo"' in r.content
    assert "/]" in r.content  # self-closing


def test_image_without_alt_omits_the_alt_attribute():
    r = render_divi4([{"kind": "image", "url": "https://x.com/a.png"}])
    assert 'src="https://x.com/a.png"' in r.content
    assert "alt=" not in r.content


def test_image_without_url_is_skipped():
    r = render_divi4([
        {"kind": "image", "url": ""},
        {"kind": "image", "alt": "no source"},
        {"kind": "paragraph", "text": "ok"},
    ])
    assert "[et_pb_image" not in r.content
    assert "<p>ok</p>" in r.content


def test_image_module_parses_and_round_trips_byte_identical():
    # The edit layer refuses any page whose content does not round-trip, so a module
    # this renderer emits must survive parse -> serialize unchanged.
    r = render_divi4([{"kind": "image", "url": "https://x.com/a.png", "alt": "A photo"}])
    root = parse_divi4(r.content)
    column = root.children[0].children[0].children[0]
    assert column.tag == "et_pb_column"
    assert [c.tag for c in column.children if c.tag != "#text"] == ["et_pb_image"]
    assert serialize_divi4(root) == r.content

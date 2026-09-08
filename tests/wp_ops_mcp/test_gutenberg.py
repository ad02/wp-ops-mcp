"""Tests for Gutenberg block-markup rendering."""
from wp_ops_mcp.builders.block_parse import parse_blocks, serialize_blocks
from wp_ops_mcp.builders.gutenberg import render_gutenberg
from wp_ops_mcp.builders.rendered import RenderedContent


def test_returns_rendered_content_no_meta():
    r = render_gutenberg([{"kind": "paragraph", "text": "hi"}])
    assert isinstance(r, RenderedContent)
    assert r.meta == {}


def test_heading_block():
    r = render_gutenberg([{"kind": "heading", "level": 3, "text": "Services"}])
    assert "<!-- wp:heading" in r.content
    assert '"level":3' in r.content
    assert "<h3>Services</h3>" in r.content
    assert "<!-- /wp:heading -->" in r.content


def test_paragraph_block():
    r = render_gutenberg([{"kind": "paragraph", "text": "Hello world"}])
    assert "<!-- wp:paragraph -->" in r.content
    assert "<p>Hello world</p>" in r.content
    assert "<!-- /wp:paragraph -->" in r.content


def test_button_block():
    r = render_gutenberg([{"kind": "button", "text": "Book", "url": "https://x/book"}])
    assert "<!-- wp:button" in r.content
    assert 'href="https://x/book"' in r.content
    assert ">Book<" in r.content


def test_html_escapes_text():
    r = render_gutenberg([{"kind": "paragraph", "text": "A & B <x>"}])
    assert "A &amp; B &lt;x&gt;" in r.content
    assert "<x>" not in r.content


def test_blocks_in_order():
    r = render_gutenberg([
        {"kind": "heading", "level": 1, "text": "Title"},
        {"kind": "paragraph", "text": "Body"},
    ])
    assert r.content.index("Title") < r.content.index("Body")


def test_unknown_kind_skipped():
    r = render_gutenberg([{"kind": "video", "text": "x"}, {"kind": "paragraph", "text": "ok"}])
    assert "<p>ok</p>" in r.content


def test_image_block():
    r = render_gutenberg([{"kind": "image", "url": "https://x/a.png", "alt": "A photo"}])
    assert "<!-- wp:image -->" in r.content
    assert '<figure class="wp-block-image">' in r.content
    assert '<img src="https://x/a.png" alt="A photo"/>' in r.content
    assert "<!-- /wp:image -->" in r.content


def test_image_block_keeps_empty_alt_attribute():
    # WP core always emits alt on core/image, empty when there is no alt text.
    r = render_gutenberg([{"kind": "image", "url": "https://x/a.png"}])
    assert '<img src="https://x/a.png" alt=""/>' in r.content


def test_image_without_url_skipped():
    r = render_gutenberg([
        {"kind": "image", "url": ""},
        {"kind": "image", "alt": "no source"},
        {"kind": "paragraph", "text": "ok"},
    ])
    assert "wp:image" not in r.content
    assert "<p>ok</p>" in r.content


def test_image_block_parses_and_round_trips_byte_identical():
    r = render_gutenberg([{"kind": "image", "url": "https://x/a.png", "alt": "A photo"}])
    root = parse_blocks(r.content)
    assert [c.tag for c in root.children if c.tag != "#text"] == ["core/image"]
    assert serialize_blocks(root) == r.content

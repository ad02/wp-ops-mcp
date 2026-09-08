import pytest
from wp_ops_mcp.builders.block_parse import (
    parse_blocks, serialize_blocks, roundtrip_ok_blocks, ParseError)

GB = ('<!-- wp:heading {"level":2} -->\n<h2>Title</h2>\n<!-- /wp:heading -->\n\n'
      '<!-- wp:paragraph -->\n<p>Body text</p>\n<!-- /wp:paragraph -->\n\n'
      '<!-- wp:spacer {"height":"40px"} /-->')

D5 = ('<!-- wp:divi/section {"module":{"advanced":{}}} -->'
      '<!-- wp:divi/row -->'
      '<!-- wp:divi/text {"content":"x"} /-->'
      '<!-- /wp:divi/row -->'
      '<!-- /wp:divi/section -->')


def test_parse_gutenberg_tree():
    root = parse_blocks(GB)
    tags = [c.tag for c in root.children if c.tag != "#text"]
    assert tags == ["core/heading", "core/paragraph", "core/spacer"]
    heading = root.children[0]
    assert heading.attrs["_json"] == '{"level":2}'
    assert heading.content == "\n<h2>Title</h2>\n"


def test_parse_divi5_nesting():
    root = parse_blocks(D5)
    sec = root.children[0]
    assert sec.tag == "divi/section"
    assert sec.children[0].tag == "divi/row"
    assert sec.children[0].children[0].self_closing is True


def test_roundtrips_are_byte_identical():
    assert serialize_blocks(parse_blocks(GB)) == GB
    assert serialize_blocks(parse_blocks(D5)) == D5
    assert roundtrip_ok_blocks(GB) is True


def test_unbalanced_block_raises():
    with pytest.raises(ParseError):
        parse_blocks('<!-- wp:divi/section --><!-- /wp:divi/row -->')


# --- adversarial cases beyond the brief (self-review) ---

NESTED = ('<!-- wp:group -->A'
          '<!-- wp:group -->B'
          '<!-- wp:paragraph -->x<!-- /wp:paragraph -->'
          '<!-- /wp:group -->'
          '<!-- /wp:group -->')


def test_nested_same_name_blocks():
    root = parse_blocks(NESTED)
    outer = root.children[0]
    assert outer.tag == "core/group"
    # inner group is a child element (after the "A" #text run)
    inner = [c for c in outer.children if c.tag != "#text"][0]
    assert inner.tag == "core/group"
    assert roundtrip_ok_blocks(NESTED) is True


def test_html_comment_that_is_not_a_block_is_content():
    # A plain <!-- ... --> comment (no wp:) stays inside inner HTML untouched.
    src = ('<!-- wp:paragraph -->\n<p><!-- keep me --></p>\n<!-- /wp:paragraph -->')
    root = parse_blocks(src)
    para = root.children[0]
    assert para.tag == "core/paragraph"
    assert "<!-- keep me -->" in para.content
    assert serialize_blocks(root) == src


def test_json_with_brace_in_string_value():
    # Non-greedy {.*?} must extend past a "}" that lives inside a JSON string.
    src = '<!-- wp:foo {"a":"}"} /-->'
    root = parse_blocks(src)
    assert root.children[0].attrs["_json"] == '{"a":"}"}'
    assert serialize_blocks(root) == src


def test_json_containing_arrow_roundtrips():
    # WordPress escapes "--" in practice; confirm the anchored regex still keeps
    # bytes intact when a value string happens to hold "-->".
    src = '<!-- wp:foo {"a":"-->"} /-->'
    assert roundtrip_ok_blocks(src) is True


def test_no_space_before_arrow_is_not_a_block():
    # WP delimiters require a space before -->; a malformed one falls through
    # to plain text rather than mis-parsing into a block.
    src = '<!-- wp:foo-->'
    root = parse_blocks(src)
    assert [c.tag for c in root.children] == ["#text"]
    assert serialize_blocks(root) == src


def test_unclosed_block_raises():
    with pytest.raises(ParseError):
        parse_blocks('<!-- wp:core/group -->still open')


def test_explicit_core_namespace_parses_as_core():
    # Source keeps its bytes via raw; the parsed tag normalizes to core/*.
    src = '<!-- wp:core/paragraph -->hi<!-- /wp:core/paragraph -->'
    root = parse_blocks(src)
    assert root.children[0].tag == "core/paragraph"
    assert serialize_blocks(root) == src  # raw slice preserves the explicit ns

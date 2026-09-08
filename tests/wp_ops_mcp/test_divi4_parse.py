import pytest
from wp_ops_mcp.builders.divi4_parse import parse_divi4, parse_attrs, ParseError
from wp_ops_mcp.builders.divi4_parse import serialize_divi4, roundtrip_ok
from wp_ops_mcp.builders.model import clear_raw_on_path, get_node

SIMPLE = ('[et_pb_section fb_built="1" _builder_version="4.27.6"]'
          '[et_pb_row _builder_version="4.27.6"]'
          '[et_pb_column type="4_4" _builder_version="4.27.6"]'
          '[et_pb_text _builder_version="4.27.6"]<p>Hello</p>[/et_pb_text]'
          '[et_pb_button button_text="Call" button_url="/contact/" /]'
          '[/et_pb_column][/et_pb_row][/et_pb_section]')


def test_parse_builds_nested_tree():
    root = parse_divi4(SIMPLE)
    sec = root.children[0]
    assert sec.tag == "et_pb_section"
    text = sec.children[0].children[0].children[0]
    assert text.tag == "et_pb_text"
    assert text.content == "<p>Hello</p>"
    button = sec.children[0].children[0].children[1]
    assert button.self_closing is True
    assert button.attrs["button_url"] == "/contact/"


def test_raw_slices_are_exact():
    root = parse_divi4(SIMPLE)
    assert root.children[0].raw == SIMPLE          # whole section is the whole source


def test_non_et_shortcodes_stay_inside_text_content():
    src = ('[et_pb_section][et_pb_row][et_pb_column type="4_4"]'
           '[et_pb_text]look [metaslider id=7] here[/et_pb_text]'
           '[/et_pb_column][/et_pb_row][/et_pb_section]')
    root = parse_divi4(src)
    text = root.children[0].children[0].children[0].children[0]
    assert text.content == "look [metaslider id=7] here"


def test_text_between_sections_preserved():
    src = ('[et_pb_section][/et_pb_section]\n\n'
           '[et_pb_section][/et_pb_section]')
    root = parse_divi4(src)
    assert [c.tag for c in root.children] == ["et_pb_section", "#text", "et_pb_section"]
    assert root.children[1].content == "\n\n"


def test_attrs_preserve_order_and_divi_encoding():
    attrs = parse_attrs(' custom_padding="10px|%22|10px" admin_label="My Row"')
    assert list(attrs) == ["custom_padding", "admin_label"]
    assert attrs["custom_padding"] == "10px|%22|10px"


def test_unbalanced_raises():
    with pytest.raises(ParseError):
        parse_divi4('[et_pb_section][et_pb_row][/et_pb_section]')


def test_attr_value_with_literal_bracket_parses_correctly():
    src = '[et_pb_row admin_label="Draft [DO NOT USE]"][/et_pb_row]'
    root = parse_divi4(src)
    assert root.children[0].attrs["admin_label"] == "Draft [DO NOT USE]"
    assert root.children[0].content == ""
    assert root.children[0].raw == src


def test_unbalanced_lookalike_in_content_raises():
    # loud failure -> EditOps will refuse the page; never a silent wrong tree
    with pytest.raises(ParseError):
        parse_divi4('[et_pb_text]See example [et_pb_button] usage[/et_pb_text]')


def test_balanced_lookalike_keeps_byte_identical_raw():
    src = '[et_pb_text]doc: [et_pb_button][/et_pb_button] end[/et_pb_text]'
    root = parse_divi4(src)
    assert root.children[0].raw == src   # phantom structure possible, bytes never lost


MESSY = ('<!-- pre -->\n'
         '[et_pb_section fb_built="1" custom_padding="0px|%22|0px"]'
         '[et_pb_row][et_pb_column type="4_4"]'
         '[et_pb_text admin_label="Body"]<p>a "quoted" thing &amp; more</p>[/et_pb_text]'
         '[et_pb_divider /]'
         '[/et_pb_column][/et_pb_row][/et_pb_section]\n')


def test_roundtrip_is_byte_identical():
    for src in (SIMPLE, MESSY):
        assert serialize_divi4(parse_divi4(src)) == src


def test_roundtrip_ok_helper():
    assert roundtrip_ok(MESSY) is True
    assert roundtrip_ok('[et_pb_section][et_pb_row][/et_pb_section]') is False


def test_edited_node_rerenders_but_siblings_stay_verbatim():
    root = parse_divi4(MESSY)
    addr = [1, 0, 0, 0]                       # the et_pb_text (index 0 is the '<!-- pre -->\n' text)
    node = get_node(root, addr)
    node.content = "<p>new body</p>"
    clear_raw_on_path(root, addr)
    out = serialize_divi4(root)
    assert "<p>new body</p>" in out
    assert '[et_pb_divider /]' in out          # untouched sibling kept verbatim
    assert out.startswith('<!-- pre -->\n')    # untouched leading text kept
    assert 'custom_padding="0px|%22|0px"' in out  # attrs of re-rendered ancestors preserved


def test_roundtrip_adversarial_strings():
    # attrs whose values contain literal "]", self-closing without a space,
    # and Divi %22 encodings must all survive byte-for-byte.
    cases = [
        '[et_pb_row admin_label="a]b]c"][/et_pb_row]',
        '[et_pb_divider/]',
        '[et_pb_section custom_margin="||%22||"][et_pb_row /][/et_pb_section]',
        'plain text with no shortcodes at all',
        '',
    ]
    for src in cases:
        assert serialize_divi4(parse_divi4(src)) == src
        assert roundtrip_ok(src) is True

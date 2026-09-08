# tests/wp_ops_mcp/test_model.py
from wp_ops_mcp.builders.model import Node, get_node, clear_raw_on_path, find_nodes


def _tree():
    leaf = Node(tag="et_pb_text", attrs={"admin_label": "Intro"}, children=[],
                content="<p>Hello world</p>", self_closing=False, raw="[et_pb_text...]")
    col = Node(tag="et_pb_column", attrs={}, children=[leaf], content="",
               self_closing=False, raw="[et_pb_column...]")
    row = Node(tag="et_pb_row", attrs={}, children=[col], content="",
               self_closing=False, raw="[et_pb_row...]")
    sec = Node(tag="et_pb_section", attrs={}, children=[row], content="",
               self_closing=False, raw="[et_pb_section...]")
    root = Node(tag="#root", attrs={}, children=[sec], content="", self_closing=False, raw=None)
    return root


def test_get_node_walks_addresses():
    root = _tree()
    assert get_node(root, [0]).tag == "et_pb_section"
    assert get_node(root, [0, 0, 0, 0]).tag == "et_pb_text"


def test_get_node_bad_address_raises():
    import pytest
    with pytest.raises(IndexError):
        get_node(_tree(), [0, 5])


def test_clear_raw_on_path_clears_target_and_ancestors():
    root = _tree()
    clear_raw_on_path(root, [0, 0, 0, 0])
    assert get_node(root, [0, 0, 0, 0]).raw is None      # target
    assert get_node(root, [0]).raw is None               # ancestor section
    assert get_node(root, [0, 0]).raw is None            # ancestor row


def test_find_by_tag_and_label_and_text():
    root = _tree()
    assert find_nodes(root, tag="et_pb_text")[0][0] == [0, 0, 0, 0]
    assert find_nodes(root, admin_label="Intro")[0][1].tag == "et_pb_text"
    assert find_nodes(root, contains_text="Hello")[0][0] == [0, 0, 0, 0]
    assert find_nodes(root, contains_text="nope") == []


def test_find_skips_text_nodes():
    text = Node(tag="#text", attrs={}, children=[], content="Hello stray",
                self_closing=False, raw="Hello stray")
    elem = Node(tag="et_pb_text", attrs={"admin_label": "Body"}, children=[],
                content="<p>real</p>", self_closing=False, raw="[et_pb_text...]")
    root = Node(tag="#root", attrs={}, children=[text, elem], content="",
                self_closing=False, raw=None)
    assert find_nodes(root, contains_text="stray") == []
    assert find_nodes(root, tag="#text") == []


def test_contains_text_does_not_match_across_field_boundaries():
    leaf = Node(tag="et_pb_text", attrs={"admin_label": "Introduction"}, children=[],
                content="Hello world", self_closing=False, raw="[et_pb_text...]")
    root = Node(tag="#root", attrs={}, children=[leaf], content="",
                self_closing=False, raw=None)
    assert find_nodes(root, contains_text="worldIntro") == []
    assert find_nodes(root, contains_text="world")[0][1] is leaf

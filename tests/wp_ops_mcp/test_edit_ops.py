import pytest
from wp_ops_mcp.builders.block_parse import (
    parse_blocks, serialize_blocks, roundtrip_ok_blocks)
from wp_ops_mcp.builders.divi4_parse import parse_divi4, serialize_divi4
from wp_ops_mcp.builders.edit_ops import (
    apply_ops, DynamicContentError, TargetError)

SRC = ('[et_pb_section]'
       '[et_pb_row][et_pb_column type="4_4"]'
       '[et_pb_text admin_label="Body"]<p>old</p>[/et_pb_text]'
       '[et_pb_button button_text="Call" button_url="/old/" /]'
       '[/et_pb_column][/et_pb_row]'
       '[/et_pb_section]')


def _root():
    return parse_divi4(SRC)


def test_update_element_content_by_label():
    root = _root()
    res = apply_ops(root, [{"op": "update_element",
                            "target": {"admin_label": "Body"},
                            "content": "<p>new</p>"}], "divi4")
    assert res[0]["status"] == "applied"
    assert "<p>new</p>" in serialize_divi4(root)
    assert "<p>old</p>" not in serialize_divi4(root)


def test_set_attr_escapes_quotes_divi_style():
    root = _root()
    apply_ops(root, [{"op": "set_attr", "target": {"tag": "et_pb_button"},
                      "key": "button_text", "value": 'Say "hi"'}], "divi4")
    assert 'button_text="Say %22hi%22"' in serialize_divi4(root)


def test_ambiguous_target_raises_with_candidates():
    root = parse_divi4(SRC.replace("</p>[/et_pb_text]",
                       "</p>[/et_pb_text][et_pb_text]<p>two</p>[/et_pb_text]"))
    with pytest.raises(TargetError) as e:
        apply_ops(root, [{"op": "update_element", "target": {"tag": "et_pb_text"},
                          "content": "x"}], "divi4")
    assert "2 matches" in str(e.value)


def test_dynamic_content_is_refused():
    src = SRC.replace("<p>old</p>", "@ET-DC@eyJkeW4iOnRydWV9@")
    root = parse_divi4(src)
    with pytest.raises(DynamicContentError):
        apply_ops(root, [{"op": "update_element", "target": {"admin_label": "Body"},
                          "content": "x"}], "divi4")


def test_insert_section_at_end_and_remove_and_duplicate():
    root = _root()
    apply_ops(root, [
        {"op": "insert_section", "position": "end",
         "blocks": [{"kind": "heading", "level": 2, "text": "FAQ"}]},
        {"op": "duplicate_element", "target": {"tag": "et_pb_button"}},
        {"op": "remove_element", "target": {"admin_label": "Body"}},
    ], "divi4")
    out = serialize_divi4(root)
    assert "<h2>FAQ</h2>" in out
    assert out.count("[et_pb_button") == 2         # original + duplicate (self-closing)
    assert "admin_label" not in out


# --- extra tests (self-review carry-over) ---

def test_insert_section_after_top_level_address():
    """position={"after":[0]} at the top level inserts a sibling section at index 1."""
    root = _root()
    res = apply_ops(root, [{"op": "insert_section", "position": {"after": [0]},
                            "blocks": [{"kind": "heading", "level": 3, "text": "Mid"}]}],
                    "divi4")
    assert res[0]["status"] == "applied"
    assert res[0]["address"] == [1]
    # new section is a sibling of the original, right after it
    assert len(root.children) == 2
    assert root.children[1].tag == "et_pb_section"
    assert "<h3>Mid</h3>" in serialize_divi4(root)


def test_update_element_attrs_merge_goes_through_escaping():
    """attrs merge on update_element escapes quotes in divi4 dialect."""
    root = _root()
    apply_ops(root, [{"op": "update_element", "target": {"tag": "et_pb_button"},
                      "attrs": {"button_text": 'Book "now"', "button_url": "/x/"}}],
              "divi4")
    out = serialize_divi4(root)
    assert 'button_text="Book %22now%22"' in out
    assert 'button_url="/x/"' in out


def test_duplicate_deepcopy_is_independent():
    """The duplicate must not share the original's mutable children list."""
    root = _root()
    apply_ops(root, [{"op": "duplicate_element", "target": {"admin_label": "Body"}}],
              "divi4")
    column = root.children[0].children[0].children[0]
    body_nodes = [c for c in column.children if c.tag == "et_pb_text"]
    assert len(body_nodes) == 2
    assert body_nodes[0] is not body_nodes[1]
    assert body_nodes[0].children is not body_nodes[1].children


# --- insert_module coverage + promoted-content preservation ---

def test_insert_module_appends_to_column():
    """insert_module appends new modules AFTER existing ones, leaving them intact."""
    root = _root()
    res = apply_ops(root, [{"op": "insert_module", "target": {"tag": "et_pb_column"},
                            "blocks": [{"kind": "paragraph", "text": "added"}]}], "divi4")
    assert res[0]["status"] == "applied"
    out = serialize_divi4(root)
    assert "<p>old</p>" in out                          # existing module untouched
    assert "<p>added</p>" in out                        # new module present
    assert out.index("<p>added</p>") > out.index("<p>old</p>")   # appended after


def test_insert_module_preserves_promoted_content():
    """A column with plain-HTML (leaf-promoted) inner must keep it when a module is added."""
    src = ('[et_pb_section][et_pb_row]'
           '[et_pb_column type="4_4"]<p>promoted</p>[/et_pb_column]'
           '[/et_pb_row][/et_pb_section]')
    root = parse_divi4(src)
    apply_ops(root, [{"op": "insert_module", "target": {"tag": "et_pb_column"},
                      "blocks": [{"kind": "paragraph", "text": "added"}]}], "divi4")
    out = serialize_divi4(root)
    assert "<p>promoted</p>" in out                     # promoted content preserved
    assert "<p>added</p>" in out                        # new module present
    assert out.index("<p>promoted</p>") < out.index("<p>added</p>")   # promoted first


# --- insert_section nesting guard ---

def test_insert_section_rejects_nested_position():
    """Sections may only be inserted as top-level siblings, never inside a row/column."""
    root = _root()
    with pytest.raises(TargetError):
        apply_ops(root, [{"op": "insert_section", "position": {"after": [0, 0]},
                          "blocks": [{"kind": "heading", "level": 2, "text": "x"}]}],
                  "divi4")


# --- update_element attrs merge atomicity ---

def test_update_attrs_merge_is_atomic_on_dc_error():
    """If any attr key hits @ET-DC@, no key is mutated (all-or-nothing merge)."""
    src = ('[et_pb_section][et_pb_row][et_pb_column type="4_4"]'
           '[et_pb_text a="@ET-DC@x" b="clean"]<p>x</p>[/et_pb_text]'
           '[/et_pb_column][/et_pb_row][/et_pb_section]')
    root = parse_divi4(src)
    node = root.children[0].children[0].children[0].children[0]
    with pytest.raises(DynamicContentError):
        apply_ops(root, [{"op": "update_element", "target": {"tag": "et_pb_text"},
                          "attrs": {"b": "new", "a": "boom"}}], "divi4")
    assert node.attrs["b"] == "clean"                   # untouched despite earlier position


def test_update_element_content_and_attrs_is_atomic_on_dc_error():
    src = ('[et_pb_section][et_pb_row][et_pb_column type="4_4"]'
           '[et_pb_text a="@ET-DC@x"]<p>old</p>[/et_pb_text]'
           '[/et_pb_column][/et_pb_row][/et_pb_section]')
    root = parse_divi4(src)
    node = root.children[0].children[0].children[0].children[0]
    with pytest.raises(DynamicContentError):
        apply_ops(root, [{"op": "update_element", "target": {"tag": "et_pb_text"},
                          "content": "<p>NEW</p>", "attrs": {"a": "boom"}}], "divi4")
    assert node.content == "<p>old</p>"      # content untouched on failed op
    assert node.attrs["a"] == "@ET-DC@x"     # attrs untouched


# --- move_element -----------------------------------------------------------

# S1: row > [col1(A,B), col2(C)]   S2: row > [col3(D)]
# addresses: S1=[0] row=[0,0] col1=[0,0,0] A=[0,0,0,0] B=[0,0,0,1]
#            col2=[0,0,1] C=[0,0,1,0] | S2=[1] col3=[1,0,0] D=[1,0,0,0]
S1_SRC = ('[et_pb_section admin_label="S1"][et_pb_row]'
          '[et_pb_column type="1_2"]'
          '[et_pb_text admin_label="A"]<p>A</p>[/et_pb_text]'
          '[et_pb_text admin_label="B"]<p>B</p>[/et_pb_text]'
          '[/et_pb_column]'
          '[et_pb_column type="1_2"]'
          '[et_pb_text admin_label="C"]<p>C</p>[/et_pb_text]'
          '[/et_pb_column]'
          '[/et_pb_row][/et_pb_section]')
S2_SRC = ('[et_pb_section admin_label="S2"][et_pb_row]'
          '[et_pb_column type="4_4"]'
          '[et_pb_text admin_label="D"]<p>D</p>[/et_pb_text]'
          '[/et_pb_column]'
          '[/et_pb_row][/et_pb_section]')
MOVE_SRC = S1_SRC + S2_SRC

P_ONE = '<!-- wp:paragraph --><p>one</p><!-- /wp:paragraph -->'
P_TWO = '<!-- wp:paragraph --><p>two</p><!-- /wp:paragraph -->'
P_X = '<!-- wp:paragraph --><p>x</p><!-- /wp:paragraph -->'
P_Y = '<!-- wp:paragraph --><p>y</p><!-- /wp:paragraph -->'
HEAD = '<!-- wp:heading --><h2>H</h2><!-- /wp:heading -->'


def _move_root():
    return parse_divi4(MOVE_SRC)


def test_move_module_before_module_in_another_column():
    """Cross-column move reorders output; every untouched node keeps its bytes."""
    root = _move_root()
    res = apply_ops(root, [{"op": "move_element", "target": {"admin_label": "A"},
                            "position": {"before": [0, 0, 1, 0]}}], "divi4")
    assert res[0]["status"] == "applied"
    assert res[0]["address"] == [0, 0, 1, 0]        # A is now first in column 2
    out = serialize_divi4(root)
    assert out.index("<p>B</p>") < out.index("<p>A</p>") < out.index("<p>C</p>")
    assert out == (
        '[et_pb_section admin_label="S1"][et_pb_row]'
        '[et_pb_column type="1_2"]'
        '[et_pb_text admin_label="B"]<p>B</p>[/et_pb_text]'
        '[/et_pb_column]'
        '[et_pb_column type="1_2"]'
        '[et_pb_text admin_label="A"]<p>A</p>[/et_pb_text]'
        '[et_pb_text admin_label="C"]<p>C</p>[/et_pb_text]'
        '[/et_pb_column]'
        '[/et_pb_row][/et_pb_section]') + S2_SRC


def test_move_section_after_section_reorders_root():
    root = _move_root()
    res = apply_ops(root, [{"op": "move_element", "target": {"admin_label": "S1"},
                            "position": {"after": [1]}}], "divi4")
    assert res[0]["address"] == [1]
    assert serialize_divi4(root) == S2_SRC + S1_SRC   # order swapped, bytes preserved


def test_move_append_puts_module_at_end_of_column():
    root = _move_root()
    res = apply_ops(root, [{"op": "move_element", "target": {"admin_label": "A"},
                            "position": {"append": [0, 0, 1]}}], "divi4")
    assert res[0]["address"] == [0, 0, 1, 1]        # appended after C
    assert serialize_divi4(root) == (
        '[et_pb_section admin_label="S1"][et_pb_row]'
        '[et_pb_column type="1_2"]'
        '[et_pb_text admin_label="B"]<p>B</p>[/et_pb_text]'
        '[/et_pb_column]'
        '[et_pb_column type="1_2"]'
        '[et_pb_text admin_label="C"]<p>C</p>[/et_pb_text]'
        '[et_pb_text admin_label="A"]<p>A</p>[/et_pb_text]'
        '[/et_pb_column]'
        '[/et_pb_row][/et_pb_section]') + S2_SRC


def test_move_within_same_parent_both_directions():
    """Removal shifts sibling indices - both directions must still land right."""
    root = _move_root()
    apply_ops(root, [{"op": "move_element", "target": {"admin_label": "A"},
                      "position": {"after": [0, 0, 0, 1]}}], "divi4")     # A after B
    out = serialize_divi4(root)
    assert out.index("<p>B</p>") < out.index("<p>A</p>")
    apply_ops(root, [{"op": "move_element", "target": {"admin_label": "A"},
                      "position": {"before": [0, 0, 0, 0]}}], "divi4")    # A before B
    assert serialize_divi4(root) == MOVE_SRC        # exact restoration


def test_move_uses_identity_not_equality_for_destination_index():
    """Twin siblings are == to each other; only identity picks the right slot."""
    src = ('[et_pb_section][et_pb_row]'
           '[et_pb_column type="1_2"]'
           '[et_pb_text admin_label="A"]<p>A</p>[/et_pb_text][/et_pb_column]'
           '[et_pb_column type="1_2"]'
           '[et_pb_text]<p>t</p>[/et_pb_text]'
           '[et_pb_text]<p>t</p>[/et_pb_text]'
           '[/et_pb_column][/et_pb_row][/et_pb_section]')
    root = parse_divi4(src)
    apply_ops(root, [{"op": "move_element", "target": {"admin_label": "A"},
                      "position": {"before": [0, 0, 1, 1]}}], "divi4")   # before 2nd twin
    col2 = root.children[0].children[0].children[1]
    assert [c.attrs.get("admin_label") for c in col2.children] == [None, "A", None]


def test_move_append_preserves_promoted_content_of_destination():
    """A leaf-promoted destination keeps its inline HTML (as insert_module does)."""
    src = ('[et_pb_section][et_pb_row]'
           '[et_pb_column type="1_2"]'
           '[et_pb_text admin_label="A"]<p>A</p>[/et_pb_text][/et_pb_column]'
           '[et_pb_column type="1_2"]<p>promoted</p>[/et_pb_column]'
           '[/et_pb_row][/et_pb_section]')
    root = parse_divi4(src)
    apply_ops(root, [{"op": "move_element", "target": {"admin_label": "A"},
                      "position": {"append": [0, 0, 1]}}], "divi4")
    assert serialize_divi4(root) == (
        '[et_pb_section][et_pb_row]'
        '[et_pb_column type="1_2"][/et_pb_column]'
        '[et_pb_column type="1_2"]<p>promoted</p>'
        '[et_pb_text admin_label="A"]<p>A</p>[/et_pb_text][/et_pb_column]'
        '[/et_pb_row][/et_pb_section]')


def test_move_into_self_closing_node_is_refused():
    """Self-closing tags never serialize children - appending would drop the node."""
    src = ('[et_pb_section][et_pb_row]'
           '[et_pb_column type="1_2"]'
           '[et_pb_text admin_label="A"]<p>A</p>[/et_pb_text][/et_pb_column]'
           '[et_pb_column type="1_2" /]'
           '[/et_pb_row][/et_pb_section]')
    root = parse_divi4(src)
    before = serialize_divi4(root)
    with pytest.raises(TargetError) as e:
        apply_ops(root, [{"op": "move_element", "target": {"admin_label": "A"},
                          "position": {"append": [0, 0, 1]}}], "divi4")
    assert "self-closing" in str(e.value)
    assert serialize_divi4(root) == before


def test_move_into_self_closing_block_is_refused():
    root = parse_blocks(P_ONE + '<!-- wp:spacer /-->')
    before = serialize_blocks(root)
    with pytest.raises(TargetError) as e:
        apply_ops(root, [{"op": "move_element", "target": {"address": [0]},
                          "position": {"append": [1]}}], "blocks")
    assert "self-closing" in str(e.value)
    assert serialize_blocks(root) == before


def test_move_into_own_subtree_is_refused():
    root = _move_root()
    before = serialize_divi4(root)
    with pytest.raises(TargetError) as e:
        apply_ops(root, [{"op": "move_element", "target": {"address": [0]},
                          "position": {"append": [0, 0, 0]}}], "divi4")
    assert "subtree" in str(e.value)
    assert serialize_divi4(root) == before


def test_move_refuses_divi4_structure_violations():
    """Row out of a section, module out of a column, and any column move."""
    root = _move_root()
    before = serialize_divi4(root)

    with pytest.raises(TargetError) as row_e:       # row -> root
        apply_ops(root, [{"op": "move_element", "target": {"address": [0, 0]},
                          "position": {"append": []}}], "divi4")
    assert "et_pb_row" in str(row_e.value) and "et_pb_section" in str(row_e.value)

    with pytest.raises(TargetError) as mod_e:       # module -> row
        apply_ops(root, [{"op": "move_element", "target": {"admin_label": "A"},
                          "position": {"append": [0, 0]}}], "divi4")
    assert "et_pb_column" in str(mod_e.value)

    with pytest.raises(TargetError) as col_e:       # column -> anywhere
        apply_ops(root, [{"op": "move_element", "target": {"address": [0, 0, 1]},
                          "position": {"append": [1, 0]}}], "divi4")
    assert "et_pb_column" in str(col_e.value) and "cannot be moved" in str(col_e.value)

    assert serialize_divi4(root) == before


def test_move_refuses_text_node_target():
    src = ('[et_pb_section][et_pb_row][et_pb_column type="4_4"]'
           'loose text[et_pb_text]<p>t</p>[/et_pb_text]'
           '[/et_pb_column][/et_pb_row][/et_pb_section]')
    root = parse_divi4(src)
    before = serialize_divi4(root)
    with pytest.raises(TargetError) as e:
        apply_ops(root, [{"op": "move_element", "target": {"address": [0, 0, 0, 0]},
                          "position": {"after": [0, 0, 0, 1]}}], "divi4")
    assert "#text" in str(e.value)
    assert serialize_divi4(root) == before


def test_move_top_level_block_in_blocks_dialect():
    root = parse_blocks(P_ONE + P_TWO + HEAD)
    res = apply_ops(root, [{"op": "move_element", "target": {"address": [0]},
                            "position": {"after": [2]}}], "blocks")
    assert res[0]["status"] == "applied"
    assert res[0]["address"] == [2]
    assert serialize_blocks(root) == P_TWO + HEAD + P_ONE


def test_move_clears_old_parent_chain_when_its_address_shifts():
    """Moving a nested node to the front shifts its old parent's address; the old
    parent must still be re-rendered or its stale raw duplicates the moved node."""
    root = parse_blocks(P_ONE + '<!-- wp:group -->' + P_X + P_Y + '<!-- /wp:group -->')
    apply_ops(root, [{"op": "move_element", "target": {"address": [1, 1]},
                      "position": {"before": [0]}}], "blocks")
    out = serialize_blocks(root)
    assert out.count("<p>y</p>") == 1
    assert out == P_Y + P_ONE + '<!-- wp:group -->' + P_X + '<!-- /wp:group -->'


def test_move_into_text_node_is_refused_in_blocks_dialect():
    root = parse_blocks(P_ONE + "\n\ntail")
    before = serialize_blocks(root)
    with pytest.raises(TargetError) as e:
        apply_ops(root, [{"op": "move_element", "target": {"address": [0]},
                          "position": {"append": [1]}}], "blocks")
    assert "#text" in str(e.value)
    assert serialize_blocks(root) == before


@pytest.mark.parametrize("op", [
    pytest.param({"op": "move_element", "target": {"admin_label": "A"},
                  "position": {"before": [0, 0, 9]}}, id="bad-dest-address"),
    pytest.param({"op": "move_element", "target": {"admin_label": "A"},
                  "position": {"before": [0, 0, 0, 0], "after": [1]}}, id="two-keys"),
    pytest.param({"op": "move_element", "target": {"admin_label": "A"}},
                 id="no-position"),
    pytest.param({"op": "move_element", "target": {"admin_label": "A"},
                  "position": {"inside": [0, 0, 1]}}, id="unknown-mode"),
    pytest.param({"op": "move_element", "target": {"admin_label": "A"},
                  "position": {"before": [0, 0, 0, 0]}}, id="relative-to-itself"),
    pytest.param({"op": "move_element", "target": {"address": []},
                  "position": {"append": [0, 0, 0]}}, id="root-as-target"),
    pytest.param({"op": "move_element", "target": {"address": [0]},
                  "position": {"after": []}}, id="after-root"),
    pytest.param({"op": "move_element", "target": {"admin_label": "A"},
                  "position": {"append": [0, 0]}}, id="module-into-row"),
])
def test_move_refusals_leave_the_tree_byte_identical(op):
    root = _move_root()
    before = serialize_divi4(root)
    with pytest.raises(TargetError):
        apply_ops(root, [op], "divi4")
    assert serialize_divi4(root) == before


# --- blocks-dialect insert_section (rendered by the gutenberg renderer) ------

IMG_URL = "https://cdn.test/hero.jpg"
IMG_BLOCK = {"kind": "image", "url": IMG_URL, "alt": "Hero"}
FAQ_BLOCK = {"kind": "heading", "level": 2, "text": "FAQ"}


def test_insert_section_blocks_dialect_appends_all_rendered_nodes():
    """Every top-level node the renderer produced lands at the root, in order."""
    root = parse_blocks(P_ONE)
    res = apply_ops(root, [{"op": "insert_section", "position": "end",
                            "blocks": [FAQ_BLOCK, IMG_BLOCK]}], "blocks")
    assert res[0]["status"] == "applied"
    assert res[0]["address"] == [1]                  # first inserted node
    assert [c.tag for c in root.children if c.tag != "#text"] == [
        "core/paragraph", "core/heading", "core/image"]
    out = serialize_blocks(root)
    assert out.startswith(P_ONE)                     # existing bytes untouched
    assert out.index("<h2>FAQ</h2>") < out.index(f'src="{IMG_URL}"')


def test_insert_section_blocks_dialect_after_top_level_address():
    root = parse_blocks(P_ONE + P_TWO)
    res = apply_ops(root, [{"op": "insert_section", "position": {"after": [0]},
                            "blocks": [{"kind": "heading", "level": 3, "text": "Mid"}]}],
                    "blocks")
    assert res[0]["status"] == "applied"
    assert res[0]["address"] == [1]
    out = serialize_blocks(root)
    assert out.index("<p>one</p>") < out.index("<h3>Mid</h3>") < out.index("<p>two</p>")
    assert out.startswith(P_ONE) and out.endswith(P_TWO)   # neighbours byte-preserved


def test_insert_section_blocks_dialect_output_round_trips():
    """The edited page must re-parse byte-identically or the edit layer refuses it."""
    root = parse_blocks(P_ONE)
    apply_ops(root, [{"op": "insert_section", "position": "end",
                      "blocks": [FAQ_BLOCK, {"kind": "paragraph", "text": "answer"},
                                 {"kind": "button", "text": "Book", "url": "/book/"},
                                 IMG_BLOCK]}], "blocks")
    out = serialize_blocks(root)
    assert roundtrip_ok_blocks(out)
    assert [c.tag for c in parse_blocks(out).children if c.tag != "#text"] == [
        "core/paragraph", "core/heading", "core/paragraph", "core/buttons", "core/image"]


def test_insert_section_blocks_rejects_nested_position():
    """Blocks mirror divi4: inserts are top-level only, never inside a group."""
    root = parse_blocks('<!-- wp:group -->' + P_X + '<!-- /wp:group -->')
    before = serialize_blocks(root)
    with pytest.raises(TargetError):
        apply_ops(root, [{"op": "insert_section", "position": {"after": [0, 0]},
                          "blocks": [FAQ_BLOCK]}], "blocks")
    assert serialize_blocks(root) == before


def test_insert_section_blocks_refuses_when_nothing_renders():
    """Unknown block kinds render to nothing - report an error, never a fake apply."""
    root = parse_blocks(P_ONE)
    res = apply_ops(root, [{"op": "insert_section", "position": "end",
                            "blocks": [{"kind": "carousel"}]}], "blocks")
    assert res[0]["status"] == "error"
    assert serialize_blocks(root) == P_ONE


def test_insert_module_blocks_dialect_returns_error_not_raise():
    """Module inserts stay divi4-only (no column concept in blocks): soft error."""
    root = parse_blocks(P_ONE)
    res = apply_ops(root, [{"op": "insert_module", "target": {"address": [0]},
                            "blocks": [{"kind": "paragraph", "text": "x"}]}], "blocks")
    assert res[0]["status"] == "error"
    assert "insert_module" in res[0]["detail"]
    assert serialize_blocks(root) == P_ONE


def test_insert_section_malformed_position_is_target_error():
    root = _root()
    with pytest.raises(TargetError, match="position must be"):
        apply_ops(root, [{"op": "insert_section", "position": {"before": [0]},
                          "blocks": [{"kind": "heading", "level": 2, "text": "x"}]}], "divi4")

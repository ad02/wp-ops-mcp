"""Edit operations over the Node tree (the core 6 + move_element). Pure - no I/O.

Ops target nodes by explicit address or a finder (tag/admin_label/contains_text)
that must match exactly one node. Divi dynamic-content values (@ET-DC@) are
read-only by policy: any op that would replace one raises DynamicContentError.

Every op validates fully before it mutates, so a refusal leaves the tree
byte-identical. move_element additionally works on node identities rather than
addresses: removing the source shifts sibling indices, and Node is a dataclass
whose == is structural, so an index/equality lookup could pick the wrong twin.
"""
from __future__ import annotations

import copy

from .model import Node, get_node, clear_raw_on_path, find_nodes
from .divi4 import render_divi4
from .divi4_parse import parse_divi4
from .block_parse import parse_blocks
from .gutenberg import render_gutenberg

_DC = "@ET-DC@"


class DynamicContentError(Exception):
    pass


class TargetError(Exception):
    pass


def _resolve(root: Node, target: dict) -> list[int]:
    if "address" in target:
        get_node(root, target["address"])          # validates; IndexError -> TargetError
        return list(target["address"])
    matches = find_nodes(root, tag=target.get("tag"),
                         admin_label=target.get("admin_label"),
                         contains_text=target.get("contains_text"))
    if len(matches) != 1:
        addrs = [a for a, _ in matches[:5]]
        raise TargetError(f"{len(matches)} matches for {target} (need exactly 1): {addrs}")
    return matches[0][0]


def _esc_divi_attr(value: str) -> str:
    return str(value).replace('"', "%22")


def _guard_dc(current: str) -> None:
    if _DC in (current or ""):
        raise DynamicContentError("target contains @ET-DC@ dynamic content (read-only)")


def _nodes_from_blocks(blocks: list[dict]) -> Node:
    """Render builder-agnostic blocks to a Divi 4 section Node (reuses create renderer)."""
    section_src = render_divi4(blocks).content
    return parse_divi4(section_src).children[0]


def _nodes_from_gutenberg(blocks: list[dict]) -> list[Node]:
    """Render blocks to Gutenberg markup and re-parse into top-level nodes.

    One node per rendered block plus the "\\n\\n" #text separators the renderer
    puts between them - all of them are inserted, in order, so the tree
    serializes back to exactly the rendered markup.
    """
    return parse_blocks(render_gutenberg(blocks).content).children


# --- move_element helpers ---------------------------------------------------
# Everything here is identity-based: a removal shifts sibling indices, and Node is
# a dataclass (== is structural), so list.index()/remove() could pick a twin.

_MOVE_MODES = ("before", "after", "append")
_D4_ROWS = ("et_pb_row", "et_pb_row_inner")


def _move_position(pos) -> tuple[str, list[int]]:
    if not isinstance(pos, dict) or len(pos) != 1 or next(iter(pos)) not in _MOVE_MODES:
        raise TargetError(
            'move_element position must be exactly one of {"before":[addr]}, '
            '{"after":[addr]}, {"append":[parent_addr]}; got ' + repr(pos))
    mode, value = next(iter(pos.items()))
    if not isinstance(value, (list, tuple)) or any(not isinstance(i, int) for i in value):
        raise TargetError(f"move_element {mode!r} needs an address "
                          f"(list of ints), got {value!r}")
    return mode, list(value)


def _is_descendant(node: Node, maybe_ancestor: Node) -> bool:
    """True when node IS maybe_ancestor or lives anywhere inside its subtree."""
    if node is maybe_ancestor:
        return True
    return any(_is_descendant(node, child) for child in maybe_ancestor.children)


def _find_parent_and_index(root: Node, node: Node) -> tuple[Node, int] | None:
    for i, child in enumerate(root.children):
        if child is node:
            return root, i
        hit = _find_parent_and_index(child, node)
        if hit is not None:
            return hit
    return None


def _require_parent(root: Node, node: Node) -> tuple[Node, int]:
    hit = _find_parent_and_index(root, node)
    if hit is None:                                 # unreachable once validated
        raise TargetError("move_element: node is not attached to the tree")
    return hit


def _address_of(root: Node, node: Node) -> list[int]:
    def walk(parent: Node) -> list[int] | None:
        for i, child in enumerate(parent.children):
            if child is node:
                return [i]
            sub = walk(child)
            if sub is not None:
                return [i] + sub
        return None

    addr = walk(root)
    if addr is None:                                # unreachable after a successful insert
        raise TargetError("move_element: moved node vanished from the tree")
    return addr


def _path_nodes(root: Node, address: list[int]) -> list[Node]:
    """Node objects along an address, root first - held by identity so later index
    shifts cannot make us clear the wrong chain."""
    out, node = [root], root
    for i in address:
        node = node.children[i]
        out.append(node)
    return out


def _divi4_move_ok(node: Node, new_parent: Node) -> str | None:
    """Divi 4 structural allowlist. Returns a violation message, or None when legal.

    Rows are held to the plan's rule (section children only), so specialty inner
    rows are refused rather than risk a layout the builder cannot render.
    """
    tag, ptag = node.tag, new_parent.tag
    if tag.startswith("et_pb_column"):
        return (f"{tag} cannot be moved: column moves break Divi's row layout-width "
                "math (move the modules inside the column instead)")
    if tag == "et_pb_section":
        if ptag != "#root":
            return f"{tag} may only live at the root level, not inside {ptag}"
    elif tag in _D4_ROWS:
        if ptag != "et_pb_section":
            return f"{tag} may only live inside an et_pb_section, not inside {ptag}"
    elif not ptag.startswith("et_pb_column"):
        return f"module {tag} may only live inside an et_pb_column, not inside {ptag}"
    return None


def _apply_one(root: Node, op: dict, dialect: str) -> dict:
    kind = op["op"]

    if kind == "insert_section":
        # divi4 -> one section node; blocks (Gutenberg and Divi 5 both parse as
        # blocks) -> the rendered core blocks as top-level siblings.
        if dialect == "divi4":
            new = [_nodes_from_blocks(op["blocks"])]
        elif dialect == "blocks":
            new = _nodes_from_gutenberg(op["blocks"])
        else:
            return {"op": kind, "status": "error",
                    "detail": f"insert_section not supported for dialect {dialect!r}"}
        if not new:                                 # unknown/empty kinds render nothing
            return {"op": kind, "status": "error",
                    "detail": "insert_section: blocks rendered no content"}
        pos = op.get("position", "end")
        if pos != "end" and not (isinstance(pos, dict) and "after" in pos):
            raise TargetError(
                'insert_section position must be "end" or {"after":[addr]}; got ' + repr(pos))
        if pos == "end":
            at = len(root.children)
        else:
            after = list(pos["after"])
            if len(after) != 1:                     # sections are top-level siblings only
                raise TargetError(
                    f"insert_section 'after' must be a top-level address, got {after}")
            get_node(root, after)
            at = after[-1] + 1
        root.children[at:at] = new
        # Only the root's own serialization changed; every inserted node still
        # carries the exact source slice it was just parsed from.
        clear_raw_on_path(root, [at])
        detail = ("section inserted" if dialect == "divi4" else
                  f"{len([n for n in new if n.tag != '#text'])} blocks inserted")
        return {"op": kind, "status": "applied", "address": [at], "detail": detail}

    addr = _resolve(root, op["target"])
    node = get_node(root, addr)

    if kind == "update_element":
        attrs = op.get("attrs") or {}
        if "content" in op:                         # guard everything before mutating anything
            _guard_dc(node.content)
        for k in attrs:
            _guard_dc(node.attrs.get(k, ""))
        if "content" in op:
            node.content = op["content"]
            node.children = []
        for k, v in attrs.items():
            node.attrs[k] = _esc_divi_attr(v) if dialect == "divi4" else str(v)
        clear_raw_on_path(root, addr)
        return {"op": kind, "status": "applied", "address": addr, "detail": "updated"}

    if kind == "set_attr":
        _guard_dc(node.attrs.get(op["key"], ""))
        node.attrs[op["key"]] = (_esc_divi_attr(op["value"]) if dialect == "divi4"
                                 else str(op["value"]))
        clear_raw_on_path(root, addr)
        return {"op": kind, "status": "applied", "address": addr, "detail": op["key"]}

    if kind == "insert_module":
        if dialect != "divi4":                      # no column concept outside divi4
            return {"op": kind, "status": "error",
                    "detail": "insert_module supported for divi4 only "
                              "(blocks dialect: use insert_section)"}
        section = _nodes_from_blocks(op["blocks"])
        modules = section.children[0].children[0].children   # section>row>column children
        if node.content and not node.children:      # preserve leaf-promoted inner HTML
            node.children.append(Node(tag="#text", content=node.content, raw=node.content))
            node.content = ""
        node.children.extend(copy.deepcopy(modules))
        clear_raw_on_path(root, addr)
        return {"op": kind, "status": "applied", "address": addr,
                "detail": f"{len(modules)} modules appended"}

    if kind == "remove_element":
        parent = get_node(root, addr[:-1]) if len(addr) > 1 else root
        parent.children.pop(addr[-1])
        clear_raw_on_path(root, addr[:-1])
        return {"op": kind, "status": "applied", "address": addr, "detail": "removed"}

    if kind == "duplicate_element":
        parent = get_node(root, addr[:-1]) if len(addr) > 1 else root
        parent.children.insert(addr[-1] + 1, copy.deepcopy(node))
        clear_raw_on_path(root, addr[:-1])
        return {"op": kind, "status": "applied", "address": addr, "detail": "duplicated"}

    if kind == "move_element":
        # -- validate everything before touching the tree (refusal => byte-identical)
        if node.tag == "#text":
            raise TargetError("#text nodes cannot be moved (target a builder element)")
        if not addr:
            raise TargetError("the root node cannot be moved")
        mode, dest_addr = _move_position(op.get("position"))
        dest = get_node(root, dest_addr)             # validates; IndexError -> TargetError
        if mode == "append":
            new_parent = dest
            if _is_descendant(new_parent, node):
                raise TargetError(f"cannot move {node.tag} into its own subtree "
                                  f"(destination {dest_addr} sits inside {addr})")
        else:
            if not dest_addr:
                raise TargetError(f"cannot move an element {mode} the root node")
            if dest is node:
                raise TargetError(f"cannot move {node.tag} {mode} itself")
            if _is_descendant(dest, node):
                raise TargetError(f"cannot move {node.tag} into its own subtree "
                                  f"(destination {dest_addr} sits inside {addr})")
            new_parent = get_node(root, dest_addr[:-1])
        if new_parent.self_closing:                 # children would never serialize
            raise TargetError(f"cannot move into self-closing {new_parent.tag} "
                              "(it has no inner region)")
        if dialect == "divi4":
            violation = _divi4_move_ok(node, new_parent)
            if violation:
                raise TargetError(violation)
        elif new_parent.tag == "#text":
            raise TargetError("cannot move an element into a #text node")

        # -- mutate: identity all the way down; nothing below can raise
        old_chain = _path_nodes(root, addr[:-1])     # objects, not addresses
        old_parent, old_i = _require_parent(root, node)
        old_parent.children.pop(old_i)
        if mode == "append":
            if new_parent.content and not new_parent.children:   # leaf-promoted HTML
                new_parent.children.append(Node(tag="#text", content=new_parent.content,
                                                raw=new_parent.content))
                new_parent.content = ""
            new_parent.children.append(node)
        else:
            dest_parent, j = _require_parent(root, dest)   # index AFTER the removal
            dest_parent.children.insert(j if mode == "before" else j + 1, node)

        for stale in old_chain:                      # old chain serializes differently now
            stale.raw = None
        new_addr = _address_of(root, node)
        clear_raw_on_path(root, new_addr[:-1])       # new parent chain; the moved
        return {"op": kind, "status": "applied",     # subtree keeps its own bytes
                "address": new_addr,
                "detail": f"moved {node.tag} from {addr} to {mode} {dest_addr}"}

    return {"op": kind, "status": "error", "detail": f"unknown op {kind!r}"}


def apply_ops(root: Node, ops: list[dict], dialect: str) -> list[dict]:
    results = []
    for op in ops:
        try:
            results.append(_apply_one(root, op, dialect))
        except (TargetError, DynamicContentError):
            raise                                   # hard failures abort the batch
        except IndexError as e:
            raise TargetError(f"bad address in {op.get('op')}: {e}") from e
    return results

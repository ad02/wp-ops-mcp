"""Builder-agnostic content tree.

One Node model for Divi 4 shortcodes, Divi 5 blocks and Gutenberg blocks.
`raw` holds the exact source slice; serializers emit it verbatim unless an edit
cleared it (that is what makes unedited round-trips byte-identical).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    tag: str                                   # "et_pb_text" | "divi/section" | "core/paragraph" | "#text" | "#root"
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    content: str = ""                          # leaf inner content (raw HTML/text)
    self_closing: bool = False
    raw: str | None = None                     # exact original slice; None => re-render


def get_node(root: Node, address: list[int]) -> Node:
    node = root
    for i in address:
        node = node.children[i]                # IndexError propagates by design
    return node


def clear_raw_on_path(root: Node, address: list[int]) -> None:
    node = root
    node.raw = None
    for i in address:
        node = node.children[i]
        node.raw = None


def _matches(node: Node, tag, admin_label, contains_text) -> bool:
    if node.tag == "#text":
        return False
    if tag is not None and node.tag != tag:
        return False
    if admin_label is not None and node.attrs.get("admin_label") != admin_label:
        return False
    if contains_text is not None:
        found = (contains_text in node.content) or any(
            contains_text in v for v in node.attrs.values())
        if not found:
            return False
    return True


def find_nodes(root: Node, tag: str | None = None, admin_label: str | None = None,
               contains_text: str | None = None) -> list[tuple[list[int], Node]]:
    out: list[tuple[list[int], Node]] = []

    def walk(node: Node, addr: list[int]) -> None:
        for i, child in enumerate(node.children):
            caddr = addr + [i]
            if _matches(child, tag, admin_label, contains_text):
                out.append((caddr, child))
            walk(child, caddr)

    walk(root, [])
    return out

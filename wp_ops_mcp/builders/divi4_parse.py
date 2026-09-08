"""Divi 4 shortcode parser: post_content -> Node tree, preserving exact source slices.

Only [et_pb_*] tags are structural. Anything else (HTML, other shortcodes) is text.
Values keep Divi's %22-style encodings untouched - decoding would break round-trips.

Known limitation: the tokenizer only recognizes a tag when its attributes are
well-formed key="value" pairs, so quoted values may safely contain "]". Balanced
et_pb-shaped text inside module content (e.g. a doc string mentioning
[et_pb_button][/et_pb_button]) parses as phantom structure, but raw slices keep
round-trips byte-identical so no bytes are ever lost. Unbalanced lookalikes raise
ParseError - a loud failure that makes the edit layer refuse the page rather than
emit a silently wrong tree. One residual edge: malformed unquoted attrs on a
nested same-name tag can silently early-close the ancestor (hand-corrupted content
only; Divi never emits unquoted attrs).
"""
from __future__ import annotations

import re

from .model import Node

_TAG = re.compile(r'\[(/?)(et_pb_[a-z0-9_]+)((?:\s+[a-z0-9_]+="[^"]*")*)\s*(/?)\]')
_ATTR = re.compile(r'([a-z0-9_]+)="([^"]*)"', re.I)


class ParseError(Exception):
    pass


def parse_attrs(attr_str: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _ATTR.finditer(attr_str or "")}


def _has_et_tag(text: str) -> bool:
    return _TAG.search(text) is not None


def parse_divi4(content: str) -> Node:
    root = Node(tag="#root")
    stack: list[tuple[Node, int]] = [(root, 0)]   # (node, source start offset)
    pos = 0
    for m in _TAG.finditer(content):
        parent = stack[-1][0]
        if m.start() > pos:                        # text run before this tag
            txt = content[pos:m.start()]
            parent.children.append(Node(tag="#text", content=txt, raw=txt))
        closing, tag, attr_str, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
        if closing:                                 # [/tag]
            node, start = stack.pop()
            if node.tag != tag or node is root:
                raise ParseError(f"unbalanced [/{tag}] at {m.start()}")
            node.raw = content[start:m.end()]
            # leaf: single #text child and no element children -> promote to .content
            if (len(node.children) == 1 and node.children[0].tag == "#text"
                    and not _has_et_tag(node.children[0].content)):
                node.content = node.children[0].content
                node.children = []
        elif selfclose:                             # [tag ... /]
            parent.children.append(Node(tag=tag, attrs=parse_attrs(attr_str),
                                        self_closing=True, raw=m.group(0)))
        else:                                       # [tag ...]
            node = Node(tag=tag, attrs=parse_attrs(attr_str))
            parent.children.append(node)
            stack.append((node, m.start()))
        pos = m.end()
    if len(stack) != 1:
        raise ParseError(f"unclosed [{stack[-1][0].tag}]")
    if pos < len(content):
        tail = content[pos:]
        root.children.append(Node(tag="#text", content=tail, raw=tail))
    return root


def _render_attrs(attrs: dict[str, str]) -> str:
    return "".join(f' {k}="{v}"' for k, v in attrs.items())


def serialize_divi4(node: Node) -> str:
    if node.raw is not None:
        return node.raw
    if node.tag == "#text":
        return node.content
    if node.tag == "#root":
        return "".join(serialize_divi4(c) for c in node.children)
    if node.self_closing:
        return f"[{node.tag}{_render_attrs(node.attrs)} /]"
    inner = node.content if not node.children else "".join(
        serialize_divi4(c) for c in node.children)
    return f"[{node.tag}{_render_attrs(node.attrs)}]{inner}[/{node.tag}]"


def roundtrip_ok(content: str) -> bool:
    try:
        return serialize_divi4(parse_divi4(content)) == content
    except ParseError:
        return False

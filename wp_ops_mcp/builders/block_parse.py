"""WP block-comment parser: Gutenberg (wp:core/*, shorthand wp:paragraph) and
Divi 5 (wp:divi/*) share this. JSON attrs are kept as the exact source string
("_json") so round-trips stay byte-identical.

The tokenizer only treats a `<!-- ... -->` comment as a block when it starts
with `wp:` and follows the WP delimiter shape (whitespace after `<!--`, a
namespaced/shorthand name, an optional single-line-or-multiline JSON object, and
whitespace before an optional `/` and `-->`). Any other HTML comment stays as
inner text/content. Balanced structure is required: an unbalanced or unclosed
`wp:` block raises ParseError - a loud failure so the edit layer refuses the
page rather than emit a silently wrong tree. Bytes are never lost regardless of
tree shape because every parsed node keeps its exact source slice in `raw`.

Two delimiter edges to note: a JSON attr value containing '} -->' raises a loud
ParseError (never a silent wrong tree); and a literal balanced wp: comment inside
a block's inner HTML parses as a phantom nested block (byte-safe; parity with WP
core's own delimiter scanning).

Note on core namespacing: `wp:paragraph` and `wp:core/paragraph` both parse to
tag `core/paragraph`; an edited (raw-cleared) core node re-renders in WP's
canonical shorthand form (`<!-- wp:paragraph -->`). Unedited nodes emit their
`raw` slice verbatim, so the source spelling is preserved byte-for-byte.
"""
from __future__ import annotations

import re

from .model import Node

_BLOCK = re.compile(
    r"<!--\s+(/?)wp:([a-z][a-z0-9_-]*(?:/[a-z][a-z0-9_-]*)?)"   # /? + name
    r"(?:\s+(\{.*?\}))?\s+(/?)-->", re.S)


class ParseError(Exception):
    pass


def _full_name(name: str) -> str:
    return name if "/" in name else f"core/{name}"


def parse_blocks(content: str) -> Node:
    root = Node(tag="#root")
    stack: list[tuple[Node, int]] = [(root, 0)]
    pos = 0
    for m in _BLOCK.finditer(content):
        parent = stack[-1][0]
        if m.start() > pos:
            txt = content[pos:m.start()]
            parent.children.append(Node(tag="#text", content=txt, raw=txt))
        closing, name, json_str, selfclose = m.groups()
        tag = _full_name(name)
        if closing:
            node, start = stack.pop()
            if node.tag != tag or node is root:
                raise ParseError(f"unbalanced /wp:{name} at {m.start()}")
            node.raw = content[start:m.end()]
            if (len(node.children) == 1 and node.children[0].tag == "#text"):
                node.content = node.children[0].content
                node.children = []
        elif selfclose:
            attrs = {"_json": json_str} if json_str else {}
            parent.children.append(Node(tag=tag, attrs=attrs, self_closing=True,
                                        raw=m.group(0)))
        else:
            attrs = {"_json": json_str} if json_str else {}
            node = Node(tag=tag, attrs=attrs)
            parent.children.append(node)
            stack.append((node, m.start()))
        pos = m.end()
    if len(stack) != 1:
        raise ParseError(f"unclosed wp:{stack[-1][0].tag}")
    if pos < len(content):
        tail = content[pos:]
        root.children.append(Node(tag="#text", content=tail, raw=tail))
    return root


def _short_name(tag: str) -> str:
    return tag[5:] if tag.startswith("core/") else tag


def serialize_blocks(node: Node) -> str:
    if node.raw is not None:
        return node.raw
    if node.tag == "#text":
        return node.content
    if node.tag == "#root":
        return "".join(serialize_blocks(c) for c in node.children)
    name = _short_name(node.tag)
    js = node.attrs.get("_json")
    head = f"<!-- wp:{name}" + (f" {js}" if js else "")
    if node.self_closing:
        return head + " /-->"
    inner = node.content if not node.children else "".join(
        serialize_blocks(c) for c in node.children)
    return f"{head} -->{inner}<!-- /wp:{name} -->"


def roundtrip_ok_blocks(content: str) -> bool:
    try:
        return serialize_blocks(parse_blocks(content)) == content
    except ParseError:
        return False

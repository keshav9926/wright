"""L2 shared machinery — the Symbol record and helpers every extractor uses.

Each language module (python.py, go.py, typescript.py) exposes one function:

    extract(source: bytes, tree) -> list[Symbol]

and this module holds everything they share, so the language files contain
ONLY what is genuinely language-specific.

Called by: the three extractor modules. Nothing here touches the DB or the
           filesystem — pure functions over tree-sitter nodes.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field


@dataclass
class Symbol:
    """One definition found in one file. The unit everything downstream
    speaks in: db.insert_symbols() stores these, `wi symbols` displays them,
    Day 2's call-graph edges will point at their IDs.
    """

    name: str                    # bare name: "parse"
    qualified_name: str          # dotted path within the file: "Parser.parse"
    kind: str                    # function | method | class | struct |
                                 # interface | type | type_alias
    start_line: int              # 1-indexed, INCLUSIVE. For decorated Python
    end_line: int                # defs this includes the decorators.
    start_byte: int              # byte offsets: exact-slice the original
    end_byte: int                # source; Day 2 edge resolution keys on these
    signature: str = ""          # one-line header, no body: "def parse(self, src)"
    docstring: str | None = None # docstring (py) or preceding comment (go/ts)
    parent: str | None = None    # qualified name of enclosing scope, or None
    is_exported: bool = True     # language-specific visibility heuristic


def node_text(node, source: bytes) -> str:
    """Exact source text of a node, via byte slicing.

    Called by: every extractor, constantly. We slice `source` ourselves
    rather than trusting node.text so behavior is identical across binding
    versions (node.text was None-prone in some).
    """
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def collapse_ws(text: str) -> str:
    """Fold any run of whitespace (incl. newlines) into single spaces.
    Turns a 5-line signature into one displayable line.

    Called by: signature_before_body() and the extractors directly.
    """
    return " ".join(text.split())


def signature_before_body(node, body_node, source: bytes) -> str:
    """Slice from a definition's start to its body's start = the header.

    `def parse(self, src: str) -> Tree:` + body   ->   "def parse(self, src: str) -> Tree"

    Called by: all three extractors for functions/methods/classes.
    Falls back to the node's first line when there is no body node (e.g.
    abstract / stub definitions).
    """
    end = body_node.start_byte if body_node is not None else node.end_byte
    sig = collapse_ws(source[node.start_byte:end].decode("utf-8", errors="replace"))
    return sig.rstrip(" {:").strip()   # drop the dangling "{" (go/ts) or ":" (py)


def preceding_comment(node, source: bytes) -> str | None:
    """Collect the contiguous comment block sitting DIRECTLY above a node.

    This is how Go and TS "docstrings" work — unlike Python, the doc lives
    outside the definition. tree-sitter puts comments in the tree as 'extra'
    nodes, so they appear as siblings and we can walk backwards:

        // Parse builds the tree.        <- prev.prev sibling, row N-2
        // It never returns nil.         <- prev sibling,      row N-1
        func Parse(src []byte) {}        <- node,               row N

    The row-adjacency check (`end row >= current row - 1`) is what makes it
    stop at blank lines: a comment 3 lines up is unrelated prose, not doc.

    Called by: go.py and typescript.py extractors.
    Returns:   marker-stripped text, or None if no adjacent comment.
    """
    lines: list[str] = []
    row = node.start_point.row
    prev = node.prev_named_sibling
    while prev is not None and prev.type == "comment" and prev.end_point.row >= row - 1:
        lines.append(node_text(prev, source))
        row = prev.start_point.row
        prev = prev.prev_named_sibling
    if not lines:
        return None
    lines.reverse()
    return _strip_comment_markers("\n".join(lines))


def _strip_comment_markers(text: str) -> str:
    """Remove //, /*, */, and leading * from comment text, keep the words.

    Called by: preceding_comment() only.
    """
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        for prefix in ("///", "//", "/**", "/*", "*/"):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        s = s.strip()
        if s.startswith("* "):
            s = s[2:]
        elif s == "*":
            s = ""
        if s.endswith("*/"):
            s = s[:-2].rstrip()
        out.append(s)
    return "\n".join(out).strip() or ""


def clean_docstring(raw: str) -> str:
    """Normalize indentation the way Python's own tooling does.
    inspect.cleandoc handles the 'first line flush, rest indented' shape.

    Called by: python.py (string docstrings), and go/ts paths for symmetry.
    """
    return inspect.cleandoc(raw)


def ancestor_scopes(node, scope_types: frozenset[str], source: bytes) -> list[str]:
    """Names of enclosing named scopes, outermost first.

    For a method `parse` inside class `Parser`, returns ["Parser"], so the
    caller builds qualified_name = "Parser.parse". Works for any grammar
    whose scope nodes carry a `name:` field (all three of ours do).

    Called by: python.py and typescript.py. (Go has no lexical nesting of
    named defs — methods attach by receiver instead, handled in go.py.)
    """
    names: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in scope_types:
            name_node = current.child_by_field_name("name")
            if name_node is not None:
                names.append(node_text(name_node, source))
        current = current.parent
    names.reverse()
    return names

"""L2 extractor: Python.

Finds: functions, methods, classes (nested defs included, qualified by
their enclosing scopes: "Outer.Inner.helper").

Called by: indexer.index_repository() via extract.EXTRACTORS["python"].
Calls:     parsers.compile_query/query_matches (cached), base.* helpers.
"""

from __future__ import annotations

from ..parsers import capture_one, compile_query, query_matches
from .base import (
    Symbol,
    ancestor_scopes,
    clean_docstring,
    node_text,
    signature_before_body,
)

# S-expression query, the tree-sitter pattern language.
#   (function_definition name: (identifier) @name) @def
# reads: "match a function_definition node whose `name` field is an
# identifier; capture the whole def as @def and the identifier as @name".
# Two patterns = two kinds of match; query_matches() tells us which fired
# via the node type of @def. `async def` is still a function_definition
# (async is just a child token), so one pattern covers both.
QUERY = """
(function_definition name: (identifier) @name) @def
(class_definition name: (identifier) @name) @def
"""

# Node types that create a named scope — used to build qualified names and
# to decide function-vs-method.
_SCOPE_TYPES = frozenset({"class_definition", "function_definition"})


def extract(source: bytes, tree) -> list[Symbol]:
    """One file's tree -> its Symbols. The module's only public function.

    Called by: indexer, once per parsed Python file.
    """
    query = compile_query("python", QUERY)   # cached after the first file
    symbols: list[Symbol] = []

    for _pattern_index, captures in query_matches(query, tree.root_node):
        def_node = capture_one(captures, "def")
        name_node = capture_one(captures, "name")
        if def_node is None or name_node is None:
            continue

        name = node_text(name_node, source)
        scopes = ancestor_scopes(def_node, _SCOPE_TYPES, source)
        qualified = ".".join(scopes + [name])

        # kind: a def directly inside a class is a method; a def inside a
        # def is still "function" (nested helper), matching Python's own view.
        if def_node.type == "class_definition":
            kind = "class"
        else:
            kind = "method" if _nearest_scope_is_class(def_node) else "function"

        # Decorators live OUTSIDE the function_definition node, wrapped in a
        # decorated_definition parent. The symbol's line range should cover
        # them (you'd want @app.route included when you slice a handler),
        # but the SIGNATURE should stay the clean `def ...` line.
        anchor = def_node
        if def_node.parent is not None and def_node.parent.type == "decorated_definition":
            anchor = def_node.parent

        body = def_node.child_by_field_name("body")

        symbols.append(Symbol(
            name=name,
            qualified_name=qualified,
            kind=kind,
            start_line=anchor.start_point.row + 1,   # tree-sitter rows are 0-based
            end_line=def_node.end_point.row + 1,
            start_byte=anchor.start_byte,
            end_byte=def_node.end_byte,
            signature=signature_before_body(def_node, body, source),
            docstring=_docstring(body, source),
            parent=".".join(scopes) or None,
            # Python's visibility is pure convention: one leading underscore
            # means "internal". Dunders (__init__) get flagged non-exported
            # too — crude, but consistent; revisit if it bites.
            is_exported=not name.startswith("_"),
        ))

    return symbols


def _nearest_scope_is_class(def_node) -> bool:
    """Walk up to the closest enclosing scope; True if it's a class.
    Distinguishes methods from module-level and nested functions.

    Called by: extract() for every function_definition.
    """
    current = def_node.parent
    while current is not None:
        if current.type in _SCOPE_TYPES:
            return current.type == "class_definition"
        current = current.parent
    return False


def _docstring(body_node, source: bytes) -> str | None:
    """PEP 257 docstring: the body's first statement, if it is a bare string.

    Tree shape we're matching:
        body (block)
          └─ expression_statement          <- first named child
               └─ string                   <- the docstring
                    ├─ string_start  (\"\"\")
                    ├─ string_content      <- what we actually want
                    └─ string_end    (\"\"\")

    Called by: extract() for both functions and classes.
    """
    if body_node is None or body_node.named_child_count == 0:
        return None
    first = body_node.named_children[0]
    if first.type != "expression_statement" or first.named_child_count == 0:
        return None
    string_node = first.named_children[0]
    if string_node.type != "string":
        return None

    # Prefer the string_content child (modern grammar gives us the text
    # without quotes for free); fall back to stripping quotes by hand.
    for child in string_node.children:
        if child.type == "string_content":
            return clean_docstring(node_text(child, source))
    raw = node_text(string_node, source).strip("rRbBuUfF")  # prefix letters
    return clean_docstring(raw.strip("\"'"))

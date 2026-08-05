"""L2 extractor: Go.

Finds: functions, methods (qualified by receiver type: "Device.Reset"),
structs, interfaces, and other named types.

Go quirks this file exists to handle:
  * Methods aren't nested in their type — `func (d *Device) Reset()` sits at
    top level and BINDS to Device via the receiver. Qualified names come
    from parsing the receiver, not from tree ancestry.
  * Docs are comments ABOVE the declaration (there is no docstring syntax).
  * Visibility is spelling: Uppercase = exported. The one language where
    is_exported is exact, not a heuristic.

Called by: indexer.index_repository() via extract.EXTRACTORS["go"].
Calls:     parsers.compile_query/query_matches, base.* helpers.
"""

from __future__ import annotations

from ..parsers import capture_one, compile_query, query_matches
from .base import (
    Symbol,
    clean_docstring,
    collapse_ws,
    node_text,
    preceding_comment,
    signature_before_body,
)

# Three patterns. The type pattern captures the INNER type_spec (that's
# where name + underlying type live) and deliberately does NOT constrain the
# `type:` field — one pattern matches struct/interface/alias alike, and
# _type_kind() classifies after the fact. The alternative (three patterns
# with (struct_type) / (interface_type) / wildcard) double-matches: the
# wildcard fires on structs too. Classify-later avoids that class of bug.
QUERY = """
(function_declaration name: (identifier) @name) @def
(method_declaration name: (field_identifier) @name) @def
(type_declaration (type_spec name: (type_identifier) @name) @def)
"""


def extract(source: bytes, tree) -> list[Symbol]:
    """One Go file's tree -> Symbols. The module's only public function.

    Called by: indexer, once per parsed .go file.
    """
    query = compile_query("go", QUERY)
    symbols: list[Symbol] = []

    for _pattern_index, captures in query_matches(query, tree.root_node):
        def_node = capture_one(captures, "def")
        name_node = capture_one(captures, "name")
        if def_node is None or name_node is None:
            continue

        name = node_text(name_node, source)

        if def_node.type == "function_declaration":
            body = def_node.child_by_field_name("body")
            symbols.append(_make(def_node, source, name, "function",
                                 qualified=name, parent=None,
                                 signature=signature_before_body(def_node, body, source)))

        elif def_node.type == "method_declaration":
            receiver = _receiver_type(def_node, source)   # "Device" from (d *Device)
            body = def_node.child_by_field_name("body")
            symbols.append(_make(def_node, source, name, "method",
                                 qualified=f"{receiver}.{name}" if receiver else name,
                                 parent=receiver,
                                 signature=signature_before_body(def_node, body, source)))

        else:  # type_spec — classify by what's on the right of the name
            kind, sig = _type_kind(def_node, source, name)
            symbols.append(_make(def_node, source, name, kind,
                                 qualified=name, parent=None, signature=sig))

    return symbols


def _make(def_node, source: bytes, name: str, kind: str, *,
          qualified: str, parent: str | None, signature: str) -> Symbol:
    """Assemble one Symbol; shared tail of all three branches in extract().

    Doc comment lookup happens here so every kind gets it uniformly.
    """
    doc = _doc_comment(def_node, source)
    return Symbol(
        name=name,
        qualified_name=qualified,
        kind=kind,
        start_line=def_node.start_point.row + 1,
        end_line=def_node.end_point.row + 1,
        start_byte=def_node.start_byte,
        end_byte=def_node.end_byte,
        signature=signature,
        docstring=doc,
        parent=parent,
        is_exported=bool(name) and name[0].isupper(),   # Go's actual rule
    )


def _receiver_type(method_node, source: bytes) -> str | None:
    """Extract the bare receiver type name from a method declaration.

        func (d *Device) Reset()      -> "Device"   (pointer stripped)
        func (c Cache[K, V]) Get(k K) -> "Cache"    (type params stripped)

    Tree shape: method_declaration's `receiver` field is a parameter_list
    holding one parameter_declaration whose `type` field is the receiver type.

    Called by: extract(), method branch only.
    """
    receiver = method_node.child_by_field_name("receiver")
    if receiver is None or receiver.named_child_count == 0:
        return None
    param = receiver.named_children[0]
    type_node = param.child_by_field_name("type")
    if type_node is None:
        return None
    text = node_text(type_node, source).lstrip("*")   # *Device -> Device
    return text.split("[")[0].strip() or None          # Cache[K,V] -> Cache


def _type_kind(spec_node, source: bytes, name: str) -> tuple[str, str]:
    """Classify a type_spec by its underlying type; build its signature.

        type Device struct {...}     -> ("struct",    "type Device struct")
        type Runner interface {...}  -> ("interface", "type Runner interface")
        type ID uint64               -> ("type",      "type ID uint64")

    Called by: extract(), type branch only.
    """
    type_node = spec_node.child_by_field_name("type")
    if type_node is None:
        return "type", f"type {name}"
    if type_node.type == "struct_type":
        return "struct", f"type {name} struct"
    if type_node.type == "interface_type":
        return "interface", f"type {name} interface"
    # alias / named basic type: show the underlying type, truncated —
    # `type Handler func(w ResponseWriter, r *Request)` is worth seeing.
    underlying = collapse_ws(node_text(type_node, source))
    if len(underlying) > 100:
        underlying = underlying[:97] + "..."
    return "type", f"type {name} {underlying}"


def _doc_comment(def_node, source: bytes) -> str | None:
    """Go doc comment: the // block directly above the declaration.

    Anchor subtlety: for `type Foo struct` the captured node is the INNER
    type_spec, but the doc comment sits above the OUTER type_declaration —
    they're the same source line for ungrouped types. So: try the spec
    itself first (handles grouped `type ( ... )` blocks where comments sit
    inside, above each spec), then fall back to the declaration.

    Called by: _make() for every symbol.
    """
    doc = preceding_comment(def_node, source)
    if doc is None and def_node.type == "type_spec" and def_node.parent is not None:
        doc = preceding_comment(def_node.parent, source)
    return clean_docstring(doc) if doc else None

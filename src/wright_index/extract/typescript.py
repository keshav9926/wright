"""L2 extractor: TypeScript (and TSX — same node types, different grammar).

Finds: functions, classes (abstract too), methods, interfaces, type aliases,
and const-arrow functions (`export const foo = () => {}`) — which is how a
huge share of real-world TS is written; skipping them would miss half of
most React codebases.

Deliberately skipped on Day 1 (each is a small add later): enums, namespace
blocks, #-private methods, overload signature groups.

Called by: indexer via extract.EXTRACTORS["typescript"] AND ["tsx"] — one
           extractor, two grammars (see config.py for why tsx differs).
Calls:     parsers.compile_query/query_matches, base.* helpers.
"""

from __future__ import annotations

from ..parsers import capture_one, compile_query, query_matches
from .base import (
    Symbol,
    ancestor_scopes,
    clean_docstring,
    collapse_ws,
    node_text,
    preceding_comment,
    signature_before_body,
)

# Last pattern: `[(arrow_function) (function_expression)]` is an alternation —
# match a variable_declarator whose value is EITHER form of function literal.
# That's the `const handler = async (req) => {...}` idiom. We capture the
# declarator (name + value together) and dig the value out in code.
QUERY = """
(function_declaration name: (identifier) @name) @def
(generator_function_declaration name: (identifier) @name) @def
(class_declaration name: (type_identifier) @name) @def
(abstract_class_declaration name: (type_identifier) @name) @def
(interface_declaration name: (type_identifier) @name) @def
(type_alias_declaration name: (type_identifier) @name) @def
(method_definition name: (property_identifier) @name) @def
(variable_declarator name: (identifier) @name value: [(arrow_function) (function_expression)]) @def
"""

_KIND_BY_NODE_TYPE = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "method_definition": "method",
    "variable_declarator": "function",     # the const-arrow case
}

# Scopes used for qualified names: classes only. We do NOT qualify by
# enclosing plain functions — `Component.handleClick` is useful,
# `useEffect.callback.inner` is noise.
_CLASS_SCOPES = frozenset({"class_declaration", "abstract_class_declaration"})


def extract(source: bytes, tree, lang_key: str = "typescript") -> list[Symbol]:
    """One TS/TSX file's tree -> Symbols. The module's only public function.

    Called by: indexer, once per parsed .ts/.tsx/.mts/.cts file. The registry
    in extract/__init__.py binds lang_key="tsx" for .tsx files — a Query only
    matches when compiled against the SAME grammar object that built the
    tree, and tsx is a separate grammar even though node names are identical.
    (Compiling a typescript query and running it on a tsx tree silently
    matches nothing — no error. Hence explicit key, not introspection.)
    """
    query = compile_query(lang_key, QUERY)
    symbols: list[Symbol] = []

    for _pattern_index, captures in query_matches(query, tree.root_node):
        def_node = capture_one(captures, "def")
        name_node = capture_one(captures, "name")
        if def_node is None or name_node is None:
            continue

        name = node_text(name_node, source)
        kind = _KIND_BY_NODE_TYPE.get(def_node.type, "function")
        scopes = ancestor_scopes(def_node, _CLASS_SCOPES, source)
        qualified = ".".join(scopes + [name])

        # --- per-shape details: anchor (what lines the symbol spans),
        #     body (for the signature cut), visibility ------------------
        if def_node.type == "variable_declarator":
            # const foo = () => {...}
            #   declarator = `foo = () => {...}`; its parent lexical_declaration
            #   includes the `const`, which is the natural start of the symbol.
            anchor = def_node.parent if def_node.parent is not None else def_node
            value = def_node.child_by_field_name("value")     # the arrow_function
            body = value.child_by_field_name("body") if value is not None else None
            signature = signature_before_body(anchor, body, source)
            exported = _under_export(anchor)
        elif def_node.type == "method_definition":
            anchor = def_node
            body = def_node.child_by_field_name("body")
            signature = signature_before_body(def_node, body, source)
            # Methods aren't export-wrapped; visibility = accessibility
            # modifier. No modifier -> public -> exported.
            exported = not _is_private_method(def_node, source)
        elif def_node.type == "type_alias_declaration":
            anchor = def_node
            signature = collapse_ws(node_text(def_node, source))
            if len(signature) > 120:
                signature = signature[:117] + "..."
            exported = _under_export(def_node)
        else:  # function / class / interface declarations
            anchor = def_node
            body = def_node.child_by_field_name("body")
            signature = signature_before_body(def_node, body, source)
            exported = _under_export(def_node)

        # JSDoc / comment above. For exported declarations the comment sits
        # above the `export` keyword, i.e. above the export_statement — so
        # look above the statement if the node itself has none.
        doc = preceding_comment(anchor, source)
        if doc is None and anchor.parent is not None and anchor.parent.type == "export_statement":
            doc = preceding_comment(anchor.parent, source)

        symbols.append(Symbol(
            name=name,
            qualified_name=qualified,
            kind=kind,
            start_line=anchor.start_point.row + 1,
            end_line=def_node.end_point.row + 1,
            start_byte=anchor.start_byte,
            end_byte=def_node.end_byte,
            signature=signature,
            docstring=clean_docstring(doc) if doc else None,
            parent=".".join(scopes) or None,
            is_exported=exported,
        ))

    return symbols


def _under_export(node) -> bool:
    """True if the definition is wrapped in an export_statement within a
    couple of hops:  export function f()  /  export const g = () => {}
    (declaration -> export_statement, or declarator -> lexical_declaration
    -> export_statement).

    Called by: extract() for everything except methods.
    """
    current = node
    for _ in range(3):
        current = current.parent
        if current is None:
            return False
        if current.type == "export_statement":
            return True
    return False


def _is_private_method(method_node, source: bytes) -> bool:
    """True for `private foo()` / `protected foo()`.

    The accessibility keyword is a plain child of method_definition (not a
    named field), so scan children for the accessibility_modifier node.

    Called by: extract(), method branch only.
    """
    for child in method_node.children:
        if child.type == "accessibility_modifier":
            return node_text(child, source) in ("private", "protected")
    return False

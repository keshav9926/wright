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
    CallSite,
    ImportRecord,
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


# --- Day 2: call sites -----------------------------------------------------
#   Foo(...)        -> plain identifier (same package — Go resolves per DIR)
#   x.Foo(...)      -> selector: x is a package alias OR a value (method call)
CALL_QUERY = """
(call_expression function: (identifier) @callee) @call
(call_expression function: (selector_expression operand: (identifier) @obj field: (field_identifier) @callee)) @call
"""

IMPORT_QUERY = """
(import_spec) @spec
"""


def extract_calls(source: bytes, tree) -> list[CallSite]:
    """Every call expression, with the Go-specific receiver trick:

    Inside `func (dev *Devices) Reset()`, a call `dev.trimMemory(x)` has
    operand == the receiver VARIABLE. The tree alone proves dev's type is
    Devices, so receiver_type_hint = "Devices" — the resolver turns this
    into Devices.trimMemory with high confidence, no type inference needed.
    This one heuristic resolves most intra-type method calls in real Go.

    Called by: indexer pass 2, per parsed .go file.
    """
    query = compile_query("go", CALL_QUERY)
    calls: list[CallSite] = []
    # var->type maps computed lazily per enclosing function, cached by the
    # function's byte span (nodes aren't hashable across queries).
    var_cache: dict[tuple[int, int], dict[str, str]] = {}
    for _pat, captures in query_matches(query, tree.root_node):
        call_node = capture_one(captures, "call")
        callee_node = capture_one(captures, "callee")
        obj_node = capture_one(captures, "obj")
        if call_node is None or callee_node is None:
            continue
        receiver = node_text(obj_node, source) if obj_node is not None else None

        hint = None
        if receiver is not None:
            recv_var, recv_type = _enclosing_receiver(call_node, source)
            if recv_var is not None and receiver == recv_var:
                hint = recv_type
            else:
                # Second tree-proof: `dev := Devices{...}` earlier in the
                # same function makes dev.Foo() a Devices method call. This
                # is how table-driven tests call the code they test, so
                # without it `wi tests-for` is blind on Go (found on HAMi).
                hint = _local_var_type(call_node, receiver, source, var_cache)

        calls.append(CallSite(
            callee=node_text(callee_node, source),
            receiver=receiver,
            receiver_type_hint=hint,
            line=call_node.start_point.row + 1,
            start_byte=call_node.start_byte,
        ))
    return calls


def _enclosing_receiver(node, source: bytes) -> tuple[str | None, str | None]:
    """(receiver var name, receiver type name) of the method containing
    `node`, or (None, None) if we're not inside a method.

    Called by: extract_calls() for selector calls only.
    """
    current = node.parent
    while current is not None:
        if current.type == "method_declaration":
            recv = current.child_by_field_name("receiver")
            if recv is None or recv.named_child_count == 0:
                return None, None
            param = recv.named_children[0]
            name_node = param.child_by_field_name("name")
            type_node = param.child_by_field_name("type")
            var = node_text(name_node, source) if name_node is not None else None
            typ = None
            if type_node is not None:
                typ = node_text(type_node, source).lstrip("*").split("[")[0].strip()
            return var, typ
        current = current.parent
    return None, None


def _local_var_type(call_node, var_name: str, source: bytes,
                    cache: dict[tuple[int, int], dict[str, str]]) -> str | None:
    """Type of a function-local variable, when the declaration proves it:

        dev := Devices{...}          -> "Devices"
        cfg := &VNPUConfig{...}      -> "VNPUConfig"   (pointer stripped)
        var s Server                 -> "Server"
        var s = Server{...}          -> "Server"

    Deliberately NOT handled: `x := NewFoo()` (needs return-type lookup),
    reassignment, multi-var `a, b := X{}, Y{}` — heuristic stays where the
    tree alone is proof. Shadowing in nested blocks is an accepted
    over-approximation.

    Called by: extract_calls() for selector calls whose operand is not the
    method receiver. The per-function map is cached by byte span so a
    function with 50 calls scans its declarations once.
    """
    func = call_node.parent
    while func is not None and func.type not in (
            "function_declaration", "method_declaration", "func_literal"):
        func = func.parent
    if func is None:
        return None

    key = (func.start_byte, func.end_byte)
    if key not in cache:
        cache[key] = _collect_var_types(func, source)
    return cache[key].get(var_name)


def _collect_var_types(func_node, source: bytes) -> dict[str, str]:
    """Scan one function's subtree for provable var declarations.
    Called by: _local_var_type() on cache miss."""
    types: dict[str, str] = {}
    stack = list(func_node.named_children)
    while stack:
        n = stack.pop()
        if n.type == "short_var_declaration":       # x := <value>
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            if (left is not None and right is not None
                    and left.named_child_count == 1 and right.named_child_count == 1
                    and left.named_children[0].type == "identifier"):
                typ = _literal_type(right.named_children[0], source)
                if typ:
                    types[node_text(left.named_children[0], source)] = typ
        elif n.type == "var_spec":                  # var x T / var x = T{...}
            name_node = n.child_by_field_name("name")
            if name_node is not None:
                type_node = n.child_by_field_name("type")
                typ = None
                if type_node is not None:
                    typ = _bare_type_name(node_text(type_node, source))
                else:
                    value = n.child_by_field_name("value")
                    if value is not None and value.named_child_count == 1:
                        typ = _literal_type(value.named_children[0], source)
                if typ:
                    types[node_text(name_node, source)] = typ
        stack.extend(n.named_children)
    return types


def _literal_type(value_node, source: bytes) -> str | None:
    """Type name out of `T{...}` or `&T{...}`, else None."""
    if value_node.type == "unary_expression":       # &T{...}
        operand = value_node.child_by_field_name("operand")
        if operand is not None:
            value_node = operand
    if value_node.type == "composite_literal":
        type_node = value_node.child_by_field_name("type")
        if type_node is not None:
            return _bare_type_name(node_text(type_node, source))
    return None


def _bare_type_name(text: str) -> str:
    """'*pkg.Device[T]' -> 'Device': strip pointer, generics, package."""
    return text.lstrip("*&").split("[")[0].split(".")[-1].strip()


def extract_imports(source: bytes, tree) -> list[ImportRecord]:
    """Every import spec. The local name (what call sites use as `pkg.`) is
    the alias if present, else the LAST path segment — which is a heuristic:
    a package's name can differ from its directory, but rarely does.

    Called by: indexer pass 2.
    """
    query = compile_query("go", IMPORT_QUERY)
    records: list[ImportRecord] = []
    for _pat, captures in query_matches(query, tree.root_node):
        spec = capture_one(captures, "spec")
        if spec is None:
            continue
        path_node = spec.child_by_field_name("path")
        if path_node is None:
            continue
        path = node_text(path_node, source).strip('"')
        name_node = spec.child_by_field_name("name")   # alias: `util "pkg/util"`
        alias = node_text(name_node, source) if name_node is not None else path.split("/")[-1]
        if alias in ("_", "."):
            # blank/dot imports don't create a callable prefix; record for
            # completeness but they resolve nothing.
            alias = path.split("/")[-1]
        records.append(ImportRecord(path, None, alias, spec.start_point.row + 1))
    return records


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

"""L1 — tree-sitter grammar registry and version-compat shims.

One job: hand out a ready-to-use Parser for a language key, loading and
caching the compiled grammar exactly once per process.

Called by: indexer.index_repository() (get_parser) and every extractor in
           extract/ (compile_query / query_matches).
Calls:     the tree_sitter binding + the per-language grammar wheels.

How grammar loading actually works, since it looks like magic:
    tree_sitter_go.language() returns a raw C pointer to a compiled grammar
    (each grammar pip package is just a .so/.pyd containing tables generated
    from the grammar.js). tree_sitter.Language() wraps that pointer into a
    Python object the Parser can use. That wrapping is cheap but not free,
    so we cache both Language and Parser objects at module level.

Why the compat shims: the tree-sitter Python binding has churned its query
API across 0.21 -> 0.25 (lang.query() became Query(), query.matches()
moved onto QueryCursor). The shims pin our extractors to ONE calling
convention and absorb the difference here, in one place.
"""

from __future__ import annotations

from tree_sitter import Language, Parser, Query

try:  # tree-sitter >= 0.24: queries execute through a QueryCursor object
    from tree_sitter import QueryCursor
except ImportError:  # older binding: query.matches() lives on Query itself
    QueryCursor = None

# Module-level caches. Key = language key from config.LANGUAGE_BY_EXTENSION.
_LANGUAGES: dict[str, Language] = {}
_PARSERS: dict[str, Parser] = {}
_QUERIES: dict[tuple[str, str], Query] = {}   # (lang_key, query_src) -> compiled


def get_language(lang_key: str) -> Language:
    """Load + cache the compiled grammar for a language key.

    Called by: get_parser() and compile_query().
    The import happens INSIDE the function on purpose: you pay the wheel
    import only for languages that actually appear in the repo.
    """
    if lang_key not in _LANGUAGES:
        if lang_key == "python":
            import tree_sitter_python
            ptr = tree_sitter_python.language()
        elif lang_key == "go":
            import tree_sitter_go
            ptr = tree_sitter_go.language()
        elif lang_key == "typescript":
            import tree_sitter_typescript
            ptr = tree_sitter_typescript.language_typescript()
        elif lang_key == "tsx":
            # Separate grammar from "typescript" — JSX makes `<T>` ambiguous
            # (opening tag vs type parameter), so it's a different parse table.
            import tree_sitter_typescript
            ptr = tree_sitter_typescript.language_tsx()
        else:
            raise ValueError(f"no grammar registered for language {lang_key!r}")
        _LANGUAGES[lang_key] = Language(ptr)
    return _LANGUAGES[lang_key]


def get_parser(lang_key: str) -> Parser:
    """Cached Parser for a language. Reuse is safe: we parse sequentially,
    and a Parser is reusable across parse() calls.

    Called by: indexer.index_repository(), once per file.
    """
    if lang_key not in _PARSERS:
        _PARSERS[lang_key] = Parser(get_language(lang_key))
    return _PARSERS[lang_key]


def compile_query(lang_key: str, query_src: str) -> Query:
    """Compile an S-expression query against a grammar, with caching.

    Called by: every extractor in extract/, once per (language, query) pair —
               the cache means 10,000 files compile the query exactly once.
    Raises:    tree_sitter.QueryError at COMPILE time if the query names a
               node type the grammar doesn't have. That's a feature: typos
               fail loudly on the first file, not silently forever.
    """
    key = (lang_key, query_src)
    if key not in _QUERIES:
        lang = get_language(lang_key)
        try:
            _QUERIES[key] = Query(lang, query_src)      # binding >= 0.22
        except TypeError:
            _QUERIES[key] = lang.query(query_src)       # older binding
    return _QUERIES[key]


def query_matches(query: Query, node):
    """Run a compiled query over a subtree; normalize the result shape.

    Called by: every extractor's extract() loop.
    Returns:   list of (pattern_index, captures_dict). captures_dict maps
               capture name (str, no '@') -> list[Node]. Captures within one
               match belong together — that's why extractors use matches()
               and not captures(): @def and @name arrive already paired.
    """
    if QueryCursor is not None:
        return QueryCursor(query).matches(node)
    return query.matches(node)


def capture_one(captures: dict, name: str):
    """First node captured under `name`, or None. Normalizes an API quirk:
    some binding versions store a bare Node, newer ones store list[Node].

    Called by: extractors, for every capture they read.
    """
    value = captures.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        return value[0] if value else None
    return value

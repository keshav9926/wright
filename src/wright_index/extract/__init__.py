"""L2 — symbol extraction. One extractor per language, one shared registry.

The registry is the ONLY thing the indexer needs from this package:

    from wright_index.extract import EXTRACTORS
    symbols = EXTRACTORS[source_file.language](content, tree)

Adding a language later = write extract/<lang>.py with an extract() function,
add one line here, add the extension to config.LANGUAGE_BY_EXTENSION. Nothing
else in the pipeline changes — the indexer is language-blind by design.
"""

import functools

from . import go, python, typescript
from .base import Symbol

# language key (from config.LANGUAGE_BY_EXTENSION) -> extract(source, tree)
# "tsx" is the SAME extractor as "typescript" with the grammar key bound:
# identical node types and logic, but the query must be compiled against
# the tsx grammar object or it silently matches nothing (see extract()
# docstring in typescript.py).
EXTRACTORS = {
    "python": python.extract,
    "go": go.extract,
    "typescript": typescript.extract,
    "tsx": functools.partial(typescript.extract, lang_key="tsx"),
}

# Day 2 registries — same shape, same tsx binding rule. The indexer's pass 2
# looks up all three by the file's language key and stays language-blind.
CALL_EXTRACTORS = {
    "python": python.extract_calls,
    "go": go.extract_calls,
    "typescript": typescript.extract_calls,
    "tsx": functools.partial(typescript.extract_calls, lang_key="tsx"),
}

IMPORT_EXTRACTORS = {
    "python": python.extract_imports,
    "go": go.extract_imports,
    "typescript": typescript.extract_imports,
    "tsx": functools.partial(typescript.extract_imports, lang_key="tsx"),
}

__all__ = ["EXTRACTORS", "CALL_EXTRACTORS", "IMPORT_EXTRACTORS", "Symbol"]

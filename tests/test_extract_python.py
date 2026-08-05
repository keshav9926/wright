"""L2 tests — Python extractor: kinds, nesting, docstrings, decorators."""

from __future__ import annotations

from wright_index.extract.python import extract

SAMPLE = '''\
"""Module docstring — must NOT be captured as a symbol."""
import os


def top_level(a, b=2) -> int:
    """Adds things."""
    return a + b


async def fetch(url):
    return url


def _private_helper():
    pass


class Parser:
    """Parses stuff."""

    def parse(self, src: str) -> dict:
        """Parse one source string."""
        def inner_helper(x):
            return x
        return {}

    @staticmethod
    def utility():
        pass


@some.decorator(arg=1)
def decorated():
    pass
'''


def _by_qname(source: str):
    from wright_index.parsers import get_parser
    data = source.encode()
    tree = get_parser("python").parse(data)
    return {s.qualified_name: s for s in extract(data, tree)}


def test_finds_all_definitions():
    syms = _by_qname(SAMPLE)
    assert set(syms) == {
        "top_level", "fetch", "_private_helper", "Parser",
        "Parser.parse", "Parser.parse.inner_helper", "Parser.utility",
        "decorated",
    }


def test_kinds():
    syms = _by_qname(SAMPLE)
    assert syms["top_level"].kind == "function"
    assert syms["fetch"].kind == "function"            # async def == function
    assert syms["Parser"].kind == "class"
    assert syms["Parser.parse"].kind == "method"       # def directly in class
    assert syms["Parser.parse.inner_helper"].kind == "function"  # nested def


def test_docstrings_extracted_without_quotes():
    syms = _by_qname(SAMPLE)
    assert syms["top_level"].docstring == "Adds things."
    assert syms["Parser"].docstring == "Parses stuff."
    assert syms["Parser.parse"].docstring == "Parse one source string."
    assert syms["fetch"].docstring is None             # no docstring present


def test_signature_is_one_clean_line():
    syms = _by_qname(SAMPLE)
    assert syms["top_level"].signature == "def top_level(a, b=2) -> int"
    assert syms["Parser"].signature == "class Parser"


def test_decorated_range_includes_decorator_but_signature_does_not():
    syms = _by_qname(SAMPLE)
    d = syms["decorated"]
    # decorator line is where the symbol STARTS...
    decorator_line = SAMPLE.splitlines().index("@some.decorator(arg=1)") + 1
    assert d.start_line == decorator_line
    # ...but the signature stays the clean def
    assert d.signature == "def decorated()"


def test_export_convention():
    syms = _by_qname(SAMPLE)
    assert syms["top_level"].is_exported
    assert not syms["_private_helper"].is_exported     # leading underscore


def test_parent_links():
    syms = _by_qname(SAMPLE)
    assert syms["Parser.parse"].parent == "Parser"
    assert syms["top_level"].parent is None

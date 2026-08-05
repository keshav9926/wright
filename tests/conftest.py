"""Shared fixtures: parse helpers so extractor tests stay one-liners.

pytest auto-discovers this file; every test module can use these fixtures
without importing them.
"""

from __future__ import annotations

import pytest

from wright_index.parsers import get_parser


@pytest.fixture
def parse_python():
    """Return a function: source str -> (bytes, tree). Used by extractor tests."""
    def _parse(src: str):
        data = src.encode("utf-8")
        return data, get_parser("python").parse(data)
    return _parse


@pytest.fixture
def parse_go():
    def _parse(src: str):
        data = src.encode("utf-8")
        return data, get_parser("go").parse(data)
    return _parse


@pytest.fixture
def parse_ts():
    def _parse(src: str):
        data = src.encode("utf-8")
        return data, get_parser("typescript").parse(data)
    return _parse

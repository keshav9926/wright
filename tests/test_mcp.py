"""Day 4 tests — the MCP tools, two levels:

1. Tool functions called directly against a real index (fast, precise).
2. One REAL stdio handshake: spawn `wi mcp`, speak JSON-RPC over pipes,
   list tools, call one. This is the test that catches protocol/framing
   breakage that in-process calls can't see.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import wright_index.mcp_server as mcp_mod
from wright_index.indexer import index_repository

REPO_FILES = {
    "core.py": (
        "def compute(x):\n    \"\"\"Doubles.\"\"\"\n    return x * 2\n\n"
        "def orchestrate(x):\n    return compute(x)\n"
    ),
    "tests/test_core.py": (
        "from core import compute\n\n"
        "def test_compute():\n    assert compute(2) == 4\n"
    ),
}


@pytest.fixture
def served_repo(tmp_path: Path, monkeypatch) -> Path:
    """Index a tiny repo and point the MCP module's globals at it."""
    repo = tmp_path / "repo"
    for rel, content in REPO_FILES.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    db_path = tmp_path / "idx.db"
    index_repository(repo, db_path=db_path)
    monkeypatch.setattr(mcp_mod, "REPO_PATH", repo)
    monkeypatch.setattr(mcp_mod, "db_path_for", lambda _: db_path)
    return repo


def test_find_symbol_tool(served_repo):
    out = mcp_mod.find_symbol("compute")
    assert "core.py:1-3" in out and "def compute(x)" in out


def test_callers_tool(served_repo):
    out = mcp_mod.callers("compute")
    assert "orchestrate" in out and "test_compute" in out


def test_blast_radius_tool(served_repo):
    out = mcp_mod.blast_radius("compute")
    assert "orchestrate" in out                  # static caller
    assert "test_compute" in out                 # covering test


def test_covering_tests_tool(served_repo):
    out = mcp_mod.covering_tests("compute")
    assert "test_compute" in out and "tests/test_core.py" in out


def test_repo_map_tool(served_repo):
    out = mcp_mod.repo_map()
    assert "python: 2 files" in out and "symbols:" in out


def test_missing_index_message(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_mod, "REPO_PATH", tmp_path)
    with pytest.raises(RuntimeError, match="wi index"):
        mcp_mod._db()


# --------------------------------------------------------------------- #
# the real thing: stdio JSON-RPC handshake against a spawned server      #
# --------------------------------------------------------------------- #

def _rpc(id_, method, **params):
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": method,
                       "params": params}) + "\n"


def test_stdio_handshake_and_tool_call(tmp_path: Path):
    """initialize -> initialized -> tools/list -> tools/call find_symbol.
    Newline-delimited JSON over pipes, exactly as Claude Code speaks it."""
    repo = tmp_path / "repo"
    for rel, content in REPO_FILES.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    index_repository(repo)          # default db location — the server's lookup

    proc = subprocess.Popen(
        [sys.executable, "-m", "wright_index.cli", "mcp", str(repo)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    # Reader thread: closing stdin too early races server shutdown against
    # the in-flight tools/call — the response never arrives. So we keep
    # stdin open, collect stdout lines in the background, and only close
    # once every expected id has answered.
    import queue
    import threading

    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=lambda: [lines.put(l) for l in iter(proc.stdout.readline, "")],
        daemon=True)
    reader.start()

    def wait_for(ids: set[int], timeout: float = 30.0) -> dict:
        got: dict = {}
        import time
        deadline = time.monotonic() + timeout
        while ids - got.keys() and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if not line.strip():
                continue
            msg = json.loads(line)
            if "id" in msg:
                got[msg["id"]] = msg
        assert not (ids - got.keys()), f"no response for ids {ids - got.keys()}"
        return got

    try:
        proc.stdin.write(_rpc(1, "initialize",
                              protocolVersion="2025-06-18",
                              capabilities={},
                              clientInfo={"name": "pytest", "version": "0"}))
        proc.stdin.flush()
        init = wait_for({1})
        assert init[1]["result"]["serverInfo"]["name"] == "wright-index"

        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.write(_rpc(2, "tools/list"))
        proc.stdin.write(_rpc(3, "tools/call", name="find_symbol",
                              arguments={"name": "compute"}))
        proc.stdin.flush()
        got = wait_for({2, 3})

        tool_names = {t["name"] for t in got[2]["result"]["tools"]}
        assert {"find_symbol", "callers", "blast_radius", "cochange",
                "covering_tests", "hot_files", "repo_map", "refs"} <= tool_names
        text = got[3]["result"]["content"][0]["text"]
        assert "core.py:1-3" in text
    finally:
        proc.kill()

"""Day 2 end-to-end: call/import extraction + resolution + graph queries.

Strategy: tiny synthetic repos per language in tmp_path, indexed through the
REAL two-pass pipeline, then assertions on the edges table — one test per
rung of the resolution ladder, plus the recursive-CTE traversal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wright_index.db import Database
from wright_index.indexer import index_repository


def _index(tmp_path: Path, files: dict[str, str]) -> Database:
    """Write files into a repo dir, index it, return an open Database."""
    repo = tmp_path / "repo"
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    db_path = tmp_path / "idx.db"
    index_repository(repo, db_path=db_path)
    return Database(db_path)


def _edge(db: Database, src_qualified: str, dst_name: str):
    """Fetch one edge by caller qualified name + callee name, or None."""
    return db.conn.execute(
        "SELECT e.*, dst.qualified_name AS dst_qualified FROM edges e"
        " JOIN symbols src ON src.id = e.src_symbol_id"
        " LEFT JOIN symbols dst ON dst.id = e.dst_symbol_id"
        " WHERE src.qualified_name = ? AND e.dst_name = ?",
        (src_qualified, dst_name),
    ).fetchone()


# --------------------------------------------------------------------- #
# Python                                                                 #
# --------------------------------------------------------------------- #

PY_REPO = {
    "app/util.py": (
        "def helper(x):\n    return x\n"
    ),
    "app/main.py": (
        "from app.util import helper\n"
        "import json\n\n"
        "def local(x):\n    return x\n\n"
        "class Runner:\n"
        "    def go(self):\n"
        "        self.stop()\n"          # receiver: self -> Runner.stop
        "        local(1)\n"             # same_file
        "        helper(2)\n"            # import -> app/util.py helper
        "        json.dumps({})\n"       # external -> unresolved
        "    def stop(self):\n        pass\n"
    ),
}


def test_python_ladder(tmp_path: Path):
    db = _index(tmp_path, PY_REPO)

    e = _edge(db, "Runner.go", "stop")
    assert e["resolution"] == "receiver" and e["dst_qualified"] == "Runner.stop"

    e = _edge(db, "Runner.go", "local")
    assert e["resolution"] == "same_file" and e["dst_qualified"] == "local"

    e = _edge(db, "Runner.go", "helper")
    assert e["resolution"] == "import" and e["dst_qualified"] == "helper"

    e = _edge(db, "Runner.go", "dumps")           # json.dumps: external module
    assert e["dst_symbol_id"] is None and e["resolution"] == "unresolved"
    db.close()


# --------------------------------------------------------------------- #
# Go                                                                     #
# --------------------------------------------------------------------- #

GO_REPO = {
    "go.mod": "module example.com/demo\n\ngo 1.22\n",
    "pkg/store/a.go": (
        "package store\n\n"
        "func Save(x int) int { return Validate(x) }\n"   # package: other file
    ),
    "pkg/store/b.go": (
        "package store\n\n"
        "func Validate(x int) int { return x }\n"
    ),
    "pkg/api/api.go": (
        "package api\n\n"
        "import \"example.com/demo/pkg/store\"\n\n"
        "type Server struct{}\n\n"
        "func (s *Server) Handle(x int) int {\n"
        "\treturn s.prep(store.Save(x))\n"    # receiver + import in one line
        "}\n\n"
        "func (s *Server) prep(x int) int { return x }\n"
    ),
}


def test_go_ladder(tmp_path: Path):
    db = _index(tmp_path, GO_REPO)

    # plain call resolves package-wide (Save in a.go -> Validate in b.go)
    e = _edge(db, "Save", "Validate")
    assert e["resolution"] == "package" and e["dst_qualified"] == "Validate"

    # s.prep(): receiver var call -> Server.prep, proven from the tree
    e = _edge(db, "Server.Handle", "prep")
    assert e["resolution"] == "receiver" and e["dst_qualified"] == "Server.prep"

    # store.Save(): import path traced through go.mod module prefix
    e = _edge(db, "Server.Handle", "Save")
    assert e["resolution"] == "import" and e["dst_qualified"] == "Save"
    db.close()


# --------------------------------------------------------------------- #
# TypeScript                                                             #
# --------------------------------------------------------------------- #

TS_REPO = {
    "src/util.ts": (
        "export function format(s: string): string { return s; }\n"
    ),
    "src/app.ts": (
        "import { format } from './util';\n\n"
        "function localHelper(): void {}\n\n"
        "export class App {\n"
        "  run(): void {\n"
        "    this.reset();\n"            # receiver: this -> App.reset
        "    localHelper();\n"           # same_file
        "    format('x');\n"             # import -> src/util.ts
        "    console.log('y');\n"        # external -> unresolved
        "  }\n"
        "  reset(): void {}\n"
        "}\n"
    ),
}


def test_typescript_ladder(tmp_path: Path):
    db = _index(tmp_path, TS_REPO)

    e = _edge(db, "App.run", "reset")
    assert e["resolution"] == "receiver" and e["dst_qualified"] == "App.reset"

    e = _edge(db, "App.run", "localHelper")
    assert e["resolution"] == "same_file"

    e = _edge(db, "App.run", "format")
    assert e["resolution"] == "import" and e["dst_qualified"] == "format"

    e = _edge(db, "App.run", "log")
    assert e["dst_symbol_id"] is None
    db.close()


# --------------------------------------------------------------------- #
# graph queries                                                          #
# --------------------------------------------------------------------- #

def test_callers_transitive_depth(tmp_path: Path):
    """a -> b -> c: callers_of(c, depth=2) must surface a at depth 2."""
    db = _index(tmp_path, {
        "m.py": (
            "def c():\n    pass\n\n"
            "def b():\n    c()\n\n"
            "def a():\n    b()\n"
        ),
    })
    target = db.find_symbols_by_name("c")[0]

    one_hop = db.callers_of(target["id"], depth=1)
    assert [r["qualified_name"] for r in one_hop] == ["b"]

    two_hop = db.callers_of(target["id"], depth=2)
    names = {r["qualified_name"]: r["depth"] for r in two_hop}
    assert names == {"b": 1, "a": 2}
    db.close()


def test_refs_include_unresolved(tmp_path: Path):
    """Interface-dispatch-style calls appear in refs even without an edge."""
    db = _index(tmp_path, {
        "a.py": (
            "def use(obj):\n"
            "    obj.process()\n"        # unknown receiver, ambiguous below
            "\n"
            "class X:\n"
            "    def process(self):\n        pass\n"
            "\n"
            "class Y:\n"
            "    def process(self):\n        pass\n"
        ),
    })
    rows = db.refs_by_name("process")
    assert len(rows) == 1
    assert rows[0]["caller"] == "use"
    assert rows[0]["resolution"] == "unresolved"   # two candidates -> no guess
    db.close()


def test_name_only_when_repo_unique(tmp_path: Path):
    """obj.method() with exactly one same-named method repo-wide -> weak edge."""
    db = _index(tmp_path, {
        "a.py": (
            "class Only:\n"
            "    def unique_method(self):\n        pass\n"
            "\n"
            "def use(obj):\n"
            "    obj.unique_method()\n"
        ),
    })
    e = _edge(db, "use", "unique_method")
    assert e["resolution"] == "name_only"
    assert e["confidence"] == pytest.approx(0.5)
    assert e["dst_qualified"] == "Only.unique_method"
    db.close()


def test_imports_table_populated(tmp_path: Path):
    db = _index(tmp_path, PY_REPO)
    rows = db.conn.execute(
        "SELECT i.module, i.symbol, i.alias, f2.path AS resolved FROM imports i"
        " LEFT JOIN files f2 ON f2.id = i.resolved_file_id"
        " ORDER BY i.module").fetchall()
    by_module = {r["module"]: r for r in rows}
    assert by_module["app.util"]["symbol"] == "helper"
    assert by_module["app.util"]["resolved"] == "app/util.py"   # resolved in-repo
    assert by_module["json"]["resolved"] is None                # stdlib: external
    db.close()

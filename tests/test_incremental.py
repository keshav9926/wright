"""Day 5 tests — incremental reindex: hash-skip, cascades, dependent
re-resolution, and the name_only invalidation subtlety.

The failure mode these guard against is SILENT STALENESS: an incremental
run that leaves a dangling edge or a stale resolution produces wrong
answers forever after, with no error anywhere.
"""

from __future__ import annotations

from pathlib import Path

from wright_index.db import Database
from wright_index.indexer import index_repository, reindex, reindex_incremental


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """Two-file repo: app.py calls lib.helper via import."""
    repo = tmp_path / "repo"
    _write(repo, "lib.py", "def helper(x):\n    return x\n")
    _write(repo, "app.py",
           "from lib import helper\n\ndef run():\n    return helper(1)\n")
    db_path = tmp_path / "idx.db"
    index_repository(repo, db_path=db_path)
    return repo, db_path


def test_unchanged_files_are_hash_skipped(tmp_path: Path):
    repo, db_path = _setup(tmp_path)
    result = reindex_incremental(repo, db_path)
    assert result.mode == "incremental"
    assert result.files_indexed == 0            # nothing reparsed
    assert result.files_unchanged == 2
    assert result.seconds < 2.0                  # the Day 5 acceptance bound


def test_modified_file_reindexed_others_skipped(tmp_path: Path):
    repo, db_path = _setup(tmp_path)
    _write(repo, "lib.py", "def helper(x):\n    return x + 1\n\ndef extra():\n    pass\n")
    result = reindex_incremental(repo, db_path)
    assert result.files_indexed == 1             # only lib.py
    assert result.files_unchanged == 1           # app.py hash-skipped

    db = Database(db_path)
    names = {r["name"] for r in db.search_symbols(file="lib.py")}
    assert names == {"helper", "extra"}          # new symbol present
    # app.py's edge re-resolved to the NEW helper symbol (old id is gone)
    edge = db.conn.execute(
        "SELECT e.resolution, dst.name FROM edges e"
        " JOIN symbols dst ON dst.id = e.dst_symbol_id"
        " WHERE e.dst_name = 'helper'").fetchone()
    assert edge is not None and edge["name"] == "helper"
    db.close()


def test_dependent_reresolved_when_target_renamed(tmp_path: Path):
    """helper -> helper2 in lib.py: app.py (unchanged!) must lose its
    proven edge — a stale edge would point at a symbol that no longer
    exists. This is THE dangling-edge case."""
    repo, db_path = _setup(tmp_path)
    _write(repo, "lib.py", "def helper2(x):\n    return x\n")
    result = reindex_incremental(repo, db_path)
    assert result.dependents_reresolved == 1     # app.py re-ran pass 2

    db = Database(db_path)
    edge = db.conn.execute(
        "SELECT dst_symbol_id, resolution FROM edges WHERE dst_name = 'helper'").fetchone()
    assert edge is not None
    assert edge["dst_symbol_id"] is None         # honestly unresolved now
    # and no edge anywhere points at a nonexistent symbol row
    dangling = db.conn.execute(
        "SELECT COUNT(*) AS n FROM edges e WHERE e.dst_symbol_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM symbols s WHERE s.id = e.dst_symbol_id)"
    ).fetchone()["n"]
    assert dangling == 0
    db.close()


def test_deleted_file_cascades_and_dependents_rerun(tmp_path: Path):
    repo, db_path = _setup(tmp_path)
    (repo / "lib.py").unlink()
    result = reindex_incremental(repo, db_path)
    assert result.dependents_reresolved == 1

    db = Database(db_path)
    assert db.search_symbols(file="lib.py") == []
    edge = db.conn.execute(
        "SELECT dst_symbol_id FROM edges WHERE dst_name = 'helper'").fetchone()
    assert edge["dst_symbol_id"] is None
    db.close()


def test_name_only_invalidated_by_new_duplicate(tmp_path: Path):
    """The subtlest case: a NEW file defining a second `unique_method`
    breaks the repo-unique assumption behind an existing name_only edge
    in an UNCHANGED file. That edge must be downgraded to unresolved."""
    repo = tmp_path / "repo"
    _write(repo, "a.py",
           "class Only:\n    def unique_method(self):\n        pass\n\n"
           "def use(obj):\n    obj.unique_method()\n")
    db_path = tmp_path / "idx.db"
    index_repository(repo, db_path=db_path)

    db = Database(db_path)
    before = db.conn.execute(
        "SELECT resolution FROM edges WHERE dst_name='unique_method'").fetchone()
    assert before["resolution"] == "name_only"   # unique -> weak edge
    db.close()

    _write(repo, "b.py",
           "class Second:\n    def unique_method(self):\n        pass\n")
    result = reindex_incremental(repo, db_path)
    assert result.dependents_reresolved == 1     # a.py re-resolved

    db = Database(db_path)
    after = db.conn.execute(
        "SELECT resolution, dst_symbol_id FROM edges WHERE dst_name='unique_method'"
        " AND resolution != 'receiver'").fetchone()
    assert after["resolution"] == "unresolved"   # two candidates -> no guess
    assert after["dst_symbol_id"] is None
    db.close()


def test_reindex_dispatch(tmp_path: Path):
    """reindex() = full when no db, incremental when one exists."""
    repo = tmp_path / "repo"
    _write(repo, "a.py", "def f():\n    pass\n")
    db_path = tmp_path / "idx.db"

    first = reindex(repo, db_path=db_path)
    assert first.mode == "full"
    second = reindex(repo, db_path=db_path)
    assert second.mode == "incremental"
    forced = reindex(repo, db_path=db_path, full=True)
    assert forced.mode == "full"

"""Day 3 tests — history mining against REAL git repos built in tmp_path.

No mocks: we run actual `git init/add/commit` sequences, because the thing
under test IS the git-log parsing and the association-rule math on top.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wright_index.db import Database
from wright_index.indexer import index_repository


def _git(repo: Path, *args: str) -> None:
    """Run one git command in the fixture repo, with a fixed identity so
    commits work on any machine (no global config needed)."""
    subprocess.run(
        ["git", "-C", str(repo),
         "-c", "user.name=Test Author", "-c", "user.email=t@example.com",
         *args],
        check=True, capture_output=True,
    )


def _commit(repo: Path, message: str, files: dict[str, str]) -> None:
    """Write files, stage, commit — one basket for the miner."""
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message, "--no-verify")


@pytest.fixture
def coupled_repo(tmp_path: Path) -> Path:
    """A repo where schema.txt and api.py ALWAYS change together (4/4
    commits), while noise.py changes independently — the textbook lift case.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    # 4 commits touching the coupled pair (content must differ each time)
    for i in range(4):
        _commit(repo, f"schema change {i}", {
            "schema.txt": f"version {i}\n",
            "api.py": f"def handler():\n    return {i}\n",
        })
    # 6 commits touching only noise.py — high change count, no coupling
    for i in range(6):
        _commit(repo, f"noise {i}", {"noise.py": f"x = {i}\n"})
    return repo


def test_cochange_lift_finds_real_coupling(coupled_repo: Path, tmp_path: Path):
    db_path = tmp_path / "idx.db"
    result = index_repository(coupled_repo, db_path=db_path)
    assert result.commits_scanned == 10
    assert result.cochange_pairs >= 1

    db = Database(db_path)
    rows = db.cochange_for("schema.txt")
    assert len(rows) == 1
    r = rows[0]
    assert r["partner"] == "api.py"
    assert r["both_count"] == 4
    # P(api.py changes | schema.txt changed) = 4/4 = 100%
    assert r["confidence"] == pytest.approx(1.0)
    # lift = (4 * 10) / (4 * 4) = 2.5 — coupled well beyond chance
    assert r["lift"] == pytest.approx(2.5)
    db.close()


def test_uncoupled_file_has_no_partners(coupled_repo: Path, tmp_path: Path):
    db_path = tmp_path / "idx.db"
    index_repository(coupled_repo, db_path=db_path)
    db = Database(db_path)
    # noise.py never co-changed with anything >= MIN_PAIR_COUNT times
    assert db.cochange_for("noise.py") == []
    db.close()


def test_file_history_stats(coupled_repo: Path, tmp_path: Path):
    db_path = tmp_path / "idx.db"
    index_repository(coupled_repo, db_path=db_path)
    db = Database(db_path)
    hot = db.hot_files(limit=3)
    assert hot[0]["path"] == "noise.py"          # 6 commits, the churn leader
    assert hot[0]["change_count"] == 6
    assert "Test Author" in hot[0]["top_authors"]
    db.close()


def test_non_git_directory_still_indexes(tmp_path: Path):
    """History is optional: a plain directory gets symbols+edges, no pairs."""
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    db_path = tmp_path / "idx.db"
    result = index_repository(repo, db_path=db_path)
    assert result.symbol_count == 1
    assert result.commits_scanned == 0
    assert result.cochange_pairs == 0


def test_bulk_commits_excluded(tmp_path: Path):
    """A 60-file sweep commit must not generate 1,770 junk pairs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "bulk sweep", {f"gen/f{i}.py": f"x = {i}\n" for i in range(60)})
    # three real commits so the repo isn't empty of signal
    for i in range(3):
        _commit(repo, f"pair {i}", {"a.py": f"a = {i}\n", "b.py": f"b = {i}\n"})

    db_path = tmp_path / "idx.db"
    result = index_repository(repo, db_path=db_path)
    assert result.commits_scanned == 4

    db = Database(db_path)
    pairs = db.conn.execute("SELECT COUNT(*) AS n FROM cochange").fetchone()["n"]
    assert pairs == 1                            # only (a.py, b.py); sweep skipped
    db.close()


def test_tests_for_via_call_edges(tmp_path: Path):
    """tests-for = Day 1 is_test flag x Day 2 edges. No history needed."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "core.py").write_text(
        "def compute(x):\n    return x * 2\n", encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "from core import compute\n\n"
        "def test_compute():\n    assert compute(2) == 4\n", encoding="utf-8")

    db_path = tmp_path / "idx.db"
    index_repository(repo, db_path=db_path)
    db = Database(db_path)
    target = db.find_symbols_by_name("compute")[0]
    tests = db.tests_for_symbol(target["id"])
    assert [t["qualified_name"] for t in tests] == ["test_compute"]
    assert tests[0]["file_path"] == "tests/test_core.py"
    db.close()

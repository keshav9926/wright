"""Storage — SQLite schema, bulk inserts, and the queries the CLI runs.

Why SQLite and not Postgres (which REPOSITORY-INTELLIGENCE.md chose): that
doc designed for a multi-tenant server; wright-index Day 1 is a local tool.
SQLite gives us zero setup, a single portable .db file per repo, and — the
part that matters for Day 2 — recursive CTEs for graph traversal, same as
Postgres. The SQL we write today survives a Postgres move nearly verbatim.

Called by: indexer.index_repository() (writes) and cli.py (reads).
Calls:     stdlib sqlite3 only.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .extract.base import Symbol
from .walker import SourceFile

SCHEMA_VERSION = "1"   # bump when the schema changes; checked on open

# ---------------------------------------------------------------------------
# Schema. Two tables today; Day 2 adds `edges` (calls/imports), Day 3 adds
# `cochange` — both will foreign-key into what's defined here, which is why
# symbols carry byte offsets already: edge endpoints resolve to byte ranges.
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id             INTEGER PRIMARY KEY,
    path           TEXT NOT NULL UNIQUE,   -- repo-relative, posix separators
    language       TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    line_count     INTEGER NOT NULL,
    content_hash   TEXT NOT NULL,          -- sha256; Day 5 incremental key
    is_test        INTEGER NOT NULL DEFAULT 0,
    parse_ok       INTEGER NOT NULL DEFAULT 1,  -- 0 = tree had ERROR nodes
    skipped_reason TEXT                    -- NULL = parsed; else why not
);

CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind           TEXT NOT NULL,
    parent         TEXT,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    start_byte     INTEGER NOT NULL,
    end_byte       INTEGER NOT NULL,
    signature      TEXT NOT NULL DEFAULT '',
    docstring      TEXT,
    is_exported    INTEGER NOT NULL DEFAULT 1
);

-- The lookups the CLI (and Day 4's MCP tools) actually run:
CREATE INDEX IF NOT EXISTS idx_symbols_name  ON symbols(name);      -- wi symbols --name
CREATE INDEX IF NOT EXISTS idx_symbols_file  ON symbols(file_id);   -- wi symbols --file
CREATE INDEX IF NOT EXISTS idx_symbols_kind  ON symbols(kind);      -- wi symbols --kind
CREATE INDEX IF NOT EXISTS idx_files_path    ON files(path);
"""


def db_path_for(repo: Path) -> Path:
    """Where a repo's index lives: ~/.wright-index/<name>-<pathhash>.db

    Central cache, NOT inside the target repo — indexing someone else's
    checkout must never dirty their `git status`. The 8-char path hash
    disambiguates two clones with the same directory name.

    Called by: cli.py (every command) and tests.
    """
    resolved = str(repo.resolve())
    tag = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]
    cache_dir = Path.home() / ".wright-index"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{repo.resolve().name}-{tag}.db"


class Database:
    """Thin, explicit wrapper over one sqlite3 connection.

    Lifecycle: indexer opens with fresh=True (Day 1 reindex = rebuild from
    scratch; incremental arrives Day 5), CLI opens with fresh=False to read.
    """

    def __init__(self, path: Path, fresh: bool = False):
        """Called by: indexer.index_repository() (fresh=True) and
        cli._open_db() (fresh=False)."""
        self.path = Path(path)
        if fresh and self.path.exists():
            self.path.unlink()   # rebuild-from-zero semantics, deliberately
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row          # rows behave like dicts
        # WAL + NORMAL: the standard bulk-write configuration. WAL lets a
        # reader (another `wi symbols`) coexist with the indexer's writes;
        # NORMAL drops an fsync per txn we don't need for a rebuildable cache.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        if fresh:
            self.set_meta("schema_version", SCHEMA_VERSION)

    # ------------------------------------------------------------------ #
    # writes — called by indexer.index_repository() only                  #
    # ------------------------------------------------------------------ #

    def insert_file(self, sf: SourceFile, parse_ok: bool) -> int:
        """One row into files; returns its id for the symbol rows.
        NOT committed here — the indexer wraps the whole run in one
        transaction (one fsync for 10k files instead of 10k fsyncs)."""
        cursor = self.conn.execute(
            "INSERT INTO files (path, language, size_bytes, line_count,"
            " content_hash, is_test, parse_ok, skipped_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sf.rel_path, sf.language, sf.size_bytes, sf.line_count,
             sf.content_hash, int(sf.is_test), int(parse_ok), sf.skipped_reason),
        )
        return int(cursor.lastrowid)

    def insert_symbols(self, file_id: int, symbols: list[Symbol]) -> None:
        """Bulk insert one file's symbols. executemany = one prepared
        statement reused, the fast path for thousands of rows."""
        self.conn.executemany(
            "INSERT INTO symbols (file_id, name, qualified_name, kind, parent,"
            " start_line, end_line, start_byte, end_byte, signature, docstring,"
            " is_exported) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(file_id, s.name, s.qualified_name, s.kind, s.parent,
              s.start_line, s.end_line, s.start_byte, s.end_byte,
              s.signature, s.docstring, int(s.is_exported)) for s in symbols],
        )

    def set_meta(self, key: str, value: str) -> None:
        """Upsert into meta (indexed_at, repo_root, commit sha, timings)."""
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # reads — called by cli.py                                            #
    # ------------------------------------------------------------------ #

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def search_symbols(self, *, file: str | None = None, name: str | None = None,
                       kind: str | None = None, exported_only: bool = False,
                       include_tests: bool = True, limit: int = 200) -> list[sqlite3.Row]:
        """The query behind `wi symbols`. Filters compose via AND; the WHERE
        clause is built up piecewise so absent flags cost nothing.

        `file` matches by suffix (LIKE '%value') so users can type
        `--file device.go` instead of the full pkg/device/ascend/device.go.
        """
        clauses: list[str] = []
        params: list[object] = []
        if file:
            clauses.append("f.path LIKE ?")
            params.append(f"%{file}")
        if name:
            clauses.append("s.name LIKE ?")           # substring match
            params.append(f"%{name}%")
        if kind:
            clauses.append("s.kind = ?")
            params.append(kind)
        if exported_only:
            clauses.append("s.is_exported = 1")
        if not include_tests:
            clauses.append("f.is_test = 0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self.conn.execute(
            f"SELECT s.*, f.path AS file_path FROM symbols s"
            f" JOIN files f ON f.id = s.file_id {where}"
            f" ORDER BY f.path, s.start_line LIMIT ?",
            (*params, limit),
        ).fetchall()

    def stats(self) -> dict:
        """Aggregates for `wi stats` and the post-index summary table."""
        files_by_lang = {
            row["language"]: {"files": row["n"], "lines": row["lines"] or 0}
            for row in self.conn.execute(
                "SELECT language, COUNT(*) AS n, SUM(line_count) AS lines"
                " FROM files WHERE skipped_reason IS NULL GROUP BY language")
        }
        symbols_by_kind = {
            row["kind"]: row["n"]
            for row in self.conn.execute(
                "SELECT kind, COUNT(*) AS n FROM symbols GROUP BY kind")
        }
        skipped = {
            row["skipped_reason"]: row["n"]
            for row in self.conn.execute(
                "SELECT skipped_reason, COUNT(*) AS n FROM files"
                " WHERE skipped_reason IS NOT NULL GROUP BY skipped_reason")
        }
        parse_errors = self.conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE parse_ok = 0").fetchone()["n"]
        total_symbols = self.conn.execute(
            "SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
        return {
            "files_by_language": files_by_lang,
            "symbols_by_kind": symbols_by_kind,
            "skipped": skipped,
            "parse_errors": parse_errors,
            "total_symbols": total_symbols,
        }

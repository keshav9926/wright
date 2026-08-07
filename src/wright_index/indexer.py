"""The orchestrator — wires L0 -> L1 -> L2 -> DB into one pass.

This is the file to read to understand the whole system. Everything else is
a subsystem it calls:

    index_repository(root)
        │
        ├─ walker.iter_source_files(root)      L0: yields SourceFile, one at
        │                                          a time (generator = flat
        │                                          memory profile)
        │   for each file:
        ├─ parsers.get_parser(lang).parse()    L1: bytes -> syntax tree
        ├─ EXTRACTORS[lang](content, tree)     L2: tree -> list[Symbol]
        ├─ db.insert_file() / insert_symbols()     accumulate in ONE txn
        │
        └─ db.set_meta(...) + commit()             single fsync at the end

Called by: cli.index() — and later, Day 5's incremental reindex will reuse
           the per-file inner loop verbatim, just fed a smaller file list.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .db import Database, db_path_for
from .extract import CALL_EXTRACTORS, EXTRACTORS, IMPORT_EXTRACTORS
from .history import mine_history
from .parsers import get_parser
from .resolver import Resolver
from .walker import iter_source_files


@dataclass
class IndexResult:
    """What one indexing run produced — cli.index() renders this as tables.

    Produced by: index_repository(). Numbers here are also persisted into
    the meta table so `wi stats` can answer without re-indexing.
    """
    repo_root: Path
    db_path: Path
    files_indexed: int = 0
    files_skipped: int = 0
    symbol_count: int = 0
    parse_errors: int = 0                       # trees containing ERROR nodes
    seconds: float = 0.0
    files_by_language: dict = field(default_factory=dict)
    symbols_by_kind: dict = field(default_factory=dict)
    skipped_by_reason: dict = field(default_factory=dict)
    # Day 2 (pass 2) tallies:
    edge_count: int = 0                         # call edges stored
    edges_resolved: int = 0                     # ... with a proven dst symbol
    import_count: int = 0
    edges_by_resolution: dict = field(default_factory=dict)
    # Day 3 (pass 3) tallies:
    commits_scanned: int = 0
    cochange_pairs: int = 0
    # Day 5 (incremental) tallies:
    mode: str = "full"                          # "full" | "incremental"
    files_unchanged: int = 0                    # skipped via content hash
    dependents_reresolved: int = 0              # unchanged files re-resolved
                                                # because their edges pointed
                                                # into changed files


def index_repository(root: Path, db_path: Path | None = None) -> IndexResult:
    """Index one repository from scratch. THE entry point of the system.

    Called by: cli.index(); tests call it directly.
    Day 1 semantics: full rebuild every run (Database(fresh=True) deletes
    the old file). Incremental — hash-diff, reparse only changed files —
    is Day 5, and content_hash is already stored to enable it.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")

    db_path = db_path or db_path_for(root)
    db = Database(db_path, fresh=True)
    result = IndexResult(repo_root=root, db_path=db_path)
    started = time.perf_counter()

    # Files that parsed in pass 1, queued for pass 2 (call/import extraction
    # needs the COMPLETE symbol table first — you can't resolve a cross-file
    # call to a symbol that hasn't been indexed yet).
    parsed_files: list[tuple[int, Path, str]] = []

    try:
        # One transaction around the entire run: 10k files = ONE fsync.
        # Crash mid-run leaves a half-empty db, and that's fine — the next
        # run rebuilds from zero anyway. (Day 5 changes this contract.)
        for sf in iter_source_files(root):

            # ---- skipped files: recorded, never parsed ----------------
            if sf.skipped_reason is not None:
                db.insert_file(sf, parse_ok=True)
                result.files_skipped += 1
                result.skipped_by_reason[sf.skipped_reason] = (
                    result.skipped_by_reason.get(sf.skipped_reason, 0) + 1)
                continue

            # ---- L1: parse. tree-sitter is error-TOLERANT: broken code
            # yields a tree with ERROR nodes, not an exception. We index
            # whatever parsed clean around the damage — this is the property
            # that lets us index a repo mid-refactor — and flag the file.
            tree = get_parser(sf.language).parse(sf.content)
            parse_ok = not tree.root_node.has_error
            if not parse_ok:
                result.parse_errors += 1

            # ---- L2: extract symbols via the language's extractor -----
            symbols = EXTRACTORS[sf.language](sf.content, tree)

            # ---- store --------------------------------------------------
            file_id = db.insert_file(sf, parse_ok=parse_ok)
            db.insert_symbols(file_id, symbols)
            parsed_files.append((file_id, sf.abs_path, sf.language))

            # ---- tallies for the summary table -------------------------
            result.files_indexed += 1
            result.symbol_count += len(symbols)
            lang_stats = result.files_by_language.setdefault(
                sf.language, {"files": 0, "lines": 0})
            lang_stats["files"] += 1
            lang_stats["lines"] += sf.line_count
            for s in symbols:
                result.symbols_by_kind[s.kind] = result.symbols_by_kind.get(s.kind, 0) + 1

        # ================= PASS 2 — the call graph (Day 2) ================
        # Re-parse each file (tree-sitter is fast; holding 10k trees in
        # memory would not be) and extract call sites + imports. The
        # Resolver sees the complete symbol table from pass 1 — same
        # connection, same open transaction, so nothing is committed yet.
        resolver = Resolver(db, root)
        for file_id, abs_path, language in parsed_files:
            try:
                content = abs_path.read_bytes()
            except OSError:
                continue  # deleted between passes; its symbols stay, no edges
            tree = get_parser(language).parse(content)
            calls = CALL_EXTRACTORS[language](content, tree)
            imports = IMPORT_EXTRACTORS[language](content, tree)

            edge_rows, import_rows = resolver.resolve_file(
                file_id, language, imports, calls)
            db.insert_edges(edge_rows)
            db.insert_imports(import_rows)

            result.edge_count += len(edge_rows)
            result.import_count += len(import_rows)
            for row in edge_rows:
                if row[1] is not None:              # dst_symbol_id present
                    result.edges_resolved += 1
                res = row[5]                        # resolution tag
                result.edges_by_resolution[res] = (
                    result.edges_by_resolution.get(res, 0) + 1)

        # ================= PASS 3 — git history mining (Day 3) ============
        # Optional by nature: a plain directory tree indexes fine, it just
        # gets no co-change layer. Still inside the single transaction.
        hist = mine_history(root, db)
        result.commits_scanned = hist.commits_scanned
        result.cochange_pairs = hist.pairs_stored

        result.seconds = time.perf_counter() - started

        # ---- run metadata: lets `wi stats` describe the index without
        # touching the repo, and gives Day 5 its baseline commit ---------
        db.set_meta("repo_root", str(root))
        db.set_meta("indexed_at", datetime.now(timezone.utc).isoformat())
        db.set_meta("index_seconds", f"{result.seconds:.2f}")
        commit_sha = _git_head(root)
        if commit_sha:
            db.set_meta("commit_sha", commit_sha)

        db.commit()   # the single fsync
    finally:
        db.close()

    return result


def reindex(root: Path, db_path: Path | None = None, full: bool = False) -> IndexResult:
    """Smart entry point: incremental when an index exists, full otherwise.

    Called by: cli.index(). `full=True` forces the rebuild path.
    """
    root = Path(root).resolve()
    db_path = db_path or db_path_for(root)
    if full or not db_path.exists():
        return index_repository(root, db_path=db_path)
    return reindex_incremental(root, db_path)


def reindex_incremental(root: Path, db_path: Path) -> IndexResult:
    """Day 5 — reindex ONLY what changed, by content hash.

    The contract Day 1 set up finally pays off: every file row carries a
    sha256. Diff stored hashes against the working tree, then:

        unchanged  -> skip entirely (the common case: ~everything)
        changed    -> delete row (CASCADE wipes its symbols; edge CASCADE
                      wipes edges in AND out), re-parse, re-insert
        new        -> parse, insert
        deleted    -> delete row, cascades do the rest

    The subtle part is DEPENDENTS: an unchanged file whose edges pointed
    into a changed file just lost those edges to the cascade — and the
    symbol it called may have moved or vanished. So before deleting we
    record who pointed in (plus who IMPORTED the changed files, plus any
    edge whose dst_name matches a symbol name the change added/removed —
    that last one catches name_only edges whose uniqueness assumption
    the change may have broken), and re-run pass 2 for exactly those files.

    Called by: reindex(). Same single-transaction discipline as full runs.
    """
    root = Path(root).resolve()
    db = Database(db_path, fresh=False)
    result = IndexResult(repo_root=root, db_path=db_path, mode="incremental")
    started = time.perf_counter()

    try:
        # ---- diff the working tree against stored hashes ----------------
        stored = {row["path"]: (row["id"], row["content_hash"])
                  for row in db.conn.execute("SELECT id, path, content_hash FROM files")}

        current: dict[str, object] = {}
        for sf in iter_source_files(root):
            current[sf.rel_path] = sf

        changed_paths = [p for p, sf in current.items()
                         if p in stored and stored[p][1] != sf.content_hash]
        new_paths = [p for p in current if p not in stored]
        deleted_paths = [p for p in stored if p not in current]
        touched_ids = [stored[p][0] for p in changed_paths + deleted_paths]

        result.files_unchanged = len(current) - len(changed_paths) - len(new_paths)

        head = _git_head(root)
        if not changed_paths and not new_paths and not deleted_paths:
            # fast path: tree identical; refresh history only if HEAD moved
            if head and head != db.get_meta("commit_sha"):
                hist = mine_history(root, db)
                result.commits_scanned = hist.commits_scanned
                result.cochange_pairs = hist.pairs_stored
                db.set_meta("commit_sha", head)
                db.commit()
            result.seconds = time.perf_counter() - started
            return result

        # ---- names defined by soon-to-be-deleted symbols. Only the
        # SYMMETRIC DIFFERENCE vs the re-parsed names can flip a name-based
        # resolution elsewhere — a name that exists identically before and
        # after changes nothing for other files. (First cut used ALL names
        # in the changed file: touching a comment in HAMi's device.go then
        # re-resolved 34 files for 8s. Added/removed only -> ~2s.)
        old_names: set[str] = set()
        placeholders = ",".join("?" * len(touched_ids)) or "NULL"
        if touched_ids:
            for row in db.conn.execute(
                    f"SELECT DISTINCT name FROM symbols WHERE file_id IN ({placeholders})",
                    touched_ids):
                old_names.add(row["name"])

        # ---- dependents, recorded BEFORE the cascade erases the evidence
        dependent_ids: set[int] = set()
        if touched_ids:
            for row in db.conn.execute(
                    f"""SELECT DISTINCT s.file_id AS fid FROM edges e
                        JOIN symbols s ON s.id = e.src_symbol_id
                        WHERE e.dst_symbol_id IN (
                            SELECT id FROM symbols WHERE file_id IN ({placeholders}))""",
                    touched_ids):
                dependent_ids.add(row["fid"])
            for row in db.conn.execute(
                    f"SELECT DISTINCT file_id AS fid FROM imports"
                    f" WHERE resolved_file_id IN ({placeholders})", touched_ids):
                dependent_ids.add(row["fid"])

        # ---- apply deletions (CASCADE: symbols, edges both ends, imports)
        if touched_ids:
            db.conn.execute(
                f"DELETE FROM files WHERE id IN ({placeholders})", touched_ids)
        dependent_ids -= set(touched_ids)

        # ---- pass 1 for changed+new files --------------------------------
        new_names: set[str] = set()
        reparse: list[tuple[int, Path, str]] = []
        for path in changed_paths + new_paths:
            sf = current[path]
            if sf.skipped_reason is not None:
                db.insert_file(sf, parse_ok=True)
                result.files_skipped += 1
                continue
            tree = get_parser(sf.language).parse(sf.content)
            parse_ok = not tree.root_node.has_error
            if not parse_ok:
                result.parse_errors += 1
            symbols = EXTRACTORS[sf.language](sf.content, tree)
            file_id = db.insert_file(sf, parse_ok=parse_ok)
            db.insert_symbols(file_id, symbols)
            reparse.append((file_id, sf.abs_path, sf.language))
            result.files_indexed += 1
            result.symbol_count += len(symbols)
            for s in symbols:
                new_names.add(s.name)

        # names whose EXISTENCE changed — the only ones that can flip a
        # name_only ("was unique") or unresolved ("might resolve now") edge
        affected_names = old_names ^ new_names

        # ---- name_only edges elsewhere whose assumption may have broken --
        if affected_names:
            name_ph = ",".join("?" * len(affected_names))
            for row in db.conn.execute(
                    f"""SELECT DISTINCT s.file_id AS fid FROM edges e
                        JOIN symbols s ON s.id = e.src_symbol_id
                        WHERE e.dst_name IN ({name_ph})
                          AND e.resolution IN ('name_only', 'unresolved')""",
                    list(affected_names)):
                dependent_ids.add(row["fid"])
        dependent_ids -= {fid for fid, _, _ in reparse}

        # ---- pass 2: changed+new files AND dependents ---------------------
        # Dependents keep their symbols; only their outgoing edges/imports
        # are stale. Wipe those, then resolve like any other file.
        dependents: list[tuple[int, Path, str]] = []
        if dependent_ids:
            dep_ph = ",".join("?" * len(dependent_ids))
            for row in db.conn.execute(
                    f"SELECT id, path, language FROM files WHERE id IN ({dep_ph})"
                    f" AND skipped_reason IS NULL", list(dependent_ids)):
                dependents.append((row["id"], root / row["path"], row["language"]))
            db.conn.execute(
                f"""DELETE FROM edges WHERE src_symbol_id IN
                    (SELECT id FROM symbols WHERE file_id IN ({dep_ph}))""",
                list(dependent_ids))
            db.conn.execute(
                f"DELETE FROM imports WHERE file_id IN ({dep_ph})", list(dependent_ids))
        result.dependents_reresolved = len(dependents)

        resolver = Resolver(db, root)     # fresh view of the patched table
        for file_id, abs_path, language in reparse + dependents:
            try:
                content = abs_path.read_bytes()
            except OSError:
                continue
            tree = get_parser(language).parse(content)
            calls = CALL_EXTRACTORS[language](content, tree)
            imports = IMPORT_EXTRACTORS[language](content, tree)
            edge_rows, import_rows = resolver.resolve_file(
                file_id, language, imports, calls)
            db.insert_edges(edge_rows)
            db.insert_imports(import_rows)
            result.edge_count += len(edge_rows)
            result.import_count += len(import_rows)

        # ---- history refresh only when HEAD moved -------------------------
        if head and head != db.get_meta("commit_sha"):
            hist = mine_history(root, db)
            result.commits_scanned = hist.commits_scanned
            result.cochange_pairs = hist.pairs_stored
            db.set_meta("commit_sha", head)

        db.set_meta("indexed_at", datetime.now(timezone.utc).isoformat())
        result.seconds = time.perf_counter() - started
        db.set_meta("index_seconds", f"{result.seconds:.2f}")
        db.commit()
    finally:
        db.close()

    return result


def _git_head(root: Path) -> str | None:
    """HEAD sha of the target repo, if it is a git repo. Optional metadata —
    Day 5's incremental reindex uses it as `git diff <sha>..HEAD` baseline.

    Called by: index_repository(), once per run. Tolerates absence of git
    or of a .git dir (plain directory trees index fine).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None

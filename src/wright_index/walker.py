"""L0 — filesystem walk and classification.

Answers one question: WHICH files deserve parsing, and what language is each?

Called by: indexer.index_repository() — the only caller.
Calls:     nothing outside the stdlib; policy comes from config.py constants.

Design choice worth noting: this module is a GENERATOR. It yields one
SourceFile at a time (with its content), and the indexer processes it and
lets it go. Memory holds ~one file, never the repo. On a 1M-LOC repo that is
the difference between 30 MB resident and 400 MB resident.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import (
    EXCLUDED_DIRS,
    GENERATED_MARKERS,
    GENERATED_SNIFF_BYTES,
    LANGUAGE_BY_EXTENSION,
    MAX_FILE_BYTES,
    TEST_DIR_NAMES,
)


@dataclass
class SourceFile:
    """Everything downstream layers need to know about one file.

    Produced by: iter_source_files()
    Consumed by: indexer.index_repository(), which passes .content to the
                 parser and the whole record to Database.insert_file().
    """

    rel_path: str            # repo-relative, ALWAYS posix separators ("pkg/x.go")
    abs_path: Path           # where it actually lives on disk
    language: str            # key into parsers / extractors ("python", "go", ...)
    size_bytes: int
    line_count: int          # 0 when skipped (we don't count what we don't read)
    content_hash: str        # sha256 hex. Day 5's incremental reindex diffs on
                             # this: hash unchanged -> skip reparse entirely.
    is_test: bool            # Day 3's test->source mapping is built on this flag
    content: bytes | None    # raw bytes for the parser; None when skipped
    skipped_reason: str | None = None   # 'generated' | 'too_large' | 'binary'
                                        # | 'unreadable' | None (= will be parsed)


def iter_source_files(root: Path) -> Iterator[SourceFile]:
    """Walk `root`, yield every classifiable source file exactly once.

    Called by: indexer.index_repository().
    Yields:    SourceFile — including SKIPPED ones (content=None), because the
               DB records skips too; `wi stats` showing "312 generated files
               excluded" is itself useful information about a repo.

    Walk order is sorted so file IDs are deterministic across runs — makes
    diffs of two index runs meaningful and tests stable.
    """
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        # THE pruning step. Mutating dirnames in-place tells os.walk not to
        # descend — node_modules/ is never even stat()ed. Hidden dirs
        # (".github", ".idea") are pruned by the startswith check.
        dirnames[:] = sorted(
            d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")
        )

        for filename in sorted(filenames):
            language = LANGUAGE_BY_EXTENSION.get(Path(filename).suffix.lower())
            if language is None:
                continue  # not a language we index; silently ignored, not recorded

            abs_path = Path(dirpath) / filename
            rel_path = abs_path.relative_to(root).as_posix()
            sf = _classify(abs_path, rel_path, language)
            if sf is not None:
                yield sf


def _classify(abs_path: Path, rel_path: str, language: str) -> SourceFile | None:
    """Read one file and decide: parse it, record it as skipped, or drop it.

    Called by: iter_source_files() for every extension-matched file.
    Returns:   a SourceFile ready for the indexer, or None only when the file
               vanished mid-walk (deleted between listdir and open).
    """
    # --- size gate BEFORE reading: don't pull 50 MB into memory to reject it
    try:
        size = abs_path.stat().st_size
    except OSError:
        return None  # disappeared during the walk; nothing to record

    is_test = _looks_like_test(rel_path, language)

    if size > MAX_FILE_BYTES:
        return SourceFile(rel_path, abs_path, language, size, 0, "",
                          is_test, None, skipped_reason="too_large")

    # --- single read: hash, sniff, and (later) parse all use these bytes
    try:
        content = abs_path.read_bytes()
    except OSError:
        return SourceFile(rel_path, abs_path, language, size, 0, "",
                          is_test, None, skipped_reason="unreadable")

    # Content hash regardless of skip status — Day 5 needs it for everything.
    digest = hashlib.sha256(content).hexdigest()

    head = content[:GENERATED_SNIFF_BYTES]

    # NUL byte in the head = binary data wearing a source extension.
    if b"\x00" in head:
        return SourceFile(rel_path, abs_path, language, size, 0, digest,
                          is_test, None, skipped_reason="binary")

    # Generated-code markers (see config.py for why head-only + caveats).
    head_text = head.decode("utf-8", errors="replace")
    if any(marker in head_text for marker in GENERATED_MARKERS):
        return SourceFile(rel_path, abs_path, language, size, 0, digest,
                          is_test, None, skipped_reason="generated")

    line_count = content.count(b"\n") + (0 if content.endswith(b"\n") or not content else 1)
    return SourceFile(rel_path, abs_path, language, size, line_count, digest,
                      is_test, content)


def _looks_like_test(rel_path: str, language: str) -> bool:
    """Per-ecosystem test naming conventions. Flags, never excludes.

    Called by: _classify(). The flag lands in the DB as files.is_test.
    """
    parts = rel_path.lower().split("/")
    name = parts[-1]

    if any(p in TEST_DIR_NAMES for p in parts[:-1]):   # lives under tests/
        return True
    if language == "python":
        return name.startswith("test_") or name.endswith("_test.py")
    if language == "go":
        return name.endswith("_test.go")               # enforced by `go test` itself
    # typescript / tsx: foo.test.ts, foo.spec.tsx
    return ".test." in name or ".spec." in name

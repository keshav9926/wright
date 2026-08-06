"""L3 — turns raw call sites into edges between symbol IDs.

This is the layer where "we saw `trimMemory(x)` on line 170" becomes
"symbol #841 (Devices.Reset) CALLS symbol #838 (Devices.trimMemory)".

THE HONESTY RULE (from REPOSITORY-INTELLIGENCE.md §L3): resolution in
dynamic languages is probabilistic. Every edge carries a confidence and a
`resolution` tag saying HOW it was proven. When we cannot prove a target,
we store the edge with dst_symbol_id NULL rather than guessing — a wrong
edge poisons every downstream query; a missing one merely under-reports.

The resolution ladder, strongest proof first:

    receiver   0.95  self./this./Go-receiver call, type proven from the tree
    package    0.95  Go plain call — package == directory, so any symbol in
                     the same dir with that name is THE target (Go has no
                     overloading and no shadowing across files)
    same_file  0.90  plain call, a definition with that name in this file
    import     0.90  name (or module alias) traced through an import to a
                     specific repo file that defines it
    name_only  0.50  exactly ONE symbol in the whole repo has this name —
                     probably it, but nothing proves it
    unresolved 0.00  external library, dynamic dispatch, or ambiguous

Called by: indexer.index_repository() pass 2 — after ALL symbols exist,
           because cross-file resolution needs the full symbol table.
Calls:     db lookups once up front (everything is dict lookups after).
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from .db import Database
from .extract.base import CallSite, ImportRecord


class Resolver:
    """Holds the repo-wide lookup tables; resolves one file at a time.

    Lifecycle: indexer builds ONE Resolver after pass 1 commits symbols,
    then feeds it every parsed file's calls+imports in pass 2.
    """

    def __init__(self, db: Database, repo_root: Path):
        self.db = db
        self.repo_root = repo_root

        # ---- lookup tables, built once from the freshly-written DB ------
        # path -> file_id, and file_id -> (path, dir) for both directions.
        self.file_by_path: dict[str, int] = {}
        self.path_by_file: dict[int, str] = {}
        for row in db.conn.execute("SELECT id, path FROM files WHERE skipped_reason IS NULL"):
            self.file_by_path[row["path"]] = row["id"]
            self.path_by_file[row["id"]] = row["path"]

        # name -> [(symbol_id, file_id, kind)] : the name_only fallback and
        # the receiver-hint lookup both start here.
        self.by_name: dict[str, list[tuple[int, int, str]]] = {}
        # qualified_name -> [(symbol_id, file_id)] : receiver-hint fast path.
        self.by_qualified: dict[str, list[tuple[int, int]]] = {}
        # file_id -> [(start_byte, end_byte, symbol_id)] sorted, for finding
        # the ENCLOSING symbol of a call site by byte containment.
        self.spans_by_file: dict[int, list[tuple[int, int, int]]] = {}
        # dir -> {name -> [symbol_id]} : Go package-wide resolution.
        self.dir_names: dict[str, dict[str, list[int]]] = {}

        for row in db.conn.execute(
                "SELECT id, file_id, name, qualified_name, kind, start_byte, end_byte FROM symbols"):
            self.by_name.setdefault(row["name"], []).append(
                (row["id"], row["file_id"], row["kind"]))
            self.by_qualified.setdefault(row["qualified_name"], []).append(
                (row["id"], row["file_id"]))
            self.spans_by_file.setdefault(row["file_id"], []).append(
                (row["start_byte"], row["end_byte"], row["id"]))
            d = posixpath.dirname(self.path_by_file.get(row["file_id"], ""))
            self.dir_names.setdefault(d, {}).setdefault(row["name"], []).append(row["id"])

        for spans in self.spans_by_file.values():
            spans.sort()

        # Go module path from go.mod — the key that maps import strings like
        # "github.com/Project-HAMi/HAMi/pkg/util" onto repo dirs ("pkg/util").
        self.go_module = self._read_go_module(repo_root)

    # ------------------------------------------------------------------ #
    # per-file entry point                                                 #
    # ------------------------------------------------------------------ #

    def resolve_file(self, file_id: int, language: str,
                     imports: list[ImportRecord],
                     calls: list[CallSite]) -> tuple[list[tuple], list[tuple]]:
        """One file's raw records -> (edge_rows, import_rows) for bulk insert.

        Called by: indexer pass 2, once per parsed file.
        """
        path = self.path_by_file[file_id]
        file_dir = posixpath.dirname(path)

        # -- step 1: resolve imports to repo files (or None = external) ---
        # alias -> (record, resolved_file_id or None, go_dir or None)
        bindings: dict[str, tuple[ImportRecord, int | None, str | None]] = {}
        import_rows: list[tuple] = []
        for imp in imports:
            resolved_id: int | None = None
            go_dir: str | None = None
            if language == "python":
                resolved_id = self._resolve_python_module(imp.module, path)
            elif language == "go":
                go_dir = self._resolve_go_dir(imp.module)
            else:  # typescript / tsx
                resolved_id = self._resolve_ts_module(imp.module, path)
            bindings[imp.alias] = (imp, resolved_id, go_dir)
            import_rows.append((file_id, imp.module, imp.symbol, imp.alias,
                                imp.line, resolved_id))

        # -- step 2: resolve each call through the ladder ------------------
        edge_rows: list[tuple] = []
        for call in calls:
            src_id = self._enclosing_symbol(file_id, call.start_byte)
            if src_id is None:
                continue  # module-level call; no symbol to hang the edge on

            dst_id, conf, how = self._resolve_call(
                call, file_id, file_dir, language, bindings)
            edge_rows.append((src_id, dst_id, call.callee, call.line, conf, how))

        return edge_rows, import_rows

    # ------------------------------------------------------------------ #
    # the ladder                                                           #
    # ------------------------------------------------------------------ #

    def _resolve_call(self, call: CallSite, file_id: int, file_dir: str,
                      language: str, bindings: dict) -> tuple[int | None, float, str]:
        """Walk the ladder top-down; first rung that proves a target wins.

        Called by: resolve_file() for every call site.
        """
        # rung 1 — receiver type proven by the extractor (self/this/Go recv)
        if call.receiver_type_hint:
            qualified = f"{call.receiver_type_hint}.{call.callee}"
            hits = self.by_qualified.get(qualified, [])
            if len(hits) == 1:
                return hits[0][0], 0.95, "receiver"
            if len(hits) > 1:
                # same type name in several packages: prefer this file's dir
                same_dir = [h for h in hits
                            if posixpath.dirname(self.path_by_file[h[1]]) == file_dir]
                if len(same_dir) == 1:
                    return same_dir[0][0], 0.9, "receiver"
                return None, 0.0, "unresolved"

        if call.receiver is None:
            # rung 2 — Go: plain name resolves package-wide (dir == package)
            if language == "go":
                ids = self.dir_names.get(file_dir, {}).get(call.callee, [])
                if len(ids) == 1:
                    return ids[0], 0.95, "package"
                if len(ids) > 1:
                    return None, 0.0, "unresolved"
            else:
                # rung 3 — same file beats imports (later defs shadow them)
                hit = self._symbol_named_in_file(call.callee, file_id)
                if hit is not None:
                    return hit, 0.9, "same_file"

                # rung 4 — from-import / named-import bound to this name
                bound = bindings.get(call.callee)
                if bound is not None:
                    imp, resolved_id, _ = bound
                    if resolved_id is not None and imp.symbol not in (None, "*", "default"):
                        hit = self._symbol_named_in_file(imp.symbol, resolved_id)
                        if hit is not None:
                            return hit, 0.9, "import"
        else:
            # dotted call: receiver may be an imported module/package alias
            bound = bindings.get(call.receiver)
            if bound is not None:
                imp, resolved_id, go_dir = bound
                if go_dir is not None:  # Go: pkg.Foo -> symbol in that dir
                    ids = self.dir_names.get(go_dir, {}).get(call.callee, [])
                    if len(ids) == 1:
                        return ids[0], 0.9, "import"
                if resolved_id is not None:  # py module / ts namespace
                    hit = self._symbol_named_in_file(call.callee, resolved_id)
                    if hit is not None:
                        return hit, 0.9, "import"

        # rung 5 — last resort: the name is unique across the entire repo.
        # For dotted calls only methods qualify (obj.foo targets a method).
        candidates = self.by_name.get(call.callee, [])
        if call.receiver is not None:
            candidates = [c for c in candidates if c[2] == "method"]
        if len(candidates) == 1:
            return candidates[0][0], 0.5, "name_only"

        return None, 0.0, "unresolved"

    # ------------------------------------------------------------------ #
    # lookups & path probing                                               #
    # ------------------------------------------------------------------ #

    def _enclosing_symbol(self, file_id: int, byte: int) -> int | None:
        """Innermost symbol whose byte range contains the call site — that's
        the CALLER. Linear scan is fine: files hold tens of symbols.

        Called by: resolve_file() for every call.
        """
        best: int | None = None
        best_start = -1
        for start, end, sid in self.spans_by_file.get(file_id, []):
            if start <= byte < end and start > best_start:
                best, best_start = sid, start
        return best

    def _symbol_named_in_file(self, name: str, file_id: int) -> int | None:
        """First symbol with this bare name defined in this file, else None."""
        for sid, fid, _kind in self.by_name.get(name, []):
            if fid == file_id:
                return sid
        return None

    def _resolve_python_module(self, module: str, importer_path: str) -> int | None:
        """Dotted module -> repo file. Handles relative imports by dot count:
        '.'  = importer's package dir, '..' = one up, etc. Absolute modules
        are probed from repo root and src/ (the two common layouts).

        Called by: resolve_file() for every python import.
        """
        if module.startswith("."):
            dots = len(module) - len(module.lstrip("."))
            rest = module.lstrip(".")
            base = posixpath.dirname(importer_path)
            for _ in range(dots - 1):
                base = posixpath.dirname(base)
            rel = posixpath.join(base, rest.replace(".", "/")) if rest else base
            roots = [rel]
        else:
            rel = module.replace(".", "/")
            roots = [rel, f"src/{rel}"]

        for candidate in roots:
            candidate = posixpath.normpath(candidate)
            for probe in (f"{candidate}.py", f"{candidate}/__init__.py"):
                fid = self.file_by_path.get(probe)
                if fid is not None:
                    return fid
        return None

    def _resolve_ts_module(self, module: str, importer_path: str) -> int | None:
        """Relative TS specifier -> repo file. Probes the extension zoo,
        including the ESM quirk where source says './x.js' but the file on
        disk is 'x.ts'. Non-relative specifiers (packages, tsconfig path
        aliases) are external — tsconfig 'paths' support is a known gap.

        Called by: resolve_file() for every ts/tsx import.
        """
        if not module.startswith("."):
            return None
        base = posixpath.normpath(
            posixpath.join(posixpath.dirname(importer_path), module))
        # ESM: imports written with .js/.jsx compile from .ts/.tsx sources
        base = re.sub(r"\.jsx?$", "", base)
        probes = [base] if base.endswith((".ts", ".tsx")) else []
        probes += [f"{base}.ts", f"{base}.tsx", f"{base}.mts", f"{base}.cts",
                   f"{base}/index.ts", f"{base}/index.tsx"]
        for probe in probes:
            fid = self.file_by_path.get(probe)
            if fid is not None:
                return fid
        return None

    def _resolve_go_dir(self, import_path: str) -> str | None:
        """Go import path -> repo directory, via the go.mod module prefix.
        'github.com/X/Y/pkg/util' with module 'github.com/X/Y' -> 'pkg/util'.
        Anything outside the module prefix is an external dependency.

        Called by: resolve_file() for every go import.
        """
        if not self.go_module:
            return None
        if import_path == self.go_module:
            return ""
        prefix = self.go_module + "/"
        if import_path.startswith(prefix):
            candidate = import_path[len(prefix):]
            if candidate in self.dir_names:
                return candidate
        return None

    @staticmethod
    def _read_go_module(repo_root: Path) -> str | None:
        """Module path from the root go.mod, or None for non-Go repos.
        Submodule go.mod files (nested modules) are a deferred edge case."""
        gomod = repo_root / "go.mod"
        if not gomod.is_file():
            return None
        try:
            for line in gomod.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^\s*module\s+(\S+)", line)
                if m:
                    return m.group(1)
        except OSError:
            pass
        return None

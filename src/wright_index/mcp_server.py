"""Day 4 — the MCP server: wright-index as tools inside Claude Code.

This is the interface the whole project was built toward. A coding agent
already reads files and greps well; what it CANNOT do is answer graph and
history questions. Each tool here is one such question, backed by the
SQLite index, returning compact plain text.

Two design rules govern everything in this file:

  1. TOOL DESCRIPTIONS ARE PROMPTS. The model decides whether to call
     `blast_radius` or grep for twelve turns based ONLY on these strings.
     Vague description = tool never called = the index may as well not
     exist. Each description says when to use it and what it returns.

  2. EVERY RESPONSE IS TOKEN-BOUNDED. A tool that dumps 40k tokens into
     the agent's context is worse than no tool — it evicts the very
     reasoning it was meant to inform. Row caps + signature truncation
     bound every answer by construction (~100-600 tokens typical).

Plumbing notes:
  * stdout is the PROTOCOL CHANNEL in stdio transport — any stray print()
    corrupts framing. Diagnostics go to stderr, always.
  * Tools are thin wrappers over pure `_impl` functions taking an open
    Database — the impls are unit-testable without any MCP client.
  * A fresh Database per call: cheap (SQLite open ~µs), never stale, and
    WAL mode means a concurrent `wi index` rebuild doesn't block reads.

Called by: Claude Code (or any MCP client) over stdio; launched by
           `wi mcp <repo>` (cli.py) which sets REPO_PATH then run().
Calls:     db.Database query methods — nothing else.
"""

from __future__ import annotations

import sys
from pathlib import Path

# SDK 2.0 renamed FastMCP -> MCPServer (same decorator API; the 1.x path
# was mcp.server.fastmcp.FastMCP). pyproject pins mcp>=2.0 to match.
from mcp.server import MCPServer

from .db import Database, db_path_for

# Bound by cli.mcp() before run(); default = cwd so bare `wi mcp` works
# when launched from inside the target repo (how Claude Code project
# config typically does it).
REPO_PATH: Path = Path.cwd()

server = MCPServer(
    "wright-index",
    instructions=(
        "Code intelligence for this repository: symbol lookup, proven call "
        "graph, git co-change history. Prefer these tools over grep when the "
        "question is about STRUCTURE (who calls what, where is X defined) or "
        "HISTORY (what changes together, which tests cover this) — they "
        "answer from a pre-built index in milliseconds."
    ),
)

_MAX_SIG = 100          # truncate signatures: they inform, they don't teach


def _db() -> Database:
    """Open the repo's index, or fail with an actionable message.
    Called by: every tool."""
    path = db_path_for(REPO_PATH)
    if not path.exists():
        raise RuntimeError(
            f"no index for {REPO_PATH} — run: wi index {REPO_PATH}")
    return Database(path)


def _sig(text: str | None) -> str:
    text = text or ""
    return text[:_MAX_SIG] + "…" if len(text) > _MAX_SIG else text


# ------------------------------------------------------------------ #
# tools                                                                #
# ------------------------------------------------------------------ #

@server.tool()
def find_symbol(name: str, kind: str = "", limit: int = 20) -> str:
    """Locate definitions by name (substring match). Use this INSTEAD of
    grep when looking for where a function/method/class/struct/interface
    is defined — it returns only real definitions, never call sites or
    string matches. kind filters to: function, method, class, struct,
    interface, type, type_alias. Returns file:line-range + signature."""
    db = _db()
    try:
        rows = db.search_symbols(name=name, kind=kind or None, limit=limit)
        if not rows:
            return f"no definitions matching '{name}'"
        out = [f"{len(rows)} definition(s) of '{name}':"]
        for r in rows:
            out.append(f"  {r['kind']:10} {r['qualified_name']}  "
                       f"{r['file_path']}:{r['start_line']}-{r['end_line']}  "
                       f"{_sig(r['signature'])}")
        return "\n".join(out)
    finally:
        db.close()


@server.tool()
def callers(symbol: str, depth: int = 1) -> str:
    """Who calls this symbol — PROVEN callers only, from the call graph
    (not name matches). Use before changing a function to see what
    depends on it. depth=2 adds callers-of-callers. If empty, the symbol
    may be called via interface dispatch: try refs instead. symbol may be
    bare (trimMemory) or qualified (Devices.trimMemory)."""
    db = _db()
    try:
        targets = db.find_symbols_by_name(symbol)
        if not targets:
            return f"no symbol named '{symbol}'"
        out = []
        for t in targets[:5]:
            rows = db.callers_of(t["id"], depth=min(depth, 3))[:40]
            out.append(f"callers of {t['qualified_name']} "
                       f"({t['file_path']}:{t['start_line']}):")
            if not rows:
                out.append("  none proven — check refs() for unproven call sites")
            for r in rows:
                out.append(f"  [{r['depth']} hop] {r['qualified_name']}  {r['file_path']}")
        return "\n".join(out)
    finally:
        db.close()


@server.tool()
def refs(name: str, limit: int = 40) -> str:
    """Every call site whose callee NAME matches — including unproven ones
    (interface dispatch, dynamic calls). The honest superset of callers().
    Use when callers() returns nothing or when hunting all usages of a
    method implemented by many types."""
    db = _db()
    try:
        rows = db.refs_by_name(name, limit=limit)
        if not rows:
            return f"no call sites reference '{name}'"
        out = [f"{len(rows)} call site(s) of '{name}':"]
        for r in rows:
            out.append(f"  {r['caller']}  {r['caller_file']}:{r['line']}  "
                       f"[{r['resolution']} {r['confidence']:.2f}]")
        return "\n".join(out)
    finally:
        db.close()


@server.tool()
def blast_radius(symbol: str) -> str:
    """Everything a change to this symbol might affect, from three evidence
    layers: (1) proven transitive callers, (2) files that historically
    change together with its file (mined from git commits — catches
    coupling with NO import relationship, like sibling implementations
    and docs), (3) tests that exercise it. Call this FIRST when planning
    a modification."""
    db = _db()
    try:
        targets = db.find_symbols_by_name(symbol)
        if not targets:
            return f"no symbol named '{symbol}'"
        t = targets[0]
        out = [f"blast radius of {t['qualified_name']} "
               f"({t['file_path']}:{t['start_line']})"]

        rows = db.callers_of(t["id"], depth=2)[:25]
        out.append("static callers (2 hops):" if rows else "static callers: none proven")
        out += [f"  [{r['depth']}] {r['qualified_name']}  {r['file_path']}" for r in rows]

        partners = db.cochange_for(t["file_path"], limit=10)
        out.append("co-change partners (git history, lift = x beyond chance):"
                   if partners else "co-change partners: none above threshold")
        out += [f"  {r['partner']}  together {r['both_count']}x, lift {r['lift']:.1f}"
                for r in partners]

        tests = db.tests_for_symbol(t["id"])[:15]
        out.append("tests to run:" if tests else "tests: none found via call edges")
        out += [f"  {r['qualified_name']}  {r['file_path']}" for r in tests]
        return "\n".join(out)
    finally:
        db.close()


@server.tool()
def cochange(file: str, limit: int = 15) -> str:
    """Files that historically change WITH this file, ranked by lift
    (co-change frequency beyond chance), mined from git history. Catches
    coupling invisible to imports/calls: parallel implementations, docs,
    configs, schemas. Use when editing a file to learn what else usually
    needs the same change. file = path suffix, e.g. 'ascend/device.go'."""
    db = _db()
    try:
        rows = db.cochange_for(file, limit=limit)
        if not rows:
            return f"no co-change partners above lift threshold for '{file}'"
        out = [f"files that change with '{file}':"]
        for r in rows:
            out.append(f"  {r['partner']}  together {r['both_count']}x, "
                       f"P={r['confidence']:.0%}, lift {r['lift']:.1f}")
        return "\n".join(out)
    finally:
        db.close()


@server.tool()
def covering_tests(symbol: str) -> str:
    """Tests that actually exercise this symbol, found via call edges from
    test files (evidence, not filename guessing). Use to know what to run
    after changing the symbol, or to find usage examples of an API."""
    db = _db()
    try:
        targets = db.find_symbols_by_name(symbol)
        if not targets:
            return f"no symbol named '{symbol}'"
        out = []
        for t in targets[:5]:
            rows = db.tests_for_symbol(t["id"])[:20]
            out.append(f"tests exercising {t['qualified_name']}:")
            out += ([f"  {r['qualified_name']}  {r['file_path']}  ({r['depth']} hop)"
                     for r in rows] or ["  none found via call edges"])
        return "\n".join(out)
    finally:
        db.close()


@server.tool()
def hot_files(limit: int = 10) -> str:
    """Most-churned files with their main authors, from git history. Use to
    find where a repo's activity concentrates and who its de-facto owners
    are — e.g. before choosing what to work on or whom to ask."""
    db = _db()
    try:
        rows = db.hot_files(limit=limit)
        if not rows:
            return "no history mined (not a git repo?)"
        out = ["hottest files (commit count | last touched | top authors):"]
        for r in rows:
            out.append(f"  {r['path']}  {r['change_count']} commits | "
                       f"{(r['last_changed'] or '')[:10]} | {r['top_authors']}")
        return "\n".join(out)
    finally:
        db.close()


@server.tool()
def repo_map() -> str:
    """One-screen orientation for an unfamiliar repository: size by
    language, symbol counts, the directories where definitions concentrate,
    and the most-churned files. Call this FIRST in a repo you don't know —
    it replaces several minutes of exploratory listing and reading."""
    db = _db()
    try:
        s = db.stats()
        out = [f"repo: {db.get_meta('repo_root')}  "
               f"(indexed {(db.get_meta('indexed_at') or '')[:10]}, "
               f"commit {(db.get_meta('commit_sha') or '')[:8]})"]
        for lang, row in sorted(s["files_by_language"].items()):
            out.append(f"  {lang}: {row['files']} files, {row['lines']:,} lines")
        kinds = ", ".join(f"{k}={n}" for k, n in sorted(
            s["symbols_by_kind"].items(), key=lambda kv: -kv[1]))
        out.append(f"  symbols: {s['total_symbols']:,} ({kinds})")

        # top-2-levels dir bucket: 'pkg/device/ascend/x.go' -> 'pkg/device'
        top_dirs = db.conn.execute(
            """SELECT CASE WHEN instr(path, '/') = 0 THEN '(root)'
                           ELSE substr(path, 1, instr(path, '/') - 1) ||
                                CASE WHEN instr(substr(path, instr(path,'/')+1), '/') > 0
                                     THEN '/' || substr(substr(path, instr(path,'/')+1), 1,
                                          instr(substr(path, instr(path,'/')+1), '/') - 1)
                                     ELSE '' END
                      END AS dir, COUNT(s.id) AS n
               FROM files f JOIN symbols s ON s.file_id = f.id
               GROUP BY dir ORDER BY n DESC LIMIT 8"""
        ).fetchall()
        out.append("  definition hotspots (dir: symbols):")
        out += [f"    {r['dir']}: {r['n']}" for r in top_dirs]

        hot = db.hot_files(limit=5)
        if hot:
            out.append("  most-churned files:")
            out += [f"    {r['path']} ({r['change_count']} commits)" for r in hot]
        return "\n".join(out)
    finally:
        db.close()


def run(repo: Path) -> None:
    """Bind the repo and serve stdio until the client hangs up.
    Called by: cli.mcp()."""
    global REPO_PATH
    REPO_PATH = repo.resolve()
    # stderr, NEVER stdout — stdout carries the JSON-RPC framing.
    print(f"wright-index mcp server: repo={REPO_PATH}", file=sys.stderr)
    server.run()   # stdio transport is the FastMCP default

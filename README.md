# wright-index

> *wright (n.) — one who builds or makes.*

A **code intelligence engine for coding agents**. It indexes a repository once —
symbols, call graph, git history — and answers questions about it that no amount of
grepping can answer, then serves those answers to Claude Code (or any agent) over MCP.

It is **not** an agent. No LLM in the hot path, no autonomy. It is the knowledge layer
that makes an existing agent measurably better on a codebase it has never seen:

| Question | Why grep can't answer it |
|---|---|
| *Who calls this function?* | Grep finds name collisions, not resolved callers. |
| *What breaks if I change it?* | Needs a call graph with transitive traversal. |
| *What else changes when this file changes?* | The answer isn't in the code at all — it's in commit history. `schema.proto` and `docs/api.md` share no import, no symbol, no string; they co-change in 90% of commits. |
| *Which tests actually cover this symbol?* | Needs a test→source mapping, not filename guessing. |

---

## Status — 5-day build, day by day

| Day | Deliverable | Status |
|---|---|---|
| **1** | **Symbol indexer** — walk, parse (tree-sitter), extract, store (SQLite). `wi index` / `wi symbols` / `wi stats` | ✅ **done** |
| 2 | **Call graph** — import + call-site resolution, `CALLS`/`CALLED_BY` edges, `wi callers`, `wi refs` | next |
| 3 | **Git history mining** — co-change coupling via association rules, `wi cochange`, `wi blast-radius`, `wi tests-for` | |
| 4 | **MCP server** — `find_symbol`, `callers`, `blast_radius`, `cochange`, `covering_tests`, `repo_map` as tools Claude Code calls | |
| 5 | **Incremental reindex + benchmark** — content-hash invalidation; measured tokens-to-answer with vs. without the index | |

Full plan with per-day scope, LOC, and demo criteria: [INDEX-ROADMAP.md](INDEX-ROADMAP.md).

**Day 1 verified against a real repo** — [HAMi](https://github.com/Project-HAMi/HAMi)
(CNCF, 163 Go files, 64k lines):

- **1,705 symbols indexed in ~10s** — functions, methods, structs, interfaces, types,
  with signatures, doc comments, and visibility
- `wi symbols --name Fit --kind method` returns **all 16 vendor implementations of the
  scheduler's `Fit()`** across `pkg/device/*` — the "who implements this interface"
  question, answered from SQL in milliseconds
- Generated code (`api_mock.go`) auto-excluded; a file with syntax the grammar couldn't
  fully parse still yielded its intact symbols (tree-sitter is error-tolerant by design)
- 30/30 tests passing

---

## Quickstart

```bash
pip install -e ".[dev]"          # Python ≥3.10

wi index  path/to/repo           # build the index (~10s per 60k lines)
wi symbols path/to/repo --file device.go             # everything in one file
wi symbols path/to/repo --name Fit --kind method     # filtered lookup
wi symbols path/to/repo --exported --no-tests        # public API surface only
wi stats  path/to/repo           # what's in the index
```

The index lives in `~/.wright-index/<repo>-<hash>.db` — indexing someone else's
checkout never dirties their `git status`.

---

## How it works

```
 wi index <repo>
    │
    ▼
 L0 walker.py ── classify by extension, prune node_modules/vendor,
    │            skip generated/oversized/binary, hash content, flag tests
    ▼
 L1 parsers.py ─ tree-sitter grammars (Python, Go, TS, TSX), cached;
    │            error-tolerant: broken files still yield partial trees
    ▼
 L2 extract/ ── S-expression queries → Symbol records: qualified name,
    │            kind, signature, docstring, byte range, visibility
    ▼
    db.py ────── SQLite, one transaction per run, WAL; recursive-CTE-ready
                 for Day 2's graph queries
```

| Module | Job |
|---|---|
| [src/wright_index/walker.py](src/wright_index/walker.py) | L0 — which files deserve parsing |
| [src/wright_index/parsers.py](src/wright_index/parsers.py) | L1 — grammar registry + binding-compat shims |
| [src/wright_index/extract/](src/wright_index/extract/) | L2 — per-language symbol extraction |
| [src/wright_index/db.py](src/wright_index/db.py) | storage + the queries behind the CLI |
| [src/wright_index/indexer.py](src/wright_index/indexer.py) | the orchestrator — read this file first |
| [src/wright_index/cli.py](src/wright_index/cli.py) | `wi` command |

Every function is commented with what it does, who calls it, and what it calls.

---

## Design lineage

wright-index is the foundation layer of **WRIGHT**, a fully-designed autonomous
software-engineering agent (issue → reviewed PR). The complete system design lives in
[docs/design/](docs/design/):

| Document | Covers |
|---|---|
| [ARCHITECTURE.md](docs/design/ARCHITECTURE.md) | Five agent architectures compared; event-sourced four-role design |
| [PIPELINE.md](docs/design/PIPELINE.md) | Issue → PR in 27 stages with failure modes and latency budgets |
| [MULTI-AGENT-DESIGN.md](docs/design/MULTI-AGENT-DESIGN.md) | Why 22 candidate agents collapse to 5 |
| [REPOSITORY-INTELLIGENCE.md](docs/design/REPOSITORY-INTELLIGENCE.md) | The nine knowledge layers — **wright-index implements L0–L3 + L6** |
| [ROADMAP.md](docs/design/ROADMAP.md) | The full 15-milestone agent build, deferred in favor of shipping this layer first |

The agent duplicates what existing tools already do well; the intelligence layer is
what they're missing. So the layer ships first, standalone, and the agent remains a
design the layer was extracted from.

---

## Stack

Python 3.12 · tree-sitter (<0.26 — 0.26.0 has a native use-after-free, see
[pyproject.toml](pyproject.toml)) · SQLite · Typer/Rich · MCP (Day 4) · pygit2 (Day 3)

## Author

**Keshav Kakani** — IIT Jodhpur, B.Tech Bioengineering + Artificial Intelligence

## License

Apache 2.0

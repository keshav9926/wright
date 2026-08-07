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
| **2** | **Call graph** — import + call-site resolution with per-edge confidence, `wi callers` / `wi calls` / `wi refs` | ✅ **done** |
| **3** | **Git history mining** — co-change via association rules (support/confidence/lift), `wi cochange` / `wi blast-radius` / `wi tests-for` / `wi hot` | ✅ **done** |
| **4** | **MCP server** — 8 token-bounded tools (`find_symbol`, `callers`, `refs`, `blast_radius`, `cochange`, `covering_tests`, `hot_files`, `repo_map`) served over stdio; verified connected from Claude Code | ✅ **done** |
| **5** | **Incremental reindex + benchmark** — content-hash invalidation with dependent re-resolution (0.64s single-file on HAMi); measured tokens-to-answer, [results](bench/results.md) | ✅ **done** |

Full plan with per-day scope, LOC, and demo criteria: [INDEX-ROADMAP.md](INDEX-ROADMAP.md).

**See it read a repository it has never met**:
[docs/wright-index-readout.html](docs/wright-index-readout.html) — a self-contained page
showing the three passes, the proof ladder, and four real queries, built entirely from
indexing [DeepSourceCorp/globstar](https://github.com/DeepSourceCorp/globstar) cold.
The scope bug it surfaced is now [globstar#235](https://github.com/DeepSourceCorp/globstar/pull/235).

**Verified against a real repo** — [HAMi](https://github.com/Project-HAMi/HAMi)
(CNCF, ~170 Go files, 69k lines):

- **1,823 symbols + 11,562 call edges indexed in ~15s** — symbols carry signatures,
  doc comments, visibility; edges carry per-site confidence and HOW they were proven
  (`receiver` / `package` / `import` / `same_file` / `name_only`)
- `wi callers trimMemory` returns the **actual three callers** of
  `Devices.trimMemory`, receiver-proven at 0.95 — grep returns name-collision soup
- `wi symbols --name Fit --kind method` returns **all 16 vendor implementations of the
  scheduler's `Fit()`** across `pkg/device/*`
- Two-hop transitive callers via recursive CTE: **0.5ms**
- Unprovable targets (interface dispatch, external packages) are stored as
  unresolved rather than guessed — a wrong edge poisons every downstream query
- **2,128 co-change pairs mined from 1,400 commits** — `wi cochange ascend/device.go`
  surfaces the other vendor backends (`mthreads`, `hygon`, `iluvatar`, `cambricon`) at
  **17–25× lift with zero imports between them**: parallel implementations that move
  in lockstep, invisible to any static analysis
- **Measured, not claimed** ([bench/results.md](bench/results.md)): same six questions
  through headless Claude Code with and without the index — **33% fewer turns
  overall, up to 2.3× fewer tokens** on call-graph questions, honest parity where
  grep was already the right tool (two cells show the index losing, reasons stated)
- **Incremental reindex**: touch one file → 0.64s (unchanged files hash-skipped;
  dependents re-resolved, including `name_only` edges whose uniqueness assumption
  the change broke)
- Generated code auto-excluded; files with parse errors still yield intact symbols
- 57/57 tests passing
- The layers already earned their keep — twice. A manual audit of this repo's device
  layer found a nil-map panic, now [fixed upstream](https://github.com/Project-HAMi/HAMi/pull/2416).
  And the co-change table *retroactively predicts that exact find*: the same bug was
  fixed in `mthreads` three days earlier, and `mthreads/device.go` is `ascend/device.go`'s
  #2 co-change partner at 24.8×. The tool now automates the discovery that took the
  audit an hour.

---

## Quickstart

```bash
pip install -e ".[dev]"          # Python ≥3.10

wi index  path/to/repo           # build the index (~15s per 70k lines)
wi symbols path/to/repo --file device.go             # everything in one file
wi symbols path/to/repo --name Fit --kind method     # filtered lookup
wi symbols path/to/repo --exported --no-tests        # public API surface only
wi callers path/to/repo trimMemory --depth 2         # who calls this (transitive)
wi calls  path/to/repo Devices.MutateAdmission       # what this calls, with proof
wi refs   path/to/repo Fit                           # every call site, incl. unproven
wi cochange path/to/repo device.go                   # what changes WITH this file
wi blast-radius path/to/repo Devices.trimMemory      # callers + coupled files + tests
wi tests-for path/to/repo compute                    # tests exercising a symbol
wi hot    path/to/repo           # churn leaders + their main authors
wi stats  path/to/repo           # what's in the index
```

The index lives in `~/.wright-index/<repo>-<hash>.db` — indexing someone else's
checkout never dirties their `git status`.

### Use it from Claude Code (MCP)

```bash
wi index /path/to/repo                                  # build the index once
claude mcp add wright-index -- wi mcp /path/to/repo     # register the server
```

New sessions then get eight tools — `repo_map`, `find_symbol`, `callers`, `refs`,
`blast_radius`, `cochange`, `covering_tests`, `hot_files`. Ask *"what breaks if I
change trimMemory?"* and the agent calls `blast_radius` once instead of grepping
for twelve turns. Every response is token-bounded by construction — a tool that
dumps 40k tokens evicts the reasoning it was meant to inform. Tool descriptions
are written as prompts: they tell the model *when* to prefer the index over grep.

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

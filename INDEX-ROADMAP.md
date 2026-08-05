# wright-index — 5-Day Build Plan

A code intelligence server. It indexes a repository once and answers questions about it
that no amount of grepping can answer — then serves those answers to Claude Code over MCP.

**It is not an agent.** There is no LLM in the hot path, no sandbox, no autonomy. It is a
knowledge layer that makes an existing agent measurably better on codebases it has never
seen.

Scope: layers **L0–L3 and L6** from [REPOSITORY-INTELLIGENCE.md](docs/design/REPOSITORY-INTELLIGENCE.md).
Languages: Python, Go, TypeScript. Storage: SQLite. ~4,000 LOC.

---

## Why this and not WRIGHT

|  | WRIGHT ([ROADMAP.md](docs/design/ROADMAP.md)) | wright-index |
|---|---|---|
| What it is | Autonomous agent: issue → merged PR | Knowledge layer serving an agent |
| Relationship to Claude Code | Replaces it | Extends it |
| Size | ~30,000 LOC, 15 milestones | ~4,000 LOC, 5 days |
| LLM in hot path | Yes, four agent roles | No |
| Needs sandbox, event sourcing, orchestrator | Yes | No |
| Fails if | Claude Code stays better (it will) | Nothing — the data is useful regardless |
| Ships by | Not in 5 days. Not in 25. | Day 5 |

WRIGHT's M3+M4 *are* this, built as a dependency of an agent that duplicates Claude Code.
Pulling them out and shipping them standalone removes the duplication and keeps the part
that's additive. If WRIGHT ever gets built, this is already its foundation.

**The claim that makes it non-obvious:** static analysis cannot tell you that `schema.proto`
and `docs/api.md` change together in 90% of commits. That fact only exists in git history.
Nothing in Claude Code mines it.

---

## The five days

Each day ends with something you can run and show. LOC includes tests.

### Day 1 · L0–L2 — Parse and extract symbols
**Build.** File walk with language classification and vendored/generated exclusion.
tree-sitter parsers for Python, Go, TypeScript. Symbol extraction — functions, methods,
classes, structs, interfaces — with byte ranges, docstrings, and signatures. SQLite schema
and bulk insert. `wi index <repo>`.

**Demo.** Index a 100k-LOC repo. `wi symbols --file x.py` lists everything in it with line
ranges. Under 60 seconds cold.

**LOC** ~900 · **Hard part** tree-sitter query syntax differs per grammar; the node types
you want are not named consistently across languages.

### Day 2 · L3 — Build the graph
**Build.** Import resolution per language. Call-site extraction and resolution to symbol
IDs, with a confidence score where resolution is ambiguous. `CALLS` / `CALLED_BY`,
`IMPORTS`, `CONTAINS`, `EXTENDS` edges. Recursive CTEs for transitive queries.
`wi callers <symbol>`, `wi refs <symbol>`.

**Demo.** Pick a function in a real repo. `wi callers` returns its actual callers — not
name collisions, which is what grep gives you. Two-hop expansion in under 100ms.

**LOC** ~800 · **Hard part** dynamic dispatch and duck typing make resolution
probabilistic. Store confidence; do not pretend it's exact.

### Day 3 · L6 — Mine git history ★
**Build.** Commit graph traversal with pygit2, bounded to the last ~5k commits.
Co-change coupling via association rules — support, confidence, lift — so `README` doesn't
correlate with everything. File ownership and recency-weighted churn. Test→source mapping
by naming convention and import edges. `wi cochange <file>`, `wi blast-radius <symbol>`,
`wi tests-for <symbol>`.

**Demo.** The one that matters. Run `wi cochange` on a file in a repo you've never opened
and surface a coupled file that shares no import, no symbol, and no string with it. Then
try to find it with grep. You can't.

**LOC** ~700 · **Hard part** naive co-occurrence is useless — every file "co-changes" with
the changelog. Lift over a support threshold is what makes the output non-obvious.

### Day 4 · MCP server
**Build.** stdio JSON-RPC server. Tool schemas for `find_symbol`, `callers`,
`blast_radius`, `cochange`, `covering_tests`, `repo_map`. Token-bounded responses — every
tool caps its own output, because a tool that returns 40k tokens is worse than no tool.
Install instructions for Claude Code.

**Demo.** Register the server. Ask Claude Code "what breaks if I change this function?" and
watch it call `blast_radius` instead of grepping for twelve turns.

**LOC** ~600 · **Hard part** schema design. The tool descriptions are prompts — vague ones
mean the model never calls them.

### Day 5 · Incremental reindex, benchmark, ship
**Build.** Content-hash-addressed invalidation: `git diff` since last index → reparse only
changed files → patch affected edges. Benchmark harness measuring tokens-to-answer on a
fixed question set, with the server and without. README, install, recorded demo.

**Demo.** Commit a change, reindex in under 2 seconds. Then the README's headline number:
*"answering the same 20 questions costs N tokens with the index and M without."*

**LOC** ~700 · **Hard part** edge invalidation. Deleting a file must remove edges pointing
*into* it, not just out of it.

---

## Aug 5 → January: what it actually buys you

The tool is not the point; it removes the specific barrier that stops you contributing to
large repos, which is that you don't know where anything is. Index a repo on day one and
you're navigating it like a six-month contributor.

| Window | Move |
|---|---|
| **Aug 5–10** | Build wright-index. Ships Day 5. |
| **Aug 10–18** | LFX Term 3 closes Aug 18 — *this is live now*. Index the target repos, land 2–3 real PRs, apply. |
| **Aug–Sep** | Algora bounties. Pick two repos, index both, go deep. First payment realistic in this window, not in 10 days. |
| **Sep–Nov** | Concentrate. 2–3 orgs, not 10. Merged PRs in one org compound; scattered PRs don't. |
| **Nov–Dec** | Outreachy December cohort and OSPP timelines land here — verify exact dates when closer. |
| **Jan** | GSoC 2027 orgs are announced ~Feb, applications ~March. By January you want to already be a known name in two of them. |

**Where you stand in January, if this goes normally:** 20–40 merged PRs concentrated in 2–3
organizations, a shipped tool with real users, some bounty income, and a live LFX or
Outreachy result. That combination is what makes a GSoC 2027 proposal strong — and it's
also what makes you hireable without GSoC, which was the actual goal.

**The failure mode to avoid:** spreading across ten orgs. Maintainers recommend people they
recognize.

---

## What you learn that First Impression didn't teach

First Impression covered agent loops, tool use, and LLM orchestration. None of the
following overlaps with that.

**1. Program analysis.** tree-sitter, error-tolerant incremental parsing, S-expression
queries, scope and reference resolution. This is compiler-adjacent work. It is the single
biggest gap between "builds with LLMs" and "builds developer tools," and almost nobody
applying for AI roles has touched it.

**2. Mining Software Repositories.** ★ Co-change coupling via association rules — support,
confidence, lift — over a commit graph. This is a real academic subfield with its own
conference, and essentially nobody has it on a resume. It is also the part of this tool
that is genuinely impossible to replicate with prompting, which makes it the strongest
thing you can point at.

**3. MCP server authorship.** Not consuming MCP — *providing* it. JSON-RPC over stdio,
capability negotiation, tool schema design, token-bounded responses. The provider side is
where the design problems are, and it's the side far fewer people have built.

**4. Graph modeling in a relational store.** Recursive CTEs, transitive closure, adjacency
lists with confidence weighting, index strategy for two-hop queries. Real database
engineering, not ORM usage.

**5. Incremental computation.** Content-hash invalidation and dependency-aware recompute —
the same problem Bazel and Turborepo solve. Transferable far beyond this project.

**6. Retrieval evaluation.** Recall@k and tokens-to-answer as measured numbers. Making an
AI system's efficiency an engineering property rather than a vibe.

The two worth optimizing for: **#2**, because it's rare and demonstrable, and **#3**,
because it's the current interface every AI company is building against.

---

## What this plan deliberately drops

L4 operational (build/test introspection), L5 documentary, L7 social (maintainer graph, PR
history), L8 semantic (embeddings). All are in
[REPOSITORY-INTELLIGENCE.md](docs/design/REPOSITORY-INTELLIGENCE.md) and all are additive later. L8 in
particular is deferred on purpose — symbol and graph lookup answers most questions, and
embeddings are the expensive layer that gets reached for first and helps least.

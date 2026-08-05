# Repository Intelligence Engine

**Understanding codebases at 1M+ LOC**
Version 0.1 · Keshav Kakani
Companion to [ARCHITECTURE.md](ARCHITECTURE.md), [PIPELINE.md](PIPELINE.md), [MULTI-AGENT-DESIGN.md](MULTI-AGENT-DESIGN.md).

---

## 1. Thesis: The File Is the Wrong Primitive

The default approach — chunk every file, embed it, store vectors, retrieve top-k — fails
on real codebases for reasons that are structural, not tunable:

- **A file is not a unit of meaning.** A 2,000-line file contains forty unrelated things.
  Embedding it produces a vector that means nothing in particular and matches everything
  weakly.
- **Code similarity is not semantic similarity.** Two functions with near-identical
  embeddings — `validateUserInput` and `validateConfigInput` — may live in unrelated
  subsystems. Meanwhile the function that *actually matters* to your change may be
  textually dissimilar and three call-hops away.
- **The critical relationships are edges, not content.** *Who calls this? What breaks if
  I change it? Which test covers it? What else changes when this changes?* No embedding
  answers these. They are graph queries.
- **At 1M LOC the top-k is meaningless.** With ~60,000 embeddable units, the top 20 by
  cosine similarity is a rounding error away from arbitrary.

**The correct primitive is the symbol, and the correct structure is a typed graph over
symbols, enriched with history and documentation.** Embeddings are one retrieval strategy
among six, and not the first one you should reach for.

**Design goal:** given an issue against a 1M-LOC repository, assemble **under 30,000
tokens** that are sufficient to make the correct change — and be able to *prove*
sufficiency before spending a frontier-model call on it.

---

## 2. Nine Layers of Repository Knowledge

Each layer answers questions the layers below it cannot.

```
L8  SEMANTIC      embeddings                   "where is rate limiting handled?"
L7  SOCIAL        issues, PRs, reviews, owners  "who decides this? what was rejected?"
L6  HISTORICAL    blame, churn, co-change       "what changes together? what's fragile?"
L5  DOCUMENTARY   README, docs, ADRs, comments  "what was this meant to do?"
L4  OPERATIONAL   CI, Docker, tests, entrypoints "how do I build and verify?"
L3  STRUCTURAL    call graph, imports, types    "what breaks if I change this?"
L2  SYMBOLIC      defs, refs, signatures        "where is X defined? who calls it?"
L1  SYNTACTIC     AST                           "what is the shape of this code?"
L0  FILESYSTEM    hierarchy, classification     "what exists?"
```

---

### L0 · Filesystem & Classification

**Extract:** path tree, file sizes, languages, and a classification per file:
`source | test | config | docs | generated | vendored | fixture | binary`.

**Why it matters more than it looks.** In a 1M-LOC repository, 60–90% of files are
typically vendored, generated, or minified. Indexing them wastes the majority of your
compute and pollutes every retrieval result. `node_modules`, `vendor/`, `*.pb.go`,
`dist/`, `*.min.js`, `third_party/`, snapshot fixtures.

Detection: path heuristics + generated-file header markers + `.gitattributes
linguist-generated` + a size/entropy check for minified content.

**This single filter is the highest-ROI operation in the entire engine.**

---

### L1 · Syntactic — AST

**Tool:** tree-sitter. Error-tolerant (parses broken code), fast (~10 MB/s), language-agnostic
via grammars, and gives byte-accurate spans.

**Extract per file:** declaration tree, spans, nesting, comment association, import
statements.

**Why tree-sitter over an LSP as the *base* layer:** it works without a build environment.
On a 1M-LOC polyglot monorepo you will not get every language server running, and a
pipeline that requires one is a pipeline that fails on arrival. LSP is an *enrichment*,
not a foundation.

---

### L2 · Symbolic — the Core Layer

**Extract:** every definition (function, method, class, interface, type, constant, macro)
with:

```
symbol_id · kind · qualified_name · file · byte_span · line_span
signature · docstring · visibility · language
receiver_type (methods) · generics · decorators/annotations
```

Plus every **reference** with its resolution confidence.

**Resolution is two-tier and honest about uncertainty:**

```
EXACT     LSP-resolved, or unambiguous single-candidate name match   confidence 1.0
HEURISTIC name match with type/scope filtering                       confidence 0.6–0.9
LEXICAL   name appears, no resolution possible                       confidence 0.3
```

Storing confidence rather than discarding low-confidence edges matters: at 1M LOC you
will never achieve full semantic resolution, and a graph that silently drops what it
can't prove is a graph that lies about connectivity. Downstream consumers treat blast
radius as a **lower bound** and can widen when confidence is low.

---

### L3 · Structural — the Graph That Does the Work

Four distinct edge types, deliberately not collapsed into one:

| Edge | Direction | Answers |
|---|---|---|
| `CALLS` | caller → callee | What does this depend on at runtime? |
| `CALLED_BY` | callee → caller | **Blast radius.** What breaks if I change this? |
| `IMPORTS` | module → module | Coarse coupling, cycle detection |
| `IMPLEMENTS` / `EXTENDS` | type → interface/base | **Sibling discovery** |
| `DEFINES` / `CONTAINS` | file → symbol, class → method | Hierarchy |
| `TESTS` | test → symbol under test | **Verification binding** |

**`IMPLEMENTS` is the most underused edge in every system I've seen.** When the issue is
*"the mthreads device panics on a nil map,"* the highest-value context is not the
documentation — it is **the other thirteen types implementing the same interface, twelve
of which handle nil correctly.** Convention is learned from siblings. This edge is how
you find them, and it is nearly free to compute.

**`TESTS` edges** are derived from three signals combined: import graph (test file imports
the module), naming convention (`TestFoo` → `Foo`, `foo_test.py` → `foo.py`), and — most
reliably — **coverage data** when the repo produces it. Coverage-derived test edges are
ground truth and worth the one-time cost of a instrumented run.

---

### L4 · Operational

**Extract, deterministically:**
```
build/test/lint/format commands   Makefile, package.json scripts, tox.ini, CI workflows
test framework + layout           manifest deps + test paths
container setup                   Dockerfile, compose, devcontainer
entry points                      main(), cmd/, bin/, exported package surface
CI topology                       which jobs run, which are required, which are slow
service boundaries                for monorepos
```

**The verified test command is the single most valuable fact in the entire index.**
Everything in verification depends on it. It is extracted *and then empirically executed*
against an unmodified tree. An unverified test command is a guess, and a guess here
silently poisons every downstream correctness signal.

---

### L5 · Documentary

README, `docs/`, ADRs, module-level doc comments, CONTRIBUTING.

**Treated as evidence, not truth.** Documentation drifts; code doesn't lie. Where docs
and code disagree, code wins and the discrepancy is recorded. Docs are most valuable for
*intent* ("this cache is intentionally unbounded because X") and *vocabulary* (mapping the
project's domain terms to symbols), which are exactly the things code cannot express.

ADRs, where present, are disproportionately valuable — they record *rejected* alternatives,
which is precisely what stops an agent from confidently proposing something the team
already decided against.

---

### L6 · Historical — the Underrated Layer

Mined from git, no LLM required:

| Signal | Computation | Use |
|---|---|---|
| **Co-change coupling** | P(B changes \| A changes) over commit history | **Implicit dependencies invisible to static analysis** |
| **Churn** | commits touching file / time | Fragility; high-churn code is high-risk to change |
| **Recency-weighted authorship** | blame, time-decayed | Who to ask; whether code is maintained or abandoned |
| **Bug-fix density** | commits matching fix patterns per file | Historically defect-prone regions |
| **Age** | last meaningful modification | Dead code detection |
| **Revert frequency** | reverts touching the file | Danger zones |

**Co-change coupling is the highest-value signal in this layer and almost nobody uses it.**
Static analysis sees no edge between `api/schema.proto` and `docs/api.md`, or between a
config struct and its validation test, or between a vendor device implementation and the
registry that lists it. Git history shows they change together 90% of the time. When an
agent modifies A and the co-change graph says B changes with A 90% of the time, **B belongs
in context and probably in the diff.** This catches an entire class of incomplete-change
bugs that no AST-based system can see.

Computation: sliding window over commit history, pairwise co-occurrence with support and
confidence thresholds, excluding mega-commits (>50 files) which are formatting sweeps and
generate spurious coupling.

---

### L7 · Social

From the GitHub API: issue history, PR history, review comments, `CODEOWNERS`, merge
patterns, label taxonomy.

**Most valuable extractions:**
- **Prior art** — "has an issue like this been fixed before?" A merged PR solving a
  similar issue is a better template than any generated plan.
- **Rejection patterns** — what gets PRs closed in *this* project. Mined from closed-
  unmerged PRs and "changes requested" reviews. This is how the system learns that a repo
  hates large diffs, or requires a test for every fix, or rejects new dependencies.
- **Review conventions** — who reviews what, what they consistently ask for.
- **Ownership** — CODEOWNERS plus empirical review history.

This layer is what makes generated PRs *mergeable* rather than merely *correct*.

---

### L8 · Semantic — Last, Not First

Embeddings, in `pgvector`. Deliberately the **final** layer and an **optional** one.

**Chunking follows syntax, never fixed token windows.** One chunk = one function, method,
or class, from its tree-sitter span. Fixed-size chunking splits functions in half and is
the most common cause of bad code RAG.

**Enrichment before embedding** — bare bodies embed poorly:
```
<repo> › <module> › <file> › <class> › <function>
<docstring>
<signature>
<body>
```
The qualified name path carries most of the retrievable signal.

**At 1M LOC, do not embed everything.** Embed the *hot* subset:
```
hotness = 0.4·normalize(churn_90d)
        + 0.3·normalize(pagerank(call_graph))
        + 0.2·normalize(recency)
        + 0.1·normalize(test_coverage)
```
Embed the top ~20%. The cold 80% remains fully reachable via symbol, lexical, and graph
retrieval — which are better for it anyway, since cold code is usually found by exact
name.

---

## 3. Representation: How This Is Stored

> **Decision: a typed property graph in PostgreSQL. Not Neo4j. Not a dedicated vector DB.**

### Why Postgres over a graph database

Do the arithmetic for a 1M-LOC repository:

```
symbols            ~50,000–80,000
CALLS edges        ~250,000–500,000
IMPORTS edges      ~30,000
IMPLEMENTS edges   ~5,000
TESTS edges        ~40,000
co-change edges    ~100,000 (thresholded)
────────────────────────────────────────
total edges        ~500,000–700,000
```

Half a million edges is **small**. It fits comfortably in Postgres with proper indexing,
and the queries this system actually runs are **1–3 hops**, not deep variable-length
traversals:

```
"who calls X"                      1 hop
"what does X call"                 1 hop
"blast radius of X"                2–3 hops, capped
"which tests cover X"              1 hop
"siblings implementing I"          1 hop
```

Recursive CTEs handle bounded-depth traversal at these sizes in single-digit milliseconds.
Neo4j's advantage — variable-length pathfinding over deep, dense graphs — is a capability
this workload does not use.

**What Neo4j would cost:** an additional stateful service to operate, back up, and scale;
a second source of truth requiring sync with Postgres; licensing considerations for the
enterprise features; and a query language the team must learn. For a workload that never
exceeds 3 hops.

**When to revisit:** >10M edges, or if deep multi-hop path queries become central (they
won't for this use case), or if a genuine ontology/reasoning requirement emerges.

### Knowledge graph?

**Yes in structure, no in formalism.** This is a typed property graph with a fixed,
domain-specific schema — not an RDF/OWL knowledge graph with inference. Formal KG
machinery (triple stores, SPARQL, reasoners) buys open-world inference that code analysis
doesn't need: the schema is closed and known.

### Schema

```sql
-- nodes
symbols(id, repo_id, commit_sha, kind, qualified_name, file_path,
        byte_start, byte_end, line_start, line_end,
        signature, docstring, visibility, language, content_hash)

files(id, repo_id, commit_sha, path, language, classification,
      loc, content_hash, churn_90d, last_modified, bugfix_count)

modules(id, repo_id, path, name, summary, centrality)

-- edges (single table, typed — simpler to index and query than N tables)
edges(src_id, dst_id, type, confidence, metadata jsonb)
  type ∈ CALLS | IMPORTS | IMPLEMENTS | EXTENDS | CONTAINS | TESTS | CO_CHANGES

-- layer 6/7
cochange(file_a, file_b, support, confidence, last_computed)
authorship(file_id, author, weighted_share, last_touch)
pr_history(repo_id, pr_number, state, files[], review_comments, merged)
conventions(repo_id, kind, statement, evidence_refs[], confidence, source)

-- layer 8 (optional)
embeddings(symbol_id, content_hash, vector vector(N), model)

-- the compressed map
repo_map(repo_id, commit_sha, markdown, token_count)
```

**Key indexes:** `edges(dst_id, type)` for reverse traversal (blast radius — the hot
path), `edges(src_id, type)`, `symbols(repo_id, qualified_name)` for anchor resolution,
GIN trigram on `qualified_name` for fuzzy name lookup, HNSW on `embeddings.vector`.

Everything is keyed by `commit_sha` and **immutable**, which makes the entire index
cacheable and shareable across every session on that commit.

---

## 4. The Retrieval Stack

Six retrievers, fused. Ordered by cost and precision — cheapest and most precise first.

| # | Retriever | Answers | Latency | Cost |
|---|---|---|---|---|
| 1 | **Anchor resolution** | Symbols/paths named in the issue | <5 ms | free |
| 2 | **Symbol index** | Exact `find_definition`, `find_references` | <10 ms | free |
| 3 | **Lexical (ripgrep)** | Exact strings, error messages, regex | <50 ms | free |
| 4 | **BM25** | Multi-term ranked over identifiers + docs + comments | <80 ms | free |
| 5 | **Graph expansion** | Callers, callees, tests, siblings, co-change | <100 ms | free |
| 6 | **Semantic (pgvector)** | NL intent with no known identifier | ~300 ms | $ |

### On BM25 specifically

**Yes, but for a narrower job than usual.** For *code*, exact match beats BM25 — you know
the identifier or you don't. BM25 earns its place over a different corpus: **docstrings,
comments, documentation, issue text, and commit messages**, where multi-term relevance
ranking genuinely helps and where exact match is too brittle.

Practical detail that matters: the tokenizer must split identifiers.
`MutateAdmission` → `mutate`, `admission`, `MutateAdmission`. Without camelCase and
snake_case splitting, BM25 over code is near-useless.

### On the reranker

**Yes — conditionally.** A cross-encoder reranker is applied only when the fused candidate
set exceeds ~50 items. Below that, reranking noise exceeds reranking benefit and you've
paid latency for nothing.

The reranker's real job at 1M LOC is not ordering the top 10 — it is **discarding the
long tail confidently** so the context budget goes to genuinely relevant material.

### Fusion

**Reciprocal Rank Fusion**, weighted by retriever precision:

```
score(d) = Σ_r  w_r / (k + rank_r(d))          k = 60

w_anchor = 3.0    w_symbol = 2.5   w_graph = 2.0
w_lexical = 1.5   w_bm25 = 1.0     w_semantic = 0.8
```

Rank-based, so scores from incomparable retrievers never need normalization — which is
the recurring bug in score-based hybrid systems.

---

## 5. Minimum Context Selection — the Core Algorithm

This is what the whole engine exists to do: **assemble the smallest sufficient context,
and know that it's sufficient.**

```
INPUT   issue, repo index @ commit_sha, token budget B (default 30k)
OUTPUT  packed context + sufficiency verdict

────────────────────────────────────────────────────────────────────
1. ANCHOR EXTRACTION                                         (no LLM)
     from issue text, deterministically:
       backticked identifiers, file paths, error strings,
       stack frame locations, linked PR/commit refs, code blocks
     ▸ These are gold. A stack trace names the change site exactly.

2. ANCHOR RESOLUTION                                         (<10 ms)
     exact match on symbols.qualified_name
     unresolved → trigram fuzzy → lexical → semantic (last resort)
     ▸ If ≥1 anchor resolves, retrieval is nearly solved.
       Issues with no anchors are the hard case (~30%).

3. SEED SET
     resolved anchors + top lexical/BM25 hits
     cap: 10 seeds
     ▸ More seeds is not better. Precision at this step
       determines everything downstream.

4. TYPED GRAPH EXPANSION — per-edge budgets, not uniform
     for each seed:
       definition                          always,  full body
       CALLED_BY   depth 1, max 15         signature only ← blast radius
       CALLS       depth 1, max 10         signature only
       IMPLEMENTS  siblings, max 5         FULL BODY      ← convention source
       TESTS       max 5                   full body      ← verification
       CO_CHANGES  confidence>0.7, max 5   file path only ← implicit deps
       CONTAINS    enclosing type/module   signature only
     ▸ Asymmetric budgets are the point. Siblings get full bodies
       because they teach the pattern; callers get signatures
       because you only need to know they exist.

5. CONVENTION BINDING                                        (cached)
     repo conventions (L5/L7) relevant to touched paths
     rejection patterns from closed PRs on similar files

6. RANK & PRUNE
     RRF fuse → cross-encoder rerank if |candidates| > 50
     drop below relevance floor

7. PROGRESSIVE PACKING (see §6)
     fill budget B by priority, at the lowest sufficient
     disclosure tier for each item

8. SUFFICIENCY VERIFICATION                                  (cheap LLM)
     FAST_CHEAP call: "Given only this context, can you name the exact
     function(s) that must change, and do you have their full bodies?"
       YES         → proceed
       NO + reason → targeted expansion, goto 4, max 2 iterations
       NO twice    → escalate: insufficient context, flag for human
────────────────────────────────────────────────────────────────────
```

**Step 8 is the step everyone skips and it is the one that matters most.** Without it,
insufficient context is discovered by the *frontier* model, mid-implementation, after
you've paid for it — and the usual symptom is a confident, wrong patch. A $0.002
`FAST_CHEAP` sufficiency check before a $0.40 frontier call is the best cost/quality
trade in the pipeline, and it converts "the agent didn't know about X" from a mystery
into a logged, diagnosable event.

---

## 6. Progressive Disclosure — the 1M-LOC Unlock

Never load a full body when a signature will do. Four tiers:

| Tier | Content | ~Tokens | When |
|---|---|---|---|
| **T0** | `qualified_name` + path | 10 | Existence proof — "this symbol exists" |
| **T1** | + signature + docstring | 60 | Neighborhood — callers, callees |
| **T2** | + full body | 300 | Change sites, siblings, covering tests |
| **T3** | + its own T1 neighborhood | 1,500 | Only for the primary change site |

### The arithmetic that makes this work

A change site with 15 callers, 10 callees, 5 siblings, 5 tests:

```
NAIVE — everything at T2:      35 symbols × 300 =  10,500 tokens
TIERED:
  change site        T3                        =   1,500
  siblings      5  × T2                        =   1,500
  tests         5  × T2                        =   1,500
  callers      15  × T1                        =     900
  callees      10  × T1                        =     600
  co-change     5  × T0                        =      50
  ────────────────────────────────────────────────────────
                                                   6,050 tokens
```

**42% reduction with more useful content** — because the naive version spends its budget
on caller bodies nobody reads, and the tiered version spends it on sibling
implementations that teach the correct pattern.

**Escalation is agent-driven.** A `expand_symbol(id, tier)` tool lets the agent pull a
body on demand. This inverts the usual design: instead of guessing what's needed and
over-supplying, supply the map and let the agent request territory. Every escalation is
logged, and escalation patterns feed back into tuning the default budgets.

---

## 7. Scaling to 1M+ LOC

### The compressed repo map

At 1M LOC, no agent can browse the tree. It needs a **map that fits in context** — 2–4k
tokens describing the architecture, generated once per commit and cached:

```
<repo> — 1.2M LOC, Go 78% / TS 15% / Python 7%

ARCHITECTURE
  Scheduler extender + device abstraction with per-vendor plugins.
  Pod admission → mutating webhook → scheduler filter/score → device plugin.

MODULES (by centrality)
  pkg/scheduler/    (82k LOC)  filter, score, bind        ← core
    └ depends on: pkg/device, pkg/util
  pkg/device/       (140k LOC) Devices interface + 14 vendor impls
    └ vendors: nvidia, amd, ascend, cambricon, ...
  pkg/util/         (31k LOC)  k8s client, nodelock, leaderelection
  ...

BUILD  make build · TEST  make test (verified ✓) · LINT  golangci-lint v2.8.0
CONVENTIONS  Apache headers required · import aliases enforced ·
             AI disclosure required in PRs · tests mandatory for bug fixes
```

This is what lets the Planner reason about a repository it cannot read. Generated by an
LLM over deterministic inputs (module centrality, dep graph clustering, L4 facts), cached
per `commit_sha`, and cheap to regenerate.

### Tiered indexing

Do not index everything to full depth. Index by value:

```
                    L0-L2   L3    L6    L8
  hot (top 20%)      ✓      ✓     ✓     ✓
  warm (next 30%)    ✓      ✓     ✓     ✗
  cold (rest)        ✓      ✓     ✗     ✗
  generated/vendored ✗      ✗     ✗     ✗     ← excluded at L0
```

Hotness = churn × call-graph centrality × recency × coverage. Cold code remains fully
reachable by exact symbol and lexical search — which is how it's actually found.

### Cost & latency at scale

| Operation | 100k LOC | 1M LOC | Notes |
|---|---|---|---|
| L0 classification | 2 s | 15 s | Parallel; excludes 60–90% of files |
| L1–L2 parse | 25 s | 3–5 min | Parallel across cores; the dominant cold cost |
| L3 graph build | 5 s | 45 s | Bulk insert + index |
| L6 history mining | 10 s | 2 min | Bounded to last ~5k commits |
| L8 embed (hot 20%) | 1 min | 8 min | Batch API, off critical path |
| **Cold total** | **~2 min** | **~15 min** | Once per repo, ever |
| **Incremental (50 files)** | **<3 s** | **<5 s** | Per commit — the number that matters |
| **Retrieval query** | 80 ms | 250 ms | p95, cache-cold |

**Cold indexing happens once per repository, not once per session.** The number that
governs day-to-day experience is the incremental one, and it stays flat as the repo grows
because it scales with *changed files*, not repo size.

### Incremental maintenance

```
push webhook → diff base..head → changed file set
  → reparse only changed files (content_hash miss)
  → recompute edges touching changed symbols
  → invalidate: repo_map if module structure changed
                repo_facts if CI/manifest/CONTRIBUTING changed
                cochange incrementally (append commit)
                embeddings only for changed hot symbols
  → new commit_sha index published; old retained per LRU
```

Content-hash addressing at the symbol level means an unchanged function inside a changed
file is never reparsed or re-embedded.

### Sharding

Partition by module for very large monorepos. Most queries are module-local, so shard
pruning eliminates most of the search space. A Bloom filter per shard answers "could this
symbol exist here?" in microseconds before any index is touched.

---

## 8. What the Agent Actually Calls

The engine exposes a small, typed tool surface. Note that most of these are **free and
sub-100ms** — the agent should call them liberally rather than reasoning from memory.

```
repo_map()                              → compressed architecture map    ~3k tok
find_symbol(name, kind?)                → definitions, T1                <10 ms
find_references(symbol_id)              → callers, T1                    <10 ms
expand_symbol(symbol_id, tier)          → escalate disclosure tier       <10 ms
blast_radius(symbol_id, depth=2)        → transitively affected, T0      <50 ms
siblings(symbol_id)                     → same-interface impls, T2       <20 ms
covering_tests(symbol_id)               → tests, T2                      <20 ms
cochange(file_path, min_conf=0.7)       → implicitly coupled files       <20 ms
grep(pattern, glob?)                    → exact matches                  <50 ms
search(natural_language_query)          → hybrid fused, reranked         ~300 ms
prior_art(issue_text)                   → similar merged PRs             ~200 ms
conventions(path)                       → applicable repo conventions    <10 ms
```

`prior_art` and `cochange` are the two most differentiated calls here — neither exists in
mainstream code-RAG systems, and both routinely change the shape of a correct fix.

---

## 9. Failure Modes

| Failure | Detection | Handling |
|---|---|---|
| Unsupported language | No tree-sitter grammar | Degrade to L0 + lexical + BM25. Still functional. |
| LSP unavailable | Startup timeout | Heuristic resolution, edges marked `confidence < 1.0` |
| Dynamic dispatch / reflection | Statically invisible | Record `graph_incompleteness`; blast radius treated as lower bound; co-change (L6) partially compensates |
| Generated code indexed | Post-hoc marker detection | Exclude, reindex; agent instructed never to edit generated files |
| Stale index vs. HEAD | `commit_sha` mismatch | Incremental reindex before retrieval; never serve a stale index silently |
| Anchor resolution fails | Zero exact matches | Escalate through fuzzy → lexical → semantic → agentic search |
| Sufficiency check fails twice | §5 step 8 | Escalate to human with what was retrieved and what was judged missing |
| Monorepo, wrong workspace | Path-scope mismatch | Scope by CODEOWNERS + co-change clustering |
| Co-change noise from mega-commits | >50 files in commit | Excluded from co-change computation |

---

## 10. Summary of Decisions

| Question | Answer |
|---|---|
| Primary primitive | **Symbol**, not file |
| Graph database? | **No** — typed property graph in Postgres; ~500k edges, 1–3 hop queries. Revisit >10M edges |
| Knowledge graph? | **Structure yes, RDF/OWL formalism no** — closed schema, no inference needed |
| Vector DB? | **No dedicated one.** `pgvector`, optional tier, hot 20% only |
| BM25? | **Yes** — over docstrings/comments/docs/issues, not as primary code search. Identifier-splitting tokenizer is mandatory |
| Reranker? | **Yes, conditionally** — only when candidates > 50; its job is confidently discarding the tail |
| Hybrid retrieval? | **Six retrievers, RRF-fused**, cheapest-and-most-precise first |
| Context builder | **Anchor → seed → typed expansion → progressive packing → sufficiency verification** |
| Most underrated signal | **Co-change coupling (L6)** — catches implicit dependencies no AST can see |
| Most underused edge | **`IMPLEMENTS`** — siblings teach the convention |
| The 1M-LOC unlock | **Progressive disclosure** + compressed repo map + tiered indexing |
| Best cost/quality trade | **Sufficiency verification** — a cheap check before an expensive call |

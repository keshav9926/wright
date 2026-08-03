# ROADMAP — Building WRIGHT

**Implementation roadmap, milestone by milestone**
Version 0.1 · Keshav Kakani
Synthesizes [ARCHITECTURE.md](ARCHITECTURE.md) · [PIPELINE.md](PIPELINE.md) · [MULTI-AGENT-DESIGN.md](MULTI-AGENT-DESIGN.md) · [REPOSITORY-INTELLIGENCE.md](REPOSITORY-INTELLIGENCE.md)

---

## 0. Decisions That Precede Milestone 1

### Language: Python 3.12, not Go

ARCHITECTURE.md used Go for concreteness. **Reversing that.** For this builder and this
project, Python wins:

| Factor | Verdict |
|---|---|
| tree-sitter, MCP SDK, provider SDKs, semgrep | Python-first ecosystem across the board |
| **Pydantic** | The typed inter-agent contracts in [MULTI-AGENT-DESIGN.md](MULTI-AGENT-DESIGN.md) are *exactly* Pydantic models. Schema validation at every handoff boundary is free. |
| FastAPI | Already your production stack; SSE and WebSockets you've shipped |
| Solo velocity | The single most important variable. Go's single-binary deploy doesn't repay the slowdown |
| Go's real advantage | Concurrency and deployment — neither is the bottleneck. The bottleneck is LLM latency |

Docker handles deployment. `asyncio` handles the concurrency that exists. **Choose the
language you're fastest in; the architecture is language-agnostic and the docs say so.**

### The demo rule

**Every milestone ends with something you can run in front of a person.** Not a passing
test suite — a demo. If a milestone can't be demoed, it's a refactor and should be folded
into a milestone that can be.

### Two time estimates per milestone

- **Team** — what a funded 3-engineer startup would take.
- **Solo** — you, part-time, alongside coursework and an internship. Roughly 3–4× team,
  and that multiplier is honest rather than pessimistic.

### Difficulty scale

`1–3` mechanical · `4–6` requires design judgment · `7–8` genuinely hard · `9–10` research-adjacent

---

## Phase Overview

```
PHASE 0 — FOUNDATION          "Does this work at all?"           M0–M2    ~6 wks solo
PHASE 1 — INTELLIGENCE        "Can it understand code?"          M3–M4    ~7 wks
PHASE 2 — AUTONOMY            "Can it work unattended?"          M5–M7   ~11 wks
PHASE 3 — PRODUCT             "Can someone else use it?"         M8–M10  ~10 wks
PHASE 4 — SCALE               "Can it run in production?"        M11–M14 ~12 wks
                                                                 ─────────────────
                                                                 ~46 wks solo
                                                                 ~14 wks team
```

**The critical milestone is M2.** Everything before it is scaffolding; everything after
assumes the core loop works. Reach M2 fast, even sloppily — it either validates the whole
design or invalidates it, and finding out early is worth more than any amount of
upfront polish.

---

# PHASE 0 — FOUNDATION

## M0 · Skeleton & Model Layer

**Objective.** A running project with multi-provider LLM access, capability-tier routing,
cost accounting, and CI. Nothing agentic yet — just the substrate everything sits on.

**Files**
```
pyproject.toml · .pre-commit-config.yaml · .github/workflows/ci.yml
wright/platform/config.py          settings, env, provider keys
wright/platform/logging.py         structlog JSON, secret redaction
wright/models/base.py              Provider protocol
wright/models/providers/{anthropic,openai,google}.py
wright/models/router.py            capability tier → concrete model
wright/models/accounting.py        token + cost ledger
wright/models/fallback.py          circuit breaker, cross-provider failover
wright/cli.py                      typer entrypoint
tests/unit/test_router.py · test_accounting.py · test_fallback.py
```

**LOC** ~1,200 · **Difficulty** 3 · **Dependencies** none
**Time** Team 3 d · **Solo 1 week**

**Demo.** `wright ask --tier FAST_CHEAP "summarize this"` → response plus a cost ledger
line. Kill the primary provider's key → watch it fail over and log the failover event.

**Acceptance criteria**
- Three providers behind one interface; swapping is a config change only
- Every call records `(model, in_tok, out_tok, cached_tok, latency, cost_usd)`
- Circuit breaker opens after N consecutive failures, closes after cooldown
- Streaming works on all three
- CI green: lint, types (`mypy --strict` on `models/`), unit tests

**Common mistakes**
- **Building a universal abstraction that flattens providers to their common
  denominator.** You lose prompt caching, extended thinking, and native tool-use
  semantics — the exact features that matter. Expose native capabilities; let the router
  know which tiers each provider can serve.
- Hardcoding model IDs in agent code. Agents request a *tier*, never a model.
- Forgetting cached-token accounting. It's a separate price and it's the number that
  proves your caching works.
- Retrying non-retryable errors. Classify first: a 401 retried five times is five wasted
  calls and a confusing log.

**Commit.** Everything. Public repo from day one. README states the goal and links the
four design docs — **the design docs are the portfolio artifact at this stage**, and they
are stronger than most people's finished projects.

---

## M1 · Sandbox & Tool Execution

**Objective.** Execute untrusted commands safely in Docker, behind a validating broker.

**Files**
```
wright/sandbox/manager.py          container lifecycle, warm pool
wright/sandbox/exec.py             streaming exec, timeouts, truncation
wright/sandbox/limits.py           cgroups, seccomp, network policy
wright/sandbox/images/             base Dockerfiles: python, node, go, generic
wright/tools/registry.py           tool registration
wright/tools/schema.py             Pydantic models per tool
wright/tools/broker.py             validate → authorize → gate → dispatch → record
wright/tools/builtin/{read,write,edit,grep,glob,bash,git}.py
tests/integration/test_sandbox.py · test_broker.py
```

**LOC** ~1,800 · **Difficulty** 6 · **Dependencies** M0
**Time** Team 1 wk · **Solo 2 weeks**

**Demo.** `wright sandbox --repo <url>` opens a shell-ish session; run `grep`, `read`,
`bash`. Then run `curl https://example.com` and watch it fail — **network-deny proven live**.
Run a fork bomb and watch the pid cap hold.

**Acceptance criteria**
- Container: non-root, `cap_drop ALL`, read-only root, tmpfs `/workspace`, pids capped
- Network denied by default; allowlisted egress proxy works for package registries
- Per-command and per-session timeouts enforced
- Output >N KB truncated head+tail with an explicit marker, full artifact to disk
- `edit_file` uses **exact-match replacement** and fails loudly on no-match
- Broker rejects malformed args before any execution
- Chaos test: kill the container mid-command → clean error, no hang

**Common mistakes**
- **Mounting the host filesystem "just for development."** It always ships. Never mount
  the host.
- Line-number-based edits. They drift the moment anything changes and corrupt unrelated
  regions. Exact-match only — a failed match is a *good* signal.
- Silent output truncation. The agent then reasons confidently over a partial log.
  Always mark the truncation.
- Running the container as root because a package install failed. Fix the image instead.
- Forgetting the pids limit — one fork bomb from generated code takes down the host.

**Commit.** All of it, including the Dockerfiles. Add a `SECURITY.md` describing the
sandbox threat model; it signals seriousness to anyone evaluating the repo.

---

## M2 · Walking Skeleton — Issue → Diff ★

**Objective.** **The milestone that validates or kills the design.** One agent, real
tools, real repo, real issue, producing a real diff. No event log, no review, no PR, no
database. Deliberately crude.

**Files**
```
wright/vcs/git.py                  clone (strategy by size), diff, branch
wright/vcs/github_read.py          fetch issue, comments, labels
wright/agents/simple.py            single ReAct loop
wright/agents/prompts/simple.md
wright/session/simple_runner.py    in-memory session
tests/e2e/test_walking_skeleton.py
```

**LOC** ~900 · **Difficulty** 5 · **Dependencies** M0, M1
**Time** Team 4 d · **Solo 1.5 weeks**

**Demo.** `wright solve https://github.com/org/repo 1234` → agent explores, edits, prints
a unified diff to stdout. **Run it on a real good-first-issue and show the diff.**

**Acceptance criteria**
- Clones, reads the issue, explores with tools, produces a syntactically valid diff
- Works on ≥3 different repos in ≥2 languages
- Solves ≥1 real issue such that a human agrees the diff is correct
- Full trajectory printed: every prompt, tool call, and result
- Cost per attempt reported

**Common mistakes**
- **Building M3–M6 before proving M2.** The single most likely way this project dies is
  three months of infrastructure before one real diff. Ship crude, then improve.
- Over-scoping the demo issue. Pick a genuine one-function bug with a clear repro.
- Assuming failure means the architecture is wrong. Distinguish *prompt* problems (fix
  cheaply) from *architecture* problems (rare at this stage).
- Not printing the full trajectory. You cannot debug what you cannot see, and this is
  the first place you'll need it.

**Commit.** Everything, plus an asciinema recording in the README. **A public repo with a
working end-to-end demo at this point already outperforms most portfolio projects.** Tag
`v0.1.0-skeleton`.

---

# PHASE 1 — INTELLIGENCE

## M3 · Repository Intelligence L0–L3

**Objective.** Postgres-backed symbol graph: classification, AST, symbols, call graph,
import graph. [Repository Intelligence](REPOSITORY-INTELLIGENCE.md) layers 0–3.

**Files**
```
wright/db/models.py · migrations/           SQLAlchemy + alembic
wright/index/classify.py                    L0 source/test/generated/vendored
wright/index/parse.py                       tree-sitter, parallel pool
wright/index/symbols.py                     L2 definitions + references
wright/index/graph.py                       L3 CALLS/IMPORTS/IMPLEMENTS/TESTS
wright/index/incremental.py                 changed-file reindex
wright/index/pipeline.py                    orchestration, commit_sha keying
wright/tools/builtin/{find_symbol,find_references,blast_radius,siblings}.py
tests/unit/test_classify.py · tests/integration/test_index_pipeline.py
```

**LOC** ~2,800 · **Difficulty** 7 · **Dependencies** M0
**Time** Team 2 wks · **Solo 4 weeks**

**Demo.** `wright index <repo>` on a 100k+ LOC project, then
`wright query find-references MutateAdmission` and `wright query blast-radius <symbol>` —
answers in milliseconds. Show incremental reindex after a commit taking <3 s.

**Acceptance criteria**
- ≥4 languages (Python, JS/TS, Go, C/C++) via tree-sitter grammars
- L0 excludes generated/vendored — **report the % excluded; on a real repo it's 60–90%**
- Symbol resolution stores `confidence`; low-confidence edges retained, not dropped
- Blast radius (depth 2) on any symbol < 50 ms
- Incremental reindex of 50 changed files < 5 s
- Everything keyed by `commit_sha` and immutable
- Cold index of 1M LOC completes < 20 min

**Common mistakes**
- **Indexing `node_modules`/`vendor/`.** You'll spend 10× the compute and every retrieval
  result will be polluted. L0 classification is the highest-ROI code in the engine.
- Requiring an LSP. It won't start on most repos. tree-sitter is the base; LSP is
  enrichment.
- Dropping unresolvable edges. Store them with low confidence — a graph that silently
  omits what it can't prove lies about connectivity.
- Fixed-size chunking anywhere. Syntax spans only.
- Reaching for Neo4j. ~500k edges and 1–3 hop queries; recursive CTEs handle it. Re-read
  [Repository Intelligence §3](REPOSITORY-INTELLIGENCE.md).
- Forgetting `edges(dst_id, type)` — reverse traversal is the hot path for blast radius.

**Commit.** All. Add a benchmark script reporting index time and symbol counts per repo;
**publishing real numbers on real repos is exactly the credibility signal maintainers and
reviewers respond to.**

---

## M4 · Retrieval & Context Assembly

**Objective.** The minimum-context algorithm from [Repository Intelligence §5](REPOSITORY-INTELLIGENCE.md), including
progressive disclosure and the sufficiency check.

**Files**
```
wright/retrieval/anchors.py         extract + resolve issue anchors
wright/retrieval/lexical.py         ripgrep wrapper
wright/retrieval/bm25.py            identifier-splitting tokenizer + BM25
wright/retrieval/graph_expand.py    typed expansion, per-edge budgets
wright/retrieval/fusion.py          weighted RRF
wright/retrieval/rerank.py          cross-encoder, conditional (>50 candidates)
wright/retrieval/disclosure.py      T0–T3 tiers
wright/retrieval/assembler.py       priority packing to budget
wright/retrieval/sufficiency.py     cheap-model verification loop
wright/index/repo_map.py            compressed architecture map
tests/unit/test_fusion.py · tests/integration/test_assembler.py
```

**LOC** ~2,200 · **Difficulty** 7 · **Dependencies** M3
**Time** Team 1.5 wks · **Solo 3 weeks**

**Demo.** `wright context <repo> <issue>` → prints selected context, token count, per-item
disclosure tier, and the sufficiency verdict. Show the same issue with naive
whole-file loading (≈40k tokens) vs. tiered (≈8k) with *better* content.

**Acceptance criteria**
- Six retrievers fused by weighted RRF
- Progressive disclosure measurably reduces tokens vs. naive — **publish the ratio**
- Sufficiency check runs on `FAST_CHEAP`, triggers ≤2 targeted expansions, then escalates
- Repo map ≤4k tokens for a 1M-LOC repo
- Retrieval p95 < 300 ms
- Every truncation emits an event naming what was dropped
- BM25 tokenizer splits `MutateAdmission` → `mutate`, `admission`, `MutateAdmission`

**Common mistakes**
- **Reaching for embeddings first.** Anchors → symbols → lexical → graph answers most
  queries better, faster, free. Semantic is retriever #6.
- Uniform expansion budgets. Siblings need *full bodies* (they teach the pattern);
  callers need *signatures* (you only need to know they exist). Asymmetry is the point.
- Skipping the sufficiency check. Then insufficient context is discovered by the frontier
  model mid-implementation, and the symptom is a confident wrong patch.
- Normalizing scores across retrievers. Use rank-based RRF; that's why it exists.
- Reranking small candidate sets — noise exceeds benefit under ~50.

**Commit.** All. Add `docs/retrieval-benchmarks.md` with token-reduction numbers.

---

# PHASE 2 — AUTONOMY

## M5 · Event Sourcing & Orchestrator

**Objective.** Replace the in-memory session with an event log, a pure reducer, a state
machine, checkpointing, and resume.

**Files**
```
wright/events/types.py             ~25 Pydantic event types, versioned
wright/events/log.py               append-only writer, causation IDs
wright/events/outbox.py            transactional outbox dispatcher
wright/orchestrator/reducer.py     PURE: events → state
wright/orchestrator/policy.py      state → next action
wright/orchestrator/budget.py      token/cost/time/tool-call caps
wright/orchestrator/checkpoint.py  FS snapshot + event seq
wright/session/runner.py           lease-based worker loop
tests/unit/test_reducer.py         ← highest-value tests in the codebase
tests/chaos/test_resume.py
```

**LOC** ~2,000 · **Difficulty** 8 · **Dependencies** M0, M1, M2
**Time** Team 2 wks · **Solo 4 weeks**

**Demo.** Start a session. **`kill -9` the worker mid-run. Restart. It resumes from the
checkpoint and finishes.** Then replay the event log to render the full trajectory.

**Acceptance criteria**
- Reducer is a pure function — no I/O, exhaustively unit-tested
- Session state is *never* stored directly; always `reduce(events)`
- Snapshots every 50 events, as a cache only — deleting them changes nothing but speed
- Event + projection + outbox commit in **one transaction**
- Chaos test: kill worker at 10 random points; all resume correctly
- Budget breach → `SUSPENDED`, not `FAILED`
- Replay to event N and fork works

**Common mistakes**
- **Putting I/O in the reducer.** It must stay pure or you lose replay, testability, and
  every recovery guarantee.
- Treating snapshots as truth. They're a cache; the log is truth.
- Event schemas without version fields. You will change them, and old sessions must still
  replay.
- Making suspension a failure. Long-running and human-gated tasks depend on the distinction.
- Writing events and projections in separate transactions — the classic dual-write bug.

**Commit.** All. This milestone is where the repo starts looking like production
infrastructure rather than a script.

---

## M6 · The Five Agents

**Objective.** Researcher, Planner, Coder, Reviewer, Debugger with Pydantic contracts and
per-role model routing. [Multi-Agent Design](MULTI-AGENT-DESIGN.md) realized.

**Files**
```
wright/agents/contract.py          ResearchBrief, Plan, ReviewVerdict, Diagnosis
wright/agents/runtime.py           shared role loop
wright/agents/{researcher,planner,coder,reviewer,debugger}/
    prompt.md · parser.py · config.py
wright/agents/validation.py        plan symbols must exist in index
wright/orchestrator/tiering.py     TRIVIAL/SIMPLE/STANDARD/COMPLEX fast paths
tests/unit/test_contracts.py · tests/integration/test_pipeline.py
```

**LOC** ~2,600 (mostly prompts + parsers) · **Difficulty** 7 · **Dependencies** M4, M5
**Time** Team 3 wks · **Solo 5 weeks**

**Demo.** Full pipeline on a real issue. **Show the Researcher consuming ~120k tokens and
emitting a 2k brief — the 60:1 compression, live.** Show the Reviewer rejecting a patch
and the Coder fixing it.

**Acceptance criteria**
- Every handoff is a validated Pydantic model; invalid → re-prompt with the error, ≤3
- **Reviewer never receives the Coder's reasoning** — assert this in a test
- Plans validated against the index; hallucinated paths rejected before implementation
- Model routing per role, verified in the cost ledger
- Complexity tiering: a typo fix uses 1 LLM call, not 5
- Coder writing outside plan scope is **rejected by the broker**, not by the prompt
- Reviewer↔Coder capped at 2 cycles

**Common mistakes**
- **Letting the Reviewer see the Coder's chain of thought.** It will agree — shared
  premises produce agreement. Independence is its entire value.
- Giving the Planner tools. It re-explores and reintroduces the context pollution the
  Researcher exists to prevent.
- Running four roles on a typo. The long tail bankrupts you without tiering.
- Free-form text between agents instead of validated schemas.
- A Reviewer on a weaker model than the Coder — it rubber-stamps, manufacturing false
  confidence, which is worse than no reviewer.
- Merging Debugger into Coder. You get patch-stacking: each iteration piling a new guess
  on the last failed one.

**Commit.** All, prompts included. **Prompts are versioned artifacts and belong in git**
— reviewed, diffed, and rolled back like code.

---

## M7 · Verification & Debug Loop

**Objective.** Tiered test execution, structured failure parsing, hypothesis-driven
debugging, convergence detection, regression gating.

**Files**
```
wright/verify/test_runner.py       tiered execution
wright/verify/parsers/             pytest, go test, jest, cargo, junit
wright/verify/selection.py         symbol → covering tests
wright/verify/baseline.py          pre-patch baseline capture
wright/verify/convergence.py       oscillation + no-progress detection
wright/verify/static.py            semgrep/lint, baseline differencing
wright/tools/builtin/run_tests.py
tests/integration/test_debug_loop.py
```

**LOC** ~2,000 · **Difficulty** 7 · **Dependencies** M6
**Time** Team 1.5 wks · **Solo 3 weeks**

**Demo.** Introduce a real bug in a real repo. Agent patches it, tier-1 tests fail, it
forms a hypothesis, reverts, patches again, converges. **Show the structured failure
(~200 tokens) beside the raw output (~40k) — same information, 200× cheaper.**

**Acceptance criteria**
- Tier 1 (covering tests) → Tier 2 (package) → Tier 3 (full, once)
- Failures parsed to structured objects; raw dumps never enter context
- **Debugger reverts to checkpoint before each new hypothesis** — no patch-stacking
- Static analysis and lint report **delta vs. baseline only**
- Convergence detector escalates on repeat/oscillation *before* the cap is hit
- Infrastructure failure vs. test failure distinguished — different recovery paths
- Flaky tests re-run ×2 and excluded from the convergence signal

**Common mistakes**
- **Feeding raw test output to the LLM.** Expensive and less useful than structured results.
- Running the full suite every iteration. Tier 1 is what makes the loop converge in
  reasonable time.
- No baseline. You'll attribute pre-existing failures to the agent and chase ghosts.
- Letting the agent modify tests to make them pass. Constrain explicitly — "make the test
  pass" and "make the code correct" diverge, and the agent takes the cheaper path.
- Conflating "test command didn't run" with "tests failed."

**Commit.** All. Tag `v0.2.0` — at this point it autonomously fixes real bugs locally.

---

# PHASE 3 — PRODUCT

## M8 · GitHub Integration & PR Creation

**Objective.** GitHub App, webhooks, branch/commit/PR, review-response loop. First
externally visible output.

**Files**
```
wright/vcs/github/app.py           App auth, installation tokens
wright/vcs/github/webhooks.py      HMAC verify, dedupe, idempotent handlers
wright/vcs/github/pr.py            create, update, comment, labels
wright/vcs/commit.py               style mined from history, DCO/sign-off detection
wright/vcs/pr_body.py              template + mandatory AI disclosure
wright/api/webhooks.py             FastAPI ingress
tests/integration/test_github.py   recorded fixtures
```

**LOC** ~1,800 · **Difficulty** 5 · **Dependencies** M7
**Time** Team 1.5 wks · **Solo 3 weeks**

**Demo.** Label an issue `wright:go` on a real repo → **a PR appears with tests, evidence,
and disclosure.** Request changes on it → agent responds.

**Acceptance criteria**
- GitHub App auth with short-lived installation tokens; **no PAT anywhere**
- Webhook signatures verified; deliveries deduped; handlers idempotent
- Commit style mined from last ~100 commits; DCO detected and honored
- PR body: what/why, `Fixes #N`, approach, test evidence, not-addressed, **AI disclosure**
- Repo's PR template respected when present
- Review comments reopen the session with new constraints
- Branch is created from the **indexed commit_sha**, never a re-fetched HEAD

**Common mistakes**
- **Omitting the AI-assistance disclosure.** Several projects require it in CONTRIBUTING
  (HAMi does). Discovery permanently destroys the maintainer relationship. Non-negotiable.
- Credentials inside the sandbox. The sandbox produces a diff; the **host** pushes.
- Implementing force-push or history rewrite as tools. Don't implement them at all —
  stronger than instructing the model not to use them.
- Branching from a re-fetched HEAD, invalidating every analysis you just did.
- Testing against a real repo. Use a scratch org; you will open garbage PRs while
  developing.

**Commit.** All. **Gate real-repo runs behind an explicit `--allow-write` flag** so a
mis-run can't spam a maintainer.

---

## M9 · Approval Policy & Human Workflow

**Objective.** Policy-as-code gates, suspend/resume across approvals, notifications,
autonomy tiers.

**Files**
```
wright/approval/policy.py          CEL/Rego evaluation
wright/approval/rules/default.yaml
wright/approval/gate.py            suspend/resume semantics
wright/approval/notify/{slack,github,webhook}.py
wright/api/approvals.py            REST endpoints
wright/memory/conventions.py       write-back from `modify` responses
tests/integration/test_approval.py
```

**LOC** ~1,400 · **Difficulty** 5 · **Dependencies** M8
**Time** Team 1 wk · **Solo 2 weeks**

**Demo.** Session hits the PR gate → Slack notification with diff, plan, cost → click
approve → session rehydrates and opens the PR. **Show the container being released during
suspension and the session costing nothing while it waits.**

**Acceptance criteria**
- Gates evaluated by **policy against structured facts**, never decided by the agent
- Default gates: PR creation, dependency changes, CI config, deletions, sensitive paths
- Suspension releases the sandbox (snapshot to storage) and the worker
- Resume rehydrates correctly after arbitrary delay
- Four autonomy tiers configurable per repository
- `modify` writes the correction back as a durable repo convention

**Common mistakes**
- Asking the LLM whether something is risky. Policy is deterministic, versioned, testable.
- Holding the container during suspension. Kills the economics of long-running tasks.
- No timeout policy on pending approvals — sessions accumulate forever.
- Treating `modify` as rejection. It's the **highest-value signal in the system** — the
  mechanism by which cost per merged PR falls with use.

**Commit.** All, including default policy YAML. Document autonomy tiers prominently in the
README — it's the feature that makes the system trustworthy enough to adopt.

---

## M10 · Evaluation Harness

**Objective.** Golden set, SWE-bench subset, trajectory scoring, CI regression gates.

**Files**
```
eval/suites/golden/                100+ curated issues w/ expectations
eval/suites/swebench/              Verified subset harness
eval/suites/adversarial/           prompt injection, secret-bait, malicious repos
eval/scorer/{outcome,trajectory,safety}.py
eval/judge.py                      LLM judge — offline only
eval/report.py                     scorecards, diffs between runs
wright/cli_eval.py                 evalctl
.github/workflows/eval.yml
```

**LOC** ~2,200 · **Difficulty** 6 · **Dependencies** M7
**Time** Team 2 wks · **Solo 3.5 weeks**

**Demo.** `evalctl run --suite golden` → scorecard with resolve rate, cost per resolved
issue, trajectory efficiency, safety violations. Change a prompt, re-run, **show the diff
between scorecards**.

**Acceptance criteria**
- Outcome **and** trajectory scored — an agent that stumbles to the answer in 90 steps is
  not equivalent to one that takes 12
- Per-role eval suites, not just end-to-end
- Adversarial suite covers injection via issue body, README, and code comments
- CI blocks merge on regression in resolve rate, cost/issue, or any safety metric
- **Judge runs offline only** — never with authority inside the production loop
- Production failures promotable into the golden set with one command

**Common mistakes**
- **Building this too late.** Without it, every prompt change is superstition. This is
  the milestone teams skip and then regret for the rest of the project.
- Optimizing for SWE-bench. You'll overfit to a benchmark frontier labs tune against.
  Your golden set of *real issues from repos you care about* is worth more.
- LLM judge inside the loop → the system optimizes against its own grader.
- Scoring only pass/fail. Cost and trajectory efficiency are where regressions hide.

**Commit.** Everything except benchmark repos (submodule or fetch script). **Publish
scorecards in the README.** Real numbers on real repos are the strongest possible
credibility signal — and nobody in this space publishes cost per merged PR.

---

# PHASE 4 — SCALE

## M11 · Observability & Trajectory Viewer

**Objective.** OTel tracing, Prometheus metrics, and the trajectory viewer.

**Files**
```
wright/platform/tracing.py · metrics.py
wright/api/trajectory.py           event log → renderable timeline
frontend/trajectory/               React: timeline, prompts, tools, diffs, replay
deploy/grafana/dashboards/
```

**LOC** ~1,900 (~1,100 frontend) · **Difficulty** 5 · **Dependencies** M5
**Time** Team 1.5 wks · **Solo 3 weeks**

**Demo.** Open a completed session in the viewer: every prompt, response, tool call, diff,
cost, and timing. Scrub to any point. **Show a session where 90k input tokens produced 40
output tokens — a context-assembly bug, visible at a glance.**

**Acceptance criteria**
- One trace per session; spans per role, LLM call, tool call, sandbox command
- Token counts and cost as span attributes
- Business metrics: `cost_per_resolved_issue`, `pr_merge_rate`, `human_intervention_rate`
- Viewer renders any session from its event log alone
- Secrets redacted at the logger, not by convention

**Common mistakes**
- Building the viewer late. **This is the highest-value internal tool in the project** —
  debugging agents by reading raw logs stops scaling in week one.
- Tracing without token/cost attributes; you lose the main diagnostic.
- Logging full prompts at INFO. Volume and secret-leak risk both explode.

**Commit.** All. **The trajectory viewer is the best screenshot in your README.**

---

## M12 · Web Frontend & API

**Objective.** The UI a non-CLI user actually operates.

**Files**
```
wright/api/{sessions,repos,approvals,auth}.py
wright/api/ws.py                   live session streaming (SSE/WS)
frontend/src/pages/{Dashboard,Session,Approvals,RepoConfig,Settings}.tsx
frontend/src/components/{DiffViewer,CostPanel,PolicyEditor}.tsx
```

**LOC** ~3,200 (~2,400 frontend) · **Difficulty** 5 · **Dependencies** M9, M11
**Time** Team 2 wks · **Solo 4 weeks**

**Demo.** Connect a repo, trigger a session on an issue, watch it stream live, approve at
the gate, see the PR open — **entirely in the browser**.

**Acceptance criteria**
- Live streaming of session progress (your SSE experience applies directly)
- Diff viewer with syntax highlighting
- Approval UI supports approve / reject / **modify**
- Per-repo autonomy tier and policy editing
- Cost dashboard per session, repo, and time period
- Auth: GitHub OAuth

**Common mistakes**
- Polling instead of streaming. You've built SSE before; use it.
- Rendering the full event log in the DOM — sessions have thousands of events. Virtualize.
- No cost visibility in the UI. It's the number users care about most.
- Making `modify` hard to use — it's the highest-value interaction.

**Commit.** All. **Deploy a public demo instance** (read-only trajectories from a scratch
org). A live URL converts a repo from "impressive doc" to "working product."

---

## M13 · MCP, Plugins & Memory

**Objective.** MCP client with sandboxed servers; Tier 3/4 memory that compounds.

**Files**
```
wright/plugins/mcp/{client,transport,negotiate,sandbox}.py
wright/plugins/registry.py
wright/memory/{semantic,procedural,compaction}.py
wright/memory/writeback.py         approvals + review rejections → conventions
tests/integration/test_mcp.py
```

**LOC** ~1,800 · **Difficulty** 6 · **Dependencies** M9
**Time** Team 1.5 wks · **Solo 3 weeks**

**Demo.** Attach an MCP server (Sentry or Postgres) via config alone — **zero Wright-side
code** — and watch the agent use its tools. Then run two issues on the same repo and show
the second costing measurably less because conventions were learned.

**Acceptance criteria**
- stdio and HTTP/SSE transports; capability negotiation
- Tools namespaced `mcp.<server>.<tool>`; same validate→authorize→gate pipeline
- **MCP servers sandboxed with explicit capability grants** — untrusted by default
- **Tool descriptions from servers treated as untrusted content** and fenced in prompts
- Review rejections and `modify` responses persist as repo conventions
- Second issue on a repo demonstrably cheaper than the first — **measure and publish it**

**Common mistakes**
- Trusting MCP tool descriptions. A server returning instruction-shaped text in a
  description is attempting prompt injection.
- Unsandboxed MCP servers — you've handed a third party your agent's capabilities.
- Unbounded procedural memory. **One wrong convention inferred from a single review
  comment propagates to every future session.** Confidence-weight it and decay it.

**Commit.** All. Ship 2–3 example MCP configs — it makes the extensibility story concrete.

---

## M14 · Deployment, Security & Multi-Tenancy

**Objective.** Production Kubernetes deployment with gVisor, secrets management, RLS
multi-tenancy, and a real security review.

**Files**
```
deploy/helm/wright/                charts, values, HPA/KEDA
deploy/terraform/                  cluster, Postgres, Redis, S3
deploy/docker/                     multi-stage, distroless, non-root
wright/platform/secrets.py         Vault / AWS SM
wright/db/rls.py                   row-level security per tenant
.github/workflows/{release,security}.yml
docs/runbooks/
```

**LOC** ~2,200 (mostly config) · **Difficulty** 7 · **Dependencies** M12
**Time** Team 2 wks · **Solo 4 weeks**

**Demo.** `helm install` on a real cluster; run 10 concurrent sessions across 3 repos;
kill a worker pod mid-run and watch sessions resume on another. Show a Grafana dashboard
with cost per merged PR.

**Acceptance criteria**
- Sandbox pods on a dedicated tainted node pool with `RuntimeClass: gvisor`
- Workers KEDA-scaled on queue depth
- Secrets from an external manager; none in images or env files
- Postgres RLS enforces tenant isolation — **tested with an adversarial query**
- Images signed (cosign) with SBOM
- Canary deploy with auto-rollback on resolve-rate or cost regression
- Security review passes: injection, egress escape, secret-leak suites all green

**Common mistakes**
- Sandbox pods on shared nodes. Dedicated, tainted pool, no exceptions.
- Secrets in ConfigMaps or images.
- Skipping RLS "because it's single-tenant for now." Retrofitting isolation is far harder
  than building it in.
- No auto-rollback. A prompt regression at 3am with no rollback is a bad night.

**Commit.** All infrastructure-as-code. Add `docs/runbooks/` and a proper `SECURITY.md`
with a disclosure policy.

---

## Summary

| M | Milestone | LOC | Diff | Team | Solo | Demo |
|---|---|---|---|---|---|---|
| 0 | Skeleton & Models | 1.2k | 3 | 3d | 1w | Multi-provider call + cost ledger |
| 1 | Sandbox & Tools | 1.8k | 6 | 1w | 2w | Network-deny proven live |
| 2 | **Walking Skeleton** ★ | 0.9k | 5 | 4d | 1.5w | **Real issue → real diff** |
| 3 | Repo Intelligence | 2.8k | 7 | 2w | 4w | Blast radius in <50ms |
| 4 | Retrieval | 2.2k | 7 | 1.5w | 3w | 40k → 8k tokens, better content |
| 5 | Event Sourcing | 2.0k | 8 | 2w | 4w | `kill -9` → resumes |
| 6 | Five Agents | 2.6k | 7 | 3w | 5w | 60:1 compression live |
| 7 | Verification | 2.0k | 7 | 1.5w | 3w | Autonomous debug convergence |
| 8 | GitHub & PR | 1.8k | 5 | 1.5w | 3w | Label issue → PR appears |
| 9 | Approval Policy | 1.4k | 5 | 1w | 2w | Slack gate → approve → PR |
| 10 | Evaluation | 2.2k | 6 | 2w | 3.5w | Scorecard diff across prompts |
| 11 | Observability | 1.9k | 5 | 1.5w | 3w | Trajectory viewer |
| 12 | Frontend | 3.2k | 5 | 2w | 4w | Full browser workflow |
| 13 | MCP & Memory | 1.8k | 6 | 1.5w | 3w | 2nd issue cheaper than 1st |
| 14 | Deploy & Security | 2.2k | 7 | 2w | 4w | K8s, pod kill, resume |
| | **Total** | **~30k** | | **~14w** | **~46w** | |

### Three things that decide whether this ships

1. **Reach M2 fast, even ugly.** The most likely failure mode is three months of
   infrastructure before one real diff. M2 validates or invalidates the entire design.
2. **Build M10 earlier than feels necessary.** Without evaluation, every prompt change is
   superstition and you will slowly make the system worse while believing otherwise.
3. **M11's trajectory viewer is not optional polish.** Debugging agents by reading logs
   stops working in week one.

### Portfolio checkpoints

| After | You can credibly claim |
|---|---|
| **M2** | "Built an autonomous agent that reads a GitHub issue and produces a working patch" |
| **M4** | "Built a repository intelligence engine handling 1M+ LOC with sub-second retrieval" |
| **M7** | "Built a self-debugging agent with convergence detection and regression gating" |
| **M8** | "Autonomous agent that opens real PRs on real repositories" |
| **M10** | "Published measured resolve rate and cost-per-merged-PR on real repos" |
| **M14** | "Production multi-tenant system on Kubernetes with sandboxed execution" |

**Any of the first four alone is a stronger portfolio artifact than a typical finished
side project.** M10 is the one that's genuinely rare — almost nobody publishes real cost
and resolve-rate numbers, which is exactly why doing so is differentiating.

### Reconciling with the earlier docs

ARCHITECTURE.md §28 proposed four phases predating the three design docs. This roadmap supersedes it:
its Phase 1 ≈ M0–M2, Phase 2 ≈ M5–M7, Phase 3 ≈ M8–M11, Phase 4 ≈ M12–M14, with M3–M4
(repository intelligence) promoted from an implicit assumption to explicit milestones —
because [REPOSITORY-INTELLIGENCE.md](REPOSITORY-INTELLIGENCE.md) established that retrieval
quality, not agent cleverness, is the binding
constraint on output quality.

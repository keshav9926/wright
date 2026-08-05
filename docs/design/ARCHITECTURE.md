# WRIGHT — Autonomous Software Engineering Agent

**System Design Document**
Version 0.1 · 2026-08-01 · Keshav Kakani

> *wright (n.) — one who builds or makes.*

An open-source autonomous software engineering agent that takes a GitHub issue and
produces a reviewed, tested pull request. Architecturally comparable to Devin,
OpenAI Codex, and Claude Code; deliberately narrower in scope than any of them.

---

## Table of Contents

1. [Problem Statement & Design Principles](#1-problem-statement--design-principles)
2. [Architecture Comparison (A–E)](#2-architecture-comparison)
3. [Recommendation & Rationale](#3-recommendation--rationale)
4. [Overall Architecture](#4-overall-architecture)
5. [Module Boundaries](#5-module-boundaries)
6. [Monolith vs Microservices](#6-monolith-vs-microservices)
7. [Folder Structure](#7-folder-structure)
8. [State Management & Event Sourcing](#8-state-management--event-sourcing)
9. [Memory Hierarchy](#9-memory-hierarchy)
10. [Event Bus, Queues & Databases](#10-event-bus-queues--databases)
11. [Caching](#11-caching)
12. [Retrieval & Repository Indexing](#12-retrieval--repository-indexing)
13. [Tool Execution Pipeline](#13-tool-execution-pipeline)
14. [Docker Sandbox](#14-docker-sandbox)
15. [GitHub Integration](#15-github-integration)
16. [Model Abstraction Layer](#16-model-abstraction-layer)
17. [MCP & Plugin System](#17-mcp--plugin-system)
18. [Security & Secrets](#18-security--secrets)
19. [Reliability: Retry, Rate Limiting, Checkpointing, Recovery](#19-reliability)
20. [Human Approval Workflow](#20-human-approval-workflow)
21. [Observability](#21-observability)
22. [Evaluation Framework](#22-evaluation-framework)
23. [Testing Strategy](#23-testing-strategy)
24. [Deployment, CI/CD & Scaling](#24-deployment-cicd--scaling)
25. [Cost Optimization](#25-cost-optimization)
26. [ASCII Architecture Diagram](#26-ascii-architecture-diagram)
27. [Sequence Diagram: Issue → Pull Request](#27-sequence-diagram-issue--pull-request)
28. [Roadmap & Open Questions](#28-roadmap--open-questions)

---

## 1. Problem Statement & Design Principles

### The problem

Given a GitHub issue against a repository of arbitrary size, autonomously produce a
pull request that resolves it, with tests, that a human maintainer would merge.

This is hard for reasons that are mostly *not* about model intelligence:

- **Context scarcity.** A 500k-LOC repository does not fit in any context window.
  The binding constraint is *selecting* the right 30k tokens, not reasoning over them.
- **Verification asymmetry.** Generating a patch is cheap. Knowing whether it is
  correct requires executing it, which requires a sandbox, a build, and a test suite
  — each of which fails in a hundred environment-specific ways.
- **Unbounded failure modes.** Long-horizon autonomy means compounding error. A 40-step
  task with 97% per-step reliability succeeds 30% of the time.
- **Trust.** An agent with write access to a repository and a shell is a security
  boundary, not a feature.

### Design principles

1. **Context isolation is the primary architectural concern.** Every design decision
   below is downstream of "which agent sees which tokens."
2. **The event log is the source of truth.** Not the agent's memory, not the process
   state. If it isn't in the log, it didn't happen. Checkpointing, resume, replay,
   and audit all fall out of this for free.
3. **Verification over generation.** Spend compute on checking, not on producing more
   candidates. A reviewer that rejects is worth more than a coder that is confident.
4. **The sandbox is adversarial.** Treat model-generated code as hostile input,
   because from a security standpoint it is indistinguishable from hostile input.
5. **Degrade, don't crash.** Every autonomous step has a defined failure path that
   ends in either a retry, a replan, or a human.
6. **Cost is a first-class design constraint.** An agent that solves the task for $40
   is a research demo. One that solves it for $0.80 is a product.

---

## 2. Architecture Comparison

### Architecture A — Single Agent

One ReAct loop. One context window. All tools attached. The agent reads, plans,
edits, tests, and commits in a single continuous conversation.

**Advantages**
- Simplest possible implementation; a working prototype in days.
- No inter-agent communication, serialization, or handoff-fidelity loss.
- Full causal context — the agent that wrote the code remembers *why*.
- Lowest latency: no coordination overhead, no redundant re-reading.
- Easiest to debug: one linear transcript.

**Disadvantages**
- **Context window collapse.** Exploration output (file listings, grep results, failed
  test logs) accumulates in the same window used for implementation. On a large repo,
  the agent is reasoning over its own garbage by step 20.
- No model routing — cheap exploration and expensive reasoning use the same model.
- No independent critic. The agent reviewing its own patch shares its blind spots.
- Failure is total: one bad turn poisons all subsequent turns.
- Context compaction becomes the dominant engineering problem, and it is lossy.

**Complexity** Low · **Scalability** Poor (vertical only; bounded by context)
**Token efficiency** High per-step, **poor per-task** (quadratic re-reading of history)
**Production readiness** Viable for narrow, small-repo tasks · **Difficulty** 2/10

**Best for** Single-file changes, small repos (<20k LOC), interactive assistants where
a human is in the loop every few turns, latency-sensitive use.

---

### Architecture B — Planner + Executor

A planner decomposes the issue into an ordered step list. An executor carries out each
step with tools. The planner may be re-invoked on failure.

**Advantages**
- Separates "what to do" from "how to do it" — the two failure modes become
  independently debuggable.
- Plan is a durable, inspectable artifact: checkpointable, resumable, human-editable.
- Executor context resets per step, bounding context growth.
- Planner can use a strong model sparingly; executor can use a cheaper one.
- Natural human approval point: gate on the plan, not on every action.

**Disadvantages**
- **Plans made before exploration are usually wrong.** The planner is guessing about a
  codebase it hasn't read. Plan quality is the system's ceiling.
- Rigid decomposition handles surprises badly — mid-task discoveries invalidate the plan
  and trigger expensive full replans.
- Still no independent verification; the executor self-reports success.
- Handoff loss: the executor lacks the planner's reasoning, so ambiguous steps get
  interpreted wrongly.

**Complexity** Moderate · **Scalability** Moderate (steps parallelize if independent)
**Token efficiency** Good — context resets per step
**Production readiness** Decent with a replan loop · **Difficulty** 4/10

**Best for** Well-specified mechanical tasks: framework migrations, dependency bumps,
API renames, codemods — anything where the plan is knowable in advance.

---

### Architecture C — Planner + Researcher + Coder + Reviewer

Four fixed roles. Researcher explores and returns a compressed brief. Planner turns the
brief into a plan. Coder implements. Reviewer independently verifies and can reject,
returning control to Coder or Planner.

**Advantages**
- **Context isolation is structural.** The Researcher burns 150k tokens of exploration
  and returns a 2k-token brief. The Coder never sees the garbage. This single property
  is why the architecture works.
- **Model routing per role.** Researcher on a fast cheap model, Planner and Reviewer on
  a strong one, Coder on a strong code model. Typically 40–60% cost reduction versus
  running everything on the top model.
- **Independent critic.** The Reviewer has no memory of the Coder's reasoning, so it
  doesn't inherit its rationalizations. This is where correctness actually comes from.
- Plans are grounded in real exploration, fixing B's central weakness.
- Roles map onto how human engineering teams already work — the mental model transfers,
  which matters for debuggability and for onboarding contributors.
- Each role is independently testable, evaluable, and replaceable.

**Disadvantages**
- 4× the prompt engineering surface; four sets of failure modes.
- Handoff fidelity is the core risk — compression is lossy, and the Researcher may omit
  the one detail the Coder needed.
- Higher latency: sequential handoffs, plus review cycles.
- Reviewer↔Coder can ping-pong without converging; needs a hard iteration cap.
- More total tokens than A on *trivial* tasks (fixed overhead of ~4 role invocations).

**Complexity** Moderate-High · **Scalability** Good (roles scale independently; research
parallelizes across sub-questions)
**Token efficiency** **Best in class for non-trivial tasks** — isolation dominates overhead
above ~10 steps
**Production readiness** High · **Difficulty** 6/10

**Best for** Real issues on real repos. Bug fixes and features requiring codebase
understanding. The mainstream case this system targets.

---

### Architecture D — Hierarchical Multi-Agent

An orchestrator recursively decomposes tasks and spawns sub-agents, which may themselves
spawn sub-agents. Arbitrary depth, dynamic role creation.

**Advantages**
- Handles genuinely large, decomposable work (multi-service refactors, repo-wide migrations).
- Parallelism across independent subtrees is real and substantial.
- Adaptive: creates the roles the task needs rather than the roles you predicted.
- Naturally handles unbounded task size.

**Disadvantages**
- **Unbounded and unpredictable cost.** Recursive spawning has no natural stopping
  condition. Cost variance across runs is enormous, which makes it unpriceable.
- **Debugging is genuinely miserable.** A failure three levels deep, in a sub-agent with
  a dynamically generated prompt, is close to unreproducible.
- Handoff loss compounds multiplicatively with depth. By level three, the leaf agent is
  working from a summary of a summary of a summary.
- Coordination overhead can exceed the useful work — sub-agents duplicating exploration
  is the common pathology.
- Deadlock, orphaned subtrees, and partial-failure semantics all need explicit handling.
- **Software engineering tasks rarely need dynamic depth.** The role set (understand →
  plan → implement → verify) is *known in advance*. Paying for dynamism you don't need
  is the definition of over-engineering.

**Complexity** Very High · **Scalability** Excellent in theory, hard to realize
**Token efficiency** Poor — redundant exploration, summary-of-summary loss
**Production readiness** Low without heavy investment · **Difficulty** 9/10

**Best for** Repo-wide migrations, multi-repo changes, research agents over large
corpora. Not the primary SWE-agent case.

---

### Architecture E — Event-Driven Agent System

No central controller. Agents are handlers subscribed to an event bus. `IssueReceived`
triggers the Researcher; `ResearchComplete` triggers the Planner; `TestsFailed` triggers
the Debugger. Control flow is emergent from subscriptions.

**Advantages**
- **Excellent durability and resumability** — the event log *is* the state. Crash
  recovery is replay.
- Naturally asynchronous, parallel, and horizontally scalable.
- Loose coupling: new agents subscribe without modifying existing ones.
- Reactive to external events (CI completion, review comments, webhook pushes) as
  first-class citizens rather than as polling hacks.
- Superb auditability — every state transition is an immutable record.

**Disadvantages**
- **Emergent control flow is hard to reason about.** "Why did the agent do that?"
  requires reconstructing a distributed trace rather than reading a transcript.
- Event storms, cycles, and infinite loops need explicit circuit breakers.
- Eventual consistency introduces races (two agents editing the same file).
- Significant infrastructure burden before the first useful task completes.
- Testing requires simulating the whole bus.
- **It is not actually a competing topology.** Any of A–D can be *implemented* on an
  event bus. E is an execution substrate, not an agent architecture — comparing it to
  C is a category error.

**Complexity** High · **Scalability** Excellent
**Token efficiency** Neutral — orthogonal to token use
**Production readiness** High for infra, low for agent logic · **Difficulty** 8/10

**Best for** Multi-tenant SaaS, long-running tasks measured in hours or days, systems
reacting to many external event sources.

---

### Comparison Matrix

| | **A** Single | **B** Plan+Exec | **C** 4-Role | **D** Hierarchical | **E** Event-Driven |
|---|---|---|---|---|---|
| Complexity | Low | Moderate | Mod-High | Very High | High |
| Scalability | Poor | Moderate | Good | Excellent* | Excellent |
| Token efficiency (small task) | **Best** | Good | Moderate | Poor | N/A |
| Token efficiency (real task) | Poor | Good | **Best** | Poor | N/A |
| Context isolation | None | Partial | **Strong** | Strong | Varies |
| Model routing | No | Partial | **Yes** | Yes | Yes |
| Independent verification | No | No | **Yes** | Yes | Yes |
| Debuggability | **Best** | Good | Good | Poor | Poor |
| Cost predictability | Good | **Good** | Good | Poor | Good |
| Resumability | Poor | Good | Good | Poor | **Best** |
| Production readiness | Low | Moderate | **High** | Low | High (infra) |
| Difficulty /10 | 2 | 4 | 6 | 9 | 8 |

\* theoretical

---

## 3. Recommendation & Rationale

> **Recommended: Architecture C — Planner + Researcher + Coder + Reviewer,
> implemented on an event-sourced execution substrate borrowed from E.**

This is one architecture, not a hedge. **E is not a rival topology to C** — it is the
persistence and execution layer underneath it. Choosing C answers "what agents exist and
how does work flow between them." Adopting event sourcing answers "how is that flow made
durable." They compose; they don't compete.

### Why C wins

**1. The real problem is context, and C solves it structurally.**
Not with prompt tricks or compaction heuristics, but with process boundaries. The
Researcher may consume 150k tokens crawling the repo and returns a 2k-token brief. The
Coder's window contains only signal. No other architecture in the list gets this
property without also getting D's cost explosion.

**2. Model routing is the biggest available cost lever, and C exposes it cleanly.**
Fixed roles mean fixed capability requirements:

| Role | Model tier | Rationale |
|---|---|---|
| Researcher | Fast / cheap | High volume, low reasoning depth; mostly search and summarize |
| Planner | Frontier | Low volume, highest reasoning leverage per token |
| Coder | Frontier code | Correctness-critical generation |
| Reviewer | Frontier | Must be *at least* as strong as the Coder or it rubber-stamps |

Measured against all-frontier operation this is typically a 40–60% cost reduction with
no measurable quality loss, because the Researcher's job genuinely does not require
frontier reasoning.

**3. Independent verification is where correctness comes from.**
A and B have no critic. An agent asked to review its own work inherits its own
misconceptions — it already decided the approach was right. The Reviewer starts from the
diff and the issue, with no memory of the Coder's reasoning, and can therefore
disconfirm. This is the highest-value single component in the system.

**4. D's dynamism is a cost we don't need to pay.**
The SWE role set is knowable in advance. Understand, plan, implement, verify. Paying
D's debuggability and cost-variance penalties to *discover* roles we already know is
over-engineering. D remains the right answer for repo-wide migration — noted as a
future extension, not a v1 requirement.

**5. C degrades gracefully into B and A.**
For a one-line docs fix, skip Research and Review — the system runs as B or even A.
The architecture supports a **complexity-tiered fast path**, which neither D nor a
naive C can do. This matters enormously for cost and latency on the long tail of
trivial issues.

### Why event sourcing underneath

The requirements include *long-running tasks* and *resume interrupted sessions*. Bolting
resumption onto a stateful in-memory agent is a chronic source of bugs. If instead every
state transition is an appended immutable event, then:

- **Checkpointing** is automatic — the log is the checkpoint.
- **Resume** is replay to the last event plus reattachment to the sandbox.
- **Audit and observability** are free — every decision is a durable record.
- **Time-travel debugging** becomes possible: replay to event N and fork.
- **Human approval** is naturally modeled as a session that blocks awaiting an
  `ApprovalGranted` event, with zero special-casing.

The cost is a reducer/projection layer and discipline about event schema versioning.
Worth it.

### Accepted trade-offs

- Higher latency than A (multiple handoffs + review cycles). Mitigated by the fast path
  and by parallelizing independent research sub-questions.
- Handoff fidelity is the top technical risk. Mitigated by *structured* briefs
  (schema-validated, not free prose) and by giving the Coder read access to the raw
  research artifacts on demand rather than only the summary.
- Four prompt surfaces to maintain and evaluate. Mitigated by per-role eval suites (§22).

---

## 4. Overall Architecture

Five planes, cleanly separated:

```
Control Plane   — API, auth, session lifecycle, approval gates
Agent Plane     — the four roles + orchestration reducer
Execution Plane — sandboxed tool execution (Docker), one container per session
Data Plane      — Postgres (truth), Redis (cache/locks), object store (artifacts)
Integration     — GitHub, LLM providers, MCP servers, notification sinks
```

**Cardinal rule: the Agent Plane never touches the host.** All side effects — file
writes, shell commands, network calls, git operations — are proposed by agents as
structured tool calls, validated by the Tool Broker, and executed inside the Execution
Plane. There is no code path from an LLM output to a host syscall.

### Request lifecycle (abridged)

```
Webhook/API → Session created → Events appended → Orchestrator reduces state
   → dispatches role → role emits tool calls → Broker validates → Sandbox executes
   → results appended as events → reduce → next role → ... → PR opened → Session closed
```

---

## 5. Module Boundaries

| Module | Responsibility | Must NOT |
|---|---|---|
| `api` | HTTP/WS ingress, authn/authz, request validation | Contain agent logic |
| `orchestrator` | Reduce event log → state; decide next role; enforce budgets & caps | Call LLMs or tools directly |
| `agents` | Role prompt assembly, response parsing, structured-output validation | Execute tools; persist state |
| `tools` | Tool registry, schema definition, arg validation, permission policy | Trust agent input |
| `sandbox` | Container lifecycle, command execution, filesystem, resource limits | Interpret task semantics |
| `retrieval` | Indexing, hybrid search, context assembly & budgeting | Decide *what* to search for |
| `vcs` | Git ops, GitHub REST/GraphQL, PR & review management | Know about agents |
| `models` | Provider abstraction, routing, streaming, token accounting, fallback | Contain prompts |
| `memory` | The five memory tiers, summarization, retention policy | Bypass the event log |
| `events` | Append-only log, projections, subscriptions, outbox dispatch | Contain business logic |
| `approval` | Policy evaluation, gate creation, notification, resolution | Decide *what* is risky (that's policy config) |
| `eval` | Benchmark harness, golden sets, trajectory scoring, regression gates | Run in the production path |
| `plugins` | MCP client, plugin discovery, capability negotiation, sandboxing | Get implicit trust |

**Dependency rule:** dependencies point inward. `agents` depends on `models` and `tools`;
neither depends on `agents`. `orchestrator` depends on `events`; `events` depends on
nothing. Cycles are a build failure.

---

## 6. Monolith vs Microservices

> **Decision: modular monolith for the control and agent planes; separately scaled
> stateless workers for execution.**

### Why not microservices

The instinct to split Planner/Researcher/Coder/Reviewer into four services is wrong:

- They share the entire domain model (session, plan, diff, brief). Splitting means
  distributed transactions over what is logically one workflow.
- They are invoked **sequentially** — network hops add latency for zero parallelism gain.
- They scale **together**, not independently. One session needs one of each.
- Debugging a four-service distributed trace for what is a linear workflow is a
  self-inflicted wound.
- Deployment, versioning, and schema-compatibility burden multiplies by four with no
  offsetting benefit at any realistic scale.

### What *is* separated, and why

| Component | Deployment | Reason |
|---|---|---|
| `wright-api` | Stateless service, N replicas | Ingress; scales with request volume |
| `wright-worker` | Stateless service, M replicas | Runs sessions; scales with concurrency |
| `wright-sandbox` | One container **per session**, ephemeral | Hard security + resource isolation |
| `wright-indexer` | Separate worker pool | Bursty, CPU-heavy, latency-tolerant |
| Postgres / Redis / S3 | Managed | Standard |

Sandboxes are separated for **security**, not modularity. The indexer is separated
because its resource profile (CPU-bound, spiky) is genuinely different from the agent
workers (I/O-bound on LLM calls).

This gives the operational simplicity of a monolith with the isolation properties that
actually matter. Modules communicate in-process via interfaces; if a boundary ever needs
to become a network boundary, the interface is already there.

---

## 7. Folder Structure

```
wright/
├── cmd/
│   ├── api/                    # HTTP/WS ingress binary
│   ├── worker/                 # session execution binary
│   ├── indexer/                # repository indexing binary
│   └── evalctl/                # evaluation harness CLI
│
├── internal/
│   ├── orchestrator/
│   │   ├── reducer.go          # event log → session state
│   │   ├── policy.go           # role dispatch decisions
│   │   ├── budget.go           # token/time/cost caps
│   │   └── fastpath.go         # complexity tiering
│   │
│   ├── agents/
│   │   ├── researcher/         # prompt, output schema, parser
│   │   ├── planner/
│   │   ├── coder/
│   │   ├── reviewer/
│   │   ├── contract/           # inter-role structured schemas
│   │   └── runtime.go          # shared role execution loop
│   │
│   ├── models/
│   │   ├── provider/           # anthropic/, openai/, google/, local/
│   │   ├── router.go           # capability tier → concrete model
│   │   ├── fallback.go         # failover chain
│   │   ├── accounting.go       # token + cost ledger
│   │   └── cache.go            # prompt-cache orchestration
│   │
│   ├── tools/
│   │   ├── registry.go
│   │   ├── schema/             # JSONSchema per tool
│   │   ├── permission.go       # capability policy
│   │   ├── builtin/            # read, edit, grep, glob, bash, test, git
│   │   └── broker.go           # validate → authorize → dispatch → record
│   │
│   ├── sandbox/
│   │   ├── docker/             # container lifecycle
│   │   ├── image/              # base images, language toolchain detection
│   │   ├── fs/                 # overlay, snapshot, diff extraction
│   │   ├── exec.go             # command execution, streaming, timeouts
│   │   └── limits.go           # cgroup, seccomp, network policy
│   │
│   ├── retrieval/
│   │   ├── index/              # tree-sitter parse, symbol graph, ctags
│   │   ├── lexical/            # ripgrep-backed exact/regex search
│   │   ├── semantic/           # embeddings (optional tier)
│   │   ├── graph/              # import/call graph traversal
│   │   ├── rank.go             # reciprocal rank fusion
│   │   └── assembler.go        # context budget packing
│   │
│   ├── vcs/
│   │   ├── git/                # clone, branch, commit, diff
│   │   ├── github/             # REST + GraphQL, App auth, webhooks
│   │   └── pr.go               # PR body synthesis, review threading
│   │
│   ├── memory/
│   │   ├── working.go          # tier 1: active context
│   │   ├── episodic.go         # tier 2: session history
│   │   ├── semantic.go         # tier 3: repo knowledge
│   │   ├── procedural.go       # tier 4: learned conventions
│   │   └── compaction.go       # summarization + eviction
│   │
│   ├── events/
│   │   ├── log.go              # append-only writer
│   │   ├── types.go            # event schema + versioning
│   │   ├── projection/         # read models
│   │   └── outbox.go           # transactional outbox dispatcher
│   │
│   ├── approval/
│   │   ├── policy.go           # gate rules (OPA/CEL)
│   │   ├── gate.go             # blocking/resume semantics
│   │   └── notify/             # slack, email, webhook sinks
│   │
│   ├── plugins/
│   │   ├── mcp/                # MCP client, transport, capability negotiation
│   │   └── registry.go
│   │
│   └── platform/
│       ├── config/  logging/  metrics/  tracing/  secrets/  ratelimit/  retry/
│
├── pkg/                        # exported client libraries
├── eval/
│   ├── suites/                 # swebench/, golden/, adversarial/
│   ├── scorer/                 # outcome + trajectory scoring
│   └── report/
├── deploy/
│   ├── docker/  helm/  terraform/
├── docs/
│   ├── ARCHITECTURE.md  ADR/  runbooks/
└── test/
    ├── integration/  e2e/  fixtures/
```

Language shown as Go for concreteness (strong concurrency story, single-binary deploys,
excellent container ecosystem). Python is a legitimate alternative if ML tooling
integration outweighs deployment simplicity. **The architecture is language-agnostic.**

---

## 8. State Management & Event Sourcing

### The event log

Every session is a stream of immutable, append-only events in Postgres.

```
events(
  session_id, seq, type, payload jsonb, actor,
  created_at, causation_id, correlation_id, schema_version
)
PRIMARY KEY (session_id, seq)
```

Representative event types:

```
SessionCreated        IssueIngested         RepoCloned          IndexBuilt
ResearchStarted       ToolCallRequested     ToolCallCompleted   ResearchCompleted
PlanProposed          PlanApproved          PlanRejected
StepStarted           PatchProposed         PatchApplied
TestsStarted          TestsPassed           TestsFailed
ReviewRequested       ReviewPassed          ReviewRejected
ApprovalRequested     ApprovalGranted       ApprovalDenied
CommitCreated         PullRequestOpened
BudgetExceeded        SessionSuspended      SessionResumed      SessionFailed
```

### Session state machine

```
    CREATED ──► INDEXING ──► RESEARCHING ──► PLANNING ──► AWAITING_APPROVAL
                                                │                  │
                                                ▼                  ▼
                                             CODING ◄────────── APPROVED
                                                │
                                                ▼
                                            TESTING ──fail──► DEBUGGING ─┐
                                                │                        │
                                              pass                    (≤N)│
                                                ▼                        │
                                            REVIEWING ◄──────────────────┘
                                          │         │
                                     reject         pass
                                          │         │
                                          ▼         ▼
                                      CODING     SUBMITTING ──► DONE

    Any state ──► SUSPENDED (budget/approval/crash) ──► resume ──► prior state
    Any state ──► FAILED (unrecoverable / cap exceeded)
```

State is **never** stored directly. It is derived by folding the event log through a
pure reducer:

```
state = reduce(events[0..n])
```

Snapshots every 50 events bound replay cost; they are a cache, never the truth.

### Why this matters

- **Crash at any point** → worker dies, another picks up the session, replays to current
  state, reattaches to the sandbox (or rebuilds it from the last filesystem snapshot).
- **Human approval** → the session simply has no valid transition until an
  `ApprovalGranted` event arrives. No special-casing, no polling, no held connections.
- **Debugging** → replay to event N, fork the session, try a different branch.
- **Audit** → a complete, immutable, legally defensible record of every action taken
  against a customer's repository.

---

## 9. Memory Hierarchy

Five tiers, distinguished by lifetime and scope:

| Tier | Name | Lifetime | Scope | Store | Contents |
|---|---|---|---|---|---|
| 1 | **Working** | Single LLM call | Role invocation | In-process | Assembled prompt: task, brief, relevant code, recent tool results |
| 2 | **Episodic** | Session | Session | Postgres event log | Full ordered history of what happened |
| 3 | **Semantic** | Persistent | Repository | Postgres + index | Repo facts: architecture, build/test commands, module map, symbol graph |
| 4 | **Procedural** | Persistent | Repository / org | Postgres | Learned conventions: review norms, commit style, test idioms, past rejections |
| 5 | **Shared** | Persistent | Global | Postgres | Cross-repo patterns: framework knowledge, common failure signatures |

### Compaction

Tier 1 is the scarce resource. Budget per role invocation, enforced by the assembler:

```
Researcher  ~120k   (exploration-heavy, mostly discarded)
Planner      ~30k   (brief + repo map + issue)
Coder        ~60k   (plan step + target files + conventions)
Reviewer     ~40k   (diff + issue + test output + conventions)
```

When Tier 2 exceeds budget for a resume, an LLM-generated **rolling summary** is
produced and stored as a `SummaryCreated` event — so summarization is itself an auditable,
replayable act, not a hidden mutation. Raw events are never deleted; the summary is an
additional projection.

**Tier 4 is the compounding asset.** Every rejected review comment, every CI failure
pattern, every "we don't do it that way here" gets written back as a durable repo fact.
The second issue in a repository should cost measurably less than the first. This is the
mechanism by which the product improves with use rather than merely being used.

---

## 10. Event Bus, Queues & Databases

### Databases

**PostgreSQL 16 — single source of truth.**

| Concern | Implementation |
|---|---|
| Event log | `events` — append-only, partitioned by month |
| Projections | `sessions`, `plans`, `tool_calls`, `approvals` — rebuildable read models |
| Repo knowledge | `repositories`, `repo_facts`, `symbols`, `conventions` |
| Vectors | `pgvector` extension, **only when semantic tier is enabled** |
| Outbox | `outbox` — transactional event publication |
| Ledger | `token_usage`, `cost_ledger` |

Rationale: one ACID store removes an entire class of consistency bug. The event log and
the projection it updates commit in the same transaction. Postgres comfortably handles
this workload well past any realistic early-stage scale, and `pgvector` removes the need
for a separate vector database.

**Redis 7 — ephemeral only.**
Prompt-fragment and retrieval caches, distributed locks (session leases), rate-limit
token buckets, live progress pub/sub for the UI. **Nothing durable lives in Redis.**
Redis loss must be survivable with only a performance penalty.

**Object storage (S3/R2)** — build logs, test output, patch artifacts, container
filesystem snapshots, evaluation traces. Anything large, immutable, and occasionally read.

### Event bus

**In-process dispatch + transactional outbox.** Not Kafka.

```
BEGIN;
  INSERT INTO events   (...);   -- the fact
  UPDATE  sessions     (...);   -- the projection
  INSERT INTO outbox   (...);   -- the intent to publish
COMMIT;
-- dispatcher polls outbox → publishes → marks sent (at-least-once)
```

This gives exactly-once *semantics* on state and at-least-once on delivery, with no
distributed transaction and no additional infrastructure. Kafka is justified only when
external consumers need a durable, replayable stream — deferred until it exists.

### Queues

| Queue | Backend | Purpose | Semantics |
|---|---|---|---|
| `session.execute` | Postgres `SELECT … FOR UPDATE SKIP LOCKED` | Session work | At-least-once, lease-based |
| `index.build` | same | Repo indexing | Idempotent, coalesced by repo |
| `notify.send` | Redis Streams | Approvals, status | Best-effort, retried |
| `eval.run` | Postgres | Benchmark jobs | Batch |

Postgres-as-queue is the right call at this scale: transactional with the event write,
trivially observable with SQL, no extra operational surface. Revisit above ~1000 jobs/sec.

---

## 11. Caching

| Layer | Store | TTL | Key | Purpose |
|---|---|---|---|---|
| **Provider prompt cache** | Provider-side | Provider-defined | Prefix hash | Largest single cost lever — see §25 |
| Retrieval results | Redis | 1 h | `repo:commit:query_hash` | Repeated searches within a session |
| Repo index | Postgres + S3 | Until commit changes | `repo:commit_sha` | Avoid full re-index |
| Symbol lookups | Redis | 24 h | `repo:commit:symbol` | Hot path in context assembly |
| Embeddings | Postgres | Until chunk changes | `content_hash` | Embeddings are expensive and content-addressed |
| Base container images | Local registry | 7 d | `lang:version:deps_hash` | Container start: minutes → seconds |
| GitHub API | Redis | 5 m + ETag | `endpoint:params` | Rate-limit conservation |

**Prompt-cache-aware prompt construction is a design constraint, not an optimization.**
Prompts are assembled **stable-prefix-first**: system prompt → repo conventions → repo
map → task → volatile tool results. Reordering these for aesthetic reasons destroys cache
hit rates and can multiply cost several-fold. This ordering is enforced by the assembler
and covered by tests.

---

## 12. Retrieval & Repository Indexing

### Vector DB or not?

> **Decision: No dedicated vector database. Lexical-first hybrid retrieval, with an
> optional embedding tier in `pgvector`.**

This is the most contrarian call in this document, so the reasoning is explicit.

**Code retrieval is not document retrieval.** The dominant queries are exact:
*where is `parseConfig` defined*, *who calls `MutateAdmission`*, *what imports this
module*, *where is this error string*. These are answered **perfectly** by ripgrep and a
symbol index, in milliseconds, at zero marginal cost, with zero hallucination risk.
Embeddings answer them approximately, slowly, and expensively.

Embeddings earn their place for exactly one query class: **natural-language intent
without known identifiers** — *"where is rate limiting handled?"* when nothing is named
`rateLimit`. That is a real and useful class, but it is the minority, and it is a
*fallback*, not the primary path.

Starting with a vector DB is the canonical over-engineering mistake in this space: it
imports embedding cost, index staleness on every commit, chunking-strategy sensitivity,
and an extra piece of infrastructure — to lose to `grep` on most queries.

**Escalation ladder:**

```
1. Symbol index      exact defs/refs        <10ms    free      ← try first
2. Lexical (ripgrep) exact/regex text       <50ms    free
3. Graph traversal   imports, callers       <100ms   free
4. Semantic (pgvector) NL intent            ~300ms   $         ← only if 1–3 thin
5. Agentic search    LLM-directed iteration  seconds  $$        ← last resort
```

### Indexing pipeline

Triggered on clone and on push webhook; incremental by changed-file set.

```
clone/pull
  → language detection (linguist heuristics)
  → tree-sitter parse per file
      → symbols (defs, refs, signatures, docstrings, line spans)
      → import edges
  → build symbol table + import graph (Postgres)
  → extract repo facts:
      build/test/lint commands (Makefile, package.json, CI config, CONTRIBUTING)
      module boundaries, ownership (CODEOWNERS), test layout
      conventions mined from last N merged PRs
  → [optional] chunk + embed for semantic tier
  → persist index @ commit_sha; publish IndexBuilt
```

Full index of a 100k-LOC repo: roughly 30–90 s. Incremental: sub-second.
Indexes are keyed by `commit_sha` and immutable, so they are cacheable and shareable
across sessions on the same commit.

### Embedding strategy (when enabled)

- **Chunking follows syntax, never fixed token windows.** One chunk = one function,
  method, or class, from tree-sitter spans. Fixed-size chunking splits functions in half
  and is the single most common cause of bad code RAG.
- **Enrich before embedding.** Each chunk is prefixed with `file path › class › function`
  plus its docstring. Bare code bodies embed poorly; the surrounding names carry most of
  the semantic signal.
- Oversized functions split at statement boundaries with overlap.
- Model: a code-specialized embedding model; dimension chosen for pgvector HNSW
  efficiency. Content-hash addressed so unchanged code is never re-embedded.
- Store: `pgvector`, HNSW index, filtered by `repo_id` and `commit_sha`.

### Hybrid fusion & context assembly

Results from lexical, symbol, graph, and semantic retrievers are merged with
**Reciprocal Rank Fusion** (rank-based, so no score normalization across incomparable
retrievers), then reranked by a cross-encoder when the candidate set is large.

The **Context Assembler** then packs the role's token budget by priority:

```
1. The issue / task statement                    (never truncated)
2. The plan step currently being executed        (never truncated)
3. Files to be modified — full text              (truncation is dangerous here)
4. Direct dependencies — signatures + docstrings
5. Relevant conventions from Tier 4 memory
6. Retrieved snippets, RRF-ranked
7. Recent tool results                           (first to be evicted)
```

Every truncation emits a `ContextTruncated` event with what was dropped — so
"the agent didn't know about X" is diagnosable after the fact instead of mysterious.

---

## 13. Tool Execution Pipeline

Every tool call traverses the same seven stages. There is no fast path around them.

```
1. PROPOSE     agent emits structured tool call
2. VALIDATE    JSONSchema; reject malformed without consuming a turn
3. AUTHORIZE   capability policy: is this tool permitted, on this path,
               in this session, at this trust level?
4. GATE        does policy require human approval? → suspend session
5. EXECUTE     dispatch into sandbox with timeout + resource limits
6. CAPTURE     stdout/stderr/exit/duration/artifacts → truncate → event
7. OBSERVE     summarized result returned to agent context
```

### Core tool surface

| Tool | Purpose | Risk |
|---|---|---|
| `read_file` | Read with line ranges | Low |
| `list_dir` / `glob` | Structure discovery | Low |
| `grep` | ripgrep-backed content search | Low |
| `find_symbol` / `find_refs` | Symbol index queries | Low |
| `edit_file` | Exact-match string replacement | **Medium** |
| `write_file` | Create/overwrite | **Medium** |
| `run_command` | Arbitrary shell in sandbox | **High** |
| `run_tests` | Structured test execution + parsing | Medium |
| `git_*` | Branch, commit, diff | Medium |
| `open_pr` | Create pull request | **Gated** |
| `ask_human` | Request clarification | — |

### Design notes

- **`edit_file` uses exact-match replacement, not line numbers.** Line numbers drift the
  moment any edit lands; exact match fails loudly instead of corrupting a different
  region. A failed match is a *good* outcome — it means the agent's model of the file was
  stale, which is exactly what you want surfaced.
- **Output truncation is aggressive and always disclosed.** A 50k-line test log is
  truncated head+tail with an explicit marker and the full artifact written to object
  storage. Silently truncating output is how agents end up confidently wrong.
- **Parallel tool calls are permitted only for read-only tools.** Writes serialize.
- **Every tool call is an event** — the trajectory is fully reconstructible.
- Per-session budgets: max calls, max wall time, max cost. Breaching one suspends the
  session rather than failing it, so a human can extend.

---

## 14. Docker Sandbox

**One container per session.** Ephemeral, resource-capped, network-restricted, disposable.

### Isolation posture

```
runtime          gVisor (runsc) default; Firecracker microVM for untrusted repos
user             non-root, no new privileges
capabilities     drop ALL
seccomp          restrictive default profile
filesystem       read-only root; writable tmpfs at /workspace only
resources        2 CPU / 4 GB RAM / 10 GB disk (per-session tunable)
pids             capped (fork-bomb containment)
network          DENY by default
                 → egress proxy with per-session allowlist:
                   package registries, the target repo host, nothing else
                 → all egress logged and attributed to the session
timeouts         per command (default 120 s) and per session (default 60 min)
```

### Rationale

Model-generated shell commands are, from a threat-modeling perspective, **untrusted
input**. Prompt injection through a repository's own README or issue text is a real and
demonstrated attack: a malicious issue can instruct the agent to exfiltrate secrets.
The sandbox is what makes that attack merely annoying instead of catastrophic. Default
network-deny is the single highest-value control, because exfiltration requires egress.

### Lifecycle

```
provision  → base image by detected language/version (warm pool for common stacks)
setup      → mount workspace, restore snapshot if resuming, install deps
                (dependency install cached by lockfile hash)
execute    → stream commands; each returns exit/stdout/stderr/duration
snapshot   → on checkpoint: filesystem diff → object storage
teardown   → extract final git diff, destroy container, never reuse
```

**Snapshots are what make long-running tasks and resume real.** A session suspended for
human approval at 2am doesn't hold a container for eight hours — it snapshots, releases
the container, and rehydrates on approval. Container-hours are a major cost line, and
this converts them from wall-clock to active-work.

---

## 15. GitHub Integration

**Auth: GitHub App**, not a personal access token. Per-installation tokens, scoped
permissions, higher rate limits, revocable, and attributable in the audit log.

Required scopes: `contents:write`, `pull_requests:write`, `issues:read`,
`checks:read`, `metadata:read`.

**Ingress** via webhooks — `issues.labeled` (e.g. `wright:go`), `issue_comment.created`
(`@wright fix this`), `pull_request_review.submitted` (address review feedback),
`check_suite.completed` (react to CI). Signatures HMAC-verified; deliveries deduplicated
by ID; handlers idempotent.

**Clone strategy** scales with repo size: full clone for small repos, `--filter=blob:none`
partial clone for large ones, sparse checkout when the plan touches a known subtree.
Bare-repo cache shared across sessions on the same repository.

**PR construction** is a product surface, not an afterthought. Body includes: what
changed and why, link to the originating issue, the plan that was executed, test evidence
(before/after), explicit list of what was *not* addressed, and — non-negotiably — an
**AI-assistance disclosure**. A growing number of projects require this in CONTRIBUTING;
omitting it burns maintainer trust permanently and is the fastest way to get an agent
banned from a community.

**Review response loop:** review comments arrive as webhook events, are appended to the
session log, and reopen the session in `CODING` with the reviewer's comments as new
constraints. The PR is a long-lived conversation, not a fire-and-forget artifact.

---

## 16. Model Abstraction Layer

### Anti-goal

**Do not build a universal LLM interface that flattens providers to their common
denominator.** That approach discards exactly the features that matter — prompt caching,
extended thinking, provider-native tool-use semantics, structured outputs.

### Instead: capability tiers

Agents request a **capability**, never a model name:

```
FAST_CHEAP        high-volume classification, summarization, search triage
BALANCED          general reasoning
FRONTIER          planning, review, hard debugging
CODE_SPECIALIZED  patch generation
EMBEDDING         retrieval
```

The router resolves capability → concrete model using configuration, current health,
cost policy, and per-session overrides. Swapping a provider is a config change, not a
code change. This is what makes "support multiple LLM providers" a one-line requirement
instead of a rewrite.

### Provider adapters

Each adapter implements: streaming completion, native tool-use, structured output,
token counting, prompt-cache control, error classification. Providers expose their
**native** capabilities; the router knows which tiers each can serve. A provider lacking
prompt caching is simply priced higher by the router.

### Failover

```
primary → (retryable error / timeout / 429 exhausted) → secondary → tertiary
```

Failover **crosses providers**, since correlated outages are the common case. Circuit
breaker per provider: N consecutive failures opens it for a cooldown. Every failover is
an event, so silent quality degradation from persistent fallback is visible in metrics
rather than discovered in an incident review.

### Accounting

Every call records model, input/output/cached tokens, latency, cost, session, role, and
outcome. This powers the budget enforcer, per-customer billing, and the cost regressions
in the eval suite (§22). **Cost per resolved issue is the headline business metric** and
must be measurable per-session from day one.

---

## 17. MCP & Plugin System

**MCP is the primary extension mechanism.** Rather than inventing a proprietary plugin
API, Wright is an MCP *client*: any MCP server — Sentry, Jira, Postgres, internal
company tooling — becomes available as agent tools with no Wright-side code.

```
config declares servers → client connects (stdio | HTTP/SSE)
  → capability negotiation → tools registered into the registry
  → namespaced (mcp.<server>.<tool>) to prevent collisions
  → subject to the same VALIDATE → AUTHORIZE → GATE pipeline as builtins
```

**MCP servers are untrusted by default.** They run in their own sandbox with an explicit
capability grant; a misbehaving or compromised server cannot exceed its declared
permissions. Tool *descriptions* returned by a server are treated as untrusted content —
a server that returns instruction-shaped text in a tool description is attempting prompt
injection, and descriptions are therefore fenced when inserted into prompts.

Native plugin points beyond MCP, for things MCP doesn't model: custom retrieval backends,
language-specific analyzers, approval-policy providers, notification sinks, and custom
role implementations.

---

## 18. Security & Secrets

### Threat model

| Threat | Control |
|---|---|
| Prompt injection via issue/README/code comments | Untrusted content fenced and labeled in prompts; agents instructed that repo content is data not instruction; **network-deny sandbox** so exfiltration fails even if injection succeeds |
| Malicious repo code executed during test runs | gVisor/Firecracker isolation; no host mount; ephemeral container |
| Secret exfiltration | Secrets never enter the sandbox or any prompt; egress allowlist; output scanning before events are persisted |
| Agent pushes to protected branches | Branch policy enforced in `vcs`, not in the prompt; PRs only, never direct push |
| Destructive git operations | `force-push`, `reset --hard`, history rewrite are non-existent tools |
| Supply-chain injection via added deps | Dependency additions are an approval-gated diff class |
| Runaway cost | Hard per-session budget; breach suspends |
| Tenant data leakage | Per-tenant DB row-level security; per-session sandboxes; per-installation GitHub tokens |

### Secrets management

- External secret manager (Vault / AWS Secrets Manager / SOPS) — never in env files,
  never in the image, never in the repository.
- **Short-lived credentials only.** GitHub App installation tokens (~1h), rotated LLM
  keys, per-session scoped credentials.
- **The sandbox never receives a secret.** Git operations requiring auth are performed by
  the host-side `vcs` module against the sandbox's produced diff. The agent can *write* a
  patch; it cannot *push* one.
- **Egress and log scanning**: outputs are scanned for credential patterns before being
  written to events, logs, or PR bodies. A matched secret is redacted and raises an alert.

---

## 19. Reliability

### Retry

Retries are classified, never blind:

| Class | Example | Strategy |
|---|---|---|
| Transient infra | 429, 503, timeout | Exponential backoff + full jitter, ≤5 attempts, cross-provider failover |
| Malformed output | Schema validation failure | Re-prompt with the validation error, ≤3 attempts |
| Tool failure | Command exit ≠ 0 | Return to agent as an *observation* — this is normal agent operation, not an error |
| Semantic failure | Tests fail after patch | Enter `DEBUGGING`; capped iterations, then replan |
| Persistent semantic failure | Replans exhausted | Escalate to human with full trajectory |

**The critical distinction:** infrastructure errors are retried by the *platform*; task
failures are handled by the *agent*. Conflating them produces systems that silently retry
their way through a fundamentally wrong approach — burning budget to arrive nowhere.

Every retry loop has a hard cap. Uncapped loops are the primary way autonomous agents
convert a small bug into a large invoice.

### Rate limiting

Multi-level token buckets: per-provider (respecting published TPM/RPM), per-tenant
(fairness), per-session (blast radius), per-GitHub-installation (respecting GitHub's
limits, with ETag conditional requests to conserve quota). Adaptive: observed 429s
tighten the local bucket below the nominal limit, since provider limits are frequently
lower in practice than documented.

### Checkpointing

A checkpoint = `(event seq, sandbox FS snapshot, git state, memory summary)`.
Taken after each plan step, before every destructive operation, on approval gates, and
on a periodic timer. Cheap because the event log already exists — only the filesystem
snapshot costs anything.

### Error recovery & session resume

```
worker crash        → lease expires → another worker claims → replay → rehydrate sandbox
sandbox death       → restore last FS snapshot → replay tool calls since → continue
provider outage     → failover chain → if all down, suspend (not fail) → resume later
budget exceeded     → suspend, notify, await human extension
approval timeout    → suspend indefinitely; resumable for N days
poison event        → quarantine, alert, halt that session only
```

**Suspension is a first-class state, distinct from failure.** A session waiting eight
hours for approval holds no container, no worker, and no LLM context — only rows in
Postgres and a blob in S3. This is what makes "long-running tasks" economically viable
rather than merely technically possible.

---

## 20. Human Approval Workflow

### Policy as code, not as prompt

Gates are evaluated by a policy engine (OPA/Rego or CEL) against structured facts about
the proposed action. The agent does not decide what is risky; **policy does**, and policy
is versioned, tested, and auditable.

Default gate conditions:

```
- opening a pull request                       (always, initially)
- diff touches > N files or > M lines
- diff touches paths matching sensitive globs  (auth/, migrations/, infra/, CI config)
- adding or upgrading a dependency
- modifying CI/CD configuration
- deleting files
- estimated remaining cost exceeds threshold
- plan confidence below threshold
- any operation the tenant marked always-gated
```

### Mechanics

An `ApprovalRequested` event suspends the session. Notification goes out (Slack / GitHub
comment / email / webhook) containing the proposed action, its diff, the plan context,
and the reasoning. A human responds **approve / reject / modify / always-allow-this-class**.
Resolution appends `ApprovalGranted|Denied`, and the session resumes from the checkpoint.

`modify` is the underrated option: the human edits the plan or the patch, and the
correction is written back into **Tier 4 memory** as a repo convention — so the same
correction is not needed twice. Approval is thus a *training signal*, not merely a brake.

**Autonomy tiers** are configurable per repository, and the intended adoption path is
that a team starts at Supervised and earns its way to Autonomous with evidence from its
own history:

```
SUPERVISED   every plan and every PR gated
STANDARD     PRs gated, plans automatic          ← sane default
TRUSTED      only sensitive-path changes gated
AUTONOMOUS   fully automatic; audit after the fact
```

---

## 21. Observability

**Structured logging** — JSON, correlation IDs propagated across session → role → tool
call. Secrets redacted at the logger, not by convention. Levels used honestly: `ERROR`
means a human must look.

**Tracing** — OpenTelemetry throughout. One trace per session; spans per role invocation,
LLM call, tool call, and sandbox command, with token counts and cost as span attributes.
An LLM call whose span shows 90k input tokens and 40 output tokens is a context-assembly
bug, and it should be visible as such at a glance.

**Metrics (Prometheus)** — the ones that actually matter:

```
Business   issues_resolved_total, pr_merge_rate, cost_per_resolved_issue,
           human_intervention_rate, time_to_pr
Quality    review_rejection_rate, test_pass_first_try_rate, replan_rate,
           context_truncation_rate
Cost       tokens_by_role_and_model, cache_hit_ratio, sandbox_container_hours
Health     provider_error_rate, failover_rate, queue_depth, session_duration,
           sandbox_start_latency
```

**Trajectory viewer** — a UI that renders a session's event log as a readable timeline:
every prompt, response, tool call, and diff, with the ability to replay from any point.
This is the single most valuable internal tool the project will have. Debugging agents by
reading raw logs does not scale past the first week.

**Alerting** on: cost-per-issue regression, merge-rate drop, provider failover sustained
beyond a window, queue depth growth, sandbox escape signals (any egress outside allowlist).

---

## 22. Evaluation Framework

Without evaluation, prompt changes are superstition. This is not optional infrastructure.

### Suites

| Suite | Contents | Purpose |
|---|---|---|
| **SWE-bench (Verified subset)** | Public real-world issue→patch pairs | External comparability |
| **Golden set** | 100–300 curated issues across languages, sizes, difficulty | Primary regression gate |
| **Adversarial** | Prompt injection in issues/READMEs, malicious repos, secret-bait | Security regression |
| **Long-horizon** | Multi-step tasks requiring >30 tool calls | Tests degradation over time |
| **Cost** | Fixed tasks with cost ceilings | Catches token regressions |

### Scoring — outcome *and* trajectory

Outcome alone is insufficient; an agent that stumbles to the right answer in 90 steps is
not equivalent to one that gets there in 12.

```
Outcome      resolved (tests pass + human rubric), patch similarity to reference,
             no regressions introduced
Trajectory   steps taken, tokens consumed, cost, wall time, retries, replans,
             unnecessary file reads, tool error rate
Quality      review rejection rate, convention adherence, test coverage delta
Safety       no gate bypassed, no secret leaked, no egress violation
```

### Practice

Per-role eval suites in addition to end-to-end — a Researcher regression is diagnosable
only if the Researcher is scored in isolation.

CI runs the fast golden subset on every PR; the full suite nightly. **A merge is blocked
on regression in resolve rate, cost per issue, or any safety metric.**

Production runs are sampled into a review queue with human rubric grading, and graded
failures are promoted into the golden set — so the eval suite grows from real failures
rather than from imagination. Shadow mode allows a candidate configuration to run against
live traffic without acting, comparing trajectories offline before promotion.

---

## 23. Testing Strategy

| Level | Scope | Notes |
|---|---|---|
| **Unit** | Reducers, parsers, schema validation, policy evaluation, context assembler | Reducers are pure functions — exhaustively testable, and the highest-value tests in the codebase |
| **Contract** | Provider adapters, GitHub client, MCP client | Recorded fixtures; live smoke tests on a schedule |
| **Integration** | Real Postgres, real Redis, real Docker; **mocked LLM** | Deterministic scripted model responses exercise the full pipeline without cost or flakiness |
| **End-to-end** | Real everything against fixture repositories | Small, slow, few — the smoke test |
| **Evaluation** | §22 | Statistical, not pass/fail per run |
| **Chaos** | Kill workers mid-session, kill sandboxes, blackhole providers, exhaust budgets | **Resume correctness cannot be trusted unless it is continuously proven** |
| **Security** | Injection corpus, egress escape attempts, secret-leak detection | Blocks merge on failure |

The mocked-LLM integration tier is the workhorse. Determinism means the orchestrator,
tool broker, sandbox, and recovery paths are all covered by fast, free, reliable tests —
leaving the expensive nondeterministic tests to the eval suite where they belong.

---

## 24. Deployment, CI/CD & Scaling

### Topology

```
                    ┌──────── Load Balancer / Ingress ────────┐
                    │                                         │
              wright-api (N replicas, HPA on RPS)
                    │
        ┌───────────┼────────────┬──────────────┐
        ▼           ▼            ▼              ▼
   Postgres     Redis      wright-worker    wright-indexer
   (primary   (cluster)   (M replicas,      (K replicas,
   + replica)              KEDA on queue)    CPU-scaled)
                                 │
                          ┌──────┴──────┐
                          ▼             ▼
                    Sandbox pods   Object storage
                    (1/session,    (S3/R2)
                     gVisor RuntimeClass)
```

Kubernetes. API and workers are stateless Deployments; sandboxes are Jobs with a
`RuntimeClass` of `gvisor` and strict resource limits, on a dedicated, tainted node pool
that runs nothing else.

### CI/CD

```
PR:      lint → unit → integration (mocked LLM) → security scan
         → fast eval subset → cost regression check
Merge:   build multi-arch images → sign (cosign) → SBOM → push
         → deploy staging → smoke → full eval suite
Release: canary 5% → watch resolve-rate & cost & error-rate 30 min
         → progressive rollout → auto-rollback on regression
Nightly: full eval, adversarial suite, dependency audit, chaos run
```

**Prompts are versioned artifacts and deploy like code** — same review, same eval gate,
same canary, same rollback. Treating prompts as configuration that can be hot-edited in
production is how quality regressions become mysteries.

### Scaling

| Dimension | Strategy |
|---|---|
| Concurrent sessions | Horizontal workers, KEDA-scaled on queue depth |
| Sandbox capacity | Dedicated node pool; warm image pool; snapshot-and-release on suspend |
| Indexing | Separate pool; incremental; shared index cache per `commit_sha` |
| Postgres | Read replicas for projections; partition `events` by month; archive to S3 |
| LLM throughput | Multi-provider routing spreads load across independent rate limits |
| Multi-region | Sandboxes near the repo host; Postgres primary single-region initially |

The realistic first bottleneck is **provider rate limits**, not compute. This is why
multi-provider routing is architectural rather than a nice-to-have.

### Cloud

Cloud-agnostic by construction: Kubernetes, Postgres, S3-compatible storage, OCI images.
No managed-service lock-in on the critical path. Self-hostable in full — which is a
genuine requirement for the enterprise segment, since many organizations will not send
proprietary source to a third-party service under any commercial terms.

---

## 25. Cost Optimization

Ordered by measured impact:

**1. Prompt caching — the dominant lever.**
Stable-prefix-first prompt construction (§11) with explicit cache breakpoints after the
system prompt, repo conventions, and repo map. In a multi-turn agent loop the prefix is
re-sent every turn; caching it converts the largest cost line into a small one. Cache hit
ratio is a monitored SLO, not an incidental metric.

**2. Model routing (§16).** Researcher on a cheap fast model. Typical 40–60% reduction.

**3. Retrieval over context stuffing.** Sending 100k tokens of "relevant" files because
selection is hard is the most common and most expensive failure mode in agent systems.
Precise retrieval is cheaper *and* more accurate — the two goals are aligned, not opposed.

**4. Complexity-tiered fast path.** Classify the issue first. A typo fix should not
invoke four roles. Route trivial issues to a single-agent path; reserve the full pipeline
for issues that need it.

**5. Early termination.** Detect non-convergence — repeated identical tool calls, no
diff progress across iterations, oscillating patches — and escalate to a human rather
than burning the remaining budget proving the agent is stuck.

**6. Sandbox economics.** Warm pools amortize cold start; snapshot-and-release on
suspension converts idle wall-clock into zero cost; aggressive dependency caching by
lockfile hash.

**7. Index reuse.** Index once per `commit_sha`, share across all sessions on that commit.

**8. Batch offline work.** Embeddings, evals, and index builds use batch APIs where
available at substantially lower rates.

**North-star metric: cost per *merged* pull request.** Not per token, not per session —
per unit of delivered value. It is the only number that makes architectural trade-offs
comparable, and it is the number an investor will ask for.

---

## 26. ASCII Architecture Diagram

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                          INGRESS / CONTROL PLANE                             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║   GitHub Webhooks      REST / WebSocket API           CLI                    ║
║          │                     │                       │                     ║
║          └─────────────────────┼───────────────────────┘                     ║
║                                ▼                                             ║
║                      ┌──────────────────┐                                    ║
║                      │   wright-api     │  authn/z · validation · rate limit  ║
║                      └────────┬─────────┘                                    ║
╚═══════════════════════════════│══════════════════════════════════════════════╝
                                ▼
                    ┌───────────────────────┐
                    │   SESSION QUEUE       │  Postgres SKIP LOCKED, leased
                    └───────────┬───────────┘
                                ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                              AGENT PLANE  (wright-worker)                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                       ┌────────────────────────┐                             ║
║                       │     ORCHESTRATOR       │                             ║
║                       │  reduce(events)→state  │                             ║
║                       │  dispatch · budget     │                             ║
║                       │  fast-path tiering     │                             ║
║                       └───┬────┬────┬────┬─────┘                             ║
║          ┌────────────────┘    │    │    └────────────────┐                  ║
║          ▼                     ▼    ▼                     ▼                  ║
║   ┌────────────┐       ┌────────────┐  ┌──────────┐  ┌──────────┐            ║
║   │ RESEARCHER │       │  PLANNER   │  │  CODER   │  │ REVIEWER │            ║
║   │ FAST_CHEAP │       │  FRONTIER  │  │   CODE   │  │ FRONTIER │            ║
║   │            │       │            │  │          │  │          │            ║
║   │ explores → │       │ brief →    │  │ step →   │  │ diff →   │            ║
║   │ 2k brief   │       │ plan       │  │ patch    │  │ verdict  │            ║
║   └─────┬──────┘       └─────┬──────┘  └────┬─────┘  └────┬─────┘            ║
║         │                    │              │             │                  ║
║         └────────────────────┴──────┬───────┴─────────────┘                  ║
║                                     ▼                                        ║
║                       ┌──────────────────────────┐                           ║
║                       │      TOOL BROKER         │                           ║
║                       │ validate→authorize→gate  │                           ║
║                       └────┬────────────────┬────┘                           ║
╚════════════════════════════│════════════════│════════════════════════════════╝
              ┌──────────────┘                └──────────────┐
              ▼                                              ▼
╔═════════════════════════════════╗          ╔═══════════════════════════════╗
║    RETRIEVAL / CONTEXT          ║          ║      EXECUTION PLANE          ║
╠═════════════════════════════════╣          ╠═══════════════════════════════╣
║  ┌──────────┐  ┌─────────────┐  ║          ║   ┌───────────────────────┐   ║
║  │ Symbol   │  │  Lexical    │  ║          ║   │  SANDBOX (per session)│   ║
║  │ Index    │  │  (ripgrep)  │  ║          ║   │  gVisor · net-deny    │   ║
║  └──────────┘  └─────────────┘  ║          ║   │  ro-root · tmpfs ws   │   ║
║  ┌──────────┐  ┌─────────────┐  ║          ║   │                       │   ║
║  │ Import   │  │  Semantic   │  ║          ║   │  clone · build · test │   ║
║  │ Graph    │  │ (pgvector)* │  ║          ║   │  edit  · run  · diff  │   ║
║  └──────────┘  └─────────────┘  ║          ║   └───────────┬───────────┘   ║
║        └──── RRF fusion ────┐   ║          ║               │ snapshot      ║
║                             ▼   ║          ╚═══════════════│═══════════════╝
║              ┌──────────────────┐║                         │
║              │CONTEXT ASSEMBLER │║                         │
║              │ budget · pack    │║                         │
║              └──────────────────┘║                         │
╚═════════════════════════════════╝                         │
                                                             │
╔════════════════════════════════════════════════════════════│═══════════════╗
║                            DATA PLANE                      ▼               ║
╠════════════════════════════════════════════════════════════════════════════╣
║  ┌────────────────────────┐  ┌──────────────┐  ┌────────────────────────┐  ║
║  │      POSTGRES          │  │    REDIS     │  │    OBJECT STORE        │  ║
║  │  events (append-only)  │  │  cache       │  │  logs · artifacts      │  ║
║  │  projections           │  │  locks       │  │  FS snapshots          │  ║
║  │  repo facts · symbols  │  │  buckets     │  │  eval traces           │  ║
║  │  outbox · cost ledger  │  │  pub/sub     │  │                        │  ║
║  │  pgvector*             │  │              │  │                        │  ║
║  └────────────────────────┘  └──────────────┘  └────────────────────────┘  ║
╚════════════════════════════════════════════════════════════════════════════╝

╔════════════════════════════════════════════════════════════════════════════╗
║                          INTEGRATION PLANE                                 ║
╠════════════════════════════════════════════════════════════════════════════╣
║  MODEL ROUTER          GITHUB (App)        MCP SERVERS       NOTIFY        ║
║  ├ Anthropic           ├ REST/GraphQL      ├ sandboxed       ├ Slack       ║
║  ├ OpenAI              ├ webhooks          ├ namespaced      ├ Email       ║
║  ├ Google              ├ short-lived       ├ capability-     ├ GitHub      ║
║  └ Local/OSS           └   tokens          └   scoped        └ Webhook     ║
╚════════════════════════════════════════════════════════════════════════════╝
        ▲                                                          ▲
        │  OpenTelemetry traces · Prometheus metrics · JSON logs    │
        └──────────────── OBSERVABILITY (cross-cutting) ────────────┘

                        * optional tier — see §12
```

---

## 27. Sequence Diagram: Issue → Pull Request

```
GitHub  API   Orch    Research  Plan   Coder  Review  Broker  Sandbox  Model  DB
  │      │      │        │       │       │      │       │        │       │     │
  │ issues.labeled "wright:go"   │       │      │       │        │       │     │
  ├─────►│      │        │       │       │      │       │        │       │     │
  │      │ verify HMAC · dedupe  │       │      │       │        │       │     │
  │      ├──────────────────────────────────────────────────────────────────►│ │
  │      │      │  SessionCreated · IssueIngested                            │ │
  │      │ enqueue                                                           │ │
  │      ├─────►│        │       │       │      │       │        │       │     │
  │      │      │                                                             │
  │      │ ═══ PHASE 1: SETUP ═══════════════════════════════════════════════ │
  │      │      │ provision sandbox                                           │
  │      │      ├───────────────────────────────────────────►│       │     │  │
  │      │      │ clone (partial, blob:none)                 │       │     │  │
  │◄─────┼──────┼────────────────────────────────────────────┤       │     │  │
  │      │      │ index: tree-sitter → symbols → graph → facts│       │     │  │
  │      │      ├──────────────────────────────────────────────────────────►│ │
  │      │      │        IndexBuilt @ commit_sha                             │ │
  │      │      │                                                             │
  │      │ ═══ PHASE 2: RESEARCH ════ (FAST_CHEAP) ══════════════════════════ │
  │      │      ├───────►│       │       │      │       │        │       │     │
  │      │      │        │ ┌── loop: explore ──────────────────────────────┐  │
  │      │      │        │ │ grep / find_symbol / read_file                │  │
  │      │      │        ├─┼──────────────────────────►│        │       │  │  │
  │      │      │        │ │            validate·authorize                 │  │
  │      │      │        │ │                           ├───────►│       │  │  │
  │      │      │        │ │◄──────────────────────────┴────────┤       │  │  │
  │      │      │        │ │ observe → next query                         │  │
  │      │      │        │ └── ~150k tokens consumed ────────────────────┘  │
  │      │      │        │ emit STRUCTURED BRIEF (~2k tokens)                │
  │      │      │◄───────┤  { root_cause, files[], symbols[], tests[],       │
  │      │      │        │    conventions[], risks[], open_questions[] }     │
  │      │      ├──────────────────────────────────────────────────────────►│ │
  │      │      │        ResearchCompleted    ◄── brief persisted            │ │
  │      │      │                                                             │
  │      │ ═══ PHASE 3: PLAN ════════ (FRONTIER) ════════════════════════════ │
  │      │      ├────────────────►│       │      │       │        │       │  │
  │      │      │   input: brief + issue + repo map (NOT raw exploration)   │  │
  │      │      │◄────────────────┤ ordered steps + files + test strategy   │  │
  │      │      ├──────────────────────────────────────────────────────────►│ │
  │      │      │        PlanProposed                                        │ │
  │      │      │                                                             │
  │      │      │ ── policy gate: plan approval required? ──                  │
  │      │      │    [STANDARD tier → auto-approve]      PlanApproved         │
  │      │      │                                                             │
  │      │ ═══ PHASE 4: IMPLEMENT ═══ (CODE_SPECIALIZED) ════════════════════ │
  │      │      │ ┌── for each plan step ───────────────────────────────────┐ │
  │      │      ├─┼──────────────────►│      │       │        │       │     │ │
  │      │      │ │ context: step + target files + conventions              │ │
  │      │      │ │        edit_file (exact-match replacement)              │ │
  │      │      │ │                   ├─────────────►│        │       │     │ │
  │      │      │ │                   │  validate·authorize   │       │     │ │
  │      │      │ │                   │              ├───────►│       │     │ │
  │      │      │ │                   │◄─────────────┴────────┤ applied     │ │
  │      │      │ ├──────────────────────────────────────────────────────►│ │ │
  │      │      │ │        PatchApplied                                    │ │ │
  │      │      │ │                                                        │ │ │
  │      │      │ │ run_tests                                              │ │ │
  │      │      │ │                   ├─────────────────────►│       │     │ │
  │      │      │ │                   │◄──── FAIL: 2 tests ──┤       │     │ │
  │      │      │ ├──────────────────────────────────────────────────────►│ │ │
  │      │      │ │        TestsFailed                                     │ │ │
  │      │      │ │                                                        │ │ │
  │      │      │ │ ┌── DEBUGGING loop (cap: 3) ──────────────────────┐    │ │ │
  │      │      │ │ │ parse failure → locate → patch → re-run tests   │    │ │ │
  │      │      │ │ │                  ├──────────────►│       │      │    │ │ │
  │      │      │ │ │                  │◄─── PASS ─────┤       │      │    │ │ │
  │      │      │ │ └─────────────────────────────────────────────────┘    │ │ │
  │      │      │ ├──────────────────────────────────────────────────────►│ │ │
  │      │      │ │        TestsPassed                                     │ │ │
  │      │      │ └── checkpoint: FS snapshot + event seq ─────────────────┘ │
  │      │      │                                                             │
  │      │ ═══ PHASE 5: REVIEW ══════ (FRONTIER, independent) ═══════════════ │
  │      │      ├─────────────────────────────►│       │        │       │     │
  │      │      │  input: diff + issue + test output + conventions           │
  │      │      │  NO access to Coder's reasoning ── this is the point       │
  │      │      │◄─────────────────────────────┤ REJECT: missing edge case   │
  │      │      ├──────────────────────────────────────────────────────────►│ │
  │      │      │        ReviewRejected(reason)                              │ │
  │      │      │                                                             │
  │      │      │ ── return to CODING with reviewer feedback (cap: 2) ──      │
  │      │      ├──────────────────►│      │       │        │       │        │
  │      │      │                   │ add test + handle case                 │
  │      │      │◄──────────────────┤                                        │
  │      │      ├─────────────────────────────►│       │        │       │     │
  │      │      │◄─────────────────────────────┤ PASS                        │
  │      │      ├──────────────────────────────────────────────────────────►│ │
  │      │      │        ReviewPassed                                        │ │
  │      │      │                                                             │
  │      │ ═══ PHASE 6: APPROVAL GATE ═══════════════════════════════════════ │
  │      │      │ policy: open_pr → ALWAYS GATED (STANDARD tier)              │
  │      │      ├──────────────────────────────────────────────────────────►│ │
  │      │      │        ApprovalRequested · SessionSuspended                │ │
  │      │      │                                                             │
  │      │      │ ── snapshot FS → S3 · release sandbox · release worker ──   │
  │      │      │                                                             │
  │      │      │ notify(Slack): diff + plan + test evidence + cost           │
  │      │      │ ······················ hours may pass ····················· │
  │      │      │                                                             │
  │      │ human clicks APPROVE                                               │
  │      ├─────►│                                                             │
  │      │      ├──────────────────────────────────────────────────────────►│ │
  │      │      │        ApprovalGranted                                     │ │
  │      │      │ ── replay events → rehydrate sandbox from snapshot ──       │
  │      │      │                                                             │
  │      │ ═══ PHASE 7: SUBMIT ══════════════════════════════════════════════ │
  │      │      │ extract final diff from sandbox                             │
  │      │      │                   ├─────────────────────►│       │     │    │
  │      │      │◄──────────────────┴─── unified diff ─────┤       │     │    │
  │      │      │                                                             │
  │      │      │ HOST-SIDE git ops (sandbox never holds credentials)         │
  │      │      │ branch → commit (conventional message) → push               │
  │◄─────┼──────┤                                                             │
  │      │      │ open PR: what/why · plan · test evidence · not-addressed    │
  │      │      │          · AI-ASSISTANCE DISCLOSURE                         │
  │◄─────┼──────┤                                                             │
  │      │      ├──────────────────────────────────────────────────────────►│ │
  │      │      │        CommitCreated · PullRequestOpened · SessionDone     │ │
  │      │      │                                                             │
  │      │      │ write back to Tier 4 memory:                                │
  │      │      │   reviewer's rejection reason → durable repo convention     │
  │      │      ├──────────────────────────────────────────────────────────►│ │
  │      │      │                                                             │
  │ ═══ ONGOING: PR REVIEW LOOP ═════════════════════════════════════════════ │
  │ pull_request_review.submitted (maintainer requests changes)               │
  ├─────►│      │ reopen session in CODING with new constraints               │
  │      ├─────►│ ... cycle repeats ...                                       │
```

**Notes on the flow**

- The Researcher consumes ~150k tokens and hands the Planner ~2k. That compression is the
  architecture's central mechanism, and it is why the Planner can run on a frontier model
  affordably.
- The Reviewer deliberately never sees the Coder's chain of reasoning. Its independence is
  the source of its value; giving it the Coder's rationale would make it agree.
- Suspension releases *all* expensive resources. A session pending approval overnight costs
  storage only.
- Credentials never enter the sandbox. The sandbox produces a diff; the host performs the
  authenticated push.

---

## 28. Roadmap & Open Questions

### Phased delivery

| Phase | Scope | Proves |
|---|---|---|
| **1 — Walking skeleton** | Single agent, Docker sandbox, lexical retrieval, one provider, CLI only, no approval gates | End-to-end issue → PR is achievable at all |
| **2 — The architecture** | Four roles, event sourcing, checkpointing, resume, approval gates, GitHub App | The design in this document, working |
| **3 — Production** | Multi-provider routing, MCP, observability stack, eval harness, cost controls | Operable by someone other than the author |
| **4 — Scale** | Multi-tenant, K8s deployment, semantic tier, autonomy tiers, plugin ecosystem | It is a product, not a project |

Phase 1 should be days, not weeks. The fastest way to invalidate a design document is to
run it against one real issue.

### Open questions

1. **Handoff fidelity** is the top technical risk. Should the Coder be able to query the
   Researcher's raw artifacts on demand, or does that reintroduce the context pollution the
   brief exists to prevent? *Leaning: on-demand retrieval against research artifacts,
   never bulk inclusion.*
2. **Should the Reviewer see the tests the Coder wrote?** Independence argues no; practicality
   argues yes. *Leaning: yes for the diff, no for the reasoning.*
3. **Is the Planner separable from the Researcher?** A combined role has better context
   continuity but loses the compression boundary. Worth an A/B on the golden set.
4. **Trust bootstrapping** — how does a repository earn its way from SUPERVISED to TRUSTED?
   Proposal: automatic promotion on a rolling merge-rate threshold with no safety incidents.
5. **Multi-repo changes** are unsupported by design in v1. This is where Architecture D
   legitimately returns.
6. **Learned procedural memory could poison itself** — one wrong convention inferred from a
   single review comment propagates to every future session. Needs confidence weighting and
   decay.
```

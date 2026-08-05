# Multi-Agent Architecture for an AI Software Engineer

**Agent roster, orchestration, and communication design**
Version 0.1 · Keshav Kakani
Companion to [ARCHITECTURE.md](ARCHITECTURE.md) and [PIPELINE.md](PIPELINE.md).

---

## 1. The Central Question Nobody Asks

The prompt lists 22 candidate agents. The instinct is to build 22 agents.

**Most of them should not be agents.**

An agent is expensive. Each one costs a context window, a prompt to maintain and
evaluate, a handoff boundary where information is lost, latency, and a failure mode.
Multi-agent systems fail not because agents are bad but because **teams add agents for
conceptual tidiness rather than for a capability that requires one.**

### The test

A component earns agent status only if it needs **all three**:

1. **Its own context window** — it consumes information the others must not see, or
   produces so much intermediate noise that isolating it is the point.
2. **A tool loop** — it iterates: act, observe, decide, act again. If it's a single
   prompt→response with no tools, it is a *function call to an LLM*, not an agent.
3. **Independent judgment** — its value comes from reaching a conclusion the others
   didn't, ideally by being able to *disagree*.

Fails any one → it's a tool, a deterministic component, or a section of another agent's
prompt.

### Applying the test to all 22

| Candidate | Own context | Tool loop | Independent judgment | **Verdict** |
|---|:---:|:---:|:---:|---|
| **Planner** | ✓ | ✗ | ✓ | **AGENT** |
| **Repository Analyst** | ✓ | ✓ | ~ | → merge into Researcher |
| **Issue Analyst** | ~ | ✗ | ~ | → merge into Researcher |
| **Retriever** | ✓ | ✓ | ✗ | → merge into Researcher |
| **Dependency Graph Builder** | ✗ | ✗ | ✗ | **DETERMINISTIC** — it's a parser |
| **Task Decomposer** | ✗ | ✗ | ✗ | **DETERMINISTIC** — it's a toposort |
| **Coder** | ✓ | ✓ | ✓ | **AGENT** |
| **Reviewer** | ✓ | ✓ | ✓ | **AGENT** |
| **Test Engineer** | ~ | ✓ | ~ | → merge into Coder + deterministic runner |
| **Debugger** | ✓ | ✓ | ✓ | **AGENT** |
| **Security Reviewer** | ✗ | ✗ | ~ | **TOOL** (semgrep/gitleaks) + Reviewer prompt section |
| **Performance Engineer** | ✗ | ✗ | ~ | **TOOL** (benchmark harness) + conditional escalation |
| **Documentation Writer** | ✗ | ✗ | ✗ | → Coder plan step |
| **Commit Writer** | ✗ | ✗ | ✗ | **LLM FUNCTION** — one cheap call |
| **PR Writer** | ✗ | ✗ | ✗ | **LLM FUNCTION** — one cheap call |
| **Memory Manager** | ✗ | ✗ | ✗ | **DETERMINISTIC SUBSYSTEM** |
| **Tool Router** | ✗ | ✗ | ✗ | **DETERMINISTIC** — registry + policy lookup |
| **Execution Manager** | ✗ | ✗ | ✗ | **DETERMINISTIC** — sandbox lifecycle |
| **Supervisor** | ✗ | ✗ | ✗ | **DETERMINISTIC** — the orchestrator reducer |
| **Critic** | — | — | — | **DUPLICATE of Reviewer** |
| **Reflection Agent** | — | — | — | **DUPLICATE of Reviewer** |
| **Judge** | — | — | — | **DUPLICATE of Reviewer** (except in eval, see §9) |

**Critic, Reflection Agent, Judge, and Reviewer are four names for one capability:
*something that evaluates the work and can reject it*.** Instantiating all four is the
canonical multi-agent-paper mistake. It triples cost and creates a diffusion-of-
responsibility failure where each defers to the others and nothing is actually caught.

### Result: 5 agents, not 22

```
AGENTS (5)              LLM FUNCTIONS (2)      DETERMINISTIC (7)
  Researcher              Commit Writer          Orchestrator/Supervisor
  Planner                 PR Writer              Task Decomposer
  Coder                                          Dependency Graph Builder
  Reviewer                                       Tool Router
  Debugger                                       Execution Manager
                                                 Memory Manager
                                                 Security/Perf scanners
```

Every deletion above is a deletion of a failure mode. **The strongest thing this design
does is refuse to build 17 components.**

---

## 2. The Five Agents

### Researcher — `FAST_CHEAP`

**Absorbs:** Repository Analyst, Issue Analyst, Retriever.

Why merged: all three do the same thing — consume large volumes of repository and issue
material and emit a small structured summary. Splitting them creates three handoff
boundaries where one suffices, and worse, the Issue Analyst's output is *useless without*
the repo context the Repository Analyst holds. They must share a context window to work.

**In:** issue, repo index, symbol graph, dep graph, Repo Facts
**Out:** structured `ResearchBrief` (~2k tokens)
**Loop:** grep → read → find_refs → expand graph → repeat until confident or budget hit
**Budget:** ~120k tokens in, ~2k out

The compression ratio (60:1) is the single most important number in this architecture.
It is what lets the Planner and Reviewer run on frontier models affordably.

```
ResearchBrief {
  root_cause_hypothesis, change_sites[], affected_symbols[],
  sibling_implementations[],       ← the convention to imitate
  covering_tests[], blast_radius, conventions[],
  open_questions[], confidence, evidence_refs[]
}
```

**`sibling_implementations` is the highest-value field and the one nobody ships.** For
*"fix the nil-map write in the mthreads device,"* the most useful context is the thirteen
other vendor devices that already handle it correctly. Convention is learned from
siblings, not from CONTRIBUTING.md.

---

### Planner — `FRONTIER`

**In:** `ResearchBrief`, Issue Intent, Repo Facts
**Out:** ordered, validated `Plan`
**Loop:** none — single call with extended thinking
**Budget:** ~30k in, ~3k out

No tool loop because it needs no new information; the Researcher already gathered it.
Adding tools here would let the Planner re-explore, which reintroduces exactly the
context pollution the Researcher exists to prevent.

Its output is **deterministically validated** before acceptance: every referenced symbol
and file path must exist in the index. Hallucinated paths are common and cost nothing to
catch.

---

### Coder — `CODE_SPECIALIZED`

**Absorbs:** Test Engineer, Documentation Writer.

Why merged: writing the fix and writing its test are the same act of understanding,
separated only by file. A separate Test Engineer must re-derive the entire mental model
the Coder just built — pure duplicated cost, and it produces tests that are *independent
of* the implementation in the wrong way (testing what it guesses the code does rather
than what the issue requires).

**In:** one plan step, target files (full text), conventions, sibling examples
**Out:** exact-match edits
**Loop:** read → edit → format → lint → repeat
**Budget:** ~60k in per step

Deliberately does **not** receive the research transcript or full plan history.

---

### Reviewer — `FRONTIER`

**Absorbs:** Critic, Reflection Agent, Judge, Security Reviewer (judgment portion),
Performance Engineer (judgment portion).

**In:** diff, Issue Intent, test results, static-analysis delta, conventions
**Out:** `PASS | REJECT(reasons[])`
**Loop:** limited — may read files around the diff
**Budget:** ~40k in

**Critical design constraint: the Reviewer never sees the Coder's reasoning.** It gets
the diff, the issue, and the evidence. Independence is the entire source of its value —
give it the Coder's chain of thought and it will agree with it, because agreeing is what
shared premises produce. This is the single most important prompt-boundary decision in
the system.

It must run on a model **at least as strong** as the Coder. A weaker reviewer rubber-stamps,
which is worse than no reviewer because it manufactures false confidence.

Reviews against explicit criteria, in order:
```
1. Does it satisfy the acceptance criteria?     (correctness)
2. Does it introduce regressions?               (safety)
3. Is it minimal?                               (scope — reject refactor creep)
4. Does it match repo conventions?              (mergeability)
5. Are the tests meaningful?                    (not just coverage theater)
6. Security/performance findings from scanners  (delta only)
```

---

### Debugger — `FRONTIER`

Separated from the Coder despite the overlap, because it needs a **fundamentally different
prompt discipline**: hypothesis-first, revert-before-retry, one change per iteration.
Folding it into the Coder produces patch-stacking — each iteration piling a new guess on
top of the last failed one until the diff is incoherent.

**In:** structured test failure, diff, source at failure site, **previous failed hypotheses**
**Out:** `Diagnosis{hypothesis, evidence, minimal_fix}`
**Loop:** localize → hypothesize → revert → patch → verify
**Budget:** ~40k in per iteration, capped at 3 iterations

That `previous failed hypotheses` input is what prevents it from proposing the same wrong
fix three times.

---

## 3. Should Agents Communicate Directly?

> **No. Star topology through the Orchestrator, with typed contracts.**

### Rejected: direct agent-to-agent messaging

Free-form inter-agent conversation (the AutoGen "group chat" pattern) fails here:

- **Token cost is quadratic-ish.** Every agent seeing every message defeats context
  isolation, which is the entire reason to have multiple agents.
- **Non-determinism compounds.** Who speaks next is itself an LLM decision, so the same
  input produces different execution paths. Unreproducible bugs.
- **No natural termination.** Conversations meander; you need artificial turn caps that
  cut off mid-reasoning.
- **Unauditable.** "Why did it do that?" requires reading a transcript where five agents
  talked past each other.

Direct messaging is appealing because it looks like a human team. But human teams have
shared persistent context, social accountability, and the ability to stop talking. LLM
agents have none of these.

### Adopted: star topology, typed contracts

```
                    ┌──────────────────┐
                    │   ORCHESTRATOR   │  ← deterministic reducer
                    │  reduce(events)  │     NOT an LLM
                    └──┬───┬───┬───┬───┘
           ┌───────────┘   │   │   └──────────┐
           ▼               ▼   ▼              ▼
     Researcher       Planner  Coder      Reviewer / Debugger
```

Every agent's input is assembled by the Orchestrator; every output is a
**schema-validated structured artifact** persisted as an event. Agents never address each
other. Properties this buys:

- **Deterministic control flow** — replayable, testable, debuggable.
- **Context isolation is enforced structurally**, not by convention.
- **Every handoff is validated** — a malformed brief fails at the boundary rather than
  confusing the next agent silently.
- **Agents are independently testable** — feed a fixed brief, assert on the plan.

**The cost:** the Orchestrator must know the workflow. That's fine — the workflow is
*known*, and encoding it in ~800 lines of deterministic reducer is strictly better than
discovering it at runtime through LLM negotiation.

---

## 4. Should They Use Shared Memory?

> **Partially — and the distinction is the whole answer.**

A naive blackboard where all agents read and write a common scratchpad **destroys context
isolation**, which is the architecture's reason for existing. If the Coder can read the
Researcher's raw exploration, you've rebuilt the single-agent context problem with extra
steps.

**Shared, read-only, structured:**
- Repo Facts (S6) — architecture, build commands, conventions
- Symbol graph, dependency graph, index
- Tier-4 procedural memory — learned repo conventions
- The current Plan

**Private per agent:**
- Working context (the assembled prompt)
- Tool-call scratch results
- Reasoning traces

**Passed explicitly as typed artifacts:**
- `ResearchBrief`, `Plan`, `Diff`, `ReviewVerdict`, `Diagnosis`

The rule: **shared memory holds *facts*; typed contracts carry *conclusions*; nothing
shares *reasoning*.** Facts are cheap and safe to share. Reasoning is expensive and
contaminating.

---

## 5. Graph, Event Bus, or Both?

**Both, at different layers — they are not alternatives.**

| Layer | Mechanism | Why |
|---|---|---|
| **Workflow topology** | Explicit state graph | The workflow is known and finite; an explicit graph is inspectable, testable, and visualizable |
| **Persistence & transitions** | Event log (append-only) | Durability, resume, audit, replay — see [ARCHITECTURE.md §8](ARCHITECTURE.md) |
| **Notification** | In-process dispatch + transactional outbox | Decouples side effects without adding Kafka |

The state graph *is* the reducer. Events are how transitions are recorded. Asking "graph
or event bus" is like asking "class or database" — different concerns.

---

## 6. LangGraph, AutoGen, CrewAI, or Custom?

> **Custom orchestration. Prototype in LangGraph; do not ship on it.**

### CrewAI — **No**

Role-play abstraction (`Agent(role="Senior Engineer", goal=..., backstory=...)`) is
optimized for demo legibility, not production control. It gives you almost no control
over context assembly — and context assembly *is the product*. Weak checkpointing, weak
determinism, weak observability. Fine for a hackathon; wrong for a system that must
resume a 40-minute session after a worker crash.

### AutoGen — **No**

Conversation-centric. Group chat burns tokens by design: every agent sees every message.
Termination conditions are heuristic. Excellent research vehicle for *emergent* multi-agent
behavior — which is precisely what you don't want when the workflow is known and the bill
is real.

### LangGraph — **Closest, but no for production**

Genuinely the best of the three: it's a real state machine with typed state, conditional
edges, and built-in checkpointing. Conceptually it matches this design.

Why not ship on it:

1. **Its checkpointer is not the event log you need.** LangGraph checkpoints state
   snapshots; this design needs an append-only event log with causation IDs, replay to
   arbitrary points, and forking. You'd end up building that alongside LangGraph and
   reconciling two sources of truth.
2. **Context assembly is where the product's value lives.** Framework abstractions over
   message construction actively obstruct prompt-cache-aware prefix ordering, which is
   the single largest cost lever (§8). Fighting the framework to control token layout is
   a bad trade.
3. **Dependency weight and version churn** in the LangChain ecosystem is a real
   operational cost for a system meant to run unattended.
4. **The orchestrator is genuinely small.** A reducer over ~25 event types with a fixed
   5-node workflow is on the order of 800–1500 lines. That is not enough code to justify
   a framework's constraints.

**Where LangGraph *is* right:** Phase 1. Building the walking skeleton in LangGraph gets
you end-to-end in days instead of weeks, validates the role decomposition against real
issues, and costs nothing to discard. Migrate the orchestrator to custom when checkpoint
semantics and cache control start to matter — which is exactly when you'll know what you
actually need.

**What to genuinely reuse regardless:** tree-sitter, ripgrep, MCP SDK, OpenTelemetry,
provider SDKs, semgrep. Build the orchestrator; buy everything below it.

---

## 7. Communication Graph

```
                          ┌─────────────────────────────────────┐
                          │            EVENT LOG                │
                          │   append-only · Postgres · truth    │
                          └──────────────▲──────────────────────┘
                                         │ every transition
                          ┌──────────────┴──────────────────────┐
                          │          ORCHESTRATOR               │
                          │  reduce(events) → state             │
                          │  dispatch · validate · budget · gate│
                          │  (deterministic — no LLM)           │
                          └─┬────────┬────────┬────────┬────────┘
      ResearchBrief ────────┤        │        │        │
                            │        │        │        │
              ┌─────────────▼──┐  ┌──▼─────┐ ┌▼───────┐ ┌▼─────────┐
              │   RESEARCHER   │  │PLANNER │ │ CODER  │ │ REVIEWER │
              │  FAST_CHEAP    │  │FRONTIER│ │  CODE  │ │ FRONTIER │
              │  ~120k → 2k    │  │30k→3k  │ │60k/step│ │  40k     │
              └───────┬────────┘  └───┬────┘ └───┬────┘ └────┬─────┘
                      │               │          │           │
                      │               │          │      ┌────▼─────┐
                      │               │          │      │ DEBUGGER │
                      │               │          │      │ FRONTIER │
                      │               │          │      └────┬─────┘
                      │               │          │           │
                      └───────────────┴────┬─────┴───────────┘
                                           ▼
                              ┌────────────────────────┐
                              │      TOOL BROKER       │
                              │ validate→authz→gate    │
                              └───────┬────────┬───────┘
                                      ▼        ▼
                        ┌─────────────────┐  ┌──────────────────┐
                        │ SHARED READ-ONLY│  │     SANDBOX      │
                        │ repo facts      │  │ Docker · gVisor  │
                        │ symbol graph    │  │ net-deny         │
                        │ dep graph       │  │ 1 per session    │
                        │ conventions(T4) │  └──────────────────┘
                        └─────────────────┘

  ══ TYPED CONTRACTS (the only inter-agent payloads) ══
     Researcher → Planner   ResearchBrief
     Planner    → Coder     Plan (validated against index)
     Coder      → Reviewer  Diff + TestResults
     Reviewer   → Coder     ReviewVerdict{REJECT, reasons[]}
     Tests      → Debugger  StructuredFailure + prior hypotheses
     Debugger   → Coder     Diagnosis{hypothesis, minimal_fix}

  ══ EXPLICITLY FORBIDDEN EDGES ══
     Coder  ↛ Researcher    (would reintroduce context pollution)
     Coder  ↛ Reviewer      (direct persuasion destroys independence)
     any    ↛ any           (no free-form agent-to-agent messaging)
```

---

## 8. Orchestration, Parallelism, Token Optimization

### Orchestration

The Orchestrator is a **pure function**: `state = reduce(events)`, then
`next_action = policy(state)`. No LLM, no hidden state, no I/O in the reducer. This makes
it exhaustively unit-testable — and the reducer tests are the highest-value tests in the
codebase, because every recovery path flows through them.

Responsibilities: dispatch by state, validate contracts at boundaries, enforce budgets
(tokens, cost, wall-clock, tool calls), evaluate approval gates, detect non-convergence,
checkpoint.

**Complexity tiering** is an orchestration decision, not an agent one:
```
TRIVIAL   (typo, doc fix)  → Coder only                    1 LLM call
SIMPLE    (localized bug)  → Researcher → Coder → Reviewer 3 calls
STANDARD  (normal issue)   → full 5-agent pipeline
COMPLEX   (multi-module)   → full pipeline + mandatory human gate
```
Running four roles on a typo is how agent systems become uneconomic on the long tail.

### Parallelism

**What parallelizes:**
- Research sub-questions — fan out independent queries, join into one brief. Biggest
  latency win available.
- Indexing — embarrassingly parallel per file.
- Read-only tool calls within a turn.
- Plan steps touching disjoint files with no dependency edge.
- Tier-1 test selection across independent packages.

**What must not:**
- Writes to the same file (in-session conflicts are unrecoverable garbage).
- Review vs. implementation — reviewing a moving diff is meaningless.
- Anything crossing a checkpoint boundary.

**Honest assessment:** the pipeline is inherently sequential — understand, then plan, then
build, then verify. Parallelism helps *within* stages, not across them. Claims of large
speedups from "parallel agents" in SWE tasks usually mean parallel *sessions* on
independent issues, which is a scheduling property, not an architectural one.

### Token optimization

Ordered by measured impact:

1. **Compression at the Researcher boundary (60:1).** The single largest structural win.
2. **Prompt-cache-aware prefix ordering.** Stable prefix first — system prompt → repo
   conventions → repo map → task → volatile tool results. Reordering these for aesthetics
   destroys cache hit rate and can multiply cost several-fold. Enforced by the assembler
   and covered by tests.
3. **Model routing per role.** Researcher on `FAST_CHEAP` is 40–60% of total savings.
4. **Structured outputs everywhere.** Schema-constrained generation eliminates the
   "explain your reasoning then give the answer" tax on stages that don't need reasoning.
5. **Structured tool results, not raw dumps.** A parsed test failure is ~200 tokens; the
   raw pytest output is 40k. Same information, 200× cheaper.
6. **Progressive disclosure.** Signatures before bodies; bodies on demand. (See
   [Repository Intelligence](REPOSITORY-INTELLIGENCE.md) §7.)
7. **Early termination on non-convergence.** Not a token optimization so much as refusing
   to pay for a known-failed outcome.

---

## 9. Where the "Judge" Actually Belongs

The Judge is a duplicate of the Reviewer **in the production path** — but it is a real,
distinct role in the **evaluation harness**, and that's worth separating explicitly.

```
PRODUCTION   Reviewer — gates a specific PR, sees the diff, can reject
EVALUATION   Judge    — scores completed trajectories against a rubric,
                        offline, no authority over the run, used to
                        rank configurations and detect regressions
```

Conflating them is a mistake in the other direction: an LLM judge with production
authority creates a feedback loop where the system optimizes against its own grader.
The Judge must be **outside** the loop it measures.

---

## 10. Failure Recovery

| Failure | Detection | Recovery |
|---|---|---|
| Agent emits invalid schema | Contract validation at boundary | Re-prompt with validation error, ≤3; then escalate |
| Researcher brief too thin | Confidence < threshold, empty change_sites | Re-run with expanded budget; then human |
| Plan references nonexistent symbols | Deterministic index validation | Regenerate with errors as feedback, ≤2 |
| Coder edit match fails | Tool returns not-found | Re-read file, retry ≤2; then step replan |
| Coder writes outside plan scope | **Broker rejects** (deterministic) | Re-prompt with scope constraint |
| Reviewer ↔ Coder ping-pong | Iteration counter | Cap at 2; escalate with both positions |
| Debugger patch-stacking | Diff-oscillation detector | Revert to checkpoint, force new hypothesis |
| Non-convergence | Repeated identical failure / no progress | Escalate immediately — don't burn the cap |
| Worker crash | Lease expiry | New worker replays log, rehydrates sandbox |
| Provider outage | Error classification | Cross-provider failover; all down → **suspend**, not fail |
| Budget exceeded | Accounting | Suspend, notify, await human extension |

**Two principles.** First, **infrastructure failures are retried by the platform; task
failures are handled by agents.** Conflating them produces systems that silently retry
their way through a fundamentally wrong approach. Second, **suspension ≠ failure** — a
suspended session holds no container, no worker, no context; only rows and a snapshot.

---

## 11. Where Human Approval Goes

Gates are **policy-evaluated against structured facts**, never agent-decided. The agent
does not get to judge what is risky; policy does, and policy is versioned and tested.

```
GATE 0  ISSUE INTAKE
        tractability = LOW/BLOCKED → post clarifying question, suspend
        ▸ Cheapest possible gate. A misunderstood issue implemented confidently
          is the worst output this system can produce.

GATE A  POST-PLAN (conditional)
        risk ≥ HIGH · confidence < threshold · diff estimate > N files
        ▸ Cheap to gate, expensive to skip. Approving a plan costs a human
          60 seconds; reviewing a wrong 400-line diff costs 30 minutes.

GATE B  PRE-PR (default: always)
        ▸ The main gate at STANDARD autonomy.

GATE C  SENSITIVE CLASS (always, regardless of tier)
        dependency add/upgrade · CI config change · file deletion ·
        migrations · auth/crypto paths · public API surface

GATE D  BUDGET / NON-CONVERGENCE
        ▸ Not really approval — a request to extend or abandon.
```

**Autonomy tiers** are per-repository and earned:
```
SUPERVISED  every plan + every write        ← new repos start here
STANDARD    PRs gated, plans automatic      ← default
TRUSTED     only sensitive classes gated
AUTONOMOUS  audit after the fact            ← narrow task classes only
```

**`modify` is the most valuable human response.** When a human edits the plan or patch
rather than approving/rejecting, that correction is written back to Tier-4 procedural
memory as a durable repo convention — so the same correction is never needed twice.
Approval becomes a training signal, not merely a brake. This is the mechanism by which
the system's cost per merged PR falls with use.

---

## 12. Summary of Decisions

| Question | Answer |
|---|---|
| How many agents? | **5** — Researcher, Planner, Coder, Reviewer, Debugger |
| Plus? | 2 LLM functions (commit, PR body); 7 deterministic components |
| Direct agent↔agent comms? | **No** — star topology, typed contracts through the Orchestrator |
| Shared memory? | **Facts yes, conclusions via contracts, reasoning never** |
| Graph or event bus? | **Both** — state graph is the reducer, event log is the truth |
| LangGraph / AutoGen / CrewAI? | **Custom.** LangGraph for the Phase-1 prototype only |
| Is the Supervisor an agent? | **No** — deterministic reducer. An LLM supervisor adds nondeterminism to control flow |
| Critic / Reflection / Judge? | **Merged into Reviewer.** Judge survives only in the offline eval harness |
| Biggest structural win? | **60:1 compression at the Researcher boundary** |
| Biggest risk? | **Handoff fidelity** — the brief is lossy compression |

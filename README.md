# WRIGHT

> *wright (n.) — one who builds or makes.*

An open-source autonomous software engineering agent. Takes a GitHub issue, produces a
reviewed, tested pull request. Architecturally comparable to Devin, OpenAI Codex, and
Claude Code — deliberately narrower in scope, fully self-hostable, and open.

**Status: design phase complete. Implementation starts at Milestone 0.**

---

## Documents

| Document | Covers |
|---|---|
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | System design. Five agent architectures compared — single, planner+executor, four-role, hierarchical, event-driven — and the choice defended. Module boundaries, state, memory hierarchy, security, deployment. |
| **[PIPELINE.md](PIPELINE.md)** | Issue → pull request in 27 stages. Each with inputs, outputs, failure modes, recovery, caching, and latency budgets. Three autonomy tiers, plus how this compares to Devin, Claude Code, Cursor, and Codex. |
| **[MULTI-AGENT-DESIGN.md](MULTI-AGENT-DESIGN.md)** | Why 22 candidate agents collapse to 5. Communication topology, orchestration, parallelism, token optimization, and where human approval belongs. |
| **[REPOSITORY-INTELLIGENCE.md](REPOSITORY-INTELLIGENCE.md)** | Understanding codebases at 1M+ LOC. Nine knowledge layers, minimum-context selection, progressive disclosure, and why the file is the wrong primitive. |
| **[ROADMAP.md](ROADMAP.md)** | 15 milestones, ~30k LOC. Each independently testable and ending in a working demo. |

---

## Roadmap

15 milestones, ~30k LOC. Each is independently testable and ends in a working demo.

| Phase | Milestones | Question it answers |
|---|---|---|
| 0 — Foundation | M0–M2 | Does this work at all? |
| 1 — Intelligence | M3–M4 | Can it understand code? |
| 2 — Autonomy | M5–M7 | Can it work unattended? |
| 3 — Product | M8–M10 | Can someone else use it? |
| 4 — Scale | M11–M14 | Can it run in production? |

Every milestone specifies objective, files, estimated LOC, difficulty, dependencies, time,
acceptance criteria, common mistakes, and what gets committed.

**M2 is the milestone that matters** — a walking skeleton that turns a real issue into a
real diff. Everything before it is scaffolding; everything after assumes the core loop
works. The most likely way a project like this dies is months of infrastructure before one
real diff.

---

## Architecture in one paragraph

Four roles — **Planner, Researcher, Coder, Reviewer** — on an event-sourced substrate.
The Researcher consumes ~120k tokens of repository exploration and emits a 2k-token
structured brief, so the Coder never sees the noise; that 60:1 compression is what makes
frontier-model planning and review economically viable. The Reviewer never sees the Coder's
reasoning, because an agent reviewing its own work inherits its own misconceptions. Every
state transition is an appended immutable event, so checkpointing, crash recovery,
resume-after-approval, and audit fall out as consequences rather than features.

**North-star metric: cost per *merged* pull request.** Not per token, not per session — it
is the only number that makes architectural trade-offs comparable.

---

## Stack

Python 3.12 · FastAPI · PostgreSQL (+ pgvector, optional tier) · Redis · Docker/gVisor ·
tree-sitter · MCP · OpenTelemetry · Kubernetes

---

## Author

**Keshav Kakani** — IIT Jodhpur, B.Tech Bioengineering + Artificial Intelligence

## License

Apache 2.0

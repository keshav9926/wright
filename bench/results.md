# tokens-to-answer: with vs. without wright-index

Six repository questions, headless Claude Code, same model and turn
cap, cwd = target repo. `with` adds only the wright-index MCP server.
tokens = input + output + cache write + cache read. n=1 per cell —
directional, not statistical.

| question | tokens with | tokens without | ratio | turns w/wo | index tools used |
|---|---|---|---|---|---|
| callers-tests | 223,291 | 507,365 | 2.3x | 7/11 | 2 |
| blast-radius | 220,129 | 219,513 | 1.0x | 7/11 | 1 |
| interface-impls | 71,168 | 71,073 | 1.0x | 2/2 | 0 |
| kunlun-impact | 300,338 | 214,394 | 0.7x | 9/9 | 2 |
| hot-owners | 292,512 | 416,293 | 1.4x | 8/24 | 2 |
| repo-orientation | 257,272 | 176,512 | 0.7x | 11/9 | 2 |

**Aggregate (successful pairs): 1,364,710 with vs. 1,605,150 without — 1.2× more tokens without the index. Turns: 44 vs 66 (33% fewer with).**

## Honest reading

The index wins **where the answer lives in the graph or the history**, and
roughly ties or loses where builtin tools were already adequate:

- **callers-tests, 2.3×** — proven callers + covering tests is the question
  grep is worst at; two index calls replaced a grep-read-grep-read chain.
- **hot-owners, 1.4× tokens but 24→8 turns** — without the index the agent
  paged through `git log` manually for 24 turns.
- **interface-impls, parity** — the agent answered with one grep in both
  conditions (0 index tools used): `func.*Fit` over a known pattern is
  something grep is genuinely good at. The index adds nothing here, and
  that's fine.
- **kunlun-impact / repo-orientation, 0.7×** — the index condition did MORE
  total work: with two toolsets available the agent used both, and eight
  extra tool schemas ride along in every turn's context. Orientation via
  `ls`+`README` is already cheap.

Method note: `tokens` is dominated by cache reads (≈ turns × context size),
so **turns is the more robust signal** — 33% fewer overall. The fair summary:
*the index converts multi-turn exploration into single tool calls on
structure/history questions, and stays out of the way (at small schema cost)
on questions grep already handles.* No benchmark inflation: two cells show
the index losing, and the reasons are stated.

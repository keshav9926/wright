"""Day 5b — the tokens-to-answer benchmark.

Measures the number the whole project stands on: how much context does an
agent burn answering repository questions WITH the index versus WITHOUT?

Method
------
Each question runs twice through headless Claude Code (`claude -p`),
cwd = the target repo, same model, same turn cap:

  with-index : --mcp-config exposes ONLY wright-index (--strict-mcp-config
               keeps the run hermetic); builtin file tools stay available —
               the comparison is "agent + index" vs "agent", not a straitjacket
  without    : --strict-mcp-config and no MCP config at all -> builtin tools only

Per run we record, from the stream-json result event:
  tokens_total = input + output + cache_creation + cache_read
    (cache reads are real context the model attended to — excluding them
     would flatter whichever condition re-reads more prompt; they are,
     however, cheaper, which is why cost is also reported)
  turns, duration, tool calls made (from assistant events), cost when present.

Threats to validity, stated up front: n=1 per cell (model variance is real),
6 questions chosen by the tool's author, one repo (HAMi, ~69k lines Go).
This is a directional measurement, not a paper.

Usage:
    python bench/run_bench.py <repo_path> [--questions bench/questions.json]
Writes bench/raw/<id>-<cond>.jsonl and bench/results.md.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).parent
MAX_TURNS = 12
TIMEOUT_S = 420


def run_one(question: str, repo: Path, with_index: bool, wi_exe: Path,
            raw_path: Path) -> dict:
    """One headless Claude Code run; returns the metrics row.
    Called by: main(), 2x per question."""
    cmd = ["claude", "-p", question,
           "--output-format", "stream-json", "--verbose",
           "--max-turns", str(MAX_TURNS),
           "--strict-mcp-config"]
    if with_index:
        mcp_cfg = json.dumps({"mcpServers": {"wright-index": {
            "command": str(wi_exe), "args": ["mcp", str(repo)]}}})
        cmd += ["--mcp-config", mcp_cfg,
                "--allowedTools", "mcp__wright-index"]

    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=TIMEOUT_S, shell=True)  # shell: claude is a .cmd shim on Windows
    wall = time.monotonic() - started

    raw_path.write_text(proc.stdout, encoding="utf-8")

    tool_calls: list[str] = []
    result_ev: dict = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tool_calls.append(block["name"])
        elif ev.get("type") == "result":
            result_ev = ev

    usage = result_ev.get("usage", {})
    tokens_total = (usage.get("input_tokens", 0)
                    + usage.get("output_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0))
    return {
        "ok": bool(result_ev) and result_ev.get("subtype") == "success",
        "tokens_total": tokens_total,
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "cache_write": usage.get("cache_creation_input_tokens", 0),
        "turns": result_ev.get("num_turns", 0),
        "seconds": round(result_ev.get("duration_ms", wall * 1000) / 1000, 1),
        "cost_usd": result_ev.get("total_cost_usd"),
        "tool_calls": tool_calls,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--questions", type=Path, default=BENCH_DIR / "questions.json")
    ap.add_argument("--wi", type=Path,
                    default=Path(sys.executable).parent / "wi.exe")
    args = ap.parse_args()

    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    raw_dir = BENCH_DIR / "raw"
    raw_dir.mkdir(exist_ok=True)
    rows = []

    for item in questions:
        for cond in ("with", "without"):
            print(f"[{item['id']}] {cond} ...", flush=True)
            try:
                metrics = run_one(item["q"], args.repo.resolve(),
                                  cond == "with", args.wi.resolve(),
                                  raw_dir / f"{item['id']}-{cond}.jsonl")
            except subprocess.TimeoutExpired:
                metrics = {"ok": False, "tokens_total": 0, "turns": 0,
                           "seconds": TIMEOUT_S, "tool_calls": [],
                           "input": 0, "output": 0, "cache_read": 0,
                           "cache_write": 0, "cost_usd": None}
            metrics.update(id=item["id"], cond=cond)
            rows.append(metrics)
            print(f"    ok={metrics['ok']} tokens={metrics['tokens_total']:,} "
                  f"turns={metrics['turns']} tools={len(metrics['tool_calls'])}",
                  flush=True)

    (BENCH_DIR / "raw" / "summary.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8")
    _write_report(questions, rows)
    print("wrote bench/results.md", flush=True)


def _write_report(questions, rows) -> None:
    """results.md: per-question table + aggregate ratio. Called by main()."""
    by = {(r["id"], r["cond"]): r for r in rows}
    lines = [
        "# tokens-to-answer: with vs. without wright-index",
        "",
        "Six repository questions, headless Claude Code, same model and turn",
        "cap, cwd = target repo. `with` adds only the wright-index MCP server.",
        "tokens = input + output + cache write + cache read. n=1 per cell —",
        "directional, not statistical.",
        "",
        "| question | tokens with | tokens without | ratio | turns w/wo | index tools used |",
        "|---|---|---|---|---|---|",
    ]
    tw_sum = two_sum = 0
    for item in questions:
        w = by.get((item["id"], "with"), {})
        wo = by.get((item["id"], "without"), {})
        tw, two = w.get("tokens_total", 0), wo.get("tokens_total", 0)
        if w.get("ok") and wo.get("ok"):
            tw_sum += tw
            two_sum += two
        ratio = f"{two / tw:.1f}x" if tw and two else "—"
        wi_tools = len([t for t in w.get("tool_calls", []) if "wright" in t])
        lines.append(
            f"| {item['id']} | {tw:,}{'' if w.get('ok') else ' (fail)'} "
            f"| {two:,}{'' if wo.get('ok') else ' (fail)'} "
            f"| {ratio} | {w.get('turns', 0)}/{wo.get('turns', 0)} | {wi_tools} |")
    if tw_sum:
        lines += ["",
                  f"**Aggregate (successful pairs): {tw_sum:,} with vs. "
                  f"{two_sum:,} without — {two_sum / tw_sum:.1f}× more tokens "
                  f"without the index.**"]
    (BENCH_DIR / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

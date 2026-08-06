"""L6 — git history mining: the knowledge that is NOT in the code.

Static analysis sees no edge between schema.proto and docs/api.md. Git
history shows they changed together in 90% of commits. That fact lives
ONLY here — no amount of parsing or grepping produces it. This layer is
the reason wright-index exists as more than a fancy ctags.

What gets mined from `git log`:

  1. CO-CHANGE PAIRS — association-rule mining over commits-as-baskets:
         support(A,B)      = P(A and B change in the same commit)
         confidence(A->B)  = P(B changes | A changed)
         lift(A,B)         = confidence / P(B)   <- the one that matters
     Lift is what separates signal from noise: README co-occurs with
     everything (high support, lift ~1 = coincidence); schema.proto with
     api.md rarely but ALWAYS together (lift 20 = coupling). Naive
     co-occurrence counting is useless — lift is the entire trick.

  2. FILE STATS — change counts, last-touched, top authors. Feeds churn
     ranking ("hot files") and, later, reviewer suggestions.

Design choice: we shell out to `git log` instead of linking pygit2.
Rationale: the tree-sitter 0.26 segfault cost an hour of native-crash
debugging on Day 1; git is ALREADY installed (we cloned with it), its
plumbing output is a stable public interface, and parsing it is ~30 lines.
Fewer native deps, same data.

Called by: indexer.index_repository() pass 3 (skipped for non-git dirs).
Calls:     subprocess git; db bulk inserts.
"""

from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

# Commits touching more files than this are bulk events — vendor updates,
# formatting sweeps, license-header stampedes. Including them would pair
# every file with every other file and drown the signal. Standard MSR
# (Mining Software Repositories) hygiene.
MAX_FILES_PER_COMMIT = 50

# Pairs must co-change at least this often to be stored. Below 3 you're
# recording coincidence, and the table bloats quadratically.
MIN_PAIR_COUNT = 3

DEFAULT_MAX_COMMITS = 5000

# Record separators: 0x1f (unit sep) never appears in author names, unlike
# '|' or ','. CRUCIAL detail: we put the ESCAPES (%x1e) in the format string
# and let git expand them — passing raw control bytes as argv on Windows
# loses them in mingw argument handling (found the hard way: every commit
# merged into one basket because the marker never appeared).
_FIELD_SEP = "\x1f"
_COMMIT_MARK = "\x1e"   # record sep marks "a new commit starts here"


@dataclass
class HistoryResult:
    """Tallies for the index summary. Produced by mine_history()."""
    commits_scanned: int = 0
    commits_skipped_bulk: int = 0     # over MAX_FILES_PER_COMMIT
    pairs_stored: int = 0
    files_tracked: int = 0
    notes: list[str] = field(default_factory=list)


def mine_history(repo_root: Path, db, max_commits: int = DEFAULT_MAX_COMMITS) -> HistoryResult:
    """Mine the last `max_commits` non-merge commits into cochange +
    file_history tables. THE entry point of this module.

    Called by: indexer pass 3, inside the same big transaction as passes
    1-2 — a crash still leaves nothing half-visible.
    """
    result = HistoryResult()

    log = _git_log(repo_root, max_commits)
    if log is None:
        result.notes.append("not a git repo (or git failed); history layer skipped")
        return result

    # ---- accumulate baskets ------------------------------------------------
    file_count: Counter[str] = Counter()          # path -> commits touching it
    pair_count: Counter[tuple[str, str]] = Counter()
    last_touched: dict[str, str] = {}             # path -> latest author date
    authors: dict[str, Counter] = defaultdict(Counter)  # path -> author -> n

    for author, date, files in log:
        result.commits_scanned += 1
        if len(files) > MAX_FILES_PER_COMMIT:
            result.commits_skipped_bulk += 1
            continue
        for f in files:
            file_count[f] += 1
            authors[f][author] += 1
            if f not in last_touched:      # log is newest-first
                last_touched[f] = date
        # commits-as-baskets: every unordered pair in this commit co-changed.
        # sorted() gives the canonical (a<b) key so (x,y) and (y,x) merge.
        for a, b in combinations(sorted(set(files)), 2):
            pair_count[(a, b)] += 1

    n_commits = result.commits_scanned - result.commits_skipped_bulk
    if n_commits == 0:
        result.notes.append("no usable commits")
        return result

    # ---- association rules -> cochange table -------------------------------
    rows = []
    for (a, b), both in pair_count.items():
        if both < MIN_PAIR_COUNT:
            continue
        ca, cb = file_count[a], file_count[b]
        support = both / n_commits
        conf_ab = both / ca            # P(b changes | a changed)
        conf_ba = both / cb
        # lift > 1: change together MORE than chance predicts. This is the
        # number that makes README(changes with everything) rank low and
        # schema.proto<->api.md rank high.
        lift = (both * n_commits) / (ca * cb)
        rows.append((a, b, both, ca, cb, support, conf_ab, conf_ba, lift))
    db.insert_cochange(rows)
    result.pairs_stored = len(rows)

    # ---- per-file stats -----------------------------------------------------
    stat_rows = []
    for path, count in file_count.items():
        top = "; ".join(f"{name} ({n})" for name, n in authors[path].most_common(3))
        stat_rows.append((path, count, last_touched.get(path, ""), top))
    db.insert_file_history(stat_rows)
    result.files_tracked = len(stat_rows)

    return result


def _git_log(repo_root: Path, max_commits: int):
    """Run git log and parse it into [(author, iso_date, [files]), ...].

    Format anatomy (one call, no per-commit subprocess cost):
        --no-merges    merge commits re-list their parents' files: pure noise
        --name-only    file list after each header
        %x1e           record mark so headers are unambiguous vs file paths
        -n             bound the window: recent coupling is what predicts
                       the next change; five-year-old coupling often doesn't

    Returns None when this isn't a git repo — mining is optional, never fatal.
    Called by: mine_history() only.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--no-merges", "--name-only",
             "--format=%x1e%an%x1f%aI", f"-n{max_commits}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None

    commits: list[tuple[str, str, list[str]]] = []
    author, date, files = "", "", []
    # split("\n"), NOT splitlines(): Python's splitlines() treats \x1e
    # (Record Separator) as a line boundary and silently CONSUMES our
    # commit marker — every commit then merges into one giant basket.
    # Cost of discovery: three failing tests and one debug script.
    for line in proc.stdout.split("\n"):
        if line.startswith(_COMMIT_MARK):
            if author or files:
                commits.append((author, date, files))
            header = line[1:]
            parts = header.split(_FIELD_SEP)
            author = parts[0] if parts else ""
            date = parts[1] if len(parts) > 1 else ""
            files = []
        elif line.strip():
            files.append(line.strip())
    if author or files:
        commits.append((author, date, files))
    return commits

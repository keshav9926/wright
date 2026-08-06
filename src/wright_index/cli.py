"""The `wi` command — the human-facing surface of Day 1.

    wi index   <repo>                 build/rebuild the index
    wi symbols <repo> [filters]       query it
    wi stats   <repo>                 what's in it

Called by: you, in a terminal. pyproject's [project.scripts] wires
`wi` -> this module's `app` (a Typer object; calling it dispatches to the
@app.command functions by CLI argument).
Calls:     indexer.index_repository() for writes; db.Database for reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .db import Database, db_path_for
from .indexer import index_repository

app = typer.Typer(
    name="wi",
    help="wright-index: index a repository's symbols, then query them.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


@app.command()
def index(
    repo: Path = typer.Argument(..., help="Path to the repository to index."),
    db: Optional[Path] = typer.Option(None, "--db", help="Override index DB location."),
) -> None:
    """Index REPO from scratch: walk -> parse -> extract -> store.

    Calls indexer.index_repository() and renders its IndexResult.
    """
    if not repo.is_dir():
        console.print(f"[red]not a directory:[/red] {repo}")
        raise typer.Exit(code=1)

    with console.status(f"indexing {repo.resolve().name}..."):
        result = index_repository(repo, db_path=db)

    # ---- files table -----------------------------------------------------
    files_table = Table(title=f"indexed {result.repo_root.name}")
    files_table.add_column("language")
    files_table.add_column("files", justify="right")
    files_table.add_column("lines", justify="right")
    for lang, s in sorted(result.files_by_language.items()):
        files_table.add_row(lang, str(s["files"]), f"{s['lines']:,}")
    console.print(files_table)

    # ---- symbols table ----------------------------------------------------
    sym_table = Table(title="symbols")
    sym_table.add_column("kind")
    sym_table.add_column("count", justify="right")
    for kind, n in sorted(result.symbols_by_kind.items(), key=lambda kv: -kv[1]):
        sym_table.add_row(kind, str(n))
    console.print(sym_table)

    # ---- one-line outcome --------------------------------------------------
    skipped = ", ".join(f"{n} {reason}" for reason, n in
                        sorted(result.skipped_by_reason.items())) or "none"
    console.print(
        f"[green]{result.symbol_count:,} symbols[/green] from "
        f"{result.files_indexed:,} files in [bold]{result.seconds:.1f}s[/bold] "
        f"(skipped: {skipped}; parse errors: {result.parse_errors})"
    )
    # Day 2: graph summary — resolution-rate is the honesty metric.
    if result.edge_count:
        rate = 100 * result.edges_resolved / result.edge_count
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(
            result.edges_by_resolution.items(), key=lambda kv: -kv[1]))
        console.print(
            f"[green]{result.edge_count:,} call edges[/green] "
            f"({result.edges_resolved:,} resolved = {rate:.0f}%; {breakdown}); "
            f"{result.import_count:,} imports"
        )
    console.print(f"index db: {result.db_path}")


@app.command()
def symbols(
    repo: Path = typer.Argument(..., help="Repository path (locates its index DB)."),
    file: Optional[str] = typer.Option(None, "--file", "-f",
                                       help="Filter by file path suffix, e.g. device.go"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Substring of symbol name."),
    kind: Optional[str] = typer.Option(None, "--kind", "-k",
                                       help="function|method|class|struct|interface|type|type_alias"),
    exported: bool = typer.Option(False, "--exported", help="Exported/public symbols only."),
    no_tests: bool = typer.Option(False, "--no-tests", help="Exclude symbols in test files."),
    limit: int = typer.Option(200, "--limit", help="Max rows."),
    db: Optional[Path] = typer.Option(None, "--db", help="Override index DB location."),
) -> None:
    """List symbols from an existing index. Pure DB read — no parsing.

    Calls Database.search_symbols(); all filters combine with AND.
    """
    database = _open_db(repo, db)
    rows = database.search_symbols(file=file, name=name, kind=kind,
                                   exported_only=exported,
                                   include_tests=not no_tests, limit=limit)
    if not rows:
        console.print("[yellow]no symbols matched.[/yellow] (is the repo indexed? try `wi index`)")
        raise typer.Exit(code=1)

    table = Table(title=f"{len(rows)} symbol(s)")
    table.add_column("kind", style="cyan")
    table.add_column("qualified name", style="bold")
    table.add_column("file:lines")
    table.add_column("signature", overflow="fold", max_width=60)
    for r in rows:
        table.add_row(
            r["kind"],
            r["qualified_name"] + ("" if r["is_exported"] else " [dim](private)[/dim]"),
            f"{r['file_path']}:{r['start_line']}-{r['end_line']}",
            r["signature"] or "",
        )
    console.print(table)
    database.close()


@app.command()
def stats(
    repo: Path = typer.Argument(..., help="Repository path (locates its index DB)."),
    db: Optional[Path] = typer.Option(None, "--db", help="Override index DB location."),
) -> None:
    """Summarize an existing index: what was indexed, skipped, and when.

    Calls Database.stats() + meta rows written by the indexer.
    """
    database = _open_db(repo, db)
    s = database.stats()

    console.print(f"repo:        {database.get_meta('repo_root')}")
    console.print(f"indexed at:  {database.get_meta('indexed_at')}")
    console.print(f"took:        {database.get_meta('index_seconds')}s")
    sha = database.get_meta("commit_sha")
    if sha:
        console.print(f"commit:      {sha[:12]}")

    table = Table(title="files")
    table.add_column("language")
    table.add_column("files", justify="right")
    table.add_column("lines", justify="right")
    for lang, row in sorted(s["files_by_language"].items()):
        table.add_row(lang, str(row["files"]), f"{row['lines']:,}")
    console.print(table)

    kinds = ", ".join(f"{k}={n}" for k, n in
                      sorted(s["symbols_by_kind"].items(), key=lambda kv: -kv[1]))
    console.print(f"symbols:      {s['total_symbols']:,}  ({kinds})")
    if s["skipped"]:
        console.print("skipped:      " + ", ".join(f"{n} {r}" for r, n in s["skipped"].items()))
    if s["parse_errors"]:
        console.print(f"parse errors: {s['parse_errors']} file(s) had ERROR nodes")
    database.close()


@app.command()
def callers(
    repo: Path = typer.Argument(..., help="Repository path (locates its index DB)."),
    symbol: str = typer.Argument(..., help="Symbol name or qualified name, e.g. trimMemory or Devices.trimMemory"),
    depth: int = typer.Option(1, "--depth", "-d", help="Transitive hops (2 = callers of callers)."),
    db: Optional[Path] = typer.Option(None, "--db", help="Override index DB location."),
) -> None:
    """Who calls SYMBOL — the question grep answers with name-collision soup.

    Calls Database.callers_of() (recursive CTE). Only PROVEN edges appear
    here; use `wi refs` to see every matching call site including unproven.
    """
    database = _open_db(repo, db)
    targets = database.find_symbols_by_name(symbol)
    if not targets:
        console.print(f"[yellow]no symbol named[/yellow] {symbol}")
        raise typer.Exit(code=1)

    for target in targets:
        rows = database.callers_of(target["id"], depth=depth)
        title = (f"callers of {target['qualified_name']} "
                 f"({target['file_path']}:{target['start_line']})")
        if not rows:
            console.print(f"{title}: [dim]none proven[/dim] "
                          f"(try `wi refs {target['name']}` for unproven sites)")
            continue
        table = Table(title=title)
        table.add_column("depth", justify="right")
        table.add_column("caller", style="bold")
        table.add_column("kind", style="cyan")
        table.add_column("file")
        for r in rows:
            table.add_row(str(r["depth"]), r["qualified_name"], r["kind"], r["file_path"])
        console.print(table)
    database.close()


@app.command()
def calls(
    repo: Path = typer.Argument(..., help="Repository path (locates its index DB)."),
    symbol: str = typer.Argument(..., help="Symbol name or qualified name."),
    db: Optional[Path] = typer.Option(None, "--db", help="Override index DB location."),
) -> None:
    """What SYMBOL calls (outgoing edges, resolved and unresolved).

    Calls Database.calls_from().
    """
    database = _open_db(repo, db)
    targets = database.find_symbols_by_name(symbol)
    if not targets:
        console.print(f"[yellow]no symbol named[/yellow] {symbol}")
        raise typer.Exit(code=1)

    for target in targets:
        rows = database.calls_from(target["id"])
        if not rows:
            console.print(f"{target['qualified_name']}: no outgoing calls recorded")
            continue
        table = Table(title=f"calls made by {target['qualified_name']}")
        table.add_column("line", justify="right")
        table.add_column("callee")
        table.add_column("resolved to", style="bold")
        table.add_column("how", style="cyan")
        table.add_column("conf", justify="right")
        for r in rows:
            resolved = f"{r['dst_qualified']} ({r['dst_file']})" if r["dst_qualified"] else "[dim]—[/dim]"
            table.add_row(str(r["line"]), r["dst_name"], resolved,
                          r["resolution"], f"{r['confidence']:.2f}")
        console.print(table)
    database.close()


@app.command()
def refs(
    repo: Path = typer.Argument(..., help="Repository path (locates its index DB)."),
    name: str = typer.Argument(..., help="Callee name to search call sites for."),
    limit: int = typer.Option(200, "--limit", help="Max rows."),
    db: Optional[Path] = typer.Option(None, "--db", help="Override index DB location."),
) -> None:
    """Every call site whose callee NAME matches — including unresolved ones.

    This is the honest superset of `wi callers`: interface dispatch and
    dynamic calls show up here even when no edge could be proven.
    Calls Database.refs_by_name().
    """
    database = _open_db(repo, db)
    rows = database.refs_by_name(name, limit=limit)
    if not rows:
        console.print(f"[yellow]no call sites reference[/yellow] {name}")
        raise typer.Exit(code=1)

    table = Table(title=f"{len(rows)} call site(s) of '{name}'")
    table.add_column("caller", style="bold")
    table.add_column("site")
    table.add_column("how", style="cyan")
    table.add_column("conf", justify="right")
    for r in rows:
        table.add_row(r["caller"], f"{r['caller_file']}:{r['line']}",
                      r["resolution"], f"{r['confidence']:.2f}")
    console.print(table)
    database.close()


def _open_db(repo: Path, db_override: Optional[Path]) -> Database:
    """Locate + open the index for a repo, with a friendly failure if the
    repo was never indexed.

    Called by: symbols() and stats().
    """
    path = db_override or db_path_for(repo)
    if not path.exists():
        console.print(f"[red]no index found[/red] for {repo} — run: wi index {repo}")
        raise typer.Exit(code=1)
    return Database(path, fresh=False)


if __name__ == "__main__":   # `python -m wright_index.cli` works too
    app()

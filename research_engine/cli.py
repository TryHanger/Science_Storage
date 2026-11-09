import sys
import uuid
from pathlib import Path
from typing import Optional
import typer
from rich.console import Console
from rich.table import Table

from research_engine.config import CONTRACTS_DIR, EXCHANGE_DIR
from research_engine.db import init_db, verify_tables
from research_engine.gold import evaluate_gold_set
from research_engine.openalex import import_works_from_fixture, import_works_from_openalex
from research_engine.queue import cancel_job, create_next_job, get_study_status
from research_engine.validator import accept_response

app = typer.Typer(
    name="research",
    help="Research Engine MVP v0.1 — Systematic literature screening CLI",
    no_args_is_help=True,
)
db_app = typer.Typer(name="db", help="Database management commands")
app.add_typer(db_app)

console = Console()
err_console = Console(stderr=True)


@db_app.command("init")
def db_init_cmd(
    schema: Optional[Path] = typer.Option(
        None,
        "--schema",
        "-s",
        help="Path to schema.sql file (defaults to contracts/schema.sql)",
    )
) -> None:
    """Initialize PostgreSQL database tables idempotently."""
    try:
        init_db(schema_file=schema)
        tables = sorted(verify_tables())
        console.print(f"[bold green]Database initialized successfully.[/bold green]")
        console.print(f"Verified {len(tables)} project tables: {', '.join(tables)}")
    except Exception as exc:
        err_console.print(f"[bold red]Database initialization failed:[/bold red] {exc}")
        sys.exit(1)


@app.command("import-openalex")
def import_openalex_cmd(
    query: str = typer.Option(..., "--query", "-q", help="Search query string"),
    limit: int = typer.Option(500, "--limit", "-l", help="Target number of works to fetch"),
    year_from: Optional[int] = typer.Option(None, "--year-from", "-y", help="Filter by publication year >= year"),
    fixture: Optional[Path] = typer.Option(None, "--fixture", "-f", help="Offline fixture file (JSON/JSONL)"),
) -> None:
    """Import and deduplicate publications from OpenAlex (or offline fixture)."""
    try:
        if fixture:
            console.print(f"[yellow]Importing from offline fixture: {fixture}[/yellow]")
            stats = import_works_from_fixture(fixture_path=fixture, query=query)
        else:
            console.print(f"[cyan]Searching OpenAlex for '{query}' (limit: {limit}, year_from: {year_from})...[/cyan]")
            stats = import_works_from_openalex(query=query, limit=limit, year_from=year_from)

        table = Table(title="Import Statistics")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="bold white")
        table.add_row("Run ID", str(stats.search_run_id))
        table.add_row("Provider", stats.provider)
        table.add_row("Query", stats.query)
        table.add_row("Status", f"[green]{stats.status}[/green]" if stats.status == "completed" else f"[red]{stats.status}[/red]")
        table.add_row("Fetched Count", str(stats.fetched_count))
        table.add_row("Inserted (New) Works", str(stats.inserted_count))
        table.add_row("Duplicate Works", str(stats.duplicate_count))
        if stats.error_message:
            table.add_row("Error", f"[red]{stats.error_message}[/red]")
        console.print(table)

        if stats.status == "failed":
            sys.exit(1)
    except Exception as exc:
        err_console.print(f"[bold red]Import failed:[/bold red] {exc}")
        sys.exit(1)


@app.command("next")
def next_job_cmd(
    study: str = typer.Option(..., "--study", help="Study key (e.g. buildingpulse_fdd)"),
    protocol: Path = typer.Option(..., "--protocol", "-p", help="Path to research_protocol.md"),
    count: int = typer.Option(50, "--count", "-c", help="Number of publications in batch (default 50)"),
) -> None:
    """Export next batch of 50 publications into ZIP review package for screening."""
    try:
        res = create_next_job(study_key=study, protocol_path=protocol, count=count)
        if res.is_reused:
            console.print(f"[bold yellow]Notice:[/bold yellow] Found active uncommitted Job [bold]{res.job_id}[/bold]. Reusing existing package.")
        else:
            console.print(f"[bold green]Created new screening Job:[/bold green] [bold]{res.job_id}[/bold]")

        console.print(f"Study: [cyan]{res.study_key}[/cyan]")
        console.print(f"Protocol SHA-256: [dim]{res.protocol_sha256}[/dim]")
        console.print(f"Papers in batch: [bold]{res.paper_count}[/bold]")
        console.print(f"Package Path: [bold underline]{res.package_path.resolve()}[/bold underline]")

        console.print("\n[bold magenta]=== Ready-to-paste Prompt for ChatGPT ===[/bold magenta]")
        console.print(res.chatgpt_prompt)
        console.print("[bold magenta]=========================================[/bold magenta]\n")
    except Exception as exc:
        err_console.print(f"[bold red]Failed to export next batch:[/bold red] {exc}")
        sys.exit(1)


@app.command("accept")
def accept_cmd(
    file: Path = typer.Option(..., "--file", "-f", help="Path to results.json file"),
) -> None:
    """Validate and commit screening results response from LLM."""
    try:
        res = accept_response(file_path=file)

        if res.status == "already_accepted":
            console.print(f"[bold yellow]{res.message}[/bold yellow]")
            sys.exit(0)

        if res.status == "rejected":
            err_console.print(f"[bold red]Validation FAILED for response: {file}[/bold red]")
            err_console.print(f"Saved rejected artifacts to: [bold]{res.rejected_dir}[/bold]\n")

            if res.issues:
                table = Table(title="Validation Errors")
                table.add_column("Type", style="red")
                table.add_column("Location / Path", style="yellow")
                table.add_column("Error Details", style="white")
                for issue in res.issues[:15]:
                    table.add_row(issue.kind, issue.path, issue.message)
                if len(res.issues) > 15:
                    table.add_row("...", "...", f"... and {len(res.issues) - 15} more errors")
                err_console.print(table)

            repair_file = (res.rejected_dir / "repair_prompt.txt") if res.rejected_dir else None
            if repair_file and repair_file.is_file():
                err_console.print(f"\n[cyan]A repair prompt has been generated at:[/cyan] {repair_file}")
            sys.exit(1)

        if res.status == "committed":
            console.print(f"[bold green]{res.message}[/bold green]")
            console.print(f"Committed [bold]{res.reviews_count}[/bold] reviews to PostgreSQL.")
            console.print(f"Raw response archived at: [dim]{res.accepted_dir}[/dim]")
            sys.exit(0)

    except Exception as exc:
        err_console.print(f"[bold red]Accept command failed:[/bold red] {exc}")
        sys.exit(1)


@app.command("status")
def status_cmd(
    study: str = typer.Option(..., "--study", help="Study key (e.g. buildingpulse_fdd)"),
    protocol: Path = typer.Option(..., "--protocol", "-p", help="Path to research_protocol.md"),
) -> None:
    """Display overall status and progress of screening study."""
    try:
        st = get_study_status(study_key=study, protocol_path=protocol)

        table = Table(title=f"Study Status: {st['study_key']}")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="bold white")
        table.add_row("Study Key", st["study_key"])
        table.add_row("Protocol SHA-256", f"{st['protocol_sha256'][:16]}...{st['protocol_sha256'][-8:]}")
        table.add_row("Total Works in DB", str(st["total_works"]))
        table.add_row("Pending Works for Review", str(st["pending_works"]))
        table.add_row("Committed Reviews", f"[green]{st['committed_reviews']}[/green]")
        table.add_row("Jobs: Exported", str(st["jobs"]["exported"]))
        table.add_row("Jobs: Repair Required", str(st["jobs"]["repair_required"]))
        table.add_row("Jobs: Committed", str(st["jobs"]["committed"]))
        table.add_row("Jobs: Cancelled", str(st["jobs"]["cancelled"]))

        if st["active_job"]:
            table.add_row("Active Job ID", f"[yellow]{st['active_job']}[/yellow]")
            table.add_row("Active Job Status", f"[yellow]{st['active_job_status']}[/yellow]")
            table.add_row("Active Package", str(st["active_job_package"]))
        else:
            table.add_row("Active Job", "[dim]None (ready for research next)[/dim]")

        console.print(table)
    except Exception as exc:
        err_console.print(f"[bold red]Failed to fetch status:[/bold red] {exc}")
        sys.exit(1)


@app.command("cancel")
def cancel_cmd(
    job: uuid.UUID = typer.Option(..., "--job", help="UUID of uncommitted review job to cancel"),
) -> None:
    """Cancel an active uncommitted review job and release reserved works."""
    try:
        msg = cancel_job(job_id=job)
        console.print(f"[bold green]{msg}[/bold green]")
    except Exception as exc:
        err_console.print(f"[bold red]Cancel failed:[/bold red] {exc}")
        sys.exit(1)


@app.command("evaluate-gold")
def evaluate_gold_cmd(
    file: Path = typer.Option(..., "--file", "-f", help="Path to gold_set_template.csv"),
    study: str = typer.Option(..., "--study", "-s", help="Study key to evaluate"),
) -> None:
    """Evaluate screening decisions against human-labeled gold set."""
    try:
        res = evaluate_gold_set(file_path=file, study_key=study)
        if res.status == "NOT RUN":
            console.print(f"[bold yellow]Quality Evaluation: NOT RUN[/bold yellow]")
            console.print(f"Reason: {res.message}")
            return

        table = Table(title=f"Gold Standard Evaluation: {study}")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="bold white")
        table.add_row("Total Gold Entries", str(res.total_gold))
        table.add_row("Gold Relevant ('include')", str(res.total_relevant))
        table.add_row("Matched in Screening Reviews", str(res.total_matched_in_reviews))
        table.add_row("True Positives", str(res.true_positives))
        table.add_row("Recall", f"{res.recall * 100:.1f}%")
        table.add_row("False Exclusions Count", str(len(res.false_exclusions or [])))
        console.print(table)

        if res.false_exclusions:
            console.print("\n[bold red]False Exclusions (Critical Screening Misses):[/bold red]")
            for item in res.false_exclusions:
                console.print(f"- [bold]{item['target']}[/bold]: {item['title']}")
                console.print(f"  Gold Reason: {item['gold_reason']}")
                console.print(f"  System Reason: {item['system_reason']}\n")

    except Exception as exc:
        err_console.print(f"[bold red]Gold evaluation failed:[/bold red] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    app()

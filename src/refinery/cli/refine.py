"""``refine``: the human's CLI, a client of factoryd's /api (D8)."""

from typing import Annotated

import typer

app = typer.Typer(help="Review and drive Refinery runs.", no_args_is_help=True)


@app.command()
def start(
    change: str,
    worktree: Annotated[
        str, typer.Option(help="The run's worktree (a factoryd root).")
    ] = ".",
) -> None:
    """Start a run on an OpenSpec change; prints the agent token once."""
    raise NotImplementedError


@app.command()
def runs(
    waiting: Annotated[bool, typer.Option(help="Only runs waiting for you.")] = False,
) -> None:
    """List runs."""
    raise NotImplementedError


@app.command()
def status(run_id: str) -> None:
    """Show one run."""
    raise NotImplementedError


@app.command()
def approve(run_id: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    """Approve the run's review gate."""
    raise NotImplementedError


@app.command()
def reject(run_id: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    """Reject the run's review gate (a note is required)."""
    raise NotImplementedError


@app.command()
def cancel(run_id: str) -> None:
    """Cancel a run."""
    raise NotImplementedError


@app.command()
def resolve(
    run_id: str, to: Annotated[str, typer.Option(help="spec, tests or impl")]
) -> None:
    """Resolve an escalation."""
    raise NotImplementedError


@app.command("retry-archive")
def retry_archive(run_id: str) -> None:
    """Retry a stopped archive."""
    raise NotImplementedError

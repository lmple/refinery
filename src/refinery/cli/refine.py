"""``refine``: the human's CLI, a client of factoryd's /api (D8).

The server URL comes from REFINE_URL (default http://127.0.0.1:8740) and the
human token from REFINE_TOKEN: tokens come from the environment, never from
files or flags (AGENTS.md).
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, cast

import httpx2
import typer

app = typer.Typer(help="Review and drive Refinery runs.", no_args_is_help=True)

DEFAULT_URL = "http://127.0.0.1:8740"

type Json = dict[str, object]


def _call(
    method: str,
    path: str,
    body: Json | None = None,
    params: Mapping[str, str] | None = None,
) -> Json:
    token = os.environ.get("REFINE_TOKEN")
    if not token:
        typer.echo(
            "REFINE_TOKEN is not set: export a human token (refinery token create)",
            err=True,
        )
        raise typer.Exit(2)
    url = os.environ.get("REFINE_URL", DEFAULT_URL).rstrip("/")
    try:
        with httpx2.Client(
            base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=120
        ) as http:
            response = http.request(method, f"/api{path}", json=body, params=params)
    except httpx2.HTTPError as exc:
        typer.echo(f"cannot reach factoryd at {url}: {exc}", err=True)
        raise typer.Exit(1) from exc
    payload: Json
    try:
        payload = cast(Json, response.json())
    except ValueError:
        payload = {"error": response.text}
    if response.status_code >= 400:
        typer.echo(
            f"refused ({response.status_code}): {payload.get('error', payload)}",
            err=True,
        )
        raise typer.Exit(1)
    return payload


def _phase_line(run: Json) -> str:
    return f"{run['run_id']}: {run['phase']}"


@app.command()
def start(
    change: str,
    worktree: Annotated[
        str, typer.Option(help="The run's worktree (a factoryd root).")
    ] = ".",
) -> None:
    """Start a run on an OpenSpec change; prints the agent token once."""
    started = _call(
        "POST", "/runs", {"change": change, "worktree": str(Path(worktree).resolve())}
    )
    typer.echo(_phase_line(started))
    typer.echo(f"agent token (shown once): {started['agent_token']}")


@app.command()
def runs(
    waiting: Annotated[bool, typer.Option(help="Only runs waiting for you.")] = False,
) -> None:
    """List runs."""
    listed = _call("GET", "/runs", params={"waiting": "true"} if waiting else None)
    for run in cast(list[Json], listed["runs"]):
        extra = (
            f"  waiting {float(str(run['waiting_s'])) / 60:.0f} min"
            if "waiting_s" in run
            else ""
        )
        typer.echo(f"{run['run_id']:<24} {run['phase']:<14} {run['change']}{extra}")


@app.command()
def status(run_id: str) -> None:
    """Show one run."""
    shown = _call("GET", f"/runs/{run_id}")
    typer.echo(_phase_line(shown))
    for key in ("change", "pr", "published", "gate", "trace", "publish_error"):
        if shown.get(key):
            typer.echo(f"  {key}: {shown[key]}")
    decision = shown.get("last_decision")
    if isinstance(decision, dict):
        d = cast(Json, decision)
        typer.echo(
            f"  last decision: {d['action']}"
            + (f" ({d['note']})" if d.get("note") else "")
        )
    for frozen in cast(list[Json], shown.get("frozen", [])):
        typer.echo(f"  frozen {frozen['artifact']} at {str(frozen['commit'])[:12]}")


@app.command()
def approve(run_id: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    """Approve the run's review gate."""
    typer.echo(_phase_line(_call("POST", f"/runs/{run_id}/approve", {"note": note})))


@app.command()
def reject(run_id: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    """Reject the run's review gate (a note is required)."""
    if not note:
        typer.echo("refine reject needs --note saying what to change", err=True)
        raise typer.Exit(2)
    typer.echo(_phase_line(_call("POST", f"/runs/{run_id}/reject", {"note": note})))


@app.command()
def cancel(run_id: str) -> None:
    """Cancel a run."""
    typer.echo(_phase_line(_call("POST", f"/runs/{run_id}/cancel")))


@app.command()
def resolve(
    run_id: str, to: Annotated[str, typer.Option(help="spec, tests or impl")]
) -> None:
    """Resolve an escalation."""
    typer.echo(_phase_line(_call("POST", f"/runs/{run_id}/resolve", {"to": to})))


@app.command("retry-archive")
def retry_archive(run_id: str) -> None:
    """Retry a stopped archive."""
    typer.echo(_phase_line(_call("POST", f"/runs/{run_id}/retry-archive")))

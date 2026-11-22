"""The ``refinery`` admin command: serve, and manage tokens."""

import asyncio
import dataclasses
import faulthandler
import logging
import math
import os
import signal
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from refinery.auth.tokens import Role, TokenStore
from refinery.factoryd.app import create_app, shutdown
from refinery.factoryd.config import ServerConfig, default_data_dir

app = typer.Typer(help="factoryd administration.", no_args_is_help=True)
token_app = typer.Typer(help="Manage access tokens.", no_args_is_help=True)
app.add_typer(token_app, name="token")

DataDir = Annotated[
    Path | None,
    typer.Option(
        "--data-dir",
        envvar="REFINERY_DATA_DIR",
        help="factoryd's data directory (default: $XDG_DATA_HOME/refinery).",
    ),
]


def _data_dir(data_dir: Path | None) -> Path:
    return data_dir if data_dir is not None else default_data_dir(os.environ)


@app.command()
def serve(
    data_dir: DataDir = None,
    root: Annotated[
        list[Path] | None,
        typer.Option("--root", help="A worktree factoryd manages (repeatable)."),
    ] = None,
    host: Annotated[str | None, typer.Option(envvar="REFINERY_HOST")] = None,
    port: Annotated[int | None, typer.Option(envvar="REFINERY_PORT")] = None,
) -> None:
    """Run factoryd."""
    config = ServerConfig.from_env(os.environ)
    config = dataclasses.replace(
        config,
        data_dir=data_dir or config.data_dir,
        roots=tuple(root) if root else config.roots,
        host=host or config.host,
        port=port or config.port,
    )
    # Diagnostics: `kill -USR1 <pid>` dumps every thread's stack to stderr,
    # for a hang like the one seen during the review-queue run (D10).
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    if not config.roots:
        typer.echo(
            "refinery serve needs at least one --root (or REFINERY_ROOTS)", err=True
        )
        raise typer.Exit(2)
    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            lifespan="on",
            # Bounded shutdown: without it uvicorn waits forever for a client
            # that keeps its connection open, even after a second Ctrl-C (D1).
            # uvicorn takes whole seconds: round up, never shorter than asked.
            timeout_graceful_shutdown=math.ceil(config.shutdown_timeout_s),
        )
    )
    # After uvicorn.Config, which configures uvicorn's loggers (D3).
    logging.getLogger("uvicorn.error").addFilter(_report_cut_off_requests)

    async def run() -> None:
        try:
            await server.serve()
        finally:
            await shutdown(app)  # a no-op unless uvicorn skipped the lifespan (D2)

    asyncio.run(run())
    if not server.started:
        raise typer.Exit(3)  # could not start, e.g. the port is in use


def _report_cut_off_requests(record: logging.LogRecord) -> bool:
    """One warning for the requests the shutdown timeout cut off (D3).

    uvicorn logs the count as an error, then one "Exception in ASGI
    application" traceback per cancelled request. The count becomes a single
    warning; tracebacks of cancelled requests are dropped. Every other
    record passes unchanged.
    """
    if record.msg == "Cancel %s running task(s), timeout graceful shutdown exceeded":
        count = record.args[0] if isinstance(record.args, tuple) else "?"
        plural = "" if count == 1 else "s"
        record.msg = f"shutdown timeout: cut off {count} request{plural} still open"
        record.args = ()
        record.levelno, record.levelname = logging.WARNING, "WARNING"
        return True
    exc_type = record.exc_info[0] if record.exc_info else None
    return not (exc_type is not None and issubclass(exc_type, asyncio.CancelledError))


@token_app.command("create")
def token_create(
    role: Annotated[
        str, typer.Option(help="Only 'human': agent tokens come from start_workflow.")
    ],
    data_dir: DataDir = None,
) -> None:
    """Create a human token and print it once."""
    if role != Role.HUMAN.value:
        typer.echo(
            f"cannot create a {role!r} token: agent tokens come from start_workflow, "
            "bound to the run they start",
            err=True,
        )
        raise typer.Exit(2)
    issued = TokenStore(_data_dir(data_dir)).create(Role.HUMAN)
    typer.echo(issued.secret)
    typer.echo(f"token {issued.token.id} (human): shown once, keep it safe", err=True)


@token_app.command("list")
def token_list(data_dir: DataDir = None) -> None:
    """List tokens (never their secrets)."""
    for token in TokenStore(_data_dir(data_dir)).list():
        state = (
            f"revoked {token.revoked_at:%Y-%m-%d %H:%M}"
            if token.revoked_at
            else "active"
        )
        typer.echo(
            f"{token.id}  {token.role.value:<5}  {token.run_id or '-':<20}  "
            f"{token.created_at:%Y-%m-%d %H:%M}  {state}"
        )


@token_app.command("revoke")
def token_revoke(
    token_id: Annotated[
        str, typer.Argument(help="The token's id (from `token list`).")
    ],
    data_dir: DataDir = None,
) -> None:
    """Revoke a token by id."""
    if not TokenStore(_data_dir(data_dir)).revoke(token_id):
        typer.echo(f"no token {token_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"revoked {token_id}")

"""A served factoryd, a fake ``claude``, and runners in-process or as a
subprocess, for the runner tests.

The fake records, per run (from its ``-n`` argument), under ``$FAKE_DIR/<run>``:
``argv`` (one argument per line), ``env``, ``cwd``, the MCP file's ``mcp_mode``
and ``mcp_json``, and appends its pid to ``pids``. ``$FAKE_DIR/behaviour``
says what it does next: ``exit <n>``, ``wait`` (until ``$FAKE_DIR/release``
exists, then exit 0) or ``hang`` (sleep for ever).
"""

import asyncio
import json
import os
import socket
import sys
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import uvicorn

from refinery.adapters.claude_code import AgentSettings
from refinery.factoryd.app import create_app
from refinery.factoryd.workflow import Workflow
from refinery.runner.config import RunnerConfig
from refinery.runner.loop import Runner
from tests.factoryd.conftest import Factory
from tests.factoryd.conftest import fx as fx

FAKE_CLAUDE = """#!/bin/sh
run=""; mcp=""; prev=""
for a in "$@"; do
  [ "$prev" = "-n" ] && run=$a
  [ "$prev" = "--mcp-config" ] && mcp=$a
  prev=$a
done
d="$FAKE_DIR/$run"; mkdir -p "$d"
if [ -f "$d/pids" ]; then
  for p in $(cat "$d/pids"); do
    kill -0 "$p" 2>/dev/null && echo "$p" >> "$d/alive_at_launch"
  done
fi
printf '%s\\n' "$@" > "$d/argv"
env > "$d/env"
pwd > "$d/cwd"
[ -n "$mcp" ] && stat -c %a "$mcp" > "$d/mcp_mode" && cat "$mcp" > "$d/mcp_json"
echo $$ >> "$d/pids"
b=$(cat "$FAKE_DIR/behaviour" 2>/dev/null || echo "exit 0")
case "$b" in
  exit*) exit ${b#exit } ;;
  wait) while [ ! -f "$FAKE_DIR/release" ]; do sleep 0.05; done; exit 0 ;;
  hang) exec sleep 1000 ;;
esac
"""

POLL_S = 0.2
CLAIM_S = 0.1
HUMAN_TOKEN = "rft_h_00000000_" + "0" * 64  # only ever checked for absence


@dataclass
class FakeClaude:
    """The fake agent's script and what it recorded."""

    directory: Path
    script: Path

    def behave(self, behaviour: str) -> None:
        (self.directory / "behaviour").write_text(behaviour + "\n")

    def release(self) -> None:
        (self.directory / "release").touch()

    def recorded(self, run_id: str, name: str) -> str | None:
        path = self.directory / run_id / name
        return path.read_text() if path.exists() else None

    def argv(self, run_id: str) -> list[str]:
        return (self.recorded(run_id, "argv") or "").splitlines()

    def env(self, run_id: str) -> dict[str, str]:
        lines = (self.recorded(run_id, "env") or "").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def pids(self, run_id: str) -> list[int]:
        return [int(p) for p in (self.recorded(run_id, "pids") or "").split()]

    def mcp_json(self, run_id: str) -> dict[str, object]:
        value: object = json.loads(self.recorded(run_id, "mcp_json") or "{}")
        assert isinstance(value, dict)
        return cast(dict[str, object], value)

    def mcp_server(self, run_id: str) -> dict[str, object]:
        """The ``refinery`` entry of Claude Code's MCP config file."""
        servers = cast(dict[str, object], self.mcp_json(run_id)["mcpServers"])
        return cast(dict[str, object], servers["refinery"])

    def agent_token(self, run_id: str) -> str:
        """The token the agent was given, from its MCP config file."""
        headers = cast(dict[str, str], self.mcp_server(run_id)["headers"])
        return headers["Authorization"].removeprefix("Bearer ")


@pytest.fixture
def fake(tmp_path: Path) -> FakeClaude:
    directory = tmp_path / "fake"
    directory.mkdir()
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(0o755)
    return FakeClaude(directory, script)


def alive(pid: int) -> bool:
    """The process exists and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Served:
    """A factoryd being served; ``workflow`` changes when it restarts."""

    url: str
    workflow: Workflow


@asynccontextmanager
async def factoryd(fx: Factory, port: int) -> AsyncGenerator[Served]:
    """factoryd on 127.0.0.1:``port`` until the block ends."""
    app = create_app(fx.config)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, lifespan="on", log_level="warning"
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.02)
        yield Served(f"http://127.0.0.1:{port}", cast(Workflow, app.state.workflow))
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


def runner_dir(fx: Factory) -> Path:
    return fx.data.parent / "runner"


def runner_env(fake: FakeClaude, token: str) -> dict[str, str]:
    """The runner's environment: both secret variables set, to be stripped."""
    return {
        **os.environ,
        "FAKE_DIR": str(fake.directory),
        "REFINERY_RUNNER_TOKEN": token,
        "REFINE_TOKEN": HUMAN_TOKEN,
    }


def runner_config(
    fx: Factory,
    fake: FakeClaude,
    url: str,
    token: str,
    *,
    agent_timeout_s: float = 60.0,
    max_jobs: int = 1,
) -> RunnerConfig:
    return RunnerConfig(
        url=url,
        token=token,
        directory=runner_dir(fx),
        max_jobs=max_jobs,
        poll_s=POLL_S,
        claim_interval_s=CLAIM_S,
        agent_timeout_s=agent_timeout_s,
        agent=AgentSettings(command=str(fake.script)),
        env=runner_env(fake, token),
    )


@dataclass
class Running:
    """An in-process runner and its task."""

    stop: asyncio.Event
    task: asyncio.Task[None]


def background(config: RunnerConfig) -> Running:
    """Runner(config).run as a task that outlives any one factoryd."""
    stop = asyncio.Event()
    return Running(stop, asyncio.create_task(Runner(config).run(stop)))


async def stopped(runner: Running) -> None:
    runner.stop.set()
    await asyncio.wait_for(runner.task, timeout=20)


@asynccontextmanager
async def running(config: RunnerConfig) -> AsyncGenerator[Running]:
    """Runner(config).run until the block ends; then stop it."""
    stop = asyncio.Event()
    task = asyncio.create_task(Runner(config).run(stop))
    try:
        yield Running(stop, task)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=20)


async def until(
    predicate: Callable[[], bool],
    timeout_s: float = 10.0,
    task: asyncio.Task[None] | None = None,
    what: str = "condition",
) -> None:
    """Wait for ``predicate``; a runner task that ended surfaces its error."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if task is not None and task.done():
            task.result()
            raise AssertionError(f"the runner stopped before: {what}")
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for: {what}")
        await asyncio.sleep(0.05)


def job_log(fx: Factory, run_id: str) -> list[dict[str, object]]:
    path = fx.data / "runs" / run_id / "jobs.jsonl"
    if not path.is_file():
        return []
    return [
        cast(dict[str, object], json.loads(line))
        for line in path.read_text().splitlines()
    ]


def events(fx: Factory, run_id: str, name: str) -> list[dict[str, object]]:
    return [e for e in job_log(fx, run_id) if e["event"] == name]


def runner_subprocess_argv() -> list[str]:
    """``refinery runner`` in this interpreter."""
    return [
        sys.executable,
        "-c",
        "from refinery.factoryd.cli import app; app()",
        "runner",
    ]


def runner_subprocess_env(
    fx: Factory, fake: FakeClaude, url: str, token: str
) -> dict[str, str]:
    return {
        **runner_env(fake, token),
        "REFINE_URL": url,
        "REFINERY_RUNNER_DIR": str(runner_dir(fx)),
        "REFINERY_RUNNER_POLL": str(POLL_S),
        "REFINERY_RUNNER_CLAIM_INTERVAL": str(CLAIM_S),
        "REFINERY_AGENT_COMMAND": str(fake.script),
    }

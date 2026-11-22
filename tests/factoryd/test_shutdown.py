"""Requirement "Shutdown" (factoryd-server), after the review-queue run where
`refinery serve` hung on Ctrl-C (and ignored SIGTERM) with Claude Code
connected. That hang could not be reproduced; these tests are a regression
guard (SIGINT with an MCP session open exits promptly) and the diagnostics
for next time (SIGUSR1 dumps every thread's stack without stopping the
server)."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from refinery.auth.tokens import Role, TokenStore
from tests.gate.conftest import GIT_ENV, make_repo

EXIT_WITHIN_S = 10.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_sigint_with_an_open_mcp_stream(tmp_path: Path) -> None:
    root, data = tmp_path / "repo", tmp_path / "data"
    make_repo(root)
    token = TokenStore(data).create(Role.HUMAN).secret
    port = free_port()
    env = {**os.environ, **GIT_ENV, "REFINERY_DATA_DIR": str(data)}
    server = subprocess.Popen(
        [sys.executable, "-c", "import refinery; refinery.main()", "serve",
         "--root", str(root), "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "factoryd did not start"
                time.sleep(0.1)

        async def hold_stream_then_interrupt() -> float:
            url = f"http://127.0.0.1:{port}/mcp"
            headers = {"Authorization": f"Bearer {token}"}
            async with (
                httpx2.AsyncClient(headers=headers, timeout=60) as http,
                streamable_http_client(
                    url, http_client=http, terminate_on_close=False
                ) as (
                    read,
                    write,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                await asyncio.sleep(0.5)  # the client's stream is open now
                server.send_signal(signal.SIGINT)
                started = time.monotonic()
                while (  # noqa: ASYNC110 - polling a child process
                    server.poll() is None
                    and time.monotonic() - started < EXIT_WITHIN_S + 5
                ):
                    await asyncio.sleep(0.1)
                return time.monotonic() - started

        elapsed = asyncio.run(hold_stream_then_interrupt())
        assert server.poll() is not None, "factoryd still running after SIGINT"
        assert elapsed < EXIT_WITHIN_S
    finally:
        if server.poll() is None:
            server.kill()
            server.wait(timeout=10)


def test_sigusr1_dumps_stacks_and_keeps_serving(tmp_path: Path) -> None:
    root, data = tmp_path / "repo", tmp_path / "data"
    make_repo(root)
    port = free_port()
    log = tmp_path / "serve.log"
    env = {**os.environ, **GIT_ENV, "REFINERY_DATA_DIR": str(data)}
    with log.open("wb") as out:
        server = subprocess.Popen(
            [sys.executable, "-c", "import refinery; refinery.main()", "serve",
             "--root", str(root), "--port", str(port)],
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )  # fmt: skip
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "factoryd did not start"
                time.sleep(0.1)
        server.send_signal(signal.SIGUSR1)
        deadline = time.monotonic() + 10
        while "most recent call first" not in log.read_text(errors="replace"):
            assert time.monotonic() < deadline, "no stack dump after SIGUSR1"
            time.sleep(0.1)
        assert server.poll() is None, "SIGUSR1 must not stop factoryd"
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    finally:
        server.kill()
        server.wait(timeout=10)

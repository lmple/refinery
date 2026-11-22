"""Requirements "Shutdown", "Bounded shutdown" and "Cut-off requests are logged
once" (factoryd-server).

`refinery serve` hung on Ctrl-C during the review-queue run, with Claude Code
connected. uvicorn's forced exit still awaits ``server.wait_closed()``, which
on Python >= 3.12.1 waits for every connection to drop, so one connection
that never closes hangs shutdown forever. The stalled-connection tests
reproduce it with a POST whose body never finishes. The first test is the
original guard: an MCP session that closes its own stream. SIGUSR1 dumps
every thread's stack without stopping the server.
"""

import asyncio
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from refinery.auth.tokens import Role, TokenStore
from refinery.proc import registry
from refinery.run.model import Action, Phase
from refinery.run.store import RunStore
from tests.gate.conftest import GIT_ENV, RUN, drive_to_verify, make_repo
from tests.proc.test_supervisor import group_alive, wait_for

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


# Bounded shutdown: a client that never lets go.

SHUTDOWN_TIMEOUT_S = 1
# The scenarios' bound: the timeout, plus cancelling gate runs (the
# supervisor's 2 s grace before SIGKILL), plus bookkeeping (design D4).
BOUND_S = 6.0


@contextmanager
def serving(
    tmp_path: Path, root: Path, data: Path, extra_env: dict[str, str] | None = None
) -> Generator[tuple[subprocess.Popen[bytes], int, Path]]:
    """`refinery serve` with a 1 s shutdown timeout; killed if still running."""
    port = free_port()
    log = tmp_path / "serve.log"
    env = {
        **os.environ,
        **GIT_ENV,
        "REFINERY_DATA_DIR": str(data),
        "REFINERY_SHUTDOWN_TIMEOUT": str(SHUTDOWN_TIMEOUT_S),
        **(extra_env or {}),
    }
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
        yield server, port, log
    finally:
        if server.poll() is None:
            server.kill()
            server.wait(timeout=10)


@contextmanager
def stalled_connection(port: int, token: str) -> Generator[socket.socket]:
    """A POST /mcp that announces 100 bytes of body and sends 10."""
    conn = socket.create_connection(("127.0.0.1", port), timeout=5)
    head = (
        "POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Content-Type: application/json\r\n"
        "Accept: application/json, text/event-stream\r\n"
        "Content-Length: 100\r\n\r\n"
    )
    conn.sendall(head.encode() + b'{"jsonrpc"')
    time.sleep(0.5)  # the server is now waiting for the rest of the body
    try:
        yield conn
    finally:
        conn.close()


def exit_time(server: subprocess.Popen[bytes], signals: list[signal.Signals]) -> float:
    """Send ``signals`` 0.5 s apart; seconds from the first until exit (or more
    than the bound if it is still running)."""
    started = time.monotonic()
    for i, sig in enumerate(signals):
        if i:
            time.sleep(0.5)
        if server.poll() is None:
            server.send_signal(sig)
    while server.poll() is None and time.monotonic() - started < BOUND_S + 4:
        time.sleep(0.05)
    return time.monotonic() - started


def stalled_shutdown(
    tmp_path: Path, signals: list[signal.Signals]
) -> tuple[float, str]:
    root, data = tmp_path / "repo", tmp_path / "data"
    make_repo(root)
    token = TokenStore(data).create(Role.HUMAN).secret
    with (
        serving(tmp_path, root, data) as (server, port, log),
        stalled_connection(port, token),
    ):
        elapsed = exit_time(server, signals)
        still_running = server.poll() is None
    assert not still_running, f"factoryd still running {elapsed:.1f}s after {signals}"
    return elapsed, log.read_text(errors="replace")


def test_sigint_with_a_stalled_connection(tmp_path: Path) -> None:
    elapsed, _ = stalled_shutdown(tmp_path, [signal.SIGINT])
    assert elapsed < BOUND_S


def test_sigterm_with_a_stalled_connection(tmp_path: Path) -> None:
    elapsed, _ = stalled_shutdown(tmp_path, [signal.SIGTERM])
    assert elapsed < BOUND_S


def test_sigint_twice_with_a_stalled_connection(tmp_path: Path) -> None:
    elapsed, _ = stalled_shutdown(tmp_path, [signal.SIGINT, signal.SIGINT])
    assert elapsed < BOUND_S


# Shutdown: the forced exit still cancels gate runs and closes the index.


def test_forced_exit_still_runs_factoryds_shutdown(tmp_path: Path) -> None:
    root, data, behaviours = tmp_path / "repo", tmp_path / "data", tmp_path / "b"
    head = make_repo(root)
    behaviours.mkdir()
    pidfile = behaviours / "test.pid"
    (behaviours / "test.sh").write_text(f"echo $$ > {pidfile}; exec sleep 30\n")
    token = TokenStore(data).create(Role.HUMAN).secret

    async def leave_in_verify() -> None:
        store = await RunStore.open(data, [root])
        await drive_to_verify(store, root, head)
        store.close()

    asyncio.run(leave_in_verify())
    pgid = None
    try:
        with serving(tmp_path, root, data, {"BEHAVIOUR_DIR": str(behaviours)}) as (
            server,
            port,
            _,
        ):
            # factoryd schedules the gate of every run in verify at startup.
            pgid = int(asyncio.run(wait_for(pidfile, limit_s=30)))
            with stalled_connection(port, token):
                elapsed = exit_time(server, [signal.SIGINT, signal.SIGINT])
                still_running = server.poll() is None
        assert not still_running, (
            f"factoryd still running {elapsed:.1f}s after 2 SIGINTs"
        )
        assert elapsed < BOUND_S
        assert not group_alive(pgid), "the gate step outlived factoryd"
    finally:
        if pgid is not None and group_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)

    async def check() -> None:
        store = await RunStore.open(data, [root])
        assert store.get(RUN).phase is Phase.VERIFY
        assert store.events(RUN)[-1].name is Action.COMPLETE_IMPL
        store.close()

    asyncio.run(check())
    assert registry.list_entries(data) == []


# Cut-off requests are logged once.


def test_cut_off_requests_are_logged_once(tmp_path: Path) -> None:
    _, log = stalled_shutdown(tmp_path, [signal.SIGINT])
    assert "Traceback" not in log
    assert "CancelledError" not in log
    cut = [line for line in log.splitlines() if re.search(r"cut off", line, re.I)]
    assert len(cut) == 1, f"expected one cut-off line, got {cut}"
    assert re.search(r"\b1 request\b", cut[0]), cut[0]

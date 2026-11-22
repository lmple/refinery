"""The supervisor: process group, start latch, time limit, cleanup (D1 to D3).

Every subprocess Refinery starts goes through ``Supervisor.run``; this is the
only module allowed to create processes. ``spec/Gate.tla`` models a call as
Spawn (latch parked), Register, Release, then Exit or Timeout.
"""

import asyncio
import contextlib
import dataclasses
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO

from refinery.proc import registry

# Waits on stdin until released, then execs the real command in place (same
# pid, same process group). If the supervisor dies first, stdin closes, read
# fails, and the command never runs.
_LATCH = ("/bin/sh", "-c", 'read -r _ || exit 125; exec "$@"', "refinery-latch")
_POLL_S = 0.02
_KILL_WAIT_S = 5.0  # how long a SIGKILLed group may take to disappear


class Outcome(StrEnum):
    """How a supervised call ended. Cancellation propagates as CancelledError."""

    EXITED = "exited"
    SIGNALLED = "signalled"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED_TO_START = "failed_to_start"


@dataclass(frozen=True)
class Capture:
    """Return stdout and stderr in the result."""


@dataclass(frozen=True)
class StreamTo:
    """Append stdout and stderr to ``path``."""

    path: Path


type Output = Capture | StreamTo


@dataclass(frozen=True)
class CallResult:
    """The result of one supervised call."""

    outcome: Outcome
    duration_s: float
    exit_code: int | None = None
    signal: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Exited with code 0."""
        return self.outcome is Outcome.EXITED and self.exit_code == 0


@dataclass(frozen=True)
class ReapReport:
    """What a startup reap did."""

    killed: tuple[int, ...]
    spared: tuple[int, ...]


def _resolve(program: str, cwd: Path, env: Mapping[str, str] | None) -> str | None:
    """The executable that would run, or None.

    Failure to start is decided here, before spawning, so every exit code a
    call reports is the command's own.
    """
    if "/" in program:
        path = Path(program) if Path(program).is_absolute() else cwd / program
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    search = (env if env is not None else os.environ).get("PATH", os.defpath)
    return shutil.which(program, path=search)


class Supervisor:
    """Starts every subprocess; owns the registry under ``data_dir``."""

    def __init__(self, data_dir: Path, *, grace_s: float = 2.0) -> None:
        """``grace_s`` is the wait between SIGTERM and SIGKILL."""
        self.data_dir = data_dir
        self.grace_s = grace_s

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        output: Output | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CallResult:
        """Run ``argv`` to completion; nothing of it survives the call."""
        started = time.monotonic()
        output = output if output is not None else Capture()
        program = _resolve(argv[0], cwd, env) if argv else None
        if program is None:
            name = argv[0] if argv else "(empty argv)"
            return CallResult(
                Outcome.FAILED_TO_START,
                time.monotonic() - started,
                reason=f"cannot execute {name!r}",
            )
        # Output goes to files, never pipes: a leftover child holding a pipe
        # open would keep asyncio's wait() from returning after the command
        # exits. Captured output uses unnamed temporary files.
        with contextlib.ExitStack() as files:
            if isinstance(output, StreamTo):
                output.path.parent.mkdir(parents=True, exist_ok=True)
                out = err = files.enter_context(output.path.open("ab"))
            else:
                out = files.enter_context(tempfile.TemporaryFile())
                err = files.enter_context(tempfile.TemporaryFile())
            try:
                proc = await asyncio.create_subprocess_exec(
                    *_LATCH,
                    program,
                    *argv[1:],
                    stdin=asyncio.subprocess.PIPE,
                    stdout=out.fileno(),
                    stderr=err.fileno(),
                    cwd=cwd,
                    env=dict(env) if env is not None else None,
                    start_new_session=True,
                )
            except OSError as exc:
                return CallResult(
                    Outcome.FAILED_TO_START, time.monotonic() - started, reason=str(exc)
                )
            result = await self._supervise(proc, argv, timeout_s, started)
            if isinstance(output, StreamTo):
                out.flush()
                os.fsync(out.fileno())
                return result
            return dataclasses.replace(result, stdout=_read(out), stderr=_read(err))

    async def _supervise(
        self,
        proc: asyncio.subprocess.Process,
        argv: Sequence[str],
        timeout_s: float,
        started: float,
    ) -> CallResult:
        pgid = proc.pid  # start_new_session: the leader's pid is the group id
        try:
            try:
                ticks = registry.process_start_ticks(pgid) or 0
                entry = registry.Entry(pgid, ticks, tuple(argv), datetime.now(UTC))
                registry.write_entry(self.data_dir, entry)
            except OSError as exc:
                await self._cleanup(proc, pgid, graceful=False)
                return CallResult(
                    Outcome.FAILED_TO_START,
                    time.monotonic() - started,
                    reason=f"cannot register process group: {exc}",
                )
            await _release(proc)
            timed_out = False
            try:
                async with asyncio.timeout(timeout_s):
                    await proc.wait()
            except TimeoutError:
                timed_out = True
            # Exited or not: whatever is left of the group goes now, and only
            # then may the call return (nothing outlives it).
            await self._cleanup(proc, pgid, graceful=timed_out)
        except asyncio.CancelledError:
            # Shielded: a second cancellation must not strand the group.
            await asyncio.shield(self._cleanup(proc, pgid, graceful=True))
            raise
        duration = time.monotonic() - started
        if timed_out:
            return CallResult(Outcome.TIMED_OUT, duration)
        code = proc.returncode
        assert code is not None
        if code < 0:
            return CallResult(Outcome.SIGNALLED, duration, signal=-code)
        return CallResult(Outcome.EXITED, duration, exit_code=code)

    async def _cleanup(
        self, proc: asyncio.subprocess.Process, pgid: int, *, graceful: bool
    ) -> None:
        """Kill the group (TERM, grace, then KILL if ``graceful``); unregister."""
        if graceful and registry.group_alive(pgid):
            _signal_group(pgid, signal.SIGTERM)
            await _wait_gone(pgid, self.grace_s)
        if registry.group_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)
            await _wait_gone(pgid, _KILL_WAIT_S)
        if proc.stdin is not None:
            proc.stdin.close()  # an unreleased latch sees EOF and exits
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        registry.remove_entry(self.data_dir, pgid)

    def reap(self) -> ReapReport:
        """Kill every registered group that is verifiably still ours (startup)."""
        killed: list[int] = []
        spared: list[int] = []
        for entry in registry.list_entries(self.data_dir):
            ticks = registry.process_start_ticks(entry.pgid)
            if ticks is not None and ticks != entry.start_ticks:
                spared.append(entry.pgid)  # the pid now belongs to someone else
            elif registry.group_alive(entry.pgid):
                # The leader is still ours, or gone while its group lives on: a
                # group id cannot be reused while the group exists.
                _signal_group(entry.pgid, signal.SIGKILL)
                _wait_gone_blocking(entry.pgid, _KILL_WAIT_S)
                killed.append(entry.pgid)
            registry.remove_entry(self.data_dir, entry.pgid)
        return ReapReport(tuple(killed), tuple(spared))


async def _release(proc: asyncio.subprocess.Process) -> None:
    """Let the latch exec the command (its registry entry is durable)."""
    if proc.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.write(b"go\n")
        await proc.stdin.drain()
    proc.stdin.close()


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


async def _wait_gone(pgid: int, limit_s: float) -> None:
    deadline = time.monotonic() + limit_s
    # Polling /proc is the only way to see a whole group (children included)
    # disappear; there is no event to wait on.
    while registry.group_alive(pgid) and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(_POLL_S)


def _wait_gone_blocking(pgid: int, limit_s: float) -> None:
    deadline = time.monotonic() + limit_s
    while registry.group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(_POLL_S)


def _read(file: IO[bytes]) -> str:
    file.seek(0)
    return file.read().decode(errors="replace")

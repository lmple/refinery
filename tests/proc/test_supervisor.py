"""Scenarios of specs/process-supervision/spec.md (gate-runner change).

Tests may start processes directly (subprocess) to play the part of an
orphan or an unrelated process; the one-entry-point rule covers src/ only.
"""

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from refinery.proc import registry
from refinery.proc.supervisor import Capture, Outcome, StreamTo, Supervisor
from tests.run.helpers import run_async

ROOT = Path(__file__).resolve().parents[2]
GRACE = 0.5


def supervisor(tmp_path: Path) -> Supervisor:
    return Supervisor(tmp_path / "data", grace_s=GRACE)


def _stat(pid: int) -> list[str] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Fields after the parenthesised command name: state is first, pgrp third.
    return text.rsplit(")", 1)[1].split()


def alive(pid: int) -> bool:
    """Exists and is not a zombie."""
    fields = _stat(pid)
    return fields is not None and fields[0] != "Z"


def group_alive(pgid: int) -> bool:
    """Any non-zombie process in process group ``pgid``."""
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = _stat(int(entry.name))
            if fields and fields[0] != "Z" and int(fields[2]) == pgid:
                return True
    return False


def _contents(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""


async def wait_for(path: Path, limit_s: float = 10.0) -> str:
    """The contents of ``path`` once a command has written it."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if text := _contents(path):
            return text
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def eventually(predicate: object, timeout: float = 5.0) -> bool:
    assert callable(predicate)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


# Requirement: One entry point for subprocesses


def test_only_the_supervisor_starts_processes() -> None:
    pattern = re.compile(
        r"\bimport subprocess\b|\bfrom subprocess\b|create_subprocess_|os\.system\("
        r"|os\.spawn|os\.exec|os\.fork\(|os\.posix_spawn"
    )
    allowed = ROOT / "src/refinery/proc/supervisor.py"
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src/refinery").rglob("*.py")
        if path != allowed and pattern.search(path.read_text())
    ]
    assert offenders == []


# Requirement: Own process group


def test_group_differs_from_the_supervisors(tmp_path: Path) -> None:
    script = "import os; print(os.getpid(), os.getpgid(0))"
    result = run_async(
        supervisor(tmp_path).run(
            [sys.executable, "-c", script], cwd=tmp_path, timeout_s=10, output=Capture()
        )
    )
    assert result.ok
    assert result.stdout is not None
    pid, pgid = map(int, result.stdout.split())
    assert pid == pgid
    assert pgid != os.getpgid(0)


# Requirement: Nothing outlives a call


def test_background_child_is_cleaned_up(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_async(
        supervisor(tmp_path).run(
            ["sh", "-c", "sleep 60 & echo $!"],
            cwd=tmp_path,
            timeout_s=10,
            output=Capture(),
        )
    )
    assert (result.outcome, result.exit_code) == (Outcome.EXITED, 0)
    assert result.stdout is not None
    assert not alive(int(result.stdout))
    # The leftover held the output pipe open; the call must not wait for it.
    assert time.monotonic() - started < 5


def test_background_child_is_cleaned_up_when_streaming(tmp_path: Path) -> None:
    log = tmp_path / "step.log"
    pidfile = tmp_path / "child.pid"
    result = run_async(
        supervisor(tmp_path).run(
            ["sh", "-c", f"sleep 60 & echo $! > {pidfile}"],
            cwd=tmp_path,
            timeout_s=10,
            output=StreamTo(log),
        )
    )
    assert result.ok
    assert not alive(int(pidfile.read_text()))


# Requirement: Time limit


def test_command_exceeds_its_limit(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    result = run_async(
        supervisor(tmp_path).run(
            ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"],
            cwd=tmp_path,
            timeout_s=0.2,
        )
    )
    assert result.outcome is Outcome.TIMED_OUT
    assert time.monotonic() - started < 0.2 + GRACE + 2
    assert not group_alive(int(pidfile.read_text()))


def test_command_ignoring_sigterm_is_killed(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = f"trap '' TERM; echo $$ > {pidfile}; while :; do sleep 0.05; done"
    started = time.monotonic()
    result = run_async(
        supervisor(tmp_path).run(["sh", "-c", script], cwd=tmp_path, timeout_s=0.2)
    )
    elapsed = time.monotonic() - started
    assert result.outcome is Outcome.TIMED_OUT
    assert elapsed >= 0.2 + GRACE - 0.05  # waited the grace period first
    assert not group_alive(int(pidfile.read_text()))


# Requirement: Cancellation


def test_caller_cancelled(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"

    async def go() -> int:
        task = asyncio.create_task(
            supervisor(tmp_path).run(
                ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"],
                cwd=tmp_path,
                timeout_s=60,
            )
        )
        pgid = int(await wait_for(pidfile))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pgid

    pgid = run_async(go())
    assert not group_alive(pgid)


# Requirement: Call outcomes


def test_missing_program(tmp_path: Path) -> None:
    result = run_async(
        supervisor(tmp_path).run(
            ["does-not-exist-refinery"], cwd=tmp_path, timeout_s=10
        )
    )
    assert result.outcome is Outcome.FAILED_TO_START
    assert result.reason
    assert not result.ok


def test_non_executable_path(tmp_path: Path) -> None:
    script = tmp_path / "not-executable.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    result = run_async(
        supervisor(tmp_path).run([str(script)], cwd=tmp_path, timeout_s=10)
    )
    assert result.outcome is Outcome.FAILED_TO_START


def test_non_zero_exit(tmp_path: Path) -> None:
    result = run_async(
        supervisor(tmp_path).run(["sh", "-c", "exit 3"], cwd=tmp_path, timeout_s=10)
    )
    assert (result.outcome, result.exit_code) == (Outcome.EXITED, 3)
    assert not result.ok


def test_killed_by_a_signal(tmp_path: Path) -> None:
    result = run_async(
        supervisor(tmp_path).run(["sh", "-c", "kill -9 $$"], cwd=tmp_path, timeout_s=10)
    )
    assert (result.outcome, result.signal) == (Outcome.SIGNALLED, signal.SIGKILL)


def test_runs_in_the_given_directory(tmp_path: Path) -> None:
    result = run_async(
        supervisor(tmp_path).run(["pwd"], cwd=tmp_path, timeout_s=10, output=Capture())
    )
    assert result.stdout == f"{tmp_path}\n"


def test_environment_is_explicit(tmp_path: Path) -> None:
    result = run_async(
        supervisor(tmp_path).run(
            ["sh", "-c", 'echo "$ONLY_THIS"'],
            cwd=tmp_path,
            timeout_s=10,
            output=Capture(),
            env={"ONLY_THIS": "yes", "PATH": os.environ["PATH"]},
        )
    )
    assert result.stdout == "yes\n"


# Requirement: Output captured or streamed


def test_captured_output(tmp_path: Path) -> None:
    result = run_async(
        supervisor(tmp_path).run(
            ["sh", "-c", "echo out; echo err >&2"],
            cwd=tmp_path,
            timeout_s=10,
            output=Capture(),
        )
    )
    assert (result.stdout, result.stderr) == ("out\n", "err\n")


def test_streamed_output(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "step.log"
    log.parent.mkdir()
    log.write_text("earlier\n")
    result = run_async(
        supervisor(tmp_path).run(
            ["sh", "-c", "echo out; echo err >&2"],
            cwd=tmp_path,
            timeout_s=10,
            output=StreamTo(log),
        )
    )
    assert result.ok
    assert (result.stdout, result.stderr) == (None, None)
    lines = log.read_text().splitlines()
    assert lines[0] == "earlier"  # appended, not truncated
    assert sorted(lines[1:]) == ["err", "out"]


def test_default_output_is_captured(tmp_path: Path) -> None:
    result = run_async(
        supervisor(tmp_path).run(["echo", "hi"], cwd=tmp_path, timeout_s=10)
    )
    assert result.stdout == "hi\n"


# Requirement: Registry before the command runs


def test_entry_exists_while_running(tmp_path: Path) -> None:
    sup = supervisor(tmp_path)
    pidfile = tmp_path / "pid"

    async def go() -> None:
        task = asyncio.create_task(
            sup.run(
                ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"],
                cwd=tmp_path,
                timeout_s=60,
            )
        )
        pgid = int(await wait_for(pidfile))
        entries = registry.list_entries(sup.data_dir)
        assert [e.pgid for e in entries] == [pgid]
        assert entries[0].start_ticks == registry.process_start_ticks(pgid)
        assert entries[0].argv[-3:] == (
            "sh",
            "-c",
            f"echo $$ > {pidfile}; exec sleep 60",
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_async(go())
    assert registry.list_entries(sup.data_dir) == []


def test_entry_removed_after_a_normal_exit(tmp_path: Path) -> None:
    sup = supervisor(tmp_path)
    assert run_async(sup.run(["true"], cwd=tmp_path, timeout_s=10)).ok
    assert registry.list_entries(sup.data_dir) == []


_DIES_BEFORE_REGISTERING = """
import asyncio, sys, time
from pathlib import Path
from refinery.proc import registry
from refinery.proc.supervisor import Supervisor

data, marker, ready = (Path(a) for a in sys.argv[1:4])

def stuck(data_dir, entry):
    ready.write_text(str(entry.pgid))
    time.sleep(3600)

registry.write_entry = stuck
command = ["sh", "-c", f"touch {marker}"]
asyncio.run(Supervisor(data).run(command, cwd=data, timeout_s=60))
"""


def test_supervisor_dies_before_registering(tmp_path: Path) -> None:
    data, marker, ready = tmp_path / "data", tmp_path / "ran", tmp_path / "ready"
    data.mkdir()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _DIES_BEFORE_REGISTERING,
            str(data),
            str(marker),
            str(ready),
        ]
    )
    try:
        assert eventually(
            lambda: ready.exists() and ready.read_text() != "", timeout=20
        )
        latch = int(ready.read_text())
        assert group_alive(latch)  # parked on its latch
        child.kill()
        child.wait(timeout=10)
        assert eventually(lambda: not group_alive(latch))
    finally:
        if child.poll() is None:
            child.kill()
    assert not marker.exists()


# Requirement: Reaping after a restart


def _entry(pid: int, ticks: int | None) -> registry.Entry:
    assert ticks is not None
    return registry.Entry(
        pgid=pid, start_ticks=ticks, argv=("sleep", "60"), started_at=datetime.now(UTC)
    )


def test_orphan_from_a_crash_is_killed(tmp_path: Path) -> None:
    sup = supervisor(tmp_path)
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        registry.write_entry(
            sup.data_dir, _entry(orphan.pid, registry.process_start_ticks(orphan.pid))
        )
        report = sup.reap()
        assert report.killed == (orphan.pid,)
        assert orphan.wait(timeout=5) == -signal.SIGKILL
        assert registry.list_entries(sup.data_dir) == []
    finally:
        if orphan.poll() is None:
            orphan.kill()


def test_orphan_group_with_dead_leader_is_killed(tmp_path: Path) -> None:
    sup = supervisor(tmp_path)
    pidfile = tmp_path / "child.pid"
    leader = subprocess.Popen(
        ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; exit 0"], start_new_session=True
    )
    ticks = registry.process_start_ticks(leader.pid)
    leader.wait(timeout=10)
    child = int(pidfile.read_text())
    try:
        registry.write_entry(sup.data_dir, _entry(leader.pid, ticks))
        report = sup.reap()
        assert report.killed == (leader.pid,)
        assert eventually(lambda: not alive(child))
    finally:
        if alive(child):
            os.kill(child, signal.SIGKILL)


def test_reused_process_id_is_spared(tmp_path: Path) -> None:
    sup = supervisor(tmp_path)
    unrelated = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        ticks = registry.process_start_ticks(unrelated.pid)
        assert ticks is not None
        registry.write_entry(sup.data_dir, _entry(unrelated.pid, ticks + 12345))
        report = sup.reap()
        assert report.spared == (unrelated.pid,)
        assert report.killed == ()
        assert unrelated.poll() is None
        assert registry.list_entries(sup.data_dir) == []
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_gone_group_is_just_forgotten(tmp_path: Path) -> None:
    sup = supervisor(tmp_path)
    gone = subprocess.Popen(["true"], start_new_session=True)
    ticks = registry.process_start_ticks(gone.pid) or 1
    gone.wait(timeout=5)
    registry.write_entry(sup.data_dir, _entry(gone.pid, ticks))
    report = sup.reap()
    assert (report.killed, report.spared) == ((), ())
    assert registry.list_entries(sup.data_dir) == []


def test_registry_round_trips(tmp_path: Path) -> None:
    entry = registry.Entry(
        pgid=4242,
        start_ticks=99,
        argv=("a", "b"),
        started_at=datetime(2026, 11, 22, tzinfo=UTC),
    )
    registry.write_entry(tmp_path, entry)
    assert registry.list_entries(tmp_path) == [entry]
    assert registry.registry_dir(tmp_path) == tmp_path / "procs"
    registry.remove_entry(tmp_path, 4242)
    registry.remove_entry(tmp_path, 4242)  # idempotent
    assert registry.list_entries(tmp_path) == []

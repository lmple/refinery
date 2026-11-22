"""GateRunner: runs a gate for a run in verify and records the result (D4).

``spec/Gate.tla`` models it: steps one at a time (OneStepAtATime), a result
only from a finished gate run (ResultOnlyFromFinishedGate), PassGate only if
every step passed (PassOnlyIfAllStepsPassed), and the running step killed if
the run leaves verify (Abort).
"""

import asyncio
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from refinery.gate.frozen import check_frozen
from refinery.proc.supervisor import Outcome, StreamTo, Supervisor
from refinery.run.model import Action, Phase, Refused, Role, TransitionRequest
from refinery.run.model import Outcome as TransitionOutcome
from refinery.run.store import RunStore


class Step(StrEnum):
    """Gate steps, in the order they run."""

    FROZEN = "frozen"
    TEST = "test"
    TYPES = "types"
    LINT = "lint"
    TLA = "tla"


DEFAULT_TIMEOUTS: Mapping[Step, float] = {
    Step.FROZEN: 60.0,
    Step.TEST: 600.0,
    Step.TYPES: 300.0,
    Step.LINT: 120.0,
    Step.TLA: 1800.0,
}


@dataclass(frozen=True)
class GateConfig:
    """How gate steps are run."""

    timeouts: Mapping[Step, float] = field(
        default_factory=lambda: dict(DEFAULT_TIMEOUTS)
    )
    make: str = "make"
    git: str = "git"
    tla_jar: Path | None = None  # passed to make as TLA_JAR
    env: Mapping[str, str] | None = None  # None: factoryd's environment
    poll_s: float = 0.2  # how often a running step checks the run is in verify


@dataclass(frozen=True)
class StepResult:
    """One step of a gate run."""

    step: Step
    passed: bool
    outcome: Outcome
    exit_code: int | None
    duration_s: float
    log: Path
    detail: str = ""


@dataclass(frozen=True)
class GateReport:
    """A gate run that finished, or stopped because the run left verify."""

    run_id: str
    gate_id: str
    directory: Path
    steps: tuple[StepResult, ...]
    passed: bool
    # The PassGate/FailGate outcome; None if the run left verify mid-gate.
    transition: TransitionOutcome | None


class GateRunner:
    """Runs gates; at most one per run at a time."""

    def __init__(
        self,
        store: RunStore,
        supervisor: Supervisor,
        config: GateConfig,
        data_dir: Path,
    ) -> None:
        """Gate records go under ``data_dir``."""
        self._store = store
        self._supervisor = supervisor
        self._config = config
        self._data_dir = data_dir
        self._tasks: dict[str, asyncio.Task[object]] = {}

    def running(self, run_id: str) -> bool:
        """Whether a gate run is in progress for ``run_id``."""
        return run_id in self._tasks

    def cancel(self, run_id: str) -> bool:
        """Cancel the run's gate run, killing its step; False if none runs."""
        task = self._tasks.get(run_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def run_gate(self, run_id: str) -> GateReport | Refused:
        """Run every step, then record PassGate or FailGate."""
        if run_id in self._tasks:
            return Refused(f"a gate run is already in progress for {run_id}")
        try:
            record = self._store.get(run_id)
        except KeyError:
            return Refused(f"unknown run {run_id}")
        if record.phase is not Phase.VERIFY:
            return Refused(f"gate runs need phase verify, not {record.phase}")
        task = asyncio.current_task()
        assert task is not None
        self._tasks[run_id] = task  # no await since the check above: atomic
        try:
            return await self._gate(run_id)
        finally:
            del self._tasks[run_id]

    async def _gate(self, run_id: str) -> GateReport:
        directory = self._new_gate_dir(run_id)
        worktree = self._store.worktree(run_id)
        results: list[StepResult] = []
        for step in Step:
            if not self._in_verify(run_id):
                return self._report(run_id, directory, results, None)
            result = await self._watched(run_id, step, directory, worktree)
            results.append(result)
            if result.outcome is Outcome.CANCELLED:
                return self._report(run_id, directory, results, None)
        passed = all(r.passed for r in results)
        _write_json(
            directory / "result.json",
            _result_json(run_id, directory.name, results, passed),
        )
        failed = [r.step.value for r in results if not r.passed]
        request = TransitionRequest(
            Action.PASS_GATE if passed else Action.FAIL_GATE,
            Role.FACTORYD,
            datetime.now(UTC),
            gate=directory.name,
            reason=None if passed else f"failed: {', '.join(failed)}",
        )
        transition = await self._store.transition(run_id, request)
        return self._report(run_id, directory, results, transition)

    def _new_gate_dir(self, run_id: str) -> Path:
        """``g<n+1>``: ids of earlier gate runs, finished or not, are never reused."""
        gates = self._data_dir / "runs" / run_id / "gates"
        taken = [int(p.name[1:]) for p in gates.glob("g*") if p.name[1:].isdigit()]
        directory = gates / f"g{max(taken, default=0) + 1}"
        directory.mkdir(parents=True)
        return directory

    def _in_verify(self, run_id: str) -> bool:
        return self._store.get(run_id).phase is Phase.VERIFY

    async def _watched(
        self, run_id: str, step: Step, directory: Path, worktree: Path
    ) -> StepResult:
        """Run one step; kill it if the run leaves verify meanwhile."""
        started = time.monotonic()
        log = directory / f"{step}.log"
        work = asyncio.create_task(self._step(step, log, run_id, worktree))
        try:
            while True:
                done, _ = await asyncio.wait({work}, timeout=self._config.poll_s)
                if done:
                    return work.result()
                if not self._in_verify(run_id):
                    work.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await work
                    _append(log, "stopped: the run left verify\n")
                    return StepResult(
                        step,
                        False,
                        Outcome.CANCELLED,
                        None,
                        time.monotonic() - started,
                        log,
                        "the run left verify",
                    )
        except asyncio.CancelledError:
            # The gate run itself was cancelled: take the step down with it.
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(work)
            raise

    async def _step(
        self, step: Step, log: Path, run_id: str, worktree: Path
    ) -> StepResult:
        _append(log, "")  # every step that started has a log
        limit = self._config.timeouts.get(step, DEFAULT_TIMEOUTS[step])
        if step is Step.FROZEN:
            started = time.monotonic()
            frozen = await check_frozen(
                self._supervisor,
                worktree,
                self._store.get(run_id),
                git=self._config.git,
                timeout_s=limit,
                env=self._config.env,
            )
            _append(log, frozen.detail + "\n")
            return StepResult(
                step,
                frozen.passed,
                Outcome.EXITED,
                0 if frozen.passed else 1,
                time.monotonic() - started,
                log,
                frozen.detail,
            )
        argv = [self._config.make, step.value]
        if self._config.tla_jar is not None:
            argv.append(f"TLA_JAR={self._config.tla_jar}")
        call = await self._supervisor.run(
            argv,
            cwd=worktree,
            timeout_s=limit,
            output=StreamTo(log),
            env=self._config.env,
        )
        if call.reason:
            _append(log, f"{call.outcome}: {call.reason}\n")
        return StepResult(
            step,
            call.ok,
            call.outcome,
            call.exit_code,
            call.duration_s,
            log,
            call.reason,
        )

    @staticmethod
    def _report(
        run_id: str,
        directory: Path,
        results: list[StepResult],
        transition: TransitionOutcome | None,
    ) -> GateReport:
        passed = transition is not None and all(r.passed for r in results)
        return GateReport(
            run_id, directory.name, directory, tuple(results), passed, transition
        )


def _result_json(
    run_id: str, gate_id: str, results: list[StepResult], passed: bool
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "gate_id": gate_id,
        "verdict": "pass" if passed else "fail",
        "steps": [
            {
                "step": r.step.value,
                "passed": r.passed,
                "outcome": r.outcome.value,
                "exit_code": r.exit_code,
                "duration_s": round(r.duration_s, 3),
                "detail": r.detail,
            }
            for r in results
        ],
    }


def _append(log: Path, text: str) -> None:
    with log.open("a", encoding="utf-8") as file:
        file.write(text)


def _write_json(path: Path, value: dict[str, object]) -> None:
    """Atomically: a crash leaves no result.json, never a partial one."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as tmp:
        json.dump(value, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
    Path(tmp_name).replace(path)

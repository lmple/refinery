"""Scenarios of specs/verify-gate/spec.md (gate-runner change) against
GateRunner, with make targets scripted per test (see conftest.py)."""

import asyncio
import json
from pathlib import Path

import pytest

from refinery.gate.runner import GateReport, GateRunner, Step
from refinery.proc.supervisor import Outcome
from refinery.run.model import Accepted, Action, Phase, Refused
from refinery.run.store import RunStore
from tests.gate.conftest import RUN, Worktree, drive_to_verify, make_repo
from tests.proc.test_supervisor import group_alive, wait_for
from tests.run.helpers import accepted, request, run_async

A = Action
P = Phase
ORDER = [Step.FROZEN, Step.TEST, Step.TYPES, Step.LINT, Step.TLA]


def report_of(outcome: GateReport | Refused) -> GateReport:
    assert isinstance(outcome, GateReport), outcome
    return outcome


def sleeper(pidfile: Path, seconds: float = 30) -> str:
    """A step that records its pid, then runs for a while."""
    return f"echo $$ > {pidfile}; exec sleep {seconds}"


# Requirement: Gate steps / Gate result becomes a transition


def test_passing_gate(wt: Worktree) -> None:
    report = report_of(run_async(wt.runner().run_gate(RUN)))
    assert [s.step for s in report.steps] == ORDER
    assert all(s.passed for s in report.steps)
    assert report.passed
    assert report.gate_id == "g1"
    assert isinstance(report.transition, Accepted)
    assert wt.record().phase is P.FINAL_REVIEW
    last = wt.store.events(RUN)[-1]
    assert (last.name, last.actor.value, last.gate) == (A.PASS_GATE, "factoryd", "g1")


def test_all_steps_run_despite_a_failure(wt: Worktree) -> None:
    wt.behave(Step.TYPES, "echo 'types broke'; exit 1")
    report = report_of(run_async(wt.runner().run_gate(RUN)))
    assert [s.step for s in report.steps] == ORDER
    assert [s.step for s in report.steps if not s.passed] == [Step.TYPES]
    failed = next(s for s in report.steps if s.step is Step.TYPES)
    # make exits 2 when a recipe fails, whatever the recipe's own exit code.
    assert (failed.outcome, failed.exit_code) == (Outcome.EXITED, 2)
    assert "types broke" in failed.log.read_text()
    assert not report.passed


def test_failing_gate(wt: Worktree) -> None:
    wt.behave(Step.LINT, "exit 2")
    wt.behave(Step.TLA, "exit 12")
    report = report_of(run_async(wt.runner().run_gate(RUN)))
    assert not report.passed
    assert wt.record().phase is P.IMPL
    last = wt.store.events(RUN)[-1]
    assert (last.name, last.gate) == (A.FAIL_GATE, report.gate_id)
    assert last.reason is not None
    assert "lint" in last.reason
    assert "tla" in last.reason
    assert "test" not in last.reason.replace("tests", "")


def test_steps_run_one_at_a_time_in_order(wt: Worktree) -> None:
    trace = wt.behaviours / "trace"
    for step in (Step.TEST, Step.TYPES, Step.LINT, Step.TLA):
        wt.behave(
            step, f"echo start-{step} >> {trace}; sleep 0.1; echo end-{step} >> {trace}"
        )
    report_of(run_async(wt.runner().run_gate(RUN)))
    assert trace.read_text().split() == [
        f"{edge}-{step}"
        for step in ("test", "types", "lint", "tla")
        for edge in ("start", "end")
    ]


def test_make_targets_get_the_configured_jar(wt: Worktree, tmp_path: Path) -> None:
    jar = tmp_path / "tla2tools.jar"
    wt.behave(Step.TLA, 'echo "jar=$1"')
    report = report_of(run_async(wt.runner(wt.config(tla_jar=jar)).run_gate(RUN)))
    tla = next(s for s in report.steps if s.step is Step.TLA)
    assert f"jar={jar}" in tla.log.read_text()


# Requirement: Gate decision fails closed


def test_timeout_fails_the_gate(wt: Worktree) -> None:
    pidfile = wt.behaviours / "test.pid"
    wt.behave(Step.TEST, sleeper(pidfile))
    config = wt.config(timeouts={Step.TEST: 0.3})
    report = report_of(run_async(wt.runner(config).run_gate(RUN)))
    test = next(s for s in report.steps if s.step is Step.TEST)
    assert (test.passed, test.outcome) == (False, Outcome.TIMED_OUT)
    assert not group_alive(int(pidfile.read_text()))
    assert not report.passed
    assert wt.record().phase is P.IMPL


def test_missing_make_fails_the_gate(wt: Worktree) -> None:
    report = report_of(
        run_async(wt.runner(wt.config(make="does-not-exist-make")).run_gate(RUN))
    )
    outcomes = {s.step: s.outcome for s in report.steps}
    assert outcomes[Step.FROZEN] is Outcome.EXITED
    assert all(outcomes[s] is Outcome.FAILED_TO_START for s in ORDER[1:])
    assert not report.passed
    assert wt.record().phase is P.IMPL


def test_signal_fails_a_step(wt: Worktree) -> None:
    wt.behave(Step.LINT, "kill -9 $$")
    report = report_of(run_async(wt.runner().run_gate(RUN)))
    lint = next(s for s in report.steps if s.step is Step.LINT)
    assert not lint.passed
    assert not report.passed


def test_frozen_change_fails_the_gate(wt: Worktree) -> None:
    (wt.root / "tests" / "test_x.py").write_text("def test_x() -> None:\n    pass\n")
    report = report_of(run_async(wt.runner().run_gate(RUN)))
    frozen = next(s for s in report.steps if s.step is Step.FROZEN)
    assert not frozen.passed
    assert "tests" in frozen.detail
    assert not report.passed
    assert wt.record().phase is P.IMPL


# Requirement: Gate records


def test_records_after_a_gate_run(wt: Worktree) -> None:
    wt.behave(Step.TEST, "echo hello from test")
    report = report_of(run_async(wt.runner().run_gate(RUN)))
    directory = wt.gate_dir("g1")
    assert report.directory == directory
    assert sorted(p.name for p in directory.glob("*.log")) == sorted(
        f"{s}.log" for s in ORDER
    )
    assert "hello from test" in (directory / "test.log").read_text()
    result = json.loads((directory / "result.json").read_text())
    assert result["verdict"] == "pass"
    assert [s["step"] for s in result["steps"]] == [s.value for s in ORDER]
    for entry in result["steps"]:
        assert set(entry) >= {"step", "outcome", "exit_code", "duration_s", "passed"}
        assert entry["outcome"] == "exited"
        assert entry["exit_code"] == 0
        assert entry["duration_s"] >= 0


def test_failed_gate_result_json(wt: Worktree) -> None:
    wt.behave(Step.TYPES, "exit 1")
    report_of(run_async(wt.runner().run_gate(RUN)))
    result = json.loads((wt.gate_dir("g1") / "result.json").read_text())
    assert result["verdict"] == "fail"
    assert [s["step"] for s in result["steps"] if not s["passed"]] == ["types"]


def test_gate_ids_count_up(wt: Worktree) -> None:
    wt.behave(Step.TEST, "exit 1")
    first = report_of(run_async(wt.runner().run_gate(RUN)))
    accepted(run_async(wt.store.transition(RUN, request(A.COMPLETE_IMPL, minute=30))))
    wt.behave(Step.TEST, "exit 0")
    second = report_of(run_async(wt.runner().run_gate(RUN)))
    assert (first.gate_id, second.gate_id) == ("g1", "g2")
    assert wt.record().phase is P.FINAL_REVIEW


# Requirement: One gate run per run, only in verify


def test_not_in_verify(wt: Worktree) -> None:
    wt.behave(Step.TEST, "exit 1")
    report_of(run_async(wt.runner().run_gate(RUN)))  # FailGate: now in impl
    assert wt.record().phase is P.IMPL
    outcome = run_async(wt.runner().run_gate(RUN))
    assert isinstance(outcome, Refused)
    assert not wt.gate_dir("g2").exists()


def test_unknown_run(wt: Worktree) -> None:
    assert isinstance(run_async(wt.runner().run_gate("nope.1")), Refused)


def test_already_running(wt: Worktree) -> None:
    pidfile = wt.behaviours / "test.pid"
    wt.behave(Step.TEST, sleeper(pidfile))
    runner = wt.runner()

    async def go() -> None:
        first = asyncio.create_task(runner.run_gate(RUN))
        await wait_for(pidfile)
        assert runner.running(RUN)
        assert isinstance(await runner.run_gate(RUN), Refused)
        assert runner.cancel(RUN)
        with pytest.raises(asyncio.CancelledError):
            await first

    run_async(go())
    assert not runner.running(RUN)


# Requirement: Cancelled gate records nothing


def test_run_cancelled_mid_gate(wt: Worktree) -> None:
    pidfile = wt.behaviours / "test.pid"
    wt.behave(Step.TEST, sleeper(pidfile))
    runner = wt.runner()

    async def go() -> int:
        gate = asyncio.create_task(runner.run_gate(RUN))
        pgid = int(await wait_for(pidfile))
        accepted(await wt.store.transition(RUN, request(A.CANCEL, minute=40)))
        assert runner.cancel(RUN)
        with pytest.raises(asyncio.CancelledError):
            await gate
        return pgid

    pgid = run_async(go())
    assert not group_alive(pgid)
    assert not (wt.gate_dir("g1") / "types.log").exists()
    assert not (wt.gate_dir("g1") / "result.json").exists()
    assert wt.store.events(RUN)[-1].name is A.CANCEL
    assert not runner.running(RUN)


def test_run_leaving_verify_stops_the_gate_by_itself(wt: Worktree) -> None:
    """Without cancel(): the runner notices the run left verify."""
    pidfile = wt.behaviours / "test.pid"
    wt.behave(Step.TEST, sleeper(pidfile))
    runner = wt.runner()

    async def go() -> tuple[GateReport | Refused, int]:
        gate = asyncio.create_task(runner.run_gate(RUN))
        pgid = int(await wait_for(pidfile))
        accepted(await wt.store.transition(RUN, request(A.CANCEL, minute=40)))
        return await asyncio.wait_for(gate, timeout=10), pgid

    outcome, pgid = run_async(go())
    report = report_of(outcome)
    assert report.transition is None
    assert not report.passed
    assert [s.step for s in report.steps] == [Step.FROZEN, Step.TEST]
    assert report.steps[-1].outcome is Outcome.CANCELLED
    assert not group_alive(pgid)
    assert not (wt.gate_dir("g1") / "types.log").exists()
    assert not (wt.gate_dir("g1") / "result.json").exists()
    assert wt.store.events(RUN)[-1].name is A.CANCEL


# Requirement: Crash leaves no result


def test_restart_after_an_interrupted_gate(wt: Worktree) -> None:
    """The interruption is a cancelled gate task, standing in for the crash.

    What a real crash adds (orphaned processes) is covered by the supervisor's
    registry and reaping tests.
    """
    pidfile = wt.behaviours / "test.pid"
    wt.behave(Step.TEST, sleeper(pidfile))
    runner = wt.runner()

    async def interrupted() -> None:
        gate = asyncio.create_task(runner.run_gate(RUN))
        await wait_for(pidfile)
        gate.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gate

    run_async(interrupted())
    wt.store.close()
    wt.store = run_async(RunStore.open(wt.data, [wt.root]))
    assert wt.record().phase is P.VERIFY
    assert wt.gate_dir("g1").exists()
    assert not (wt.gate_dir("g1") / "result.json").exists()
    wt.behave(Step.TEST, "exit 0")
    report = report_of(run_async(wt.runner().run_gate(RUN)))
    assert report.gate_id == "g2"
    assert wt.record().phase is P.FINAL_REVIEW


def test_two_runs_gate_independently(tmp_path: Path, wt: Worktree) -> None:
    """Different runs (worktrees) do not block each other."""
    other_root = tmp_path / "other"
    other_head = make_repo(other_root)

    async def go() -> None:
        other = await RunStore.open(wt.data / "other", [other_root])
        await drive_to_verify(other, other_root, other_head)
        a = GateRunner(wt.store, wt.supervisor, wt.config(), wt.data)
        b = GateRunner(other, wt.supervisor, wt.config(), wt.data / "other")
        ra, rb = await asyncio.gather(a.run_gate(RUN), b.run_gate(RUN))
        assert report_of(ra).passed
        assert report_of(rb).passed
        other.close()

    run_async(go())


def test_worktree_of_a_run(wt: Worktree) -> None:
    assert wt.store.worktree(RUN) == wt.root
    with pytest.raises(KeyError):
        wt.store.worktree("nope.1")

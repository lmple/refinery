"""Requirements "Dry-run gates" and "Dry runs and gate runs never overlap"
(verify-gate, from mcp-workflow) against GateRunner directly."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from refinery.gate.runner import GateConfig, GateReport, GateRunner, Step
from refinery.proc.supervisor import Outcome, Supervisor
from refinery.run.model import Action, Phase, Refused
from refinery.run.store import RunStore
from tests.gate.conftest import CHANGE, GIT_ENV, RUN, make_repo
from tests.proc.test_supervisor import group_alive, wait_for
from tests.run.helpers import HAPPY_PATH, accepted, request

A = Action
P = Phase


class Repo:
    """A repo with run demo.1 driven to a given phase, and scripted make targets."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        self.data = tmp_path / "data"
        self.behaviours = tmp_path / "behaviours"
        self.head = make_repo(self.root)
        self.behaviours.mkdir()
        for step in (Step.TEST, Step.TYPES, Step.LINT, Step.TLA):
            self.behave(step, "exit 0")

    def behave(self, step: Step, script: str) -> None:
        (self.behaviours / f"{step}.sh").write_text(script + "\n")

    def runner(self, store: RunStore) -> GateRunner:
        env = {**os.environ, **GIT_ENV, "BEHAVIOUR_DIR": str(self.behaviours)}
        config = GateConfig(timeouts=dict.fromkeys(Step, 10.0), env=env, poll_s=0.05)
        return GateRunner(store, Supervisor(self.data, grace_s=0.5), config, self.data)

    async def store_at(self, phase: Phase) -> RunStore:
        store = await RunStore.open(self.data, [self.root])
        accepted(await store.start_run(CHANGE, self.root, request(A.START_RUN)))
        for minute, name in enumerate(HAPPY_PATH, start=1):
            if store.get(RUN).phase is phase:
                break
            commit = self.head if name in (A.APPROVE_SPEC, A.APPROVE_TESTS) else None
            accepted(
                await store.transition(RUN, request(name, minute=minute, commit=commit))
            )
        assert store.get(RUN).phase is phase
        return store


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def report_of(outcome: GateReport | Refused) -> GateReport:
    assert isinstance(outcome, GateReport), outcome
    return outcome


def test_dry_run_leaves_the_run_alone(repo: Repo) -> None:
    repo.behave(Step.TYPES, "exit 1")

    async def go() -> None:
        store = await repo.store_at(P.IMPL)
        before = store.events(RUN)
        report = report_of(await repo.runner(store).dry_run(RUN))
        assert report.gate_id == "d1"
        assert report.transition is None
        assert not report.passed
        assert [s.step for s in report.steps] == list(Step)
        assert [s.step for s in report.steps if not s.passed] == [Step.TYPES]
        directory = repo.data / "runs" / RUN / "dry-runs" / "d1"
        assert report.directory == directory
        result = json.loads((directory / "result.json").read_text())
        assert result["verdict"] == "fail"
        assert store.get(RUN).phase is P.IMPL
        assert store.events(RUN) == before
        assert not (repo.data / "runs" / RUN / "gates").exists()
        store.close()

    asyncio.run(go())


def test_passing_dry_run_reports_passed(repo: Repo) -> None:
    async def go() -> None:
        store = await repo.store_at(P.IMPL)
        report = report_of(await repo.runner(store).dry_run(RUN))
        assert report.passed
        assert report.transition is None
        assert store.get(RUN).phase is P.IMPL
        store.close()

    asyncio.run(go())


def test_dry_run_allowed_in_tests(repo: Repo) -> None:
    async def go() -> None:
        store = await repo.store_at(P.TESTS)
        report_of(await repo.runner(store).dry_run(RUN))
        assert store.get(RUN).phase is P.TESTS
        store.close()

    asyncio.run(go())


@pytest.mark.parametrize("phase", [P.SPEC, P.SPEC_REVIEW, P.TESTS_REVIEW, P.VERIFY])
def test_dry_run_refused_elsewhere(repo: Repo, phase: Phase) -> None:
    async def go() -> None:
        store = await repo.store_at(phase)
        assert isinstance(await repo.runner(store).dry_run(RUN), Refused)
        assert not (repo.data / "runs" / RUN / "dry-runs").exists()
        store.close()

    asyncio.run(go())


def test_dry_runs_count_up(repo: Repo) -> None:
    async def go() -> None:
        store = await repo.store_at(P.IMPL)
        runner = repo.runner(store)
        first = report_of(await runner.dry_run(RUN))
        second = report_of(await runner.dry_run(RUN))
        assert (first.gate_id, second.gate_id) == ("d1", "d2")
        store.close()

    asyncio.run(go())


def test_dry_run_killed_when_the_run_leaves_impl(repo: Repo) -> None:
    pidfile = repo.behaviours / "test.pid"
    repo.behave(Step.TEST, f"echo $$ > {pidfile}; exec sleep 30")

    async def go() -> int:
        store = await repo.store_at(P.IMPL)
        runner = repo.runner(store)
        dry = asyncio.create_task(runner.dry_run(RUN))
        pgid = int(await wait_for(pidfile))
        accepted(await store.transition(RUN, request(A.CANCEL, minute=40)))
        report = report_of(await asyncio.wait_for(dry, timeout=10))
        assert report.steps[-1].outcome is Outcome.CANCELLED
        assert not (report.directory / "types.log").exists()
        store.close()
        return pgid

    assert not group_alive(asyncio.run(go()))


def test_no_dry_run_while_another_is_in_progress(repo: Repo) -> None:
    pidfile = repo.behaviours / "test.pid"
    repo.behave(Step.TEST, f"echo $$ > {pidfile}; exec sleep 30")

    async def go() -> None:
        store = await repo.store_at(P.IMPL)
        runner = repo.runner(store)
        first = asyncio.create_task(runner.dry_run(RUN))
        await wait_for(pidfile)
        assert runner.running(RUN)
        assert isinstance(await runner.dry_run(RUN), Refused)
        assert runner.cancel(RUN)
        with pytest.raises(asyncio.CancelledError):
            await first
        store.close()

    asyncio.run(go())


def test_gate_run_and_dry_run_never_overlap(repo: Repo) -> None:
    """A gate run in progress (verify) refuses a dry run of the same run."""
    pidfile = repo.behaviours / "test.pid"
    repo.behave(Step.TEST, f"echo $$ > {pidfile}; exec sleep 30")

    async def go() -> None:
        store = await repo.store_at(P.VERIFY)
        runner = repo.runner(store)
        gate = asyncio.create_task(runner.run_gate(RUN))
        await wait_for(pidfile)
        assert isinstance(await runner.dry_run(RUN), Refused)
        runner.cancel(RUN)
        with pytest.raises(asyncio.CancelledError):
            await gate
        store.close()

    asyncio.run(go())

"""A factoryd over a throwaway git repo whose make targets behave as scripted.

Every scenario runs inside one event loop (``Factory.run``): the workflow's
gate runs are background tasks of that loop.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from refinery.auth.tokens import Principal, Role
from refinery.factoryd.config import ServerConfig
from refinery.factoryd.workflow import Json, Workflow
from refinery.gate.runner import GateConfig, Step
from refinery.run.model import Action, Phase, RunRecord
from tests.gate.conftest import CHANGE, GIT_ENV, make_repo
from tests.run.helpers import accepted, request

A = Action
P = Phase
HUMAN = Principal("h0000001", Role.HUMAN, None)


@dataclass
class Factory:
    root: Path
    data: Path
    behaviours: Path
    head: str
    timeouts: dict[Step, float] = field(
        default_factory=lambda: dict.fromkeys(Step, 10.0)
    )

    def behave(self, step: Step, script: str) -> None:
        """What ``make <step>`` does in the repo."""
        (self.behaviours / f"{step}.sh").write_text(script + "\n")

    @property
    def config(self) -> ServerConfig:
        env = {**os.environ, **GIT_ENV, "BEHAVIOUR_DIR": str(self.behaviours)}
        return ServerConfig(
            data_dir=self.data,
            roots=(self.root,),
            gate=GateConfig(timeouts=self.timeouts, env=env, poll_s=0.05),
        )

    def run[T](self, scenario: Callable[[Workflow], Awaitable[T]]) -> T:
        """Open a Workflow, run ``scenario`` on it, close it: one event loop."""

        async def go() -> T:
            workflow = await Workflow.open(self.config)
            try:
                return await scenario(workflow)
            finally:
                await workflow.close()

        return asyncio.run(go())


@pytest.fixture
def fx(tmp_path: Path) -> Factory:
    root, data, behaviours = (
        tmp_path / "repo",
        tmp_path / "data",
        tmp_path / "behaviours",
    )
    head = make_repo(root)
    behaviours.mkdir()
    factory = Factory(root, data, behaviours, head)
    for step in (Step.TEST, Step.TYPES, Step.LINT, Step.TLA):
        factory.behave(step, "exit 0")
    return factory


async def start_with_token(
    wf: Workflow, fx: Factory, change: str = CHANGE
) -> tuple[str, str]:
    """A run with its record in the root, as runs started before the runner
    change are, and an agent token for it; returns its id and the token.

    Starting through start_workflow (worktree creation) has its own tests;
    the scenarios here are about what happens after the start.
    """
    outcome = await wf.store.start_run(change, fx.root, request(A.START_RUN))
    run_id = accepted(outcome).record.run_id
    return run_id, wf.tokens.create(Role.AGENT, run_id=run_id).secret


async def start(
    wf: Workflow, fx: Factory, change: str = CHANGE
) -> tuple[str, Principal]:
    """A started run (see ``start_with_token``); returns its id and its agent."""
    run_id, token = await start_with_token(wf, fx, change)
    agent = wf.authenticate(token)
    assert agent is not None
    return run_id, agent


async def human(wf: Workflow, fx: Factory, run_id: str, name: Action) -> RunRecord:
    """A human decision, applied to the store (review tools come later)."""
    commit = fx.head if name in (A.APPROVE_SPEC, A.APPROVE_TESTS) else None
    return accepted(
        await wf.store.transition(run_id, request(name, commit=commit))
    ).record


async def to_phase(
    wf: Workflow, fx: Factory, run_id: str, agent: Principal, target: Phase
) -> None:
    """Drive a run with agent submissions and human approvals up to ``target``."""
    steps: list[tuple[Phase, Callable[[], Awaitable[object]]]] = [
        (P.SPEC, lambda: wf.submit_for_review(agent)),
        (P.MODEL, lambda: wf.submit_for_review(agent)),
        (P.SPEC_REVIEW, lambda: human(wf, fx, run_id, A.APPROVE_SPEC)),
        (P.TESTS, lambda: wf.submit_for_review(agent)),
        (P.TESTS_REVIEW, lambda: human(wf, fx, run_id, A.APPROVE_TESTS)),
    ]
    for phase, step in steps:
        if wf.store.get(run_id).phase is target:
            return
        assert wf.store.get(run_id).phase is phase
        await step()
    assert wf.store.get(run_id).phase is target


def field_of(result: Json, key: str) -> object:
    assert key in result, f"{key!r} missing from {sorted(result)}"
    return result[key]


def rows(value: object) -> list[dict[str, object]]:
    """A tool result's list of objects, checked and narrowed."""
    assert isinstance(value, list)
    items = cast(list[object], value)
    assert all(isinstance(item, dict) for item in items)
    return [
        {str(k): v for k, v in cast(dict[object, object], item).items()}
        for item in items
    ]

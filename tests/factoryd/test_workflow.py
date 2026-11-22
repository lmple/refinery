"""Scenarios of specs/agent-tools/spec.md (mcp-workflow) against the Workflow
service, with Principals standing in for verified tokens."""

from pathlib import Path
from typing import cast

import pytest

from refinery.auth.tokens import Principal, Role
from refinery.factoryd.workflow import Workflow, WorkflowError
from refinery.gate.runner import Step
from refinery.run.model import Action, Artifact, BugKind, Phase
from tests.factoryd.conftest import (
    HUMAN,
    Factory,
    field_of,
    human,
    rows,
    start,
    to_phase,
)
from tests.gate.conftest import CHANGE
from tests.run.helpers import request

A = Action
P = Phase


# Requirement: Tools are restricted by role


def test_agent_cannot_start_a_run(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        _, agent = await start(wf, fx)
        with pytest.raises(WorkflowError):
            await wf.start_workflow(agent, "other", str(fx.root))
        assert [r.run_id for r in wf.store.runs()] == ["demo.1"]

    fx.run(scenario)


@pytest.mark.parametrize("tool", ["get_task", "submit_for_review", "run_gate"])
def test_human_token_cannot_use_agent_tools(fx: Factory, tool: str) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, _ = await start(wf, fx)
        with pytest.raises(WorkflowError):
            await getattr(wf, tool)(HUMAN)
        assert wf.store.get(run_id).phase is P.SPEC

    fx.run(scenario)


def test_human_cannot_escalate_or_report(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        await start(wf, fx)
        with pytest.raises(WorkflowError):
            await wf.escalate(HUMAN, "spec_bug", "because")
        with pytest.raises(WorkflowError):
            await wf.report_progress(HUMAN, "hello")

    fx.run(scenario)


# Requirement: Agent calls act on the token's run


def test_two_runs_two_agents(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        a, agent_a = await start(wf, fx, "a")
        b, _ = await start(wf, fx, "b")
        await wf.submit_for_review(agent_a)
        assert (wf.store.get(a).phase, wf.store.get(b).phase) == (P.MODEL, P.SPEC)

    fx.run(scenario)


def test_agent_of_an_ended_run_is_not_authenticated(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        started = await wf.start_workflow(HUMAN, CHANGE, str(fx.root))
        token = str(started["agent_token"])
        assert wf.authenticate(token) is not None
        await human(wf, fx, str(started["run_id"]), A.CANCEL)
        assert wf.authenticate(token) is None

    fx.run(scenario)


def test_principal_for_a_run_that_does_not_exist_is_refused(fx: Factory) -> None:
    ghost = Principal("a0000009", Role.AGENT, "ghost.1")

    async def scenario(wf: Workflow) -> None:
        with pytest.raises(WorkflowError):
            await wf.get_task(ghost)

    fx.run(scenario)


# Requirement: Starting a workflow


def test_start(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        started = await wf.start_workflow(HUMAN, CHANGE, str(fx.root))
        assert (started["run_id"], started["phase"]) == ("demo.1", "spec")
        agent = wf.authenticate(str(started["agent_token"]))
        assert agent is not None
        assert (agent.role, agent.run_id) == (Role.AGENT, "demo.1")
        assert wf.store.worktree("demo.1") == fx.root

    fx.run(scenario)


def test_worktree_outside_the_roots(fx: Factory, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    async def scenario(wf: Workflow) -> None:
        with pytest.raises(WorkflowError, match="root"):
            await wf.start_workflow(HUMAN, CHANGE, str(elsewhere))
        assert wf.store.runs() == []

    fx.run(scenario)


def test_change_already_has_a_run(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        await start(wf, fx)
        with pytest.raises(WorkflowError, match=r"demo\.1"):
            await wf.start_workflow(HUMAN, CHANGE, str(fx.root))

    fx.run(scenario)


# Requirement: Getting the task


def test_task_in_the_spec_phase(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        task = await wf.get_task(agent)
        assert task["run_id"] == run_id
        assert task["change"] == CHANGE
        assert task["phase"] == "spec"
        assert task["turn"] == "agent"
        assert task["worktree"] == str(fx.root)
        instructions = str(field_of(task, "instructions"))
        assert f"openspec/changes/{CHANGE}" in instructions
        assert "submit_for_review" in instructions

    fx.run(scenario)


def test_task_while_waiting_for_review(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.SPEC_REVIEW)
        task = await wf.get_task(agent)
        assert (task["phase"], task["turn"]) == ("spec_review", "human")
        assert "wait" in str(task["instructions"]).lower()

    fx.run(scenario)


def test_task_lists_frozen_artifacts(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        frozen = rows(field_of(await wf.get_task(agent), "frozen"))
        assert {(f["artifact"], f["commit"]) for f in frozen} == {
            (Artifact.SPEC.value, fx.head),
            (Artifact.TESTS.value, fx.head),
        }
        tests = next(f for f in frozen if f["artifact"] == "tests")
        assert tests["paths"] == ["tests/"]

    fx.run(scenario)


def test_task_after_a_failed_gate(fx: Factory) -> None:
    fx.behave(Step.TYPES, "exit 1")

    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        await wf.submit_for_review(agent)
        await wf.wait_for_gates()
        task = await wf.get_task(agent)
        assert task["phase"] == "impl"
        assert task["gate"] == {
            "id": "g1",
            "verdict": "fail",
            "failed_steps": ["types"],
        }

    fx.run(scenario)


def test_task_has_no_gate_before_the_first(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        _, agent = await start(wf, fx)
        task = await wf.get_task(agent)
        assert task["gate"] is None
        assert task["dry_run"] is None
        assert task["progress"] == []

    fx.run(scenario)


# Requirement: Submitting completes the agent phase


def test_submitting_the_spec_and_the_model(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        assert (await wf.submit_for_review(agent))["phase"] == "model"
        assert (await wf.submit_for_review(agent))["phase"] == "spec_review"
        names = [e.name for e in wf.store.events(run_id)]
        assert names == [A.START_RUN, A.COMPLETE_SPEC, A.COMPLETE_MODEL]

    fx.run(scenario)


def test_skipping_the_model(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await wf.submit_for_review(agent)
        await wf.submit_for_review(agent, skip_model_reason="docs only")
        last = wf.store.events(run_id)[-1]
        assert (last.name, last.reason) == (A.SKIP_MODEL, "docs only")

    fx.run(scenario)


def test_skip_reason_outside_the_model_phase(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        with pytest.raises(WorkflowError):
            await wf.submit_for_review(agent, skip_model_reason="nope")
        assert wf.store.get(run_id).phase is P.SPEC

    fx.run(scenario)


def test_submitting_tests(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.TESTS)
        assert (await wf.submit_for_review(agent))["phase"] == "tests_review"

    fx.run(scenario)


def test_not_the_agents_turn(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.TESTS_REVIEW)
        before = wf.store.events(run_id)
        with pytest.raises(WorkflowError, match="human"):
            await wf.submit_for_review(agent)
        assert wf.store.events(run_id) == before

    fx.run(scenario)


def test_submitting_implementation_starts_the_gate(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        result = await wf.submit_for_review(agent)
        assert result["phase"] == "verify"
        assert wf.gates.running(run_id)
        await wf.wait_for_gates()
        assert wf.store.get(run_id).phase is P.FINAL_REVIEW
        last = wf.store.events(run_id)[-1]
        assert (last.name, last.gate) == (A.PASS_GATE, "g1")

    fx.run(scenario)


# Requirement: Escalating


def test_escalate_a_test_bug(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        result = await wf.escalate(agent, "test_bug", "test 3 contradicts scenario 2")
        assert result["phase"] == "escalated"
        last = wf.store.events(run_id)[-1]
        assert (last.name, last.bug, last.reason) == (
            A.ESCALATE,
            BugKind.TEST_BUG,
            "test 3 contradicts scenario 2",
        )

    fx.run(scenario)


def test_escalate_with_an_unknown_classification(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        with pytest.raises(WorkflowError):
            await wf.escalate(agent, "feature_request", "please")
        assert wf.store.get(run_id).phase is P.IMPL

    fx.run(scenario)


# Requirement: Refused transitions are tool errors


def test_escalate_without_a_reason(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        before = wf.store.events(run_id)
        with pytest.raises(WorkflowError, match="reason"):
            await wf.escalate(agent, "spec_bug", "")
        assert wf.store.events(run_id) == before

    fx.run(scenario)


# Requirement: Dry-run gate for agents


def test_dry_run_in_impl(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        before = wf.store.events(run_id)
        result = await wf.run_gate(agent)
        steps = rows(field_of(result, "steps"))
        assert [s["step"] for s in steps] == [s.value for s in Step]
        assert all("log" in s for s in steps)
        assert result["passed"] is True
        assert wf.store.get(run_id).phase is P.IMPL
        assert wf.store.events(run_id) == before
        assert (await wf.get_task(agent))["dry_run"] == {
            "id": "d1",
            "verdict": "pass",
            "failed_steps": [],
        }

    fx.run(scenario)


def test_dry_run_outside_tests_and_impl(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        _, agent = await start(wf, fx)
        with pytest.raises(WorkflowError):
            await wf.run_gate(agent)

    fx.run(scenario)


# Requirement: Progress reports fail open


def test_progress_recorded(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        assert (await wf.report_progress(agent, "tests for scenario 3 written"))[
            "recorded"
        ]
        lines = (fx.data / "runs" / run_id / "progress.jsonl").read_text().splitlines()
        assert "tests for scenario 3 written" in lines[-1]
        progress = rows(field_of(await wf.get_task(agent), "progress"))
        assert progress[-1]["message"] == "tests for scenario 3 written"

    fx.run(scenario)


def test_progress_keeps_the_last_five(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        _, agent = await start(wf, fx)
        for n in range(8):
            await wf.report_progress(agent, f"step {n}")
        progress = rows(field_of(await wf.get_task(agent), "progress"))
        assert [p["message"] for p in progress] == [f"step {n}" for n in range(3, 8)]

    fx.run(scenario)


def test_progress_cannot_be_written(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        (fx.data / "runs" / run_id / "progress.jsonl").mkdir(parents=True)
        before = wf.store.events(run_id)
        result = await wf.report_progress(agent, "lost")
        assert result["recorded"] is False
        assert result["warning"]
        assert wf.store.events(run_id) == before
        assert wf.store.get(run_id).phase is P.SPEC

    fx.run(scenario)


# review-queue: the agent sees the human's last decision (agent-tools), and
# every review is of a commit (review-queue).


def test_last_decision_before_any(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        _, agent = await start(wf, fx)
        task = await wf.get_task(agent)
        assert task["last_decision"] is None
        assert task["pr"] is None

    fx.run(scenario)


def test_last_decision_after_a_rejection(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.SPEC_REVIEW)
        rejection = request(A.REJECT_SPEC, note="scenario 2 has no THEN")
        await wf.store.transition(run_id, rejection)
        task = await wf.get_task(agent)
        assert task["phase"] == "spec"
        assert isinstance(task["last_decision"], dict)
        decision = cast(dict[str, object], task["last_decision"])
        assert decision["action"] == "RejectSpec"
        assert decision["note"] == "scenario 2 has no THEN"
        assert decision["at"]

    fx.run(scenario)


def test_submit_with_uncommitted_work(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.TESTS)
        (fx.root / "tests" / "test_new.py").write_text("def test_new() -> None: ...\n")
        with pytest.raises(WorkflowError, match=r"tests/test_new\.py"):
            await wf.submit_for_review(agent)
        assert wf.store.get(run_id).phase is P.TESTS

    fx.run(scenario)


def test_the_run_record_does_not_count(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await wf.submit_for_review(agent)  # spec -> model
        record = fx.root / ".refinery" / "runs" / f"{run_id}.json"
        assert record.exists()
        assert (await wf.submit_for_review(agent))["phase"] == "spec_review"

    fx.run(scenario)


def test_handing_the_spec_to_the_model_phase_needs_no_commit(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        (fx.root / "openspec" / "changes" / "demo" / "proposal.md").write_text(
            "draft\n"
        )
        assert (await wf.submit_for_review(agent))["phase"] == "model"
        assert wf.store.get(run_id).phase is P.MODEL

    fx.run(scenario)

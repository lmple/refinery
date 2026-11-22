"""Requirements "Gate starts automatically", "Dry runs and gate runs never
overlap" (submit side) and "Gate errors fail closed" (verify-gate, from
mcp-workflow) against the Workflow service."""

import asyncio

import pytest

from refinery.factoryd.workflow import Workflow, WorkflowError
from refinery.gate.runner import Step
from refinery.run.model import Action, Phase
from refinery.run.store import RunStore
from tests.factoryd.conftest import Factory, start, to_phase
from tests.gate.conftest import RUN, drive_to_verify
from tests.proc.test_supervisor import wait_for

A = Action
P = Phase


def test_entering_verify_starts_the_gate(fx: Factory) -> None:
    pidfile = fx.behaviours / "test.pid"
    fx.behave(Step.TEST, f"echo $$ > {pidfile}; sleep 0.3")

    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        await wf.submit_for_review(agent)
        await wait_for(pidfile)  # the gate's test step is running
        assert wf.gates.running(run_id)
        await wf.wait_for_gates()
        assert wf.store.events(run_id)[-1].name is A.PASS_GATE

    fx.run(scenario)


def test_submit_during_a_dry_run_is_refused(fx: Factory) -> None:
    pidfile = fx.behaviours / "test.pid"
    fx.behave(Step.TEST, f"echo $$ > {pidfile}; exec sleep 30")

    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        dry = asyncio.create_task(wf.run_gate(agent))
        await wait_for(pidfile)
        with pytest.raises(WorkflowError):
            await wf.submit_for_review(agent)
        assert wf.store.get(run_id).phase is P.IMPL
        wf.gates.cancel(run_id)
        with pytest.raises((asyncio.CancelledError, WorkflowError)):
            await dry

    fx.run(scenario)


def test_gate_restarts_after_a_restart(fx: Factory) -> None:
    """A run left in verify (its gate never ran) gets a gate run on startup."""

    async def leave_in_verify() -> None:
        store = await RunStore.open(fx.data, [fx.root])
        await drive_to_verify(store, fx.root, fx.head)
        store.close()

    asyncio.run(leave_in_verify())

    async def scenario(wf: Workflow) -> None:
        assert wf.gates.running(RUN) or wf.store.get(RUN).phase is not P.VERIFY
        await wf.wait_for_gates()
        assert wf.store.get(RUN).phase is P.FINAL_REVIEW
        assert wf.store.events(RUN)[-1].gate == "g1"

    fx.run(scenario)


def test_internal_gate_error_fails_closed(fx: Factory) -> None:
    async def scenario(wf: Workflow) -> None:
        run_id, agent = await start(wf, fx)
        await to_phase(wf, fx, run_id, agent, P.IMPL)
        # The gate records cannot be created: a file where the directory goes.
        (fx.data / "runs" / run_id).mkdir(parents=True, exist_ok=True)
        (fx.data / "runs" / run_id / "gates").write_text("not a directory\n")
        await wf.submit_for_review(agent)
        await wf.wait_for_gates()
        assert wf.store.get(run_id).phase is P.IMPL
        last = wf.store.events(run_id)[-1]
        assert last.name is A.FAIL_GATE
        assert last.reason is not None
        assert last.reason.startswith("gate error:")

    fx.run(scenario)

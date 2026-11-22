"""The workflow service: the MCP tools as plain async methods (D1, D3 to D7).

``spec/Workflow.tla`` models it: agent tools need a live run-bound token
(AgentNeedsLiveToken), submit from impl schedules the gate and is refused
during a dry run (GateAndDryRunExclusive), and startup reschedules the gate of
every run in verify (VerifyResolves).
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, cast

from refinery.auth.tokens import Principal, Role, TokenStore
from refinery.factoryd.config import ServerConfig
from refinery.factoryd.progress import append_progress, read_progress
from refinery.gate.frozen import frozen_paths
from refinery.gate.runner import GateRunner
from refinery.proc.supervisor import Supervisor
from refinery.run.archive import TraceValidator
from refinery.run.model import (
    TERMINAL,
    Action,
    Artifact,
    BugKind,
    Phase,
    Refused,
    RunRecord,
    TransitionRequest,
)
from refinery.run.model import Role as RunRole
from refinery.run.store import RunStore
from refinery.run.transitions import RULES

type Json = dict[str, object]

_log = logging.getLogger(__name__)

# submit_for_review: the transition that completes each agent phase (D3).
_SUBMIT = {
    Phase.SPEC: Action.COMPLETE_SPEC,
    Phase.MODEL: Action.COMPLETE_MODEL,
    Phase.TESTS: Action.COMPLETE_TESTS,
    Phase.IMPL: Action.COMPLETE_IMPL,
}

# get_task: what the agent should do now, condensed from AGENTS.md (D7).
_INSTRUCTIONS = {
    Phase.SPEC: (
        "Write the OpenSpec change in openspec/changes/{change}/ (proposal, design, "
        "tasks, spec deltas; every requirement with SHALL/MUST and a #### Scenario:), "
        "then call submit_for_review."
    ),
    Phase.MODEL: (
        "Update or create the TLA+ spec in spec/ (TypeOK, every safety property as an "
        "invariant, a reachability witness; make tla passes), then call "
        "submit_for_review. If the change does not affect behaviour, call "
        "submit_for_review with skip_model_reason instead."
    ),
    Phase.TESTS: (
        "Write failing tests (unit, hypothesis properties mirroring the TLA+ "
        "invariants), show they fail, then call submit_for_review. Spec files are "
        "frozen. If the spec looks wrong, call escalate."
    ),
    Phase.IMPL: (
        "Implement until every check passes; run_gate runs a dry-run gate. Spec and "
        "tests are frozen: if one looks wrong, call escalate instead of changing it. "
        "When done, call submit_for_review to start the real gate."
    ),
    Phase.VERIFY: "The gate is running. Call get_task later for its result.",
    Phase.ESCALATED: "Waiting for the human to resolve the escalation.",
    Phase.ARCHIVE: "The run is being archived.",
}
_WAIT_FOR_REVIEW = "Waiting for the human's review. Call get_task later."


class WorkflowError(Exception):
    """A tool call that cannot be honoured; nothing changed."""


class Workflow:
    """factoryd's state and its tool operations."""

    def __init__(
        self,
        config: ServerConfig,
        store: RunStore,
        tokens: TokenStore,
        supervisor: Supervisor,
        gates: GateRunner,
    ) -> None:
        """Use ``open``: it runs the startup sequence first."""
        self.config = config
        self.store = store
        self.tokens = tokens
        self.supervisor = supervisor
        self.gates = gates
        self._gate_tasks: dict[str, asyncio.Task[None]] = {}

    @classmethod
    async def open(
        cls, config: ServerConfig, *, validator: TraceValidator | None = None
    ) -> Self:
        """Startup: reap orphans, recover runs, schedule gates of runs in verify."""
        supervisor = Supervisor(config.data_dir)
        reaped = supervisor.reap()
        if reaped.killed:
            _log.warning("killed orphaned process groups %s", reaped.killed)
        store = await RunStore.open(config.data_dir, list(config.roots))
        gate_config = config.gate
        if config.tla_jar is not None and gate_config.tla_jar is None:
            gate_config = dataclasses.replace(gate_config, tla_jar=config.tla_jar)
        gates = GateRunner(store, supervisor, gate_config, config.data_dir)
        workflow = cls(config, store, TokenStore(config.data_dir), supervisor, gates)
        for record in store.runs():
            if record.phase is Phase.VERIFY:
                workflow._schedule_gate(record.run_id)
        await asyncio.sleep(0)  # let the scheduled gate runs register
        return workflow

    async def close(self) -> None:
        """Shutdown: cancel running gates (recording nothing), close the store."""
        tasks = list(self._gate_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()

    def authenticate(self, presented: str) -> Principal | None:
        """The caller behind a token (agent tokens only while their run is live)."""
        token = self.tokens.verify(presented)
        if token is None:
            return None
        if token.role is Role.AGENT:
            record = self._record(token.run_id)
            if record is None or record.phase in TERMINAL:
                return None
        return Principal(token.id, token.role, token.run_id)

    async def wait_idle(self) -> None:
        """Wait until every gate run and archive sequence has finished."""
        raise NotImplementedError

    async def wait_for_gates(self) -> None:
        """Wait until every scheduled gate run has finished (tests, shutdown)."""
        while self._gate_tasks:
            await asyncio.wait(list(self._gate_tasks.values()))
            # Drop finished tasks here rather than waiting for their done
            # callbacks: awaiting tasks that are already done does not yield
            # to the loop, so the callbacks would never get to run.
            for run_id, task in list(self._gate_tasks.items()):
                if task.done():
                    del self._gate_tasks[run_id]

    # Tools

    async def start_workflow(self, who: Principal, change: str, worktree: str) -> Json:
        """Human: StartRun on ``change`` in ``worktree``; returns the agent token."""
        _require(who, Role.HUMAN, "start_workflow")
        root = self._root(worktree)
        outcome = await self.store.start_run(
            change, root, TransitionRequest(Action.START_RUN, RunRole.HUMAN, _now())
        )
        if isinstance(outcome, Refused):
            raise WorkflowError(outcome.reason)
        issued = self.tokens.create(Role.AGENT, run_id=outcome.record.run_id)
        return {
            "run_id": outcome.record.run_id,
            "phase": outcome.record.phase.value,
            "agent_token": issued.secret,
            "token_id": issued.token.id,
        }

    async def get_task(self, who: Principal) -> Json:
        """Agent: phase, whose turn, what to do, frozen paths, gates and progress."""
        record = self._agent_run(who, "get_task")
        paths = frozen_paths(record.change)
        commits = {
            Artifact.SPEC: record.spec_commit,
            Artifact.TESTS: record.tests_commit,
        }
        runs = self.config.data_dir / "runs" / record.run_id
        return {
            "run_id": record.run_id,
            "change": record.change,
            "phase": record.phase.value,
            "turn": _turn(record.phase),
            "worktree": str(self.store.worktree(record.run_id)),
            "instructions": _instructions(record),
            "frozen": [
                {"artifact": a.value, "commit": commits[a], "paths": list(paths[a])}
                for a in sorted(record.frozen)
            ],
            "gate": _latest(runs / "gates", "g"),
            "dry_run": _latest(runs / "dry-runs", "d"),
            "progress": read_progress(self.config.data_dir, record.run_id),
        }

    async def submit_for_review(
        self, who: Principal, skip_model_reason: str | None = None
    ) -> Json:
        """Agent: complete the current agent phase; from impl, start the gate."""
        record = self._agent_run(who, "submit_for_review")
        name = _SUBMIT.get(record.phase)
        if name is None:
            raise WorkflowError(
                f"it is the {_turn(record.phase)}'s turn ({record.phase})"
            )
        if skip_model_reason is not None:
            if record.phase is not Phase.MODEL:
                raise WorkflowError("skip_model_reason is only for the model phase")
            name = Action.SKIP_MODEL
        if record.phase is Phase.IMPL and self.gates.running(record.run_id):
            raise WorkflowError(
                "a dry run is in progress; wait for it before submitting"
            )
        request = TransitionRequest(
            name, RunRole.AGENT, _now(), reason=skip_model_reason
        )
        outcome = await self.store.transition(record.run_id, request)
        if isinstance(outcome, Refused):
            raise WorkflowError(outcome.reason)
        result: Json = {
            "run_id": record.run_id,
            "phase": outcome.record.phase.value,
            "event": name.value,
        }
        if outcome.record.phase is Phase.VERIFY:
            self._schedule_gate(record.run_id)
            await asyncio.sleep(0)  # let the gate run register
            result["gate"] = "started"
        return result

    async def escalate(self, who: Principal, bug: str, reason: str) -> Json:
        """Agent: Escalate with a classification and a reason."""
        record = self._agent_run(who, "escalate")
        try:
            kind = BugKind(bug)
        except ValueError:
            kinds = ", ".join(k.value for k in BugKind)
            raise WorkflowError(f"bug must be one of {kinds}, not {bug!r}") from None
        request = TransitionRequest(
            Action.ESCALATE, RunRole.AGENT, _now(), bug=kind, reason=reason
        )
        outcome = await self.store.transition(record.run_id, request)
        if isinstance(outcome, Refused):
            raise WorkflowError(outcome.reason)
        return {"run_id": record.run_id, "phase": outcome.record.phase.value}

    async def run_gate(self, who: Principal) -> Json:
        """Agent: a dry-run gate in tests or impl; never moves the run."""
        record = self._agent_run(who, "run_gate")
        report = await self.gates.dry_run(record.run_id)
        if isinstance(report, Refused):
            raise WorkflowError(report.reason)
        return {
            "run_id": record.run_id,
            "gate_id": report.gate_id,
            "passed": report.passed,
            "steps": [
                {
                    "step": s.step.value,
                    "passed": s.passed,
                    "outcome": s.outcome.value,
                    "exit_code": s.exit_code,
                    "duration_s": round(s.duration_s, 3),
                    "log": str(s.log),
                }
                for s in report.steps
            ],
        }

    async def report_progress(self, who: Principal, message: str) -> Json:
        """Agent: append to the progress log; fails open."""
        record = self._agent_run(who, "report_progress")
        if append_progress(self.config.data_dir, record.run_id, message, _now()):
            return {"recorded": True}
        return {"recorded": False, "warning": "the progress log could not be written"}

    # Internals

    def _record(self, run_id: str | None) -> RunRecord | None:
        if run_id is None:
            return None
        try:
            return self.store.get(run_id)
        except KeyError:
            return None

    def _agent_run(self, who: Principal, tool: str) -> RunRecord:
        """The agent's own run; only agent tokens, only live runs."""
        _require(who, Role.AGENT, tool)
        record = self._record(who.run_id)
        if record is None:
            raise WorkflowError(f"run {who.run_id} does not exist")
        if record.phase in TERMINAL:
            raise WorkflowError(f"run {record.run_id} has ended ({record.phase})")
        return record

    def _root(self, worktree: str) -> Path:
        wanted = Path(worktree).resolve()
        for root in self.config.roots:
            if root.resolve() == wanted:
                return root
        raise WorkflowError(f"{worktree} is not one of factoryd's configured roots")

    def _schedule_gate(self, run_id: str) -> None:
        if run_id in self._gate_tasks:
            return
        task = asyncio.create_task(self._gate(run_id))
        self._gate_tasks[run_id] = task
        task.add_done_callback(lambda _: self._gate_tasks.pop(run_id, None))

    async def _gate(self, run_id: str) -> None:
        outcome = await self.gates.run_gate(run_id)
        if isinstance(outcome, Refused):
            _log.warning("gate run for %s not started: %s", run_id, outcome.reason)
        elif outcome.transition is None:
            _log.info(
                "gate run %s for %s stopped: run left verify", outcome.gate_id, run_id
            )


def _require(who: Principal, role: Role, tool: str) -> None:
    if who.role is not role:
        raise WorkflowError(f"{tool} needs a {role} token, not a {who.role} token")


def _turn(phase: Phase) -> str:
    """Who moves the run out of ``phase``: agent, human or factoryd."""
    if phase in TERMINAL:
        return "nobody"
    roles = {
        rule.role
        for rule in RULES.values()
        if phase in rule.sources and rule.name is not Action.CANCEL
    }
    for role in (RunRole.AGENT, RunRole.FACTORYD, RunRole.HUMAN):
        if role in roles:
            return role.value
    return RunRole.HUMAN.value


def _instructions(record: RunRecord) -> str:
    template = _INSTRUCTIONS.get(record.phase, _WAIT_FOR_REVIEW)
    return template.format(change=record.change)


def _latest(base: Path, prefix: str) -> Json | None:
    """The newest finished run under ``base``: id, verdict and failed steps."""
    finished = [
        p
        for p in base.glob(f"{prefix}*")
        if p.name[1:].isdigit() and (p / "result.json").exists()
    ]
    if not finished:
        return None
    newest = max(finished, key=lambda p: int(p.name[1:]))
    with contextlib.suppress(OSError, ValueError):
        result = cast(
            dict[str, object], json.loads((newest / "result.json").read_text())
        )
        steps = cast(list[dict[str, object]], result.get("steps", []))
        return {
            "id": newest.name,
            "verdict": result.get("verdict"),
            "failed_steps": [s["step"] for s in steps if not s.get("passed")],
        }
    return None


def _now() -> datetime:
    return datetime.now(UTC)

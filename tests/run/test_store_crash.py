"""Stateful test of RunStore with crashes, mirroring spec/Run.tla's Crash and
Recover: LogMatchesRecord, CommittedTraceValid, CommittedEventsImmutable and
OneActiveRunPerChange must hold after every step, including restarts."""

import asyncio
import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from refinery.run import eventlog, record
from refinery.run.archive import ReplayValidator, TraceValidator, ValidationResult
from refinery.run.model import TERMINAL, Accepted, Action, Event, Phase, Role
from refinery.run.store import RunStore
from tests.run.helpers import EDGES, ROLE_OF, at, request

A = Action
CHANGES = ("a", "b")
STEP_ACTIONS = [a for a in Action if a not in (A.START_RUN, A.ARCHIVE)]


class SimulatedCrash(BaseException):
    """Not an Exception: like SIGKILL, no error handling in the store sees it."""


def crash_on_record_replace(real: Callable[..., Any]) -> Callable[..., Any]:
    """os.replace that kills the process when a run record is about to land."""

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(dst).suffix == ".json" and Path(dst).parent.name == "runs":
            raise SimulatedCrash
        return real(src, dst, *args, **kwargs)

    return replace


class AlwaysFails:
    """A validator that rejects every log."""

    async def validate(self, run_id: str, events: Sequence[Event]) -> ValidationResult:
        return ValidationResult(False, "rejected")


class StoreWithCrashes(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.tmp = Path(tempfile.mkdtemp(prefix="refinery-crash-"))
        self.data = self.tmp / "data"
        self.worktree = self.tmp / "worktree"
        self.worktree.mkdir()
        self.loop = asyncio.new_event_loop()
        self.store: RunStore | None = None
        self.committed: dict[str, list[Event]] = {}
        self.clock = 0

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return self.loop.run_until_complete(coro)

    def tick(self) -> int:
        self.clock += 1
        return self.clock

    @property
    def live(self) -> RunStore:
        assert self.store is not None
        return self.store

    def reopen(self) -> None:
        self.store = self.run(RunStore.open(self.data, [self.worktree]))

    @initialize()
    def boot(self) -> None:
        self.reopen()

    def teardown(self) -> None:
        if self.store is not None:
            self.store.close()
        self.loop.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Rules

    @rule(change=st.sampled_from(CHANGES))
    def start(self, change: str) -> None:
        outcome = self.run(
            self.live.start_run(
                change, self.worktree, request(A.START_RUN, minute=self.tick())
            )
        )
        if isinstance(outcome, Accepted):
            self.committed[outcome.record.run_id] = [outcome.event]

    @precondition(lambda self: bool(self.committed))
    @rule(data=st.data())
    def step(self, data: st.DataObject) -> None:
        run_id = data.draw(st.sampled_from(sorted(self.committed)))
        phase = self.live.get(run_id).phase
        enabled = [a for a in STEP_ACTIONS if phase in EDGES[a][0]]
        name = data.draw(st.sampled_from(enabled or STEP_ACTIONS))
        outcome = self.run(
            self.live.transition(run_id, request(name, minute=self.tick()))
        )
        if isinstance(outcome, Accepted):
            self.committed[run_id].append(outcome.event)

    @precondition(lambda self: bool(self.committed))
    @rule(data=st.data(), passes=st.booleans())
    def archive(self, data: st.DataObject, passes: bool) -> None:
        run_id = data.draw(st.sampled_from(sorted(self.committed)))
        validator: TraceValidator = ReplayValidator() if passes else AlwaysFails()
        outcome = self.run(self.live.archive(run_id, validator, at(self.tick()), 5.0))
        if isinstance(outcome, Accepted):
            self.committed[run_id].append(outcome.event)

    @precondition(lambda self: bool(self.committed))
    @rule(data=st.data())
    def crash_mid_transition(self, data: st.DataObject) -> None:
        """Crash after the event is appended, before the record is replaced."""
        run_id = data.draw(st.sampled_from(sorted(self.committed)))
        phase = self.live.get(run_id).phase
        enabled = [a for a in STEP_ACTIONS if phase in EDGES[a][0]]
        if not enabled:
            return
        name = data.draw(st.sampled_from(enabled))
        crashing = crash_on_record_replace(os.replace)
        with (
            mock.patch.object(os, "replace", crashing),
            contextlib.suppress(SimulatedCrash),
        ):
            self.run(self.live.transition(run_id, request(name, minute=self.tick())))
        # The process is gone: abandon the store without closing it, restart.
        self.loop.close()
        self.loop = asyncio.new_event_loop()
        self.reopen()

    @rule()
    def restart(self) -> None:
        self.live.close()
        self.reopen()

    # Invariants

    @invariant()
    def log_matches_record(self) -> None:
        for run_id in self.committed:
            rec = record.read_record(record.record_path(self.worktree, run_id))
            on_disk = eventlog.read_events(eventlog.log_path(self.data, run_id))
            assert len(on_disk) == rec.seq
            assert self.live.get(run_id) == rec

    @invariant()
    def committed_events_immutable(self) -> None:
        for run_id, expected in self.committed.items():
            assert self.live.events(run_id) == expected

    @invariant()
    def committed_trace_valid(self) -> None:
        for run_id, events in self.committed.items():
            verdict = self.run(ReplayValidator().validate(run_id, events))
            assert verdict.passed, verdict.reason
            assert events[-1].to_phase is self.live.get(run_id).phase

    @invariant()
    def one_active_run_per_change(self) -> None:
        active = [
            self.live.get(r).change
            for r in self.committed
            if self.live.get(r).phase not in TERMINAL
        ]
        assert len(active) == len(set(active))

    @invariant()
    def actors_match_roles(self) -> None:
        for events in self.committed.values():
            assert all(e.actor is ROLE_OF[e.name] for e in events)
            assert all(isinstance(e.actor, Role) for e in events)
            assert all(e.from_phase is not Phase.NONE or e.seq == 1 for e in events)


StoreWithCrashes.TestCase.settings = settings(
    max_examples=60, stateful_step_count=40, deadline=None
)
TestStoreWithCrashes = StoreWithCrashes.TestCase

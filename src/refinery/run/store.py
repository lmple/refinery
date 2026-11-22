"""RunStore: applies transitions with the durability protocol (D5).

Every transition: append and fsync the event, atomically replace the run
record, then update the index. ``spec/Run.tla`` models the first two steps
(Begin, CommitRecord) and a crash between them (Crash, Recover).
"""

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Self

from refinery.run import eventlog, record, transitions
from refinery.run.archive import TraceValidator, ValidationResult
from refinery.run.index import INDEX_FILENAME, Index, rebuild_index
from refinery.run.model import (
    Accepted,
    Action,
    Event,
    Outcome,
    Phase,
    Refused,
    Role,
    RunRecord,
    TraceResult,
    TransitionRequest,
    Verdict,
)

# Stands in for the digest while the Archive event is still pending; the real
# one exists only once that event is in the log.
_PROVISIONAL_SHA = "0" * 64


class RunStore:
    """Owns all runs under one data directory."""

    def __init__(self, data_dir: Path, index: Index) -> None:
        """Use ``open``: it recovers and rebuilds before handing out a store."""
        self._data_dir = data_dir
        self._index = index
        self._start_lock = asyncio.Lock()
        self._run_locks: dict[str, asyncio.Lock] = {}

    @classmethod
    async def open(cls, data_dir: Path, roots: Sequence[Path]) -> Self:
        """Recover event logs and rebuild the index from records under ``roots``."""
        return cls(data_dir, _recover(data_dir, roots))

    def close(self) -> None:
        """Close the index."""
        self._index.close()

    def get(self, run_id: str) -> RunRecord:
        """The current record of ``run_id``; ``KeyError`` if unknown."""
        rec = self._index.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        return rec

    def runs(self) -> list[RunRecord]:
        """Every known run, ordered by run id."""
        return self._index.runs()

    def worktree(self, run_id: str) -> Path:
        """The worktree holding the run's record; ``KeyError`` if unknown."""
        path = self._index.record_path(run_id)
        if path is None:
            raise KeyError(run_id)
        # <worktree>/.refinery/runs/<run-id>.json
        return path.parents[len(record.RECORD_DIR.parts)]

    def events(self, run_id: str) -> list[Event]:
        """The committed events of ``run_id``."""
        return self._index.events(run_id)

    async def start_run(
        self, change: str, worktree: Path, request: TransitionRequest
    ) -> Outcome:
        """StartRun on ``change``, with its record in ``worktree``."""
        # Held across the whole commit: the model's Claims guard covers a start
        # that is still in flight (see RunPair.cfg).
        async with self._start_lock:
            run_id = self._next_run_id(change)
            outcome = transitions.start_run(
                run_id, change, request, self._index.active_run(change)
            )
            if isinstance(outcome, Refused):
                return outcome
            return self._commit(outcome, record.record_path(worktree, run_id))

    async def transition(self, run_id: str, request: TransitionRequest) -> Outcome:
        """Apply any transition except StartRun and Archive."""
        if request.name in (Action.START_RUN, Action.ARCHIVE):
            return Refused(f"{request.name} has its own entry point")
        async with self._lock(run_id):
            current = self._index.get(run_id)
            path = self._index.record_path(run_id)
            if current is None or path is None:
                return Refused(f"unknown run {run_id}")
            outcome = transitions.apply(current, request)
            if isinstance(outcome, Refused):
                return outcome
            return self._commit(outcome, path)

    async def archive(
        self, run_id: str, validator: TraceValidator, at: datetime, timeout_s: float
    ) -> Outcome:
        """Archive after validating the complete log; a failure stays in archive."""
        async with self._lock(run_id):
            current = self._index.get(run_id)
            path = self._index.record_path(run_id)
            if current is None or path is None:
                return Refused(f"unknown run {run_id}")
            request = TransitionRequest(Action.ARCHIVE, Role.FACTORYD, at)
            # Checks phase, role and verdict, and yields the event to validate.
            provisional = TraceResult(Verdict.PASS, current.seq + 1, _PROVISIONAL_SHA)
            probe = transitions.archive(current, request, provisional)
            if isinstance(probe, Refused):
                return probe
            log = eventlog.log_path(self._data_dir, run_id)
            try:
                eventlog.append_event(log, probe.event)
            except eventlog.EventLogError as exc:
                return Refused(str(exc))
            result = await _validate(
                validator, run_id, eventlog.read_events(log), timeout_s
            )
            if result.passed:
                digest = eventlog.digest_log(log)
                trace = TraceResult(Verdict.PASS, digest.events, digest.sha256)
                outcome = transitions.archive(current, request, trace)
                if isinstance(outcome, Accepted):
                    return self._commit_archive(outcome, path, log)
                result = ValidationResult(False, outcome.reason)
            # AbortArchive: withdraw the Archive event, record the verdict.
            eventlog.truncate_events(log, current.seq)
            failed = transitions.abort_archive(current, at)
            record.write_record(path, failed)
            self._index.apply(failed, path, None)
            return Refused(f"trace validation failed: {result.reason}")

    def _commit(self, outcome: Accepted, path: Path) -> Outcome:
        """Event first, then record, then index (D5)."""
        log = eventlog.log_path(self._data_dir, outcome.record.run_id)
        try:
            eventlog.append_event(log, outcome.event)
        except eventlog.EventLogError as exc:
            return Refused(str(exc))  # fail closed: nothing changed
        try:
            record.write_record(path, outcome.record)
        except OSError as exc:
            # Withdraw the event; if even that fails, startup recovery will.
            with contextlib.suppress(OSError):
                eventlog.truncate_events(log, outcome.record.seq - 1)
            return Refused(f"cannot write run record {path}: {exc}")
        self._index.apply(outcome.record, path, outcome.event)
        return outcome

    def _commit_archive(self, outcome: Accepted, path: Path, log: Path) -> Outcome:
        """Record the archived run, then seal the log it vouches for."""
        try:
            record.write_record(path, outcome.record)
        except OSError as exc:
            with contextlib.suppress(OSError):
                eventlog.truncate_events(log, outcome.record.seq - 1)
            return Refused(f"cannot write run record {path}: {exc}")
        eventlog.seal_log(log)
        self._index.apply(outcome.record, path, outcome.event)
        return outcome

    def _next_run_id(self, change: str) -> str:
        """``<change>.<n>``; skips ids whose log survived a start that crashed."""
        n = self._index.runs_started(change) + 1
        while eventlog.log_path(self._data_dir, f"{change}.{n}").exists():
            n += 1
        return f"{change}.{n}"

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self._run_locks.setdefault(run_id, asyncio.Lock())


def _recover(data_dir: Path, roots: Sequence[Path]) -> Index:
    """Recover (as in spec/Run.tla) every run found under ``roots``, then index."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for rec, _ in record.find_records(roots).values():
        log = eventlog.log_path(data_dir, rec.run_id)
        # Events past the record's seq were never committed.
        eventlog.truncate_events(log, rec.seq)
        # A crash between the archived record and sealing leaves it writable.
        if rec.phase is Phase.ARCHIVED and log.exists() and not eventlog.is_sealed(log):
            eventlog.seal_log(log)
    db = data_dir / INDEX_FILENAME
    rebuild_index(db, roots, data_dir)
    return Index(db)


async def _validate(
    validator: TraceValidator, run_id: str, events: Sequence[Event], timeout_s: float
) -> ValidationResult:
    """Gates fail closed: an error or a timeout is a failed validation."""
    try:
        async with asyncio.timeout(timeout_s):
            return await validator.validate(run_id, events)
    except TimeoutError:
        return ValidationResult(False, f"validator timed out after {timeout_s}s")
    except Exception as exc:
        return ValidationResult(False, f"validator error: {exc!r}")

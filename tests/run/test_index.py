"""Requirement "Index is rebuildable" (specs/run-record/spec.md) and design D6."""

import dataclasses
import shutil
import sqlite3
from pathlib import Path

import pytest

from refinery.run import eventlog, record, transitions
from refinery.run.archive import ReplayValidator
from refinery.run.index import INDEX_FILENAME, Index, rebuild_index
from refinery.run.model import Action, Phase, RunRecord
from refinery.run.store import RunStore
from tests.run.helpers import HAPPY_PATH, accepted, at, request, run_async

A = Action
P = Phase


def build(data: Path, worktree: Path) -> None:
    """Three runs: one archived, one cancelled, one waiting at tests review."""

    async def go() -> None:
        store = await RunStore.open(data, [worktree])
        for change in ("done", "dropped", "busy"):
            accepted(await store.start_run(change, worktree, request(A.START_RUN)))
        for minute, name in enumerate(HAPPY_PATH, start=1):
            accepted(await store.transition("done.1", request(name, minute=minute)))
        accepted(await store.archive("done.1", ReplayValidator(), at(30), 5.0))
        accepted(await store.transition("dropped.1", request(A.CANCEL, minute=2)))
        for minute, name in enumerate(HAPPY_PATH[:4], start=3):
            accepted(await store.transition("busy.1", request(name, minute=minute)))
        store.close()

    run_async(go())


def snapshot(db: Path) -> tuple[list[RunRecord], dict[str, list[object]]]:
    index = Index(db)
    try:
        runs = index.runs()
        return runs, {r.run_id: list(index.events(r.run_id)) for r in runs}
    finally:
        index.close()


@pytest.fixture
def built(tmp_path: Path) -> tuple[Path, Path]:
    data, worktree = tmp_path / "data", tmp_path / "worktree"
    worktree.mkdir()
    build(data, worktree)
    return data, worktree


def test_index_uses_wal(built: tuple[Path, Path]) -> None:
    data, _ = built
    with sqlite3.connect(data / INDEX_FILENAME) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_rebuild_after_deleting_the_database(built: tuple[Path, Path]) -> None:
    data, worktree = built
    db = data / INDEX_FILENAME
    before = snapshot(db)
    assert [r.phase for r in before[0]] == [P.TESTS_REVIEW, P.ARCHIVED, P.CANCELLED]
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    report = rebuild_index(db, [worktree], data)
    assert report.runs == 3
    assert report.events_missing == frozenset()
    assert snapshot(db) == before


def test_store_rebuilds_on_open(built: tuple[Path, Path]) -> None:
    data, worktree = built
    db = data / INDEX_FILENAME
    before = snapshot(db)
    db.unlink()

    async def go() -> None:
        store = await RunStore.open(data, [worktree])
        assert store.get("busy.1").phase is P.TESTS_REVIEW
        store.close()

    run_async(go())
    assert snapshot(db) == before


def test_event_log_missing(built: tuple[Path, Path]) -> None:
    data, worktree = built
    db = data / INDEX_FILENAME
    shutil.rmtree(eventlog.log_path(data, "dropped.1").parent)
    db.unlink()
    report = rebuild_index(db, [worktree], data)
    assert report.events_missing == frozenset({"dropped.1"})
    index = Index(db)
    try:
        restored = index.get("dropped.1")
        assert restored is not None
        assert restored.phase is P.CANCELLED
        assert index.events("dropped.1") == []
    finally:
        index.close()


def test_higher_seq_wins_between_copies(
    built: tuple[Path, Path], tmp_path: Path
) -> None:
    data, worktree = built
    stale_root = tmp_path / "main"
    stale = record.read_record(record.record_path(worktree, "busy.1"))
    older = dataclasses.replace(stale, seq=2, phase=P.MODEL)
    target = record.record_path(stale_root, "busy.1")
    target.parent.mkdir(parents=True)
    record.write_record(target, older)
    db = data / INDEX_FILENAME
    db.unlink()
    rebuild_index(db, [stale_root, worktree], data)
    index = Index(db)
    try:
        current = index.get("busy.1")
        assert current is not None
        assert (current.seq, current.phase) == (stale.seq, stale.phase)
        assert index.record_path("busy.1") == record.record_path(worktree, "busy.1")
    finally:
        index.close()


def test_rebuild_ignores_events_beyond_the_record(built: tuple[Path, Path]) -> None:
    data, worktree = built
    rec = record.read_record(record.record_path(worktree, "busy.1"))
    events = eventlog.read_events(eventlog.log_path(data, "busy.1"))
    extra = dataclasses.replace(
        events[-1],
        seq=rec.seq + 1,
        name=A.CANCEL,
        from_phase=rec.phase,
        to_phase=P.CANCELLED,
    )
    eventlog.append_event(eventlog.log_path(data, "busy.1"), extra)
    db = data / INDEX_FILENAME
    db.unlink()
    rebuild_index(db, [worktree], data)
    index = Index(db)
    try:
        assert len(index.events("busy.1")) == rec.seq
    finally:
        index.close()


def test_one_active_run_per_change_in_the_index(tmp_path: Path) -> None:
    """D6: a partial unique index backs the rule up inside SQLite."""
    first = accepted(transitions.start_run("c.1", "c", request(A.START_RUN), None))
    second = accepted(transitions.start_run("c.2", "c", request(A.START_RUN), None))
    index = Index(tmp_path / INDEX_FILENAME)
    try:
        index.apply(first.record, tmp_path / "c.1.json", first.event)
        with pytest.raises(sqlite3.IntegrityError):
            index.apply(second.record, tmp_path / "c.2.json", second.event)
        assert index.active_run("c") == "c.1"
        assert index.runs_started("c") == 1
    finally:
        index.close()

"""factoryd's SQLite index of runs and events: a cache rebuilt from files."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from refinery.run import eventlog, record
from refinery.run.model import TERMINAL, Event, RunRecord

INDEX_FILENAME = "index.sqlite3"

_TERMINAL_SQL = ", ".join(f"'{p.value}'" for p in sorted(TERMINAL))

# Records and events are stored in their on-disk JSON form, so the index
# round-trips them exactly; change and phase are columns for querying and for
# the partial unique index that backs up the one-active-run-per-change rule.
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    change      TEXT NOT NULL,
    phase       TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    record      TEXT NOT NULL,
    record_path TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_change
    ON runs (change) WHERE phase NOT IN ({_TERMINAL_SQL});
CREATE TABLE IF NOT EXISTS events (
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    name    TEXT NOT NULL,
    event   TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


@dataclass(frozen=True)
class RebuildReport:
    """What a rebuild found."""

    runs: int
    events_missing: frozenset[str]


class Index:
    """Runs and events in SQLite (WAL)."""

    def __init__(self, db_path: Path) -> None:
        """Open or create the index at ``db_path``."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the database."""
        self._db.close()

    def apply(self, record: RunRecord, record_path: Path, event: Event | None) -> None:
        """Store ``record`` (and ``event``, if any) in one transaction."""
        with self._db:
            self._put(record, record_path)
            if event is not None:
                self._add(record.run_id, event)

    def load(
        self, record: RunRecord, record_path: Path, events: Sequence[Event]
    ) -> None:
        """Store a run with all its events in one transaction (rebuild)."""
        with self._db:
            self._put(record, record_path)
            for event in events:
                self._add(record.run_id, event)

    def clear(self) -> None:
        """Forget everything (rebuild starts from here)."""
        with self._db:
            self._db.execute("DELETE FROM events")
            self._db.execute("DELETE FROM runs")

    def _put(self, rec: RunRecord, path: Path) -> None:
        # An upsert on run_id only: a conflict on the partial unique index must
        # raise, not silently replace another run as INSERT OR REPLACE would.
        self._db.execute(
            """INSERT INTO runs (run_id, change, phase, seq, record, record_path)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (run_id) DO UPDATE SET
                   change = excluded.change, phase = excluded.phase,
                   seq = excluded.seq, record = excluded.record,
                   record_path = excluded.record_path""",
            (
                rec.run_id,
                rec.change,
                rec.phase.value,
                rec.seq,
                record.to_json(rec),
                str(path),
            ),
        )

    def _add(self, run_id: str, event: Event) -> None:
        self._db.execute(
            "INSERT INTO events (run_id, seq, name, event) VALUES (?, ?, ?, ?)",
            (run_id, event.seq, event.name.value, eventlog.to_json(event)),
        )

    def get(self, run_id: str) -> RunRecord | None:
        """The indexed record of ``run_id``."""
        row: tuple[str] | None = self._db.execute(
            "SELECT record FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else record.from_json(row[0])

    def runs(self) -> list[RunRecord]:
        """All indexed runs, ordered by run id."""
        rows: list[tuple[str]] = self._db.execute(
            "SELECT record FROM runs ORDER BY run_id"
        ).fetchall()
        return [record.from_json(r[0]) for r in rows]

    def events(self, run_id: str) -> list[Event]:
        """Indexed events of ``run_id``, in order."""
        rows: list[tuple[str]] = self._db.execute(
            "SELECT event FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [eventlog.from_json(r[0]) for r in rows]

    def record_path(self, run_id: str) -> Path | None:
        """Where the record of ``run_id`` lives."""
        row: tuple[str] | None = self._db.execute(
            "SELECT record_path FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else Path(row[0])

    def active_run(self, change: str) -> str | None:
        """The unfinished run on ``change``, if any."""
        query = "SELECT run_id FROM runs WHERE change = ? AND phase NOT IN "
        row: tuple[str] | None = self._db.execute(
            f"{query}({_TERMINAL_SQL})", (change,)
        ).fetchone()
        return None if row is None else row[0]

    def runs_started(self, change: str) -> int:
        """How many runs were ever started on ``change``."""
        row: tuple[int] = self._db.execute(
            "SELECT COUNT(*) FROM runs WHERE change = ?", (change,)
        ).fetchone()
        return row[0]


def rebuild_index(
    db_path: Path, roots: Sequence[Path], data_dir: Path
) -> RebuildReport:
    """Recreate the index from run records under ``roots`` and logs in ``data_dir``.

    The tables are cleared and reloaded in place rather than the file being
    deleted: a connection left open by a crashed process must never be able to
    touch the new database's files.
    """
    index = Index(db_path)
    missing: set[str] = set()
    try:
        index.clear()
        found = record.find_records(roots)
        for run_id in sorted(found):
            rec, path = found[run_id]
            log = eventlog.log_path(data_dir, run_id)
            if not log.exists():
                missing.add(run_id)
            committed = [e for e in eventlog.read_events(log) if e.seq <= rec.seq]
            index.load(rec, path, committed)
        return RebuildReport(len(found), frozenset(missing))
    finally:
        index.close()

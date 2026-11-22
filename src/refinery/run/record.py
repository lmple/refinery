"""The run record file, ``.refinery/runs/<run-id>.json``."""

import contextlib
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from refinery.run import _json
from refinery.run.model import (
    SCHEMA_VERSION,
    Artifact,
    Phase,
    RunRecord,
    TraceResult,
    Verdict,
)

RECORD_DIR = Path(".refinery") / "runs"


def record_path(worktree: Path, run_id: str) -> Path:
    """Where the record of ``run_id`` lives in ``worktree``."""
    return worktree / RECORD_DIR / f"{run_id}.json"


def to_json(record: RunRecord) -> str:
    """The record as it is written to disk (sorted keys, trailing newline)."""
    trace: dict[str, object] = {
        "verdict": record.trace.verdict.value,
        "events": record.trace.events,
        "sha256": record.trace.sha256,
    }
    return _json.dump_object(
        {
            "schema": SCHEMA_VERSION,
            "run_id": record.run_id,
            "change": record.change,
            "phase": record.phase.value,
            "frozen": sorted(a.value for a in record.frozen),
            "seq": record.seq,
            "trace": trace,
            "approved": {"spec": record.spec_commit, "tests": record.tests_commit},
            "started_at": record.started_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        },
        pretty=True,
    )


def from_json(text: str) -> RunRecord:
    """Parse a record written by ``to_json``."""
    obj = _json.load_object(text)
    if _json.get_int(obj, "schema") != SCHEMA_VERSION:
        raise _json.FormatError(f"unsupported run record schema {obj.get('schema')}")
    trace = _json.get_object(obj, "trace")
    # Records written before approvals carried commits have no "approved".
    approved = (
        _json.get_object(obj, "approved") if obj.get("approved") is not None else {}
    )
    return RunRecord(
        run_id=_json.get_str(obj, "run_id"),
        change=_json.get_str(obj, "change"),
        phase=Phase(_json.get_str(obj, "phase")),
        frozen=frozenset(Artifact(a) for a in _json.get_str_list(obj, "frozen")),
        seq=_json.get_int(obj, "seq"),
        trace=TraceResult(
            verdict=Verdict(_json.get_str(trace, "verdict")),
            events=_json.get_opt_int(trace, "events"),
            sha256=_json.get_opt_str(trace, "sha256"),
        ),
        started_at=_json.get_datetime(obj, "started_at"),
        updated_at=_json.get_datetime(obj, "updated_at"),
        spec_commit=_json.get_opt_str(approved, "spec"),
        tests_commit=_json.get_opt_str(approved, "tests"),
    )


def write_record(path: Path, record: RunRecord) -> None:
    """Atomically replace the record at ``path``.

    Write a temporary file in the same directory, fsync it, rename it over the
    record, then fsync the directory, so a crash leaves either the old or the
    new record and never a partial one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(to_json(record))
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        tmp.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    fsync_dir(path.parent)


def read_record(path: Path) -> RunRecord:
    """Parse the record at ``path``."""
    return from_json(path.read_text(encoding="utf-8"))


def find_records(roots: Sequence[Path]) -> dict[str, tuple[RunRecord, Path]]:
    """Every run record under ``roots``; of two copies, the higher seq wins."""
    found: dict[str, tuple[RunRecord, Path]] = {}
    for root in roots:
        for path in sorted((root / RECORD_DIR).glob("*.json")):
            rec = read_record(path)
            known = found.get(rec.run_id)
            if known is None or rec.seq > known[0].seq:
                found[rec.run_id] = (rec, path)
    return found


def fsync_dir(directory: Path) -> None:
    """Make a rename or file creation in ``directory`` durable."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

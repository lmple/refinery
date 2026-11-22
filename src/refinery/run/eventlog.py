"""A run's ``events.jsonl``, kept in factoryd's data directory."""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from refinery.run import _json
from refinery.run.model import Action, BugKind, Event, Phase, Role
from refinery.run.record import fsync_dir

_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class EventLogError(Exception):
    """The event log could not be written; the transition must be refused."""


@dataclass(frozen=True)
class LogDigest:
    """Event count and sha256 of a log's exact bytes."""

    events: int
    sha256: str


def log_path(data_dir: Path, run_id: str) -> Path:
    """Where the event log of ``run_id`` lives under ``data_dir``."""
    return data_dir / "runs" / run_id / "events.jsonl"


def to_json(event: Event) -> str:
    """One log line, without its newline; optional fields only when set."""
    obj: dict[str, object] = {
        "seq": event.seq,
        "name": event.name.value,
        "from": event.from_phase.value,
        "to": event.to_phase.value,
        "actor": event.actor.value,
        "at": event.at.isoformat(),
    }
    optional = {
        "note": event.note,
        "reason": event.reason,
        "bug": None if event.bug is None else event.bug.value,
        "gate": event.gate,
    }
    obj |= {k: v for k, v in optional.items() if v is not None}
    return _json.dump_object(obj, pretty=False)


def from_json(line: str) -> Event:
    """Parse one log line."""
    obj = _json.load_object(line)
    bug = _json.get_opt_str(obj, "bug")
    return Event(
        seq=_json.get_int(obj, "seq"),
        name=Action(_json.get_str(obj, "name")),
        from_phase=Phase(_json.get_str(obj, "from")),
        to_phase=Phase(_json.get_str(obj, "to")),
        actor=Role(_json.get_str(obj, "actor")),
        at=_json.get_datetime(obj, "at"),
        note=_json.get_opt_str(obj, "note"),
        reason=_json.get_opt_str(obj, "reason"),
        bug=None if bug is None else BugKind(bug),
        gate=_json.get_opt_str(obj, "gate"),
    )


def append_event(path: Path, event: Event) -> None:
    """Append ``event`` and fsync; raise ``EventLogError`` on failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        with path.open("a", encoding="utf-8") as log:
            log.write(to_json(event) + "\n")
            log.flush()
            os.fsync(log.fileno())
        if created:
            fsync_dir(path.parent)
    except OSError as exc:
        raise EventLogError(f"cannot append to {path}: {exc}") from exc


def _complete_lines(data: bytes) -> list[bytes]:
    """Newline-terminated lines; a torn last line (crash mid-write) is ignored."""
    return data.split(b"\n")[:-1]


def read_events(path: Path) -> list[Event]:
    """All events in the log, in order."""
    if not path.exists():
        return []
    return [
        from_json(line.decode("utf-8")) for line in _complete_lines(path.read_bytes())
    ]


def truncate_events(path: Path, seq: int) -> int:
    """Drop events whose seq is greater than ``seq``; return how many.

    Events are appended in seq order, so the kept ones are a byte prefix of
    the file: truncating in place keeps their bytes, and so the digest,
    unchanged. A torn last line counts as dropped.
    """
    if not path.exists():
        return 0
    data = path.read_bytes()
    keep_bytes = 0
    lines = _complete_lines(data)
    kept = 0
    for line in lines:
        if from_json(line.decode("utf-8")).seq > seq:
            break
        keep_bytes += len(line) + 1
        kept += 1
    torn = 1 if data and not data.endswith(b"\n") else 0
    dropped = len(lines) - kept + torn
    if dropped:
        with path.open("r+b") as log:
            log.truncate(keep_bytes)
            log.flush()
            os.fsync(log.fileno())
    return dropped


def digest_log(path: Path) -> LogDigest:
    """Count and hash the log."""
    data = path.read_bytes()
    return LogDigest(len(_complete_lines(data)), hashlib.sha256(data).hexdigest())


def seal_log(path: Path) -> LogDigest:
    """Make the log read-only and return its digest."""
    path.chmod(path.stat().st_mode & ~_WRITE_BITS)
    return digest_log(path)


def is_sealed(path: Path) -> bool:
    """Whether the log has no write permission bits left."""
    return path.stat().st_mode & _WRITE_BITS == 0


def verify_log(path: Path, events: int, sha256: str) -> bool:
    """Whether the log still matches the digest stored at archive."""
    return path.exists() and digest_log(path) == LogDigest(events, sha256)

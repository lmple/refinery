"""On-disk registry of live process groups, so a restart can kill orphans (D2).

One file per group, ``<data>/procs/<pgid>.json``, written atomically and
fsynced before the group's command is released, and removed once the group
is gone.
"""

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

# /proc/<pid>/stat fields counted after the ")" that ends the command name:
# index 0 is field 3 (state), so field 22 (starttime) is index 19.
_STATE, _PGRP, _STARTTIME = 0, 2, 19


@dataclass(frozen=True)
class Entry:
    """One registered process group."""

    pgid: int
    start_ticks: int  # leader start time, /proc/<pid>/stat field 22
    argv: tuple[str, ...]
    started_at: datetime


def registry_dir(data_dir: Path) -> Path:
    """Where entries live under factoryd's data directory."""
    return data_dir / "procs"


def write_entry(data_dir: Path, entry: Entry) -> None:
    """Durably record ``entry`` (atomic write, fsync)."""
    directory = registry_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        {
            "pgid": entry.pgid,
            "start_ticks": entry.start_ticks,
            "argv": list(entry.argv),
            "started_at": entry.started_at.isoformat(),
        },
        sort_keys=True,
    )
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(text)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        tmp.replace(directory / f"{entry.pgid}.json")
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_dir(directory)


def remove_entry(data_dir: Path, pgid: int) -> None:
    """Forget the group ``pgid``; missing entries are fine."""
    directory = registry_dir(data_dir)
    path = directory / f"{pgid}.json"
    if path.exists():
        path.unlink()
        _fsync_dir(directory)


def list_entries(data_dir: Path) -> list[Entry]:
    """All recorded entries, by group id."""
    directory = registry_dir(data_dir)
    if not directory.exists():
        return []
    entries: list[Entry] = []
    for path in sorted(directory.glob("[0-9]*.json")):
        raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        argv = cast(list[str], raw["argv"])
        entries.append(
            Entry(
                pgid=int(str(raw["pgid"])),
                start_ticks=int(str(raw["start_ticks"])),
                argv=tuple(argv),
                started_at=datetime.fromisoformat(str(raw["started_at"])),
            )
        )
    return sorted(entries, key=lambda e: e.pgid)


def _stat_fields(pid: int) -> list[str] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return text.rsplit(")", 1)[1].split()


def process_start_ticks(pid: int) -> int | None:
    """Start time of process ``pid`` in clock ticks since boot, or None."""
    fields = _stat_fields(pid)
    return None if fields is None else int(fields[_STARTTIME])


def group_alive(pgid: int) -> bool:
    """Whether any non-zombie process belongs to process group ``pgid``.

    ``killpg(pgid, 0)`` cannot be used: it also succeeds for zombies, which
    are dead but not yet reaped by their parent.
    """
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = _stat_fields(int(entry.name))
            if (
                fields is not None
                and fields[_STATE] != "Z"
                and int(fields[_PGRP]) == pgid
            ):
                return True
    return False


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

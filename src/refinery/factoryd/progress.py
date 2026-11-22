"""report_progress: a per-run progress log that fails open (D6).

Telemetry, not the run record: no fsync, not validated, never a transition.
Writing it must never block an agent (AGENTS.md: logging fails open).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import cast


def progress_path(data_dir: Path, run_id: str) -> Path:
    """``<data>/runs/<run-id>/progress.jsonl``."""
    return data_dir / "runs" / run_id / "progress.jsonl"


def append_progress(data_dir: Path, run_id: str, message: str, at: datetime) -> bool:
    """Append ``message``; False (never an exception) if it cannot be written."""
    path = progress_path(data_dir, run_id)
    line = json.dumps({"at": at.isoformat(), "run_id": run_id, "message": message})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        return False
    return True


def read_progress(data_dir: Path, run_id: str, last: int = 5) -> list[dict[str, str]]:
    """The last ``last`` readable entries, oldest first."""
    try:
        lines = progress_path(data_dir, run_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, str]] = []
    for line in lines:
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if isinstance(raw, dict):
            entry = cast(dict[str, object], raw)
            entries.append({k: str(v) for k, v in entry.items()})
    return entries[-last:]
